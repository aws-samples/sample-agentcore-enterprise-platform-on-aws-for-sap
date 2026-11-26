# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The operator's re-run: `ops.py rerun` puts one existing case back on the agent queue.

The agent queue's policy admits its producers' roles alone, so an operator reaches it by
invoking this function (IAM `lambda:InvokeFunction`, which the stack's admin credentials
hold) rather than by sending to it. It takes a case id and builds the message itself:
trigger `operator-rerun` and no payload. The message therefore claims no human decision,
and the invoker grants it no approval: it reads one only off a `console-action` or
`ticket-action` message, and then only when the case records it.

Refused: a case that does not exist; a settled one (`complete`, `sap_updated`), which
the invoker would drop anyway and whose SAP write may already be posted; and, unless
`force`, one parked on a person, whose re-run overwrites what they are asked to answer.
"""

import logging

import emergency_stop
from case_key import try_normalize_case_id
from case_status import PARKED_ON_PERSON_STATUSES, SETTLED_STATUSES
from case_store import QUEUE_URL, get_cases_table, send

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TRIGGER = "operator-rerun"


def refusal(status: str | None, force: bool) -> str | None:
    """Why a case in `status` (None: no such case) may not be re-run, or None."""
    if status is None:
        return "no such case"
    if status in SETTLED_STATUSES:
        return (
            f"settled as {status!r}: the invoker drops a run on a settled case, "
            "and its SAP write may already be posted"
        )
    if status in PARKED_ON_PERSON_STATUSES and not force:
        return (
            f"parked on a person as {status!r}: a re-run overwrites what they are "
            "asked to answer; pass --force to re-run it anyway"
        )
    return None


def handler(event, context):
    if not QUEUE_URL:
        return {"queued": False, "error": "no agent queue"}
    raw = event.get("case_id")
    case_id = try_normalize_case_id(raw) if isinstance(raw, str) else None
    if not case_id:
        return {"queued": False, "error": f"not a case id: {raw!r}"}
    item = (
        get_cases_table()
        .get_item(
            Key={"case_id": case_id},
            ProjectionExpression="#s",
            ExpressionAttributeNames={"#s": "status"},
            ConsistentRead=True,
        )
        .get("Item")
    )
    status = None if item is None else str(item.get("status") or "")
    reason = refusal(status, event.get("force") is True)
    if not reason and (stop := emergency_stop.standing()):
        reason = emergency_stop.refusal(stop)
    if reason:
        logger.warning("Refused re-run of case=%s: %s", case_id, reason)
        return {"queued": False, "case_id": case_id, "status": status, "error": reason}
    message_id = send(case_id, TRIGGER, dedup_id=context.aws_request_id)
    logger.info("Queued operator re-run of case=%s (was %r)", case_id, status)
    return {
        "queued": True,
        "case_id": case_id,
        "status": status,
        "trigger": TRIGGER,
        "message_id": message_id,
    }
