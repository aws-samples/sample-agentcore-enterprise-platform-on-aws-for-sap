# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""A human's decision on a case, recorded on the case before the run is queued.

An earlier version recorded no approver: the `sub` rode the queue message, became the run's
`initiator` on a trace, and was lost when the trace cap evicted it. Both decision
routes (a console reply and a ticket action) now append an `action_log` entry in one
conditional update, so the record exists before the agent runs, and a decision on a
case that settled in the meantime is refused by DynamoDB, not by a read-then-write race.

An approval is scoped to the write the human saw. The write is the latest
`proposed_write` in the case's traces (the one the console renders as the approval
card); its sha256 goes in the entry and on the queued message, so whatever authorizes
the write can check it is that one. The update is conditioned on the run count read
just before it (`cost_summary.invocation_count`, which every trace save increments in
the same statement), so a run that proposed something else in between makes the
decision fail rather than approve a write nobody read. Not the length of the trace
list: that stops growing at the trace cap.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from botocore.exceptions import ClientError
from case_status import PARKED_ON_PERSON_STATUSES, SETTLED_STATUSES
from decision_options import digest as _digest

#: Above this amount a proposed SAP write needs two different approvers. A pack constant,
#: absent by default; where declared, an override may lower it but never raise it.
DUAL_LIMIT_SYMBOL = "DUAL_APPROVAL_LIMIT_USD"
_LIMIT_CURRENCY = "USD"


class Settled(Exception):
    """The case is missing, already decided, or changed since the caller read it."""


class AlreadyApproved(Exception):
    """This person already approved this write, with nothing decided since: a second
    approval must come from someone else, so nothing is recorded."""


#: What both decision routes answer, with 409, for an `AlreadyApproved`.
ALREADY_APPROVED = (
    "You already approved this change. A different approver must approve it too."
)


#: `authorize`'s refusals. Two, because a clerk who may read the case should be told
#: to find an approver, not that they cannot see a pack they plainly can.
NOT_ENTITLED = "not_entitled"
NOT_AN_APPROVER = "not_an_approver"


def authorize(caller, pack_id: object, decision: str) -> str | None:
    """None if `caller` may record `decision` on a case of `pack_id`, else why not.

    The one approval gate, for both decision routes (a console reply and a ticket
    action). Every decision takes the pack's `approve_groups`: `approved` authorizes a
    SAP write, and a denial or a free-text reply resumes a run whose unapproved writes
    at or under the pack's limit the run checks pass. `caller` is an
    `entitlement.CallerEntitlement`.
    """
    if not caller.may_see(pack_id):
        return NOT_ENTITLED
    if not caller.may_approve(pack_id):
        return NOT_AN_APPROVER
    return None


def _proposed_writes(traces: object):
    """Every write a run asked permission for, newest first."""
    ordered = sorted(
        (t for t in traces or [] if isinstance(t, dict)),
        key=lambda t: str(t.get("timestamp") or ""),
        reverse=True,
    )
    for trace in ordered:
        segments = trace.get("segments")
        for segment in reversed(segments if isinstance(segments, list) else []):
            evidence = segment.get("evidence") if isinstance(segment, dict) else None
            if (
                isinstance(evidence, dict)
                and evidence.get("kind") == "notification"
                and evidence.get("proposed_write")
            ):
                yield evidence["proposed_write"]


def latest_proposed_write(traces: object) -> dict | None:
    """The write the latest run asked permission for, as the console finds it."""
    return next(_proposed_writes(traces), None)


def proposed_write_with_digest(traces: object, digest: str) -> dict | None:
    """The proposed write whose `write_digest` is `digest`, or None."""
    return next(
        (w for w in _proposed_writes(traces) if write_digest(w) == digest), None
    )


def write_digest(proposed_write: object) -> str | None:
    """sha256 of the write's canonical JSON, or None when there is none."""
    return _digest(proposed_write)


def option_decision(option: dict) -> str:
    """The decision choosing `option` records: an approval of its write, or a choice
    that approves none, as a denial approves none."""
    return "approved" if option.get("write") else "chose"


def record_decision(
    table,
    case_id: str,
    *,
    decision: str,
    actor: str,
    actor_display: str,
    source: str,
    case: dict,
    option: dict | None = None,
    option_set: str | None = None,
) -> dict:
    """Append the decision to the case's action log; return the entry written.

    `case` is the item as the caller read it, with `agent_traces` and `cost_summary`.
    Raises :class:`Settled` when the case is gone, settled, or has had a run since, and
    :class:`AlreadyApproved`, writing nothing, when `actor` already approved the same write
    with nothing decided since.

    With `option` (one of the case's `decision_options`, from the set `option_set`), the
    decision is that option's: its write is the one approved, copied onto the entry as
    the person saw it, and the update also requires the case still to offer that set
    and to be parked on a person.
    """
    if option is not None:
        write = option.get("write") or None
        decision = option_decision(option)
        approved_write = write_digest(write)
        chosen = {
            "option": option["id"],
            "option_label": option["label"],
            **({"option_write": write} if write else {}),
        }
    else:
        approved_write = write_digest(latest_proposed_write(case.get("agent_traces")))
        chosen = {}
    log = case.get("action_log") or []
    if (
        decision == "approved"
        and approved_write
        and any(e.get("actor") == actor for _, e in approvals_of(log, approved_write))
    ):
        raise AlreadyApproved(case_id)
    entry = {
        "action": f"human_{decision}",
        "timestamp": datetime.now(UTC).isoformat(),
        "actor": actor,
        "actor_display": actor_display,
        "source": source,
        **chosen,
        **({"approved_write": approved_write} if approved_write else {}),
    }
    settled = sorted(SETTLED_STATUSES)
    values: dict = {":empty": [], ":entry": [entry]}
    values |= {f":s{i}": s for i, s in enumerate(settled)}
    runs = (case.get("cost_summary") or {}).get("invocation_count")
    if runs is not None:
        unchanged = "cost_summary.invocation_count = :n"
        values[":n"] = runs
    else:
        unchanged = "attribute_not_exists(cost_summary.invocation_count)"
    condition = (
        "attribute_exists(case_id) AND NOT (#s IN ("
        + ", ".join(f":s{i}" for i in range(len(settled)))
        + f")) AND {unchanged}"
    )
    if decision == "approved":
        # The log as read: two approvals racing record one, so the check above holds.
        values[":len"] = len(log)
        condition += (
            " AND (attribute_not_exists(action_log) OR size(action_log) = :len)"
            if not log
            else " AND size(action_log) = :len"
        )
    if option is not None:
        parked = sorted(PARKED_ON_PERSON_STATUSES)
        values[":set"] = option_set
        values |= {f":p{i}": s for i, s in enumerate(parked)}
        condition += (
            " AND decision_options.set_id = :set AND #s IN ("
            + ", ".join(f":p{i}" for i in range(len(parked)))
            + ")"
        )
    try:
        table.update_item(
            Key={"case_id": case_id},
            UpdateExpression=(
                "SET action_log = list_append(if_not_exists(action_log, :empty), :entry)"
            ),
            ConditionExpression=condition,
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues=values,
        )
    except ClientError as error:
        if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise Settled(case_id) from error
        raise
    return entry


def approvals_of(action_log: object, digest: object) -> list[tuple[int, dict]]:
    """The unused approvals of the write `digest` at the end of the case's decisions,
    newest first, as `(index in action_log, entry)`.

    A denial, a reply, an approval of another write or one a run already used ends the
    run of approvals, so a second approval counts only for the same write, with nothing
    decided in between.
    """
    found: list[tuple[int, dict]] = []
    for index, entry in reversed(list(enumerate(action_log or []))):
        if not isinstance(entry, dict) or not str(entry.get("action", "")).startswith(
            "human_"
        ):
            continue
        if (
            entry.get("action") != "human_approved"
            or entry.get("approved_write") != digest
            or entry.get("consumed_by_run")
        ):
            break
        found.append((index, entry))
    return found


def needs_second_approver(
    dual_limit: object, attributes: object, approvers: set[str], pushed: bool = False
) -> bool:
    """Whether a write on a case with these `attributes` still needs a second person.

    In doubt it does: a declared limit that cannot be read, an amount that cannot be,
    a currency the limit is not in, or a case a sender pushed, whose amount is the
    sender's word (the run checks count it the same way).
    """
    if dual_limit is None or len(approvers) >= 2:
        return False
    if pushed:
        return True
    attributes = attributes if isinstance(attributes, dict) else {}
    try:
        limit = Decimal(str(dual_limit))
        amount = Decimal(str(attributes.get("amount")))
    except (InvalidOperation, TypeError):
        return True
    if not (limit.is_finite() and amount.is_finite()):
        return True
    return str(attributes.get("currency") or "") != _LIMIT_CURRENCY or amount > limit


def dual_limit(pack_id: object):
    """The pack's DUAL_APPROVAL_LIMIT_USD with any override applied, or None when the pack
    declares none (one approval releases any write, as before)."""
    import config_overrides
    import registry

    declared = ((registry.packs().get(str(pack_id)) or {}).get("pack") or {}).get(
        "constants"
    ) or {}
    if DUAL_LIMIT_SYMBOL not in declared:
        return None
    return config_overrides.resolved_constants(
        declared, config_overrides.constant_overrides(str(pack_id))
    )[DUAL_LIMIT_SYMBOL]


def awaiting_second_approver(case: dict, entry: dict, pack_id: object) -> bool:
    """Whether the approval `entry`, just recorded on `case`, still needs another person
    before the run may resume. `case` is the item as read before the entry was written."""
    digest = entry.get("approved_write")
    if entry.get("action") != "human_approved" or not digest:
        return False
    log = [*(case.get("action_log") or []), entry]
    approvers = {
        str(e["actor"]) for _, e in approvals_of(log, digest) if e.get("actor")
    }
    return needs_second_approver(
        dual_limit(pack_id),
        case.get("attributes"),
        approvers,
        pushed=case.get("origin") == "sap-push",
    )
