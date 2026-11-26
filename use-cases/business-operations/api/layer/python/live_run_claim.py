# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The server-side claim on a case's `live_run` attribute.

`agent/utils/live_run.py` owns a run's *progress*; this owns its *start*.
A run is claimed before the agent is invoked, so two browsers cannot start the same
case twice — and the claim is written in the shape the agent later overwrites, so the
browser's `live_run` watcher needs no special case for a claim it has seen no segments
for yet.

In the layer rather than in one Lambda because two callers need it: `api/runs`
claims, and `agent_invoker` releases when a start it was handed fails outright.

Ceiling worth knowing: `LiveRunWriter.maybe_write` writes `live_run` with only
`attribute_exists(case_id)` as its condition, so the agent clobbers a competing claim
rather than losing to it. The claim narrows the race to the invocation window; it does
not close it. And `maybe_write` returns early when there are no segments yet, so
`updated_at` does not advance until the agent's first segment — a cold start spends
part of the stale window below.
"""

import logging
from datetime import datetime, timedelta, timezone

from botocore.exceptions import ClientError
from contracts import LIVE_RUN_STALE_SECONDS

logger = logging.getLogger(__name__)

# The schema's live-run window. A claim older than this belongs to a run whose agent
# died without writing a terminal event, and it must not wedge the case forever.
STALE_SECONDS = LIVE_RUN_STALE_SECONDS


class AlreadyRunning(Exception):
    """A live, non-stale run already holds this case."""

    def __init__(self, run_id: str):
        super().__init__(f"case already has live run {run_id!r}")
        self.run_id = run_id


class MissingCase(Exception):
    """There is no case row to claim."""


class CaseSettled(Exception):
    """The case is in a status the claim was told to refuse."""

    def __init__(self, status: str):
        super().__init__(f"case is settled as {status!r}")
        self.status = status


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def claim_live_run(
    table,
    case_key: dict,
    *,
    run_id: str,
    trigger: str,
    prompt: str,
    refuse: frozenset[str] = frozenset(),
) -> None:
    """Take the case for `run_id`, or say who has it.

    `table` is a DynamoDB Table resource and `case_key` the full key from
    `to_case_key` — the cases table has one partition key and no sort key, so the
    conditional update below is the whole mutual-exclusion mechanism. A case whose
    status is in `refuse` is not claimed, in the same condition, so a run cannot
    start on it between a read and the claim.
    """
    stale_before = (
        datetime.now(timezone.utc) - timedelta(seconds=STALE_SECONDS)
    ).isoformat()
    now = _now_iso()
    refused = {f":refuse{i}": s for i, s in enumerate(sorted(refuse))}
    condition = (
        "attribute_exists(case_id) AND "
        "(attribute_not_exists(live_run) OR live_run.updated_at < :stale)"
    )
    if refused:
        condition += f" AND NOT (#st IN ({', '.join(refused)}))"
    try:
        table.update_item(
            Key=case_key,
            UpdateExpression="SET live_run = :live",
            ConditionExpression=condition,
            **({"ExpressionAttributeNames": {"#st": "status"}} if refused else {}),
            ExpressionAttributeValues={
                **refused,
                ":live": {
                    "run_id": run_id,
                    "trigger": trigger,
                    "prompt": prompt,
                    "started_at": now,
                    "updated_at": now,
                    "segments": [],
                },
                ":stale": stale_before,
            },
            # Without this a failed condition cannot say *why* it failed, and "no such
            # case" would be reported to the browser as "already running".
            ReturnValuesOnConditionCheckFailure="ALL_OLD",
        )
    except ClientError as error:
        if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        # Returned in low-level AttributeValue form even through the resource API.
        item = error.response.get("Item") or {}
        if not item:
            raise MissingCase(f"no case for {case_key}") from error
        status = item.get("status", {}).get("S", "")
        if status in refuse:
            raise CaseSettled(status) from error
        held_by = item.get("live_run", {}).get("M", {}).get("run_id", {}).get("S", "")
        raise AlreadyRunning(held_by) from error


def release_live_run(table, case_key: dict, *, run_id: str) -> bool:
    """Drop `live_run` if `run_id` still holds it. True when it did.

    Conditioned on the run id so a late release cannot un-run a later run that has
    since claimed the case.
    """
    try:
        table.update_item(
            Key=case_key,
            UpdateExpression="REMOVE live_run",
            ConditionExpression="live_run.run_id = :run_id",
            ExpressionAttributeValues={":run_id": run_id},
        )
        return True
    except ClientError as error:
        if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.info("live_run for %s is no longer held by %s", case_key, run_id)
            return False
        raise
