# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The batch sweeper: enqueue cases detected but never handed to the agent.

The poller and SAP push enqueue a case only when they create it, so a case created
under the `manual` trigger mode, or whose enqueue failed after its put, sits in `detected`
with no unattended caller. This is that caller. It obeys the same switches: under a
`manual` deployment trigger mode it does nothing, and it skips a case whose pack is `manual`. The run it queues carries no human: the invoker starts it
as the use case's machine client.
"""

import logging
import os
from datetime import UTC, datetime, timedelta

from boto3.dynamodb.conditions import Key
from case_key import try_normalize_case_id
from case_store import QUEUE_URL, Gate, get_cases_table, send
from warm_stagger import Stagger, remaining_seconds

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Above the poller's schedule, so the sweep stays off a case its producer has only
# just raised. The double-run guard is the invoker's claim, not this.
MIN_AGE_MINUTES = 15
# One sweep's bound; the next sweep takes the rest.
MAX_CASES = 100
WARM_DELAY_SECONDS = int(os.environ.get("WARM_DELAY_SECONDS", "0"))


def stale_detected_cases(table, cutoff: str) -> list[dict]:
    """Cases still `detected` and created at or before `cutoff`, at most MAX_CASES."""
    cases: list[dict] = []
    kwargs = {
        "IndexName": "status-index",
        "KeyConditionExpression": Key("status").eq("detected"),
        "FilterExpression": "created_at <= :cutoff",
        "ExpressionAttributeValues": {":cutoff": cutoff},
    }
    while len(cases) < MAX_CASES:
        resp = table.query(**kwargs)
        cases.extend(resp.get("Items", []))
        if not resp.get("LastEvaluatedKey"):
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return cases[:MAX_CASES]


def _enqueue(case: dict) -> bool:
    case_id = try_normalize_case_id(case.get("case_id"))
    if not case_id:
        logger.warning("Skipping case with unusable id: %r", case.get("case_id"))
        return False
    send(case_id, "batch")
    return True


def handler(event, context):
    if not QUEUE_URL:
        logger.error("No AGENT_QUEUE_URL: nothing to enqueue into")
        return {"swept": 0, "enqueued": 0, "skipped": "no-queue"}
    gate = Gate()
    if gate.stop:
        logger.info("An emergency stop stands: the sweep is a no-op")
        return {"swept": 0, "enqueued": 0, "skipped": "emergency-stop"}
    if gate.deployment != "auto":
        logger.info("trigger-mode=%s: the sweep is a no-op", gate.deployment)
        return {"swept": 0, "enqueued": 0, "skipped": "manual"}

    cutoff = (datetime.now(UTC) - timedelta(minutes=MIN_AGE_MINUTES)).isoformat()
    stale = stale_detected_cases(get_cases_table(), cutoff)
    # A pack on `manual` keeps its cases for a person; its mode is read once per sweep.
    cases = [c for c in stale if gate.allows(str(c.get("pack_id") or ""))]
    stagger = Stagger(
        pack_of=lambda c: c.get("pack_id", ""),
        send=_enqueue,
        warm_delay=WARM_DELAY_SECONDS,
    )
    for case in cases:
        stagger.offer(case)
    enqueued = stagger.flush(remaining_seconds(context))
    logger.info(
        "Batch sweep: %d stale, %d on a manual pack, %d enqueued",
        len(stale),
        len(stale) - len(cases),
        enqueued,
    )
    return {"swept": len(stale), "enqueued": enqueued}
