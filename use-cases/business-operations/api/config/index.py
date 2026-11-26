# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""This stack's settings, a pack's rules and controls, and what this deployment runs.

    GET     /registry                      the packs the caller may see
    GET PUT /deployment/settings           contacts, and where the deployment runs
    GET     /packs/{pack_id}/rules         the pack's rule values
    GET PUT /packs/{pack_id}/controls      its limits (PUT lowers them), approvers and
                                           write checks

**One home per value.** A *rule* constant (a tolerance, an escalation age) is the
pack's content: edited in development with `PATCH /packs/{pack_id}` and promoted by
bundle, so `/rules` only reads it. A *contact* is the stack's: `platform.yaml`, then an
override here. A *limit* (`pack_tiers.LIMIT_SYMBOLS`) is the pack's ceiling, which
`PUT /packs/{pack_id}/controls` may lower and never raise, in any stack. Each answer says
which kind a symbol is and where its effective value comes from. Approvers and write
checks are pack structure, read on `/controls` and changed with `PATCH`.

Every value here is substituted into a SOP before the agent reads it, so a bad
write is an instruction defect: a mistyped contact makes the agent notify a
literal placeholder, and a fat-fingered limit changes which invoices
auto-post. Writes are allowlisted by symbol and range-checked, and a request
carrying any invalid field is rejected whole rather than written in part.

Contact defaults come from CONTACTS_JSON, which the stack renders from the use case's
`contacts` setting in platform.yaml. The constants come from the published registry —
the same packs the agent resolves. The table holds only overrides, so a fresh deploy
with zero rows behaves exactly as it did before this API existed.

`GET /registry` lives here because this is the Lambda that reads the pack corpus to
answer the rest. It is scoped to what the caller may see, and it omits `base_prompt` —
the browser has no business with the agent's instructions.

Each pack's SAP system and its system role are in `GET /registry`, and the deployment's
mode with every system's role in `GET /deployment/settings`, behind sign-in
(`system_roles.py`).
"""

import json
import logging
import os
import re
from datetime import UTC, datetime
from decimal import Decimal

import boto3
import config_overrides
import emergency_stop
import guard_rules
import pack_access
import pack_status
import registry
import system_roles
from api_http import HttpError, field, json_body, respond, route
from contracts import PACK_ID_PATTERN
from ddb_paginate import paginate
from entitlement import CallerEntitlement, caller_groups, caller_identity
from pack_tiers import LIMIT_SYMBOLS, bounds

logger = logging.getLogger()
logger.setLevel(logging.INFO)

table = boto3.resource("dynamodb").Table(os.environ["CONFIG_TABLE"])
s3 = boto3.client("s3")

_ddb = None


def _ddb_client():
    """Low-level DynamoDB client for TransactWriteItems — the resource API has no wrapper."""
    global _ddb
    if _ddb is None:
        _ddb = boto3.client("dynamodb")
    return _ddb


_PACK_ID = re.compile(PACK_ID_PATTERN)

CONTACT_NS = "contact"
CONSTANT_NS_PREFIX = "constant#"

#: Shared 403 body for an unauthorized config write.
_FORBIDDEN_AUTHOR = {
    "error": "You are not authorized to change this deployment's config"
}

# Contacts are notification targets, so a value that is not an address is a
# defect the agent cannot recover from. Deliberately loose on the local part and
# strict on the shape — this rejects typos, it is not an RFC 5322 parser.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")
_MAX_VALUE_LEN = 254


def _defaults() -> tuple[dict, dict]:
    """(contacts, {pack_id: {SYMBOL: value}}) as declared by the deployment.

    Contacts are a deploy-time value; the constants allowlist is whatever is
    published, so a pack that went live an hour ago is tunable now.
    """
    return json.loads(os.environ.get("CONTACTS_JSON", "{}")), registry.constants()


def _overrides() -> list[dict]:
    """Every override row. Small by construction — one row per edited symbol."""
    return paginate(table.scan)


def _origin(pack_id: str) -> str:
    """`pack v7`: the version a pack's own values come from, `pack` before it has one."""
    active = pack_status.pointer(s3, os.environ.get("REGISTRY_BUCKET", ""), pack_id)
    return f"pack v{active['version']}" if active else "pack"


def _write_guards(pack_id: str) -> list:
    """The write guards the published pack enables."""
    document = (registry.packs().get(pack_id) or {}).get("pack") or {}
    return list(document.get("write_guards") or [])


def _actor(event: dict) -> str:
    """Identity from the trusted authorizer context, never from the body.

    The shared resolver, not a copy of it: an override written here and an approval
    recorded by `api/cases` have to name the same operator the same way, and this
    route reads the API layer already.
    """
    return caller_identity(event).display or "unknown"


def _validate_contact(key: str, value, allowed: dict) -> str | None:
    if key not in allowed:
        return f"Unknown contact {key!r}. Known: {sorted(allowed)}"
    if not isinstance(value, str) or not _EMAIL_RE.match(value.strip()):
        return f"Contact {key!r} must be an email address"
    if len(value.strip()) > _MAX_VALUE_LEN:
        return f"Contact {key!r} exceeds {_MAX_VALUE_LEN} characters"
    return None


def _validate_constant(skill: str, symbol: str, value, allowed: dict) -> str | None:
    if skill not in allowed:
        return f"Unknown skill {skill!r}. Known: {sorted(allowed)}"
    if symbol not in allowed[skill]:
        return f"Unknown constant {symbol!r} for {skill!r}. Known: {sorted(allowed[skill])}"
    # A rule's value is the pack's, so it moves with the pack: a stack that overrode
    # one would run a tolerance no other stack, and no version, records.
    if symbol not in LIMIT_SYMBOLS:
        return (
            f"{symbol} is a rule value of {skill!r}, which is pack content: set it with "
            f"PATCH /packs/{skill} (its constants, a new version) in development and "
            "promote that version by bundle. Controls lower only "
            f"{' and '.join(sorted(LIMIT_SYMBOLS))}."
        )
    # bool is an int subclass, and `True` would silently store as 1 in a
    # threshold the SOP compares numerically.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return f"Constant {symbol!r} must be a number"
    low, high = bounds(symbol)
    if not low <= value <= high:
        return f"Constant {symbol!r} must be between {low} and {high}"
    # A limit's override may not RAISE the pack's value. The pack owner reviewed that
    # number; widening AUTO_WRITE_LIMIT_USD lets the agent write more to SAP without a
    # human, and widening DUAL_APPROVAL_LIMIT_USD lets one person approve more.
    ceiling = allowed[skill].get(symbol)
    if ceiling is not None and value > ceiling:
        return (
            f"{symbol} may not be raised above the pack's value ({ceiling}). An "
            "override may lower it, not widen it."
        )
    return None


def _plan_contacts(body: dict, contacts: dict) -> tuple[list, list]:
    """Return (writes, errors). A write is (namespace, key, value-or-None)."""
    writes, errors = [], []
    for key, value in (field(body, "contacts", dict) or {}).items():
        if value is None:
            writes.append((CONTACT_NS, key, None))
            continue
        error = _validate_contact(key, value, contacts)
        if error:
            errors.append(error)
        else:
            writes.append((CONTACT_NS, key, value.strip()))
    return writes, errors


def _plan_limits(body: dict, pack_id: str, constants: dict) -> tuple[list, list]:
    """Return (writes, errors) for `PUT /packs/{pack_id}/controls`. Collects every
    error: an operator editing a form wants all of it back, not one field at a time."""
    writes, errors = [], []
    limits = field(body, "limits", dict)
    if limits is None:
        return [], ["`limits` is required: {SYMBOL: number, or null to clear}"]
    for symbol, value in limits.items():
        if value is None:
            writes.append((CONSTANT_NS_PREFIX + pack_id, symbol, None))
            continue
        error = _validate_constant(pack_id, symbol, value, constants)
        if error:
            errors.append(error)
        else:
            writes.append((CONSTANT_NS_PREFIX + pack_id, symbol, value))
    return writes, errors


def _transact(event: dict, writes: list) -> dict:
    """All-or-nothing: DynamoDB TransactWriteItems across up to 100 items. A partially
    applied edit leaves the corpus in a state no operator chose and no default describes."""
    if not writes:
        raise HttpError(400, "No valid fields provided")
    if len(writes) > 100:
        raise HttpError(
            400, f"Transaction exceeds 100 items ({len(writes)}); split the request"
        )
    actor, now = _actor(event), datetime.now(UTC).isoformat()
    transact_items = []
    for namespace, key, value in writes:
        if value is None:
            transact_items.append(
                {
                    "Delete": {
                        "TableName": os.environ["CONFIG_TABLE"],
                        "Key": {
                            "namespace": {"S": namespace},
                            "config_key": {"S": key},
                        },
                    }
                }
            )
            continue
        item = {
            "namespace": {"S": namespace},
            "config_key": {"S": key},
            "updated_by": {"S": actor},
            "updated_at": {"S": now},
        }
        if isinstance(value, float):
            item["value"] = {"N": str(Decimal(str(value)))}
        elif isinstance(value, (int, Decimal)):
            item["value"] = {"N": str(value)}
        else:
            item["value"] = {"S": str(value)}
        transact_items.append(
            {"Put": {"TableName": os.environ["CONFIG_TABLE"], "Item": item}}
        )
    _ddb_client().transact_write_items(TransactItems=transact_items)
    return {"updated": len(writes), "updated_by": actor}


def _registry(event: dict) -> dict:
    # `may_see` answers True for every pack while the floor is inert, so an undeclared
    # deployment gets the whole corpus and a declared one gets its slice.
    caller = CallerEntitlement.resolve(event)
    groups = caller_groups(event)
    packs = registry.packs()
    rows = registry.public_view(
        {pack_id for pack_id in packs if caller.may_see(pack_id)}
    )
    for row in rows:
        document = (packs.get(row["id"]) or {}).get("pack") or {}
        # What this caller may do in each, for the console to badge; each route still
        # decides for itself.
        row["access"] = pack_access.access(groups, caller, row["id"], document)
        # Which system the pack writes to, and whether it is production.
        row["sap_system"] = system_roles.pack_system(document)
    return respond(200, {"packs": rows}, event)


def _contact_overrides() -> dict:
    return {
        item["config_key"]: item["value"]
        for item in _overrides()
        if item["namespace"] == CONTACT_NS
    }


def _limit_overrides(pack_id: str) -> dict:
    """This stack's rows for one pack's constants, as stored (a rule row left by an
    older release included, which `effective_constants` ignores)."""
    return {
        item["config_key"]: item["value"]
        for item in _overrides()
        if item["namespace"] == CONSTANT_NS_PREFIX + pack_id
    }


#: Where `POST /deployment/stop` keeps the last stop (`api/stop/index.py`).
STOP_NAMESPACE = "deployment"
STOP_KEY = "last-stop"


def _last_stop() -> dict | None:
    """The last stop's record, with `stands` and the clear from the stop's own
    parameter, which is what every gate reads. None before any stop."""
    item = table.get_item(
        Key={"namespace": STOP_NAMESPACE, "config_key": STOP_KEY}
    ).get("Item")
    state = emergency_stop.read()
    if not item and state is None:
        return None
    # `respond` writes the Decimals DynamoDB returns as JSON numbers.
    record = {
        k: v for k, v in (item or {}).items() if k not in ("namespace", "config_key")
    }
    if state is not None:
        record |= emergency_stop.public(state)
        record["stands"] = state.get("stands") is not False
    return record


def _settings(event: dict, method: str) -> dict:
    """`/deployment/settings`: the contacts, defaults and overrides apart, and where the
    deployment runs. Scoped as `/registry` is: only to a caller entitled to some pack."""
    caller = CallerEntitlement.resolve(event)
    contacts, _ = _defaults()
    if method == "GET":
        if not caller.visible_packs:
            return respond(403, {"error": "Not entitled to any pack"}, event)
        overrides = _contact_overrides()
        return respond(
            200,
            {
                # Apart, not pre-merged: the UI says "this differs from what was
                # deployed", and a merged map cannot.
                "contacts": {
                    "defaults": contacts,
                    "overrides": overrides,
                    "effective": config_overrides.effective_contacts(
                        contacts, overrides
                    ),
                },
                "deployment": system_roles.deployment(pack_access.ROLE),
                # The last emergency stop (`api/stop`), or None before any.
                "last_stop": _last_stop(),
                # What the caller may do across the deployment, by the rules the
                # write routes decide with.
                "access": {
                    "may_create_pack": pack_access.may_create_pack(
                        caller_groups(event)
                    ),
                    "may_edit_contacts": pack_access.may_edit_contacts(caller),
                },
            },
            event,
        )
    if method == "PUT":
        body = json_body(event)
        # Contacts are deployment-wide, so the author floor alone gates them. Closed by
        # default: with no floor every write is refused.
        if not pack_access.may_edit_contacts(caller):
            return respond(403, _FORBIDDEN_AUTHOR, event)
        if field(body, "contacts", dict) is None:
            raise HttpError(400, "`contacts` is required")
        writes, errors = _plan_contacts(body, contacts)
        if errors:
            raise HttpError(400, "Invalid fields", details=errors)
        return respond(200, _transact(event, writes), event)
    return respond(405, {"error": "Method not allowed"}, event)


def _pack(event: dict) -> tuple[str, dict, CallerEntitlement]:
    pack_id = str((event.get("pathParameters") or {}).get("pack_id") or "")
    caller = CallerEntitlement.resolve(event)
    entry = registry.packs().get(pack_id) if _PACK_ID.fullmatch(pack_id) else None
    if not entry:
        raise HttpError(404, f"No published pack {pack_id}")
    if not caller.may_see(pack_id):
        raise HttpError(403, "Not entitled to this pack")
    return pack_id, entry.get("pack") or {}, caller


def _effective(pack_id: str, declared: dict) -> dict:
    """Every declared constant, effective here, with `guarded_by`."""
    return guard_rules.annotate(
        config_overrides.effective_constants(
            declared, _limit_overrides(pack_id), _origin(pack_id)
        ),
        _write_guards(pack_id),
    )


def _rules(event: dict) -> dict:
    pack_id, document, caller = _pack(event)
    # The published constants, as `PUT` validates against them.
    declared = registry.constants().get(pack_id) or {}
    rules = {
        s: row
        for s, row in _effective(pack_id, declared).items()
        if s not in LIMIT_SYMBOLS
    }
    bucket = os.environ.get("REGISTRY_BUCKET", "")
    active = pack_status.pointer(s3, bucket, pack_id)
    # Which process types' SOPs cite each rule, from the active version's stored SOPs:
    # one bundle read per version per function instance, never one per SOP.
    cited = pack_status.active_citations(s3, bucket, pack_id, active)
    rules = {
        s: {**row, "cited_by": None if cited is None else cited.get(s, [])}
        for s, row in rules.items()
    }
    access = pack_access.access(caller_groups(event), caller, pack_id, document)
    return respond(
        200,
        {
            "pack_id": pack_id,
            "version": active["version"] if active else None,
            "rules": rules,
            "bounds": {s: bounds(s) for s in rules},
            "may_edit": access["edit"]["rule_values"],
        },
        event,
    )


def _controls(event: dict, method: str) -> dict:
    pack_id, document, caller = _pack(event)
    declared = registry.constants().get(pack_id) or {}
    if method == "GET":
        limits = {
            s: {**row, "ceiling": config_overrides._plain(declared[s])}
            for s, row in _effective(pack_id, declared).items()
            if s in LIMIT_SYMBOLS
        }
        access = pack_access.access(caller_groups(event), caller, pack_id, document)
        return respond(
            200,
            {
                "pack_id": pack_id,
                "limits": limits,
                "bounds": {s: bounds(s) for s in limits},
                **{
                    k: document.get(k)
                    for k in (
                        "entitlement",
                        "write_guards",
                        "verifier",
                        "write_amount_field",
                        "write_limit_waiver",
                    )
                },
                "may_lower": access["lower_limits"],
            },
            event,
        )
    if method == "PUT":
        # `business-operations-limit-owners`, then the pack's `limit_groups`: the
        # function `access.lower_limits` reports. Closed when the group is unset.
        if not pack_access.may_lower_limits(caller, pack_id):
            raise HttpError(
                403, f"You are not authorized to lower the limits of {pack_id!r}"
            )
        writes, errors = _plan_limits(json_body(event), pack_id, registry.constants())
        if errors:
            raise HttpError(400, "Invalid fields", details=errors)
        return respond(200, _transact(event, writes), event)
    return respond(405, {"error": "Method not allowed"}, event)


@route
def handler(event):
    method = event.get("httpMethod", "GET")
    # Told apart by the mapped resource rather than the raw path, so a stage prefix
    # cannot change the answer.
    resource = (event.get("resource") or "").rstrip("/")
    if resource.endswith("/registry"):
        if method != "GET":
            return respond(405, {"error": "Method not allowed"}, event)
        return _registry(event)
    if resource.endswith("/deployment/settings"):
        return _settings(event, method)
    if resource.endswith("/rules") and method == "GET":
        return _rules(event)
    if resource.endswith("/controls"):
        return _controls(event, method)
    return respond(405, {"error": "Method not allowed"}, event)
