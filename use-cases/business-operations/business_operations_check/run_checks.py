# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The Gateway authorization run checks, as a pure decision.

`__init__.py` does every read and hands them in through `Lookups`, so this module
has no AWS dependency and every branch is a unit test. The platform interceptor
runs it only for this use case's targets (`business-operations-*`) and the SAP
layer's (`sap-*`), before Cedar, so a call Cedar would deny still reaches here:
every check that needs no read comes first, and the grant, the case and the registry are read only when the
call gets that far.

These are the one gate for what a run may do; the agent repeats none of it. What it
decides, for the calls it gates:

- On a SAP target (the SAP layer's), only for this use case's own apps, the ones
  its SAP claim names: any other caller is another use case's, and passes
  untouched. The layer's own check runs too, and refuses `odata_delete` and any
  caller no use case claims.
- Every caller needs the run grant the invoker wrote for its run, named by the
  `x-audit-correlation-id` header, for its own client id, unexpired, and the grant's
  secret (`x-business-operations-run-grant`), which only the invoker and the agent
  hold. The run id alone is not enough: the browser holds it, and under the direct
  Entra mode with on-behalf-of SAP its token is the run's own caller key.
- `demo_get_ticket` reads a ticket by id, so it needs a grant like a case tool, for a
  person's token and an app's alike.
- A rehearsal's grant (`rehearsal`) and an ask turn's (`ask`) refuse every tool that
  leaves a mark: a SAP write, a case update, a ticket and a notification.
- A SAP write must name the run's own case's document: the case's pack declares
  which write parameters must equal which `key` fields of the case
  (`case_document_check`), and a pack that declares none has every write refused.
- It then passes the amount gate. An approval lifts that gate for the one write the
  human approved (`approved_write` on the grant), not for the run.
- A pack that declares `write_amount_field` also has the amount the write itself carries
  held to the case's amount and to the limit: a create or update must carry it, and a
  function import (Release, Cancel: the key of a posted document) is held to it only
  when it names the field.
- A SAP read by the agent's own client (the apps `agent_callers` names) needs a live run
  grant too. A grant that names packs reads only those packs' `sap_system`; a grant with
  none (a discovery run) reads any system this use case claims. Every other caller's
  reads pass untouched, as before.
- A case, ticket, process-type or pack tool must stay inside the grant's packs.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from . import run_grant

LIMIT_SYMBOL = "AUTO_WRITE_LIMIT_USD"
#: Above this an approved write needs two different approvers on its grant.
DUAL_LIMIT_SYMBOL = "DUAL_APPROVAL_LIMIT_USD"
LIMIT_CURRENCY = "USD"

SAP_TARGET_PREFIX = "sap-"
# The SAP targets' first name: still a SAP target while a deployment renames them,
# so a write in between meets the run grant and the amount gate.
LEGACY_SAP_TARGET_PREFIX = "business-operations-sap-"
_SAP = (SAP_TARGET_PREFIX, LEGACY_SAP_TARGET_PREFIX)
#: This use case's own targets. Any other target, beside the SAP layer's, is another
#: use case's, so its call passes without a read.
TARGET_PREFIX = "business-operations-"
SAP_WRITE_TOOLS = frozenset({"odata_create", "odata_update", "odata_function_import"})
# demo_create_ticket records its ticket on the case it names, so it is a case tool.
CASE_ARG_TOOLS = frozenset(
    {"get_case_state", "update_case_state", "send_notification", "demo_create_ticket"}
)
# demo_get_ticket names a ticket, and the ticket names its case.
TICKET_ARG_TOOLS = frozenset({"demo_get_ticket"})
PROCESS_TYPE_ARG_TOOLS = frozenset({"load_sop", "get_precedent"})
# The SOP knowledge base filters on the pack the call names.
PACK_ARG_TOOLS = frozenset({"search_sap_sops"})
#: Every tool that leaves a mark outside the run. A rehearsal's grant and an ask turn's
#: refuse them.
READ_ONLY_DENIED_TOOLS = SAP_WRITE_TOOLS | {
    "update_case_state",
    "demo_create_ticket",
    "send_notification",
}
#: Where a SAP write's arguments name fields, by tool: the function's parameters, the
#: record's key, and the body.
_VALUE_ARGS = ("parameters", "identifier_fields", "payload")

NO_RUN = "this call belongs to no run the server started, so it cannot be authorized"
REHEARSAL = (
    "this run is a rehearsal, so it may not write to SAP, change a case, open a ticket "
    "or send a notification"
)
#: Worded for the model's next turn: it answers, and says where the person acts.
ASKING = (
    "this run answers a question, so it may not write to SAP, change a case, open a "
    "ticket or send a notification. Say what you would do, and that the person does it "
    "with Process on the case, or with the case's decision when a change waits for one"
)
# What the model does next, so a refused write ends in a question to a person.
_ASK_FOR_APPROVAL = (
    'Raise an approval request for this write: a ticket with response_type "approval" '
    "and the proposed_write, not a notification and not a free-text ticket. Then set "
    "the case to awaiting_human_input and stop; an approved decision is what lets this "
    "write through on the resumed run"
)
_ASK_FOR_CASE = "Ask the operator which case this write belongs to"


@dataclass(frozen=True)
class Grant:
    run_id: str
    client_id: str
    case_id: str | None
    packs: frozenset[str] | None
    approved: bool
    expires_at: int
    #: The write a human approved: {op, service, entity, fields: {name: value}}.
    approved_write: dict | None = None
    rehearsal: bool = False
    #: A conversation turn (`POST /runs` with no `intent: process`): reads only.
    ask: bool = False
    #: SHA-256 (hex) of the secret the invoker gave the run; empty matches nothing.
    secret_sha256: str = ""
    #: Who approved `approved_write`, distinct people.
    approvers: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Case:
    """What the run checks read off a case, as stored."""

    pack: str
    amount: str = ""
    currency: str = ""
    #: The case's natural key, `{field: value}`: what document it is.
    key: dict = field(default_factory=dict)
    #: A sender's typed records made the case, so its amount is the sender's word.
    pushed: bool = False


@dataclass(frozen=True)
class Lookups:
    grant: Callable[[str], Grant | None]
    #: The pack's limit with overrides applied; None when it declares none;
    #: raises ValueError when a declared limit is unreadable.
    limit: Callable[[str], Decimal | None]
    any_limit_declared: Callable[[], bool]
    #: The case, or None when it cannot be read.
    case: Callable[[str], Case | None]
    case_pack: Callable[[str], str | None]
    #: The ticket's case id; None when the ticket is missing or names no case.
    ticket_case: Callable[[str], str | None]
    process_type_pack: Callable[[str], str | None]
    #: The pack's `case_document_check`, `{write parameter: case key field}`; None when it
    #: declares none; raises ValueError when it is unreadable.
    case_document_check: Callable[[str], dict | None]
    #: The pack's dual-approval limit with overrides applied; None when it declares none;
    #: raises ValueError when a declared limit is unreadable.
    dual_limit: Callable[[str], Decimal | None] = lambda pack: None
    #: The write field that holds the amount the call posts, from the pack's
    #: `write_amount_field`; None when it declares none; raises ValueError when unreadable.
    amount_field: Callable[[str], str | None] = lambda pack: None
    #: The pack's `sap_system`; None when it names none (the only system, then); raises
    #: ValueError when the pack is unknown or unreadable.
    pack_system: Callable[[str], str | None] = lambda pack: None


def split(tool: str) -> tuple[str, str]:
    """`<target>___<tool>` to (target, tool)."""
    target, _, name = tool.partition("___")
    return (target, name) if name else ("", target)


_OP_BY_TOOL = {
    "odata_update": "update",
    "odata_create": "create",
    "odata_function_import": "function_import",
}


def _value(v: object) -> str:
    # The proposal's values are strings; a JSON boolean in the call is `true`, not `True`.
    if isinstance(v, bool):
        return "true" if v else "false"
    return "" if v is None else str(v)


def _fields(raw: object) -> dict | None:
    """A write argument that names fields, as a dict: it may arrive as a JSON string."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    return raw if isinstance(raw, dict) else None


def is_approved_write(approved: dict | None, name: str, arguments: object) -> bool:
    """Is this call the write the human approved, field for field?

    Every scalar field the call sends must be one the human saw with the same value,
    and the reverse. A nested value (a `to_Item` envelope) is something no approval
    card renders, so a call carrying one is never the approved write.
    """
    if not approved or not isinstance(arguments, dict):
        return False
    function = name == "odata_function_import"
    values = _fields(arguments.get("parameters" if function else "payload"))
    if values is None or any(isinstance(v, (dict, list)) for v in values.values()):
        return False
    call = {
        "op": _OP_BY_TOOL.get(name),
        "service": arguments.get("service_name"),
        "entity": arguments.get("function_name" if function else "entity_set"),
        "fields": {str(k): _value(v) for k, v in values.items()},
    }
    return all(call[k] == approved.get(k) for k in call)


def same_document_number(a: object, b: object) -> bool:
    """Two SAP key values compared as SAP's ALPHA conversion does: an OData literal's
    quotes and leading zeros do not make a different document."""

    def plain(v: object) -> str:
        return _value(v).strip().strip("'").lstrip("0")

    return plain(a) == plain(b)


def _case_document_refusal(
    grant: Grant, lookups: Lookups, arguments: dict
) -> str | None:
    """Why this write is not on the run's own case's document, or None.

    Every parameter the pack's case document check names must appear in the call, and every place it appears
    must equal the case's key field, so a second copy cannot smuggle in another document.
    """
    if not grant.case_id:
        return (
            "this run names no case, so the write cannot be tied to a case's "
            f"document. {_ASK_FOR_CASE}"
        )
    case = lookups.case(grant.case_id)
    if case is None:
        return f"case '{grant.case_id}' could not be read"
    try:
        document_check = lookups.case_document_check(case.pack)
    except ValueError:
        return f"the case_document_check declared for '{case.pack}' is unreadable"
    if not document_check:
        return (
            f"use case '{case.pack}' declares no case_document_check, so no SAP write can be "
            f"tied to case '{grant.case_id}'"
        )
    fields = [f for f in map(_fields, (arguments.get(a) for a in _VALUE_ARGS)) if f]
    for param, key_field in sorted(document_check.items()):
        expected = case.key.get(key_field)
        if expected is None or _value(expected) == "":
            return f"case '{grant.case_id}' has no {key_field} to tie the write to"
        named = [f[param] for f in fields if param in f]
        if not named:
            return f"the write names no {param}, so it cannot be tied to case '{grant.case_id}'"
        for value in named:
            if not same_document_number(value, expected):
                return (
                    f"the write names {param} {_value(value).strip(chr(39))}, but case "
                    f"'{grant.case_id}' is {key_field} {_value(expected)}: a run writes "
                    "only its own case's document"
                )
    return None


def _amount_refusal(
    grant: Grant, lookups: Lookups, name: str, arguments: object
) -> str | None:
    if grant.approved and is_approved_write(grant.approved_write, name, arguments):
        return _dual_refusal(grant, lookups)
    if refusal := _payload_amount_refusal(grant, lookups, name, arguments):
        return refusal
    if not lookups.any_limit_declared():
        return None
    if not grant.case_id:
        return (
            "this run names no case, so the amount it would post cannot be checked "
            f"against the approval limit. {_ASK_FOR_CASE}"
        )
    case = lookups.case(grant.case_id)
    if case is None:
        return f"the amount on case '{grant.case_id}' could not be read"
    pack, amount, currency = case.pack, case.amount, case.currency
    try:
        limit = lookups.limit(pack)
    except ValueError:
        return f"the approval limit configured for '{pack}' is unreadable"
    if limit is None:
        return None
    if not limit.is_finite():
        return f"the approval limit configured for '{pack}' is unreadable"
    if case.pushed:
        return (
            f"case '{grant.case_id}' was made from records a sender pushed, so its amount "
            f"cannot lift the approval limit. {_ASK_FOR_APPROVAL}"
        )
    # An empty currency is doubt like any other: assuming USD would compare a yen
    # amount against a dollar limit.
    if currency != LIMIT_CURRENCY:
        return (
            f"case '{grant.case_id}' is in {currency or 'no stated currency'} and the "
            f"approval limit is in {LIMIT_CURRENCY}, so the two cannot be compared"
        )
    try:
        value = Decimal(amount)
    except InvalidOperation:
        value = None
    # Decimal("NaN") parses, then raises on comparison; infinity is no amount either.
    if value is None or not value.is_finite():
        return f"case '{grant.case_id}' carries no readable amount, so it cannot be checked against the approval limit"
    if value > limit:
        if grant.approved:
            return (
                f"case '{grant.case_id}' is {amount} {LIMIT_CURRENCY}, over the {limit} "
                f"{LIMIT_CURRENCY} limit, and this call is not the write the human approved"
            )
        return (
            f"case '{grant.case_id}' is {amount} {LIMIT_CURRENCY}, over the {limit} "
            f"{LIMIT_CURRENCY} a human must approve above. {_ASK_FOR_APPROVAL}"
        )
    return None


def _payload_amount_refusal(
    grant: Grant, lookups: Lookups, name: str, arguments: dict
) -> str | None:
    """Why the amount this write carries is over the case's amount or the limit, or None.

    Only for a pack that declares `write_amount_field`. In doubt it refuses: a create or
    update with no such field, or one that is not a finite number, is refused.
    """
    if not grant.case_id:
        return None  # _case_document_refusal has already refused a run with no case
    case = lookups.case(grant.case_id)
    if case is None:
        return f"case '{grant.case_id}' could not be read"
    try:
        field_name = lookups.amount_field(case.pack)
    except ValueError:
        return f"the write_amount_field declared for '{case.pack}' is unreadable"
    if not field_name:
        return None
    fields = [f for f in map(_fields, (arguments.get(a) for a in _VALUE_ARGS)) if f]
    named = [f[field_name] for f in fields if field_name in f]
    if not named:
        if name == "odata_function_import":
            return None  # Release and Cancel carry a document's key, not its amount
        return (
            f"the write names no {field_name}, so the amount it posts cannot be checked "
            "against the case and the approval limit"
        )
    try:
        limit = lookups.limit(case.pack)
        cap = Decimal(case.amount)
    except (ValueError, InvalidOperation):
        limit, cap = None, None
    if (
        cap is None
        or not cap.is_finite()
        or (limit is not None and not limit.is_finite())
    ):
        return f"case '{grant.case_id}' carries no readable amount or limit"
    for raw in named:
        try:
            value = abs(Decimal(_value(raw).strip().strip("'")))
        except InvalidOperation:
            value = None
        if value is None or not value.is_finite():
            return f"the write's {field_name} is not a number, so it cannot be checked"
        if value > cap:
            return (
                f"the write's {field_name} {value} is more than case "
                f"'{grant.case_id}' amount {cap}"
            )
        if limit is not None and value > limit:
            if case.currency != LIMIT_CURRENCY:
                return (
                    f"case '{grant.case_id}' is in {case.currency or 'no stated currency'} "
                    f"and the approval limit is in {LIMIT_CURRENCY}, so the two cannot "
                    "be compared"
                )
            return (
                f"the write's {field_name} {value} is over the {limit} {LIMIT_CURRENCY} "
                f"a human must approve above. {_ASK_FOR_APPROVAL}"
            )
    return None


def _dual_refusal(grant: Grant, lookups: Lookups) -> str | None:
    """Why an approved write still needs a second approver, or None.

    In doubt it does, as `_amount_refusal` refuses in doubt: an unreadable limit, amount
    or case, a currency the limit is not in, or a case made from records a sender
    pushed, whose amount is the sender's word and not SAP's.
    """
    if len(grant.approvers) >= 2 or not grant.case_id:
        return None
    case = lookups.case(grant.case_id)
    if case is None:
        return f"case '{grant.case_id}' could not be read"
    try:
        limit = lookups.dual_limit(case.pack)
    except ValueError:
        return f"the dual-approval limit configured for '{case.pack}' is unreadable"
    if limit is None:
        return None
    try:
        amount = Decimal(case.amount)
    except InvalidOperation:
        amount = None
    if (
        amount is None
        or not amount.is_finite()
        or not limit.is_finite()
        or case.currency != LIMIT_CURRENCY
        or case.pushed
        or amount > limit
    ):
        return (
            f"case '{grant.case_id}' is over the dual-approval limit, so this write needs "
            "a second approver: ask another member of the pack's approve groups to "
            "approve it as well"
        )
    return None


def _scope_refusal(
    name: str, arguments: dict, packs: frozenset[str] | None, lookups: Lookups
) -> str | None:
    if packs is None:
        return None
    if name in CASE_ARG_TOOLS:
        case_id = arguments.get("case_id")
        if isinstance(case_id, str) and case_id:
            pack = lookups.case_pack(case_id)
            if pack is None or pack not in packs:
                return f"case '{case_id}' is not in a use case this run may touch"
    if name in TICKET_ARG_TOOLS:
        ticket_id = arguments.get("ticket_id")
        if isinstance(ticket_id, str) and ticket_id:
            case_id = lookups.ticket_case(ticket_id)
            pack = lookups.case_pack(case_id) if case_id else None
            if pack is None or pack not in packs:
                return f"ticket '{ticket_id}' is not in a use case this run may touch"
    if name in PROCESS_TYPE_ARG_TOOLS:
        process_type = arguments.get("process_type")
        if isinstance(process_type, str) and process_type:
            pack = lookups.process_type_pack(process_type)
            if pack is None or pack not in packs:
                return f"process type '{process_type}' is not in a use case this run may touch"
    if name in PACK_ARG_TOOLS:
        pack_id = arguments.get("pack_id")
        if isinstance(pack_id, str) and pack_id and pack_id not in packs:
            return f"pack '{pack_id}' is not a use case this run may touch"
    return None


def _is_sap_read(target: str, name: str) -> bool:
    """A SAP target's tool that is neither a write nor `odata_delete` (the SAP layer's
    to refuse): a read, which the run checks hold to a run grant for the agent's own client."""
    return (
        target.startswith(_SAP)
        and name not in SAP_WRITE_TOOLS
        and name != "odata_delete"
    )


def _read_refusal(grant: Grant, target: str, lookups: Lookups) -> str | None:
    """Why this grant may not read this SAP system, or None.

    A grant that names packs reads only their `sap_system`. A pack that names none reads
    the only system there is, so it limits nothing; a grant with no packs (a discovery
    run) reads any system the use case claims.
    """
    if not grant.packs:
        return None
    system = target.removeprefix(
        LEGACY_SAP_TARGET_PREFIX
        if target.startswith(LEGACY_SAP_TARGET_PREFIX)
        else SAP_TARGET_PREFIX
    )
    try:
        systems = {lookups.pack_system(p) for p in grant.packs}
    except ValueError:
        return (
            "a pack this run may touch could not be read, so its SAP system is unknown"
        )
    if None in systems or system in systems:
        return None
    return f"this run's use cases do not read the SAP system '{system}'"


def gated(tool: str) -> bool:
    """Does this tool need a decision at all? No reads: the cheap first cut."""
    target, name = split(tool)
    if target.startswith(_SAP):
        return name in SAP_WRITE_TOOLS
    if not target.startswith(TARGET_PREFIX):
        return False
    # A tool that leaves a mark needs the grant, which says whether the run reads only.
    return (
        name in READ_ONLY_DENIED_TOOLS - SAP_WRITE_TOOLS
        or name in CASE_ARG_TOOLS
        or name in TICKET_ARG_TOOLS
        or name in PROCESS_TYPE_ARG_TOOLS
        or name in PACK_ARG_TOOLS
    )


def decide(
    *,
    tool: str,
    arguments: object,
    client_id: Callable[[], str],
    run_id: str | None,
    grant_secret: str | None,
    app_id: Callable[[], str],
    sap_callers: Callable[[], frozenset[str]],
    now: int,
    lookups: Lookups,
    agent_callers: Callable[[], frozenset[str]] = lambda: frozenset(),
) -> str | None:
    """The refusal reason for this call, or None to let it through.

    `client_id` is the caller key a run grant names; `app_id` the calling app, as
    the SAP claim names it. Both, and the settings, are callables so a call passed
    on its name alone never decodes a token or reads a parameter. `agent_callers` are the
    apps that run the agent; only their SAP reads need a grant.
    """
    target, name = split(tool)
    sap_read = _is_sap_read(target, name)
    if not (gated(tool) or sap_read):
        return None
    sap_write = target.startswith(_SAP) and not sap_read
    if (sap_write or sap_read) and app_id() not in sap_callers():
        return None
    if sap_read and app_id() not in agent_callers():
        return None

    if not run_id:
        return NO_RUN
    grant = lookups.grant(run_id)
    if grant is None or not run_grant.live(grant, client_id(), grant_secret, now):
        return NO_RUN
    if grant.rehearsal and name in READ_ONLY_DENIED_TOOLS:
        return REHEARSAL
    if grant.ask and name in READ_ONLY_DENIED_TOOLS:
        return ASKING
    if not isinstance(arguments, dict):
        return "the call's arguments are malformed"

    if sap_read:
        return _read_refusal(grant, target, lookups)
    if sap_write:
        return _case_document_refusal(grant, lookups, arguments) or _amount_refusal(
            grant, lookups, name, arguments
        )
    return _scope_refusal(name, arguments, grant.packs, lookups)
