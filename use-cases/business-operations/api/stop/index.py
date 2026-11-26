# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Emergency stop: nothing new starts, and every run under way is stopped, until an
admin clears it.

    POST /deployment/stop          {"reason": "..."}
    POST /deployment/stop/clear    {"reason": "..."}

Admins only, as the deployment's trigger mode is (`entitlement.ADMIN_ROUTES`). Its own Lambda,
because it is the one route that holds the switch, the stop and the right to end runs.
`ops.py emergency-stop` invokes it directly over IAM (`_direct`).

A stop, in this order, each step whatever the one before it managed, because a stop
that gives up halfway is the worst answer to an emergency:

0. The stop stands (`emergency_stop.py`): every path that starts a run refuses from
   here, so nothing starts while the steps below sweep the runs. The parameter's write
   fires the audit-change alert, and so does a clear's.
1. The deployment's trigger mode to `manual`, written every time, so the audit-change alert on
   the switch fires for every stop (`audit_alerts.trigger_mode_pattern`).
1a. The agent queue's consumer paused, so queued work waits on the queue.
2. Every unexpired run grant revoked. The run checks refuse each Gateway call a run makes
   without its grant, so a run that ignores the session stop below reads and writes
   nothing from here on. This role may delete grants and never write one: the grants
   table's own policy keeps minting to the invoker.
3. Each run with a case: its runtime session stopped, its `live_run` released, and its
   case parked when it reads `processing`, so the invoker's redelivery of a queued run
   finds a case parked on a person and does not start it again.
4. The stop recorded where `GET /deployment/settings` reads it back.

A SAP write that passed the run checks before step 2 is not undone: SAP has it. Its run may
not have recorded it on the case, so a parked case says to check SAP first.

A clear writes the stop `stands: false` and resumes the consumer. It leaves the trigger mode
`manual` and starts nothing: a parked case is started again by a person.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import UTC, datetime

import boto3
import emergency_stop
import runtime_session
from api_http import HttpError, json_body, respond, route
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError
from case_key import CaseKeyError, to_case_key, to_runtime_session_id
from ddb_paginate import paginate
from delegating_token import get_delegating_token
from entitlement import caller_identity
from live_run_claim import release_live_run

logger = logging.getLogger()
logger.setLevel(logging.INFO)

#: `/<project>/<env>/use-cases/business-operations/autonomy`.
PREFIX = os.environ["TRIGGER_MODE_PREFIX"]
MODE = "trigger-mode"
ACTOR_SUFFIX = "-last-changed-by"
CASES_TABLE = os.environ["CASES_TABLE"]
CONFIG_TABLE = os.environ["CONFIG_TABLE"]
#: Empty until the invoker is wired: there are no runs, so nothing to revoke.
RUN_GRANTS_TABLE = os.environ.get("RUN_GRANTS_TABLE", "")
RUNTIME_ARN_PARAMETER = os.environ.get("RUNTIME_ARN_PARAMETER", "")
#: The stop's own parameter (`names.trigger_mode_parameters` `stop`).
STOP_PARAMETER = os.environ.get(emergency_stop.PARAMETER_ENV, "")
#: The invoker's agent-queue consumer; empty without the pipeline.
QUEUE_MAPPING_ID = os.environ.get("AGENT_QUEUE_MAPPING_ID", "")
QUEUE_MAPPING_TRIES = 6
QUEUE_MAPPING_WAIT_SECONDS = 2
REGION = os.environ.get("AWS_REGION", "us-east-1")

#: Where the last stop is kept: one row in the config table, under its own namespace,
#: which this role alone may write (`dynamodb:LeadingKeys`).
STOP_NAMESPACE = "deployment"
STOP_KEY = "last-stop"

REASON_MAX = 500
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
#: The record lists this many runs; `runs_total` counts them all.
RUNS_LISTED = 100
#: Inside API Gateway's 29 seconds, with room for the record.
BUDGET_SECONDS = 20
SESSION_TIMEOUT_SECONDS = 5
WORKERS = 10

ssm = boto3.client("ssm")
dynamodb = boto3.resource("dynamodb")
lambda_client = boto3.client("lambda")

ACTOR_MAX = 256


class NothingStands(Exception):
    """A clear with no stop standing."""


def dispatch(event, context=None):
    """The function's entry point. API Gateway's proxy events always carry
    `httpMethod`; a direct invoke over IAM (`ops.py emergency-stop`) never does, and
    needs `lambda:InvokeFunction`."""
    if "httpMethod" not in event:
        return _direct(event)
    return handler(event, context)


@route(refresh=False)
def handler(event):
    if event.get("httpMethod") != "POST":
        return respond(405, {"error": "Method not allowed"}, event)
    reason = _reason(json_body(event))
    if str(event.get("resource") or "").endswith("/clear"):
        try:
            return respond(200, {"stop": clear(reason, _actor(event))}, event)
        except NothingStands:
            raise HttpError(409, "No emergency stop stands") from None
    return respond(200, {"stop": stop(reason, _actor(event))}, event)


def _direct(event: dict) -> dict:
    """`{"action": "stop"|"clear", "reason", "actor"}` from an operator with IAM. The
    actor is what the operator's tool says (its STS identity); CloudTrail names the
    IAM principal that invoked."""
    action = event.get("action")
    actor = str(event.get("actor") or "").strip()[:ACTOR_MAX] or "unknown"
    if action not in ("stop", "clear"):
        return {"ok": False, "error": "action must be stop or clear"}
    try:
        reason = _reason(event)
    except HttpError as error:
        return {"ok": False, "error": error.body["error"]}
    actor = f"{actor} (IAM)"
    if action == "clear":
        try:
            return {"ok": True, "stop": clear(reason, actor)}
        except NothingStands:
            return {"ok": False, "error": "No emergency stop stands"}
        except HttpError as error:
            return {"ok": False, "error": error.body["error"]}
    return {"ok": True, "stop": stop(reason, actor)}


def stop(reason: str, actor: str, *, budget: float = BUDGET_SECONDS) -> dict:
    """Do the steps and return the record (the contract's `stop`)."""
    deadline = time.monotonic() + budget
    stopped_at = datetime.now(UTC).isoformat(timespec="seconds")
    stands = _write_state(
        {
            "stands": True,
            "stopped_at": stopped_at,
            "stopped_by": actor,
            "reason": reason,
        }
    )
    mode_set = _manual(actor, stopped_at)
    queue_held = _consumer(enabled=False)
    grants = _live_grants()
    for grant in grants:
        _revoke(grant["run_id"])
    runs, complete = _stop_runs(grants, reason, actor, deadline)
    record = {
        "stopped_at": stopped_at,
        "stopped_by": actor,
        "reason": reason,
        "stands": stands,
        "queue_held": queue_held,
        "trigger_mode_set": mode_set,
        "runs_total": len(runs),
        "runs": runs[:RUNS_LISTED],
        "complete": complete,
    }
    # One line a log search finds, with every run, whatever the record could keep.
    logger.info(f"[STOP] {record | {'runs': runs}}")
    try:
        dynamodb.Table(CONFIG_TABLE).put_item(
            Item={"namespace": STOP_NAMESPACE, "config_key": STOP_KEY, **record}
        )
    except Exception as error:  # noqa: BLE001 - the stop itself has happened
        logger.error(f"[STOP] could not record the stop: {type(error).__name__}")
    return record


def clear(reason: str, actor: str) -> dict:
    """End the standing stop. The trigger mode stays `manual`; nothing is started."""
    state = emergency_stop.read(ssm)
    if state is None or state.get("stands") is False or state.get("unreadable"):
        if state and state.get("unreadable"):
            raise HttpError(
                503, "The emergency stop could not be read; nothing cleared"
            )
        raise NothingStands
    cleared = {
        **emergency_stop.public(state),
        "stands": False,
        "cleared_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "cleared_by": actor,
        "clear_reason": reason,
    }
    # The one write that matters: an unwritten clear leaves the stop standing, which is
    # the safe side, and the caller is told.
    if not _write_state(cleared):
        raise HttpError(502, "The emergency stop could not be cleared; it still stands")
    resumed = _consumer(enabled=True)
    logger.info(f"[STOP] cleared {cleared}, consumer resumed={resumed}")
    record = {}
    try:
        table = dynamodb.Table(CONFIG_TABLE)
        record = (
            table.update_item(
                Key={"namespace": STOP_NAMESPACE, "config_key": STOP_KEY},
                UpdateExpression=(
                    "SET stands = :f, cleared_at = :a, cleared_by = :b, "
                    "clear_reason = :r, queue_resumed = :q"
                ),
                ExpressionAttributeValues={
                    ":f": False,
                    ":a": cleared["cleared_at"],
                    ":b": actor,
                    ":r": reason,
                    ":q": resumed,
                },
                ReturnValues="ALL_NEW",
            ).get("Attributes")
            or {}
        )
    except Exception as error:  # noqa: BLE001 - the stop is cleared regardless
        logger.error(f"[STOP] could not record the clear: {type(error).__name__}")
    record = {k: v for k, v in record.items() if k not in ("namespace", "config_key")}
    return {**record, **cleared, "queue_resumed": resumed}


def _write_state(state: dict) -> bool:
    """The stop's parameter. True when written."""
    if not STOP_PARAMETER:
        logger.error("[STOP] no stop parameter configured")
        return False
    try:
        ssm.put_parameter(
            Name=STOP_PARAMETER,
            Value=json.dumps(state, sort_keys=True),
            Type="String",
            Overwrite=True,
            Description=str(state.get("cleared_by") or state.get("stopped_by"))[:1024],
        )
        return True
    except Exception as error:  # noqa: BLE001 - reported on the record
        logger.error(f"[STOP] could not write the stop: {type(error).__name__}")
        return False


def _consumer(*, enabled: bool) -> bool:
    """Pause or resume the invoker's agent-queue consumer. True when it took."""
    if not QUEUE_MAPPING_ID:
        return False
    # A mapping still Enabling or Disabling from the last change answers
    # ResourceInUseException, as a clear seconds after a stop does: wait it out,
    # well inside the function's 30 seconds.
    for attempt in range(QUEUE_MAPPING_TRIES):
        try:
            lambda_client.update_event_source_mapping(
                UUID=QUEUE_MAPPING_ID, Enabled=enabled
            )
            return True
        except Exception as error:  # noqa: BLE001 - the invoker holds messages regardless
            code = getattr(error, "response", {}).get("Error", {}).get("Code")
            if code == "ResourceInUseException" and attempt + 1 < QUEUE_MAPPING_TRIES:
                time.sleep(QUEUE_MAPPING_WAIT_SECONDS)
                continue
            logger.error(
                f"[STOP] could not {'resume' if enabled else 'pause'} the agent queue: "
                f"{code or type(error).__name__}"
            )
            return False
    return False


def _reason(body: dict) -> str:
    raw = body.get("reason")
    reason = raw.strip() if isinstance(raw, str) else ""
    if not reason or len(reason) > REASON_MAX or _CONTROL.search(reason):
        raise HttpError(
            400,
            f"`reason` is required: 1 to {REASON_MAX} characters on one line",
            field="reason",
        )
    return reason


def _actor(event: dict) -> str:
    """Who stopped it, from the authorizer's claims only, as `api/trigger_mode` names it."""
    identity = caller_identity(event)
    return f"{identity.display or 'unknown'} ({identity.subject or 'no sub'})"


def _manual(actor: str, stamp: str) -> bool:
    """The switch to `manual`, whatever it reads now. True when it was written."""
    try:
        ssm.put_parameter(
            Name=f"{PREFIX}/{MODE}",
            Value="manual",
            Type="String",
            Overwrite=True,
            # SSM keeps a description per version: the switch's history names the stop.
            Description=f"{actor} (emergency stop)",
        )
    except Exception as error:  # noqa: BLE001 - the runs are stopped regardless
        logger.error(
            f"[STOP] could not set the trigger mode to manual: {type(error).__name__}"
        )
        return False
    try:
        ssm.put_parameter(
            Name=f"{PREFIX}/{MODE}{ACTOR_SUFFIX}",
            Value=f"manual set by {actor} at {stamp} (emergency stop)",
            Type="String",
            Overwrite=True,
        )
    except Exception as error:  # noqa: BLE001 - best-effort, as a flip's attribution is
        logger.warning(f"[STOP] could not record the actor: {type(error).__name__}")
    return True


def _live_grants() -> list[dict]:
    """Every grant not yet expired. TTL deletes lag, so the expiry is filtered here."""
    if not RUN_GRANTS_TABLE:
        return []
    try:
        items = paginate(
            dynamodb.Table(RUN_GRANTS_TABLE).scan,
            FilterExpression=Attr("expires_at").gt(int(time.time())),
            ProjectionExpression="run_id, case_id",
        )
    except Exception as error:  # noqa: BLE001 - the trigger mode is already manual
        logger.error(f"[STOP] could not list the run grants: {type(error).__name__}")
        return []
    return sorted(
        (
            {"run_id": str(i["run_id"]), "case_id": str(i.get("case_id") or "")}
            for i in items
            if i.get("run_id")
        ),
        key=lambda g: g["run_id"],
    )


def _revoke(run_id: str) -> None:
    try:
        dynamodb.Table(RUN_GRANTS_TABLE).delete_item(Key={"run_id": run_id})
    except Exception as error:  # noqa: BLE001 - the grant expires on its own
        logger.error(
            f"[STOP] could not revoke the grant of run {run_id}: {type(error).__name__}"
        )


def _stop_runs(
    grants: list[dict], reason: str, actor: str, deadline: float
) -> tuple[list[dict], bool]:
    """One entry per grant, and whether every case run was handled in time."""
    entries = {
        g["run_id"]: {
            "run_id": g["run_id"],
            "case_id": g["case_id"] or None,
            "session_stopped": False,
            "released": False,
            "parked": False,
        }
        for g in grants
    }
    cased = [g for g in grants if g["case_id"]]
    if not cased:
        return list(entries.values()), True
    pool = ThreadPoolExecutor(max_workers=WORKERS)
    futures = {
        pool.submit(_stop_case_run, g, reason, actor): g["run_id"] for g in cased
    }
    done, _late = wait(futures, timeout=max(0.0, deadline - time.monotonic()))
    # Not waited for: a late session stop goes on in the background, and its grant is
    # already revoked.
    pool.shutdown(wait=False, cancel_futures=True)
    for future in done:
        try:
            entries[futures[future]].update(future.result())
        except Exception as error:  # noqa: BLE001 - this run's entry stays all False
            logger.error(f"[STOP] run {futures[future]}: {type(error).__name__}")
    return list(entries.values()), len(done) == len(futures)


def _stop_case_run(grant: dict, reason: str, actor: str) -> dict:
    """`{session_stopped, released, parked}` for one run on one case."""
    try:
        case_key = to_case_key(grant["case_id"])
    except CaseKeyError:
        return {}
    session_stopped = runtime_session.stop(
        to_runtime_session_id(case_key["case_id"]),
        runtime_arn_parameter=RUNTIME_ARN_PARAMETER,
        region=REGION,
        token=get_delegating_token,
        timeout=SESSION_TIMEOUT_SECONDS,
    )
    table = dynamodb.Table(CASES_TABLE)
    released = release_live_run(table, case_key, run_id=grant["run_id"])
    return {
        "session_stopped": session_stopped,
        "released": released,
        "parked": _park(table, case_key, reason, actor),
    }


def _park(table, case_key: dict, reason: str, actor: str) -> bool:
    """`processing` to `manual_review_required`, naming the stop. False otherwise."""
    try:
        table.update_item(
            Key=case_key,
            UpdateExpression="SET #s = :parked, status_reason = :r, updated_at = :t",
            ConditionExpression="#s = :processing",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":parked": "manual_review_required",
                ":processing": "processing",
                ":r": (
                    f"Stopped by an emergency stop: {reason} ({actor}). Check SAP "
                    "for a write the run made before it stopped."
                ),
                ":t": datetime.now(UTC).isoformat(),
            },
        )
        return True
    except ClientError as error:
        if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
