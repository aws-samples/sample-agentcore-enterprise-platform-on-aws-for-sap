# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Agent Invoker Lambda

Consumes from the agent-invocations SQS FIFO queue and invokes the
Amazon Bedrock AgentCore agent. SQS event source mapping maxConcurrency
controls how many agents run in parallel (default 5).

What queues a run: the SAP poller, `POST /erp-events`, the SAP ingest queue's
consumer, the batch sweeper, the operator re-run, and the console's `/cases`
(enqueue, reply) and `/tickets` (decision) routes. `/runs` invokes this function
directly instead, for a run it has already claimed (`_handle_direct`).

Each message contains:
  { "case_id": "4500012345-00010", "trigger": <transport id>,
    "payload": { ... optional: a reviewer's decision or reply ... },
    "subject": "..." }     // optional — the acting human's OIDC `sub`

``case_id`` is the canonical identity from the ``case_key`` codec, and every
producer sends one (it is also the FIFO message group). One that does not parse is
a producer bug: it is logged, and the run goes ahead with no case to update.

``trigger`` names the transport that raised the work. It is passed through to the
trace verbatim and nothing here branches on it, so a new transport needs no change
in this file .
"""

import base64
import codecs
import hashlib
import json
import logging
import os
import secrets
import time
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime

import boto3
import emergency_stop
from botocore.exceptions import ClientError

# Canonical case identity codec, from the API layer.
from case_key import (
    CaseKeyError,
    to_case_key,
    to_runtime_session_id,
    try_normalize_case_id,
)
from case_status import (
    PARKED_ON_PERSON_STATUSES,
    RUN_TERMINAL_STATUSES,
    SETTLED_STATUSES,
)
from decisions import approvals_of, proposed_write_with_digest, write_digest
from delegating_token import get_delegating_token
from live_run_claim import AlreadyRunning, claim_live_run, release_live_run

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")
dynamodb = boto3.resource("dynamodb")

# SSM, read per run: the runtime stack deploys after this one.
RUNTIME_ARN_PARAMETER = os.environ["RUNTIME_ARN_PARAMETER"]
TABLE_NAME = os.environ.get("CASES_TABLE", "")
# The Gateway interceptor's run grants. Without one the run checks refuse the run's calls, so
# a missing table stops the run before it starts.
RUN_GRANTS_TABLE = os.environ.get("RUN_GRANTS_TABLE", "")
AGENT_CLIENT_ID = os.environ.get("AGENT_CLIENT_ID", "")
#: The only client a `verify_grant` names: the use case's read-scoped `reader`.
VERIFY_CLIENT_ID = os.environ.get("VERIFY_CLIENT_ID", "")
#: Long enough for verify.py's probes, short enough that nothing else can use one.
_VERIFY_GRANT_TTL_SECONDS = 120
#: A person's Entra token, forwarded by POST /runs for an obo SAP system; the runtime
#: re-verifies it and calls the Gateway with it (business_operations/identity.py).
USER_TOKEN_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token"
# The run grant's secret, to the runtime only: the agent sends it on every Gateway call
# and the run checks match it against the grant's hash. Never in the payload or a response,
# so the run id, which the browser does hold, is not enough to use a grant.
RUN_GRANT_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Run-Grant"


def _grant_secret() -> tuple[str, str]:
    """(secret, its SHA-256): the grant stores only the hash."""
    secret = secrets.token_urlsafe(32)
    return secret, hashlib.sha256(secret.encode()).hexdigest()


def person_key(token: str) -> str:
    """The run checks' caller key for a person's token: `user:<oid>`.

    business_operations_check._client_id derives the same key from the same token at
    the Gateway, so the grant names this person and no one else holding the console
    app's `azp`. Read unverified: the token came from POST /runs, behind the API's
    authorizer, and a wrong key only fails closed at the run checks.
    """
    payload = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    oid = claims.get("oid")
    if not isinstance(oid, str) or not oid:
        raise ValueError("the forwarded token names no oid")
    return f"user:{oid}"


# Silence longer than a few of the agent's 15s keepalive comments means the stream is
# dead rather than slow. This is a per-read socket timeout, not a run budget.
_IDLE_TIMEOUT_SECONDS = 60
# Headroom left inside the Lambda's remaining time to record a status and return a
# batch failure before the runtime kills the process. It has to exceed one blocking
# read, because the deadline is checked *before* a read that can hold for the whole
# idle timeout: a smaller margin cannot win that race and the run is killed mid-read
# with nothing recorded. The queue's 16-minute visibility timeout already sits above
# the 15-minute function timeout, so widening this costs nothing.
_DEADLINE_MARGIN_SECONDS = _IDLE_TIMEOUT_SECONDS + 15
_MIN_RUN_BUDGET_SECONDS = 30
# The function's own timeout (business_operations/invoker.py TIMEOUT_SECONDS, which a test
# pins to this). Only used to derive
# the fallback below; the real budget comes from the invocation context.
_FUNCTION_TIMEOUT_SECONDS = 900
# Used only when no invocation context is available, e.g. under direct test calls.
# Derived, so it cannot end up above the ceiling the margin implies.
_FALLBACK_RUN_BUDGET_SECONDS = _FUNCTION_TIMEOUT_SECONDS - _DEADLINE_MARGIN_SECONDS
# A grant must outlive the run it authorizes. The run cannot outlive this function: on its
# deadline `_invoke_agent` stops the orphaned Runtime session. The margin covers that stop.
_GRANT_TTL_SECONDS = _FUNCTION_TIMEOUT_SECONDS + 300
# How long a recorded approval stays the one this resume acts on: an hour, and not a
# forged message a day later. Single-use as well (`_consume_approval`), so a redelivery
# or a replay inside the hour runs unapproved.
_APPROVAL_MAX_AGE_SECONDS = 3600
_READ_CHUNK_BYTES = 65536
# A terminal event is a type, a code and a short sanitized message. A line longer than
# this is some other event (a tool result can be megabytes), so it is dropped unparsed
# rather than buffered.
_MAX_TERMINAL_LINE_CHARS = 16384
# RUN_ERROR codes the agent uses for deliberate processing-limit stops. Retrying
# reruns the same work and can repeat tool side effects, so these go to review.
_DELIBERATE_STOP_CODES = (
    "MAX_TURNS_REACHED",
    "MAX_TOKENS_REACHED",
    "RUN_BUDGET_EXCEEDED",
    "REPEATED_TOOL_ERRORS",
)
# Ceiling on `status_reason`. A sentence or two, which is what the case view renders.
_REASON_MAX = 500
# A normally ended agent stream is safe to acknowledge only when the case reached one
# of these schema-defined outcomes. ``sap_updated`` is included because retrying after
# a recorded ERP side effect risks applying it twice.
#
# In the shared layer because `cases_api` has to agree with this Lambda about which
# statuses are already decided — it refuses a second decision on `SETTLED_STATUSES`,
# the subset of this one that is not merely parked on a person.
_BUSINESS_TERMINAL_STATUSES = RUN_TERMINAL_STATUSES


#: The `intent` of a direct start that may write: Process on a case. Any other, or none,
#: is a conversation turn, which reads only (`_handle_direct`).
PROCESS = "process"


def _handle_direct(event: dict, context) -> dict:
    """Run a case the invoke route has already claimed.

    Its `intent` decides whether the run may write: `process` may, within the run checks,
    and anything else is a conversation turn whose grant carries `ask`.

    Invoked with `InvocationType='Event'`, so the return value is discarded and the
    SQS batch-failure protocol is meaningless here. A failure therefore has exactly
    one job: release the claim. The browser is watching `live_run`, and a claim nobody
    will ever write to reads as a busy case for a full stale window.

    Deliberately does NOT touch case status. The browser-driven path never wrote
    `processing` and never flipped a case to `manual_review_required` for a chat turn
    that failed to start; moving the call server-side must not change that.
    """
    case_id = try_normalize_case_id(event.get("case_id") or "") or ""
    run_id = event.get("run_id") or str(uuid.uuid4())
    session_id = event.get("session_id") or ""
    # `POST /runs` refuses while an emergency stop stands; this catches a start it let
    # through just before the stop. The claim is released so the case is not left busy.
    if stop := emergency_stop.standing():
        logger.warning(
            f"[DIRECT] run {run_id} not started: {emergency_stop.refusal(stop)}"
        )
        if TABLE_NAME and case_id:
            try:
                release_live_run(
                    dynamodb.Table(TABLE_NAME), to_case_key(case_id), run_id=run_id
                )
            except Exception as release_error:  # noqa: BLE001
                logger.error(
                    f"[DIRECT] could not release claim for {case_id}: {release_error}"
                )
        return {}
    # The browser's erpPayload, field for field: the agent reads these off
    # forwardedProps.erpPayload and builds the prompt from `prompt`.
    payload = {
        "prompt": event.get("prompt", ""),
        "case_id": case_id,
        "trigger": "manual",
        "process_type": event.get("process_type", ""),
        "run_id": run_id,
        "thread_id": session_id,
    }
    # Forwarded, not derived: the invoke route resolved it from the caller's verified
    # group claim, and this function has no token to resolve it from. Absent means the
    # entitlement floor is inert (`scoped_packs` in agent/utils/payload.py).
    if isinstance(event.get("visible_packs"), list):
        payload["visible_packs"] = [str(p) for p in event["visible_packs"]]
    # Same reason: only `POST /runs` can authorize a rehearsal (it checks that the caller
    # may author the pack), and this function has no token of its own to re-check with.
    if event.get("rehearse"):
        payload["rehearse"] = True
    elif event.get("intent") != PROCESS:
        # Asking is the default: only a start that says it processes a case may write.
        # `POST /runs` sends `process` only for a case and a caller its pack lets drive
        # the agent; anything else that invokes this function directly reads only.
        payload["ask"] = True
    try:
        result = _invoke_agent(
            payload,
            subject=event.get("subject", ""),
            context=context,
            session_id=session_id,
            run_id=run_id,
            attended=True,
            user_token=str(event.get("user_token") or ""),
        )
        # HTTP 200 covers a failed run too, so _invoke_agent returns rather than
        # raising for it. Raise here, into the release below, rather than repeating
        # that block: a run that ended without a good terminal is over either way, and
        # a claim nobody will write to again reads as a busy case. A deliberate stop
        # (_DELIBERATE_STOP_CODES) is included for the same reason — the queued path
        # treats those specially only to skip a retry, and there is no retry here.
        if result["terminal"] != "RUN_FINISHED":
            raise RuntimeError(
                f"agent run did not complete: terminal={result['terminal']}"
            )
        logger.info(f"[DIRECT] run {run_id} for {case_id or '(no case)'} finished")
    except Exception as error:  # noqa: BLE001
        logger.error(
            f"[DIRECT] run {run_id} for {case_id or '(no case)'} failed: {error}"
        )
        if TABLE_NAME and case_id:
            try:
                release_live_run(
                    dynamodb.Table(TABLE_NAME), to_case_key(case_id), run_id=run_id
                )
            # A release that itself fails leaves the case busy for one stale
            # window. Self-healing, and the alternative is losing the original error.
            except Exception as release_error:  # noqa: BLE001
                logger.error(
                    f"[DIRECT] could not release claim for {case_id}: {release_error}"
                )
    return {}


def _verify_grant(spec: dict) -> dict:
    """A two-minute run grant for verify.py's probes through the Gateway.

    The table's policy lets only this function write grants, so verify.py asks for its
    probes here. A probe grant names the `reader` client and no case, carries no
    approval, and expires on its own: `packs` (a list, or absent for no scope) and
    `rehearsal` are all a caller chooses.
    """
    if not (RUN_GRANTS_TABLE and VERIFY_CLIENT_ID):
        raise RuntimeError("RUN_GRANTS_TABLE and VERIFY_CLIENT_ID must both be set")
    packs = spec.get("packs")
    if packs is not None and not isinstance(packs, list):
        raise ValueError("packs must be a list")
    now = int(time.time())
    secret, digest = _grant_secret()
    item = {
        "run_id": f"verify-{uuid.uuid4()}",
        "client_id": VERIFY_CLIENT_ID,
        "secret_sha256": digest,
        "approved": False,
        "expires_at": now + _VERIFY_GRANT_TTL_SECONDS,
    }
    if packs is not None:
        item["packs"] = sorted({str(p) for p in packs})
    if spec.get("rehearsal") is True:
        item["rehearsal"] = True
    dynamodb.Table(RUN_GRANTS_TABLE).put_item(Item=item)
    # The operator's verify.py asked for it, so the secret goes back to it.
    return {"run_id": item["run_id"], "grant": secret, "expires_at": item["expires_at"]}


def handler(event, context):
    """Process one SQS message (event source mapping is pinned to batchSize=1)."""
    if "verify_grant" in event:
        return _verify_grant(event["verify_grant"] or {})
    records = event.get("Records", [])
    if not records:
        # Not an SQS delivery: a run the invoke route already claimed and handed over.
        return _handle_direct(event, context)
    record = records[0]
    table = dynamodb.Table(TABLE_NAME) if TABLE_NAME else None
    msg_id = record["messageId"]
    # Read once, used by both the redelivery guard below and the last-retry escalation
    # in the except block. SQS's own default of 1 applies when the attribute is absent.
    receive_count = int(
        record.get("attributes", {}).get("ApproximateReceiveCount", "1")
    )
    case_id = ""
    case_key = None
    trigger = "unknown"
    try:
        body = json.loads(record["body"])
        raw_case_id = body["case_id"]
        # Normalize once, here, so every status write and the forwarded payload use
        # one representation. Every producer sends an id; one we cannot parse is a
        # producer bug worth surfacing.
        case_id = try_normalize_case_id(raw_case_id) or ""
        if raw_case_id and not case_id:
            logger.warning(f"Unparseable case_id from producer: {raw_case_id!r}")
        case_key = to_case_key(case_id) if case_id else None
        trigger = body.get("trigger", "unknown")
        payload = body.get("payload", {})

        logger.info(f"Processing case={case_id or '(unresolved)'} trigger={trigger}")

        # While an emergency stop stands no run starts. The stop pauses this consumer,
        # so this is a message received before the pause, or every message if the pause
        # failed: mark its case and hand the message back to the queue, where it waits.
        if stop := emergency_stop.standing():
            logger.warning(
                f"Holding case={case_id or '(unresolved)'}: {emergency_stop.refusal(stop)}"
            )
            if table and case_key:
                _mark_held(table, case_key, str(stop.get("stopped_at") or "unknown"))
            return {"batchItemFailures": [{"itemIdentifier": msg_id}]}

        run_id = ""
        if table and case_key:
            # Redelivery guard, at the one funnel all six producers route through.
            # Three different questions, and all three have to be asked here:
            #
            # 1. Is the decision already recorded? Then this delivery is a duplicate
            #    of work that finished, and the `processing` write below would erase
            #    the evidence and let the SOP post to SAP a second time.
            # 2. Is the case parked on a person, and is *this* delivery a redelivery
            #    rather than a fresh one? An orphaned run can finish and park a case
            #    on a human after this Lambda gave up on it, releasing `live_run` on
            #    its way out — so the guard below would find nothing to collide with.
            #    A human's reply is always a brand-new message (its own dedup id), so
            #    ApproximateReceiveCount > 1 here can only mean SQS is redelivering the
            #    same delivery that orphaned, not an operator acting on the case. Do
            #    not widen SETTLED_STATUSES to close this — `awaiting_human_input` and
            #    `manual_review_required` are exactly where that reply is expected to
            #    land, and widening the set would refuse it too.
            # 3. Is a run still in flight? A run whose socket died is redelivered
            #    after the visibility timeout, but the container it abandoned may
            #    still be working — SQS FIFO serializes *Lambdas* per message group,
            #    not the AgentCore containers behind them. The claim's `updated_at`
            #    advances with every agent segment, so a live run holds it and a dead
            #    one lets it go stale.
            status = _current_status(table, case_key)
            if status in SETTLED_STATUSES:
                logger.warning(
                    f"Ignoring redelivery of case={case_id}: already settled as "
                    f"{status!r}"
                )
                return {}
            # A message held by an emergency stop is not a redelivery of a run that
            # orphaned: it never started. Its mark lets it through, once.
            if (
                receive_count > 1
                and status in PARKED_ON_PERSON_STATUSES
                and not _held(table, case_key)
            ):
                logger.warning(
                    f"Ignoring redelivery of case={case_id}: parked as {status!r} "
                    f"and receive_count={receive_count} says this is not a fresh reply"
                )
                return {}
            # Minted here rather than inside _invoke_agent so the claim and the run it
            # protects carry the same id.
            run_id = str(uuid.uuid4())
            try:
                claim_live_run(
                    table,
                    case_key,
                    run_id=run_id,
                    trigger=trigger,
                    # Built agent-side for a queued case, so there is none to publish.
                    # Nothing reads `live_run.prompt`; the agent overwrites the whole
                    # attribute with its first segment.
                    prompt="",
                )
            except AlreadyRunning as running:
                logger.warning(
                    f"Case {case_id} is already running as {running.run_id!r}; "
                    "returning it to the queue rather than starting a second run"
                )
                return {"batchItemFailures": [{"itemIdentifier": msg_id}]}
            _update_status(table, case_key, "processing", trigger)

        # An attribution value off validated claims, never off the prompt.
        subject = body.get("subject", "")

        # The prompt is built agent-side by _build_prompt, which fences and
        # sanitizes inbound content and appends the SOP instruction for a plain
        # case. Sending a prompt here would instead be taken as an explicit user
        # message — the enqueue integration stamps trigger="manual" — and would strip
        # that instruction from every enqueued case.
        agent_payload = {
            "case_id": case_id,
            "trigger": trigger,
            "payload": payload,
        }
        result = _invoke_agent(
            agent_payload, subject=subject, context=context, run_id=run_id
        )
        terminal = result["terminal"]

        if terminal == "RUN_FINISHED":
            business_status = _verify_business_outcome(table, case_id, trigger)
            if business_status:
                logger.info(
                    f"Agent completed case={case_id} business_status={business_status}"
                )
            else:
                # This is reachable only in tests or a deployment without a cases table.
                # Keep the transport-compatible behavior, but do not claim business
                # success when there was no persisted case to verify.
                logger.warning(
                    f"Agent stream finished case={case_id}; "
                    "business outcome was not verified"
                )
        elif terminal in _DELIBERATE_STOP_CODES:
            # The agent stopped at a deterministic processing limit. Retrying can
            # replay already-completed tool side effects, so send it straight to a
            # human instead of spending the redrive budget.
            logger.warning(
                f"Agent stopped at its processing limit ({terminal}) for case={case_id}; "
                f"routing to manual review without retry"
            )
            if (
                table
                and case_key
                and not _update_status(
                    table,
                    case_key,
                    "manual_review_required",
                    trigger,
                    # Without this the case parks carrying nothing: the run never
                    # reached its closing `update_case_state`, so `action_log` is
                    # empty and the status alone cannot tell a human that the
                    # research was complete and only the write was lost. Naming the
                    # limit is what separates this from a case the agent failed.
                    reason=(
                        f"Run stopped at its {terminal} limit before it could write "
                        f"a terminal state; work completed before the stop is in the "
                        f"run trace, not in action_log"
                    ),
                )
            ):
                # The escalation is all that stands between this case and silence.
                # Acking now would delete the message and leave the case in
                # `processing` with nothing queued, nothing sweeping it (batch_runner
                # only sweeps `detected`) and no alarm watching — the DLQ is the one
                # signal something actually looks at. So pay for it with the redrive:
                # two more replays of a run that already hit its limit beats a case
                # nobody ever sees again. Raise rather than return, so the redrive
                # path below stays the single place that decides what to do with a
                # failed delivery. The settled-status guard cannot rescue this one —
                # the write that would have made the case settled is the write that
                # just failed.
                raise RuntimeError(
                    f"could not park case={case_id} for manual review after {terminal}"
                )
        else:
            # HTTP 200 with no successful terminal event. Raise so the existing
            # redrive path applies rather than deleting the message and leaving the
            # case stuck in "processing" forever.
            raise RuntimeError(
                f"agent run did not complete for case={case_id}: terminal={terminal}"
            )

    except Exception as e:  # noqa: BLE001
        logger.error(f"Failed msg={msg_id}: {e}")
        # Last retry: flag for manual review instead of leaving the case stuck in "processing".
        if table and case_key and receive_count >= 3:
            # T29: this field renders verbatim on the case-detail panel to any
            # operator the entitlement floor admits, so it must not carry the
            # exception text — a boto3/requests exception can hold a table name, an
            # endpoint, a request id, or a fragment of a SAP response body. The full
            # exception is already in the log line above, keyed on msg_id, which an
            # operator with log access can join to this case; the class name alone
            # tells them what kind of failure it was without repeating that text on
            # the lower-privilege case view.
            if _update_status(
                table,
                case_key,
                "manual_review_required",
                trigger,
                reason=(
                    f"Parked after {receive_count} failed attempts; last error: "
                    f"{type(e).__name__}"
                ),
            ):
                logger.warning(
                    f"Case {case_id} moved to manual_review_required after {receive_count} failures"
                )
            else:
                # Report what happened, not what was attempted. This line used to claim
                # the escalation unconditionally, so a case still sitting in `processing`
                # read as parked in the one log an operator goes to. The message reaches
                # the DLQ either way — this is the report, not the recovery.
                logger.error(
                    f"Case {case_id} could NOT be parked for manual review after "
                    f"{receive_count} failures; it stays in its previous status"
                )
        return {"batchItemFailures": [{"itemIdentifier": msg_id}]}

    return {}


def _mark_held(table, case_key: dict, stopped_at: str) -> None:
    """Mark a case whose message an emergency stop handed back to the queue, so its
    next delivery is read as fresh (`_held`). Best-effort: unmarked, a reply to a case
    parked on a person is dropped on redelivery, which the stop's record names."""
    try:
        table.update_item(
            Key=case_key,
            UpdateExpression="SET held_by_stop = :t",
            ConditionExpression="attribute_exists(case_id)",
            ExpressionAttributeValues={":t": stopped_at},
        )
    except Exception as error:  # noqa: BLE001
        logger.warning(f"Could not mark {case_key} held: {type(error).__name__}")


def _held(table, case_key: dict) -> bool:
    """Whether an emergency stop held this case's message (`_mark_held`)."""
    try:
        item = table.get_item(
            Key=case_key, ConsistentRead=True, ProjectionExpression="held_by_stop"
        ).get("Item")
    except Exception as error:  # noqa: BLE001 - unreadable reads as not held
        logger.warning(f"Could not read held_by_stop for {case_key}: {error}")
        return False
    return bool((item or {}).get("held_by_stop"))


def _current_status(table, case_key: dict) -> str | None:
    """The case's current status, or None if it cannot be read.

    `ConsistentRead` because a write this guards against may have landed seconds ago,
    on the run whose redelivery we are now looking at. An eventually consistent read
    here would see the pre-decision status and wave the duplicate through, which is
    the entire failure being prevented.

    A read that fails returns None: refusing to process every case because DynamoDB
    is briefly unavailable is worse than the duplicate this guards, and `_update_status`
    below is conditioned on the case existing anyway.
    """
    try:
        response = table.get_item(
            Key=case_key,
            ConsistentRead=True,
            ProjectionExpression="#s",
            ExpressionAttributeNames={"#s": "status"},
        )
    except Exception as error:  # noqa: BLE001
        logger.warning(f"Could not read status for {case_key}: {error}")
        return None
    return (response.get("Item") or {}).get("status")


def _verify_business_outcome(table, case_id: str, trigger: str) -> str | None:
    """Verify the persisted ERP outcome after a normally completed AG-UI stream.

    ``RUN_FINISHED`` means the transport/model loop ended normally; it does not mean
    the ERP workflow succeeded. A completed stream must not be blindly retried after
    possible SAP side effects, so a present case in any nonterminal or invalid state is
    routed to manual review. Failure to read the case is different: without a durable
    outcome there is nothing safe to acknowledge, so the caller redrives the message.
    """
    if not table:
        return None
    try:
        key = to_case_key(case_id)
    except CaseKeyError:
        # No resolvable case (a producer sent an unparseable id) — nothing to verify.
        return None

    response = table.get_item(
        Key=key,
        ConsistentRead=True,
        ProjectionExpression="#s",
        ExpressionAttributeNames={"#s": "status"},
    )
    case = response.get("Item")
    if case is None:
        raise RuntimeError(f"cannot verify business outcome; case not found: {case_id}")

    status = case.get("status")
    if status in _BUSINESS_TERMINAL_STATUSES:
        return status

    observed = repr(status) if status is not None else "missing"
    reason = (
        "Agent stream finished without a safe terminal case state; "
        f"observed status={observed}."
    )
    logger.warning(f"{reason} Routing case={case_id} to manual review without retry")
    if not _update_status(
        table,
        key,
        "manual_review_required",
        trigger,
        reason=reason,
    ):
        raise RuntimeError(
            f"cannot record manual review after unverified business outcome: {case_id}"
        )
    return "manual_review_required"


def _update_status(
    table,
    case_key: dict,
    status: str,
    trigger: str,
    *,
    reason: str | None = None,
) -> bool:
    """Update case status in DynamoDB and report whether it was persisted.

    ``case_key`` is the DynamoDB key from ``to_case_key(case_id)`` — callers no
    longer split an id apart to build it.
    """
    try:
        from datetime import datetime

        update_expression = "SET #s = :s, updated_at = :t"
        expression_values = {
            ":s": status,
            ":t": datetime.now(UTC).isoformat(),
        }
        if reason:
            update_expression += ", status_reason = :r"
            # Truncated here rather than at each caller: one of them passes a raw
            # exception string, and boto3 and requests both put a whole HTTP response
            # body in one. This field renders as a paragraph on the case, so an
            # unbounded value is a page of JSON where a sentence belongs. The full text
            # is in the log line the caller already wrote.
            expression_values[":r"] = reason[:_REASON_MAX] + (
                "…" if len(reason) > _REASON_MAX else ""
            )
        else:
            # A reason describes the status it was written with. Kept, a case an
            # earlier run parked would still show that error once a later run settled it.
            update_expression += " REMOVE status_reason"
        if status == "processing":
            # A run starting leaves no earlier stop's options behind: the run that
            # parks the case again records its own (`decision_options.py`). A decision
            # already recorded carries its option's write on its action log entry. Nor
            # an emergency stop's hold, which this run has now consumed.
            update_expression += (
                ", decision_options, held_by_stop"
                if "REMOVE" in update_expression
                else " REMOVE decision_options, held_by_stop"
            )

        table.update_item(
            Key=case_key,
            UpdateExpression=update_expression,
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues=expression_values,
            ConditionExpression="attribute_exists(case_id)",
        )
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Status update failed for {case_key}: {e}")
        return False


def _human_approved(payload: dict) -> bool:
    """Whether the message that started this run claims a human's `approved`.

    A claim only: `_approved_write` checks it against the record on the case.
    """
    ctx = payload.get("payload") or {}
    trigger = payload.get("trigger", "")
    if trigger == "ticket-action":
        return ctx.get("ticket_decision") == "approved"
    if trigger == "console-action":
        return ctx.get("decision") == "approved"
    return False


def _approved_write(
    payload: dict, case_id: str, now: int
) -> tuple[dict, list[int], list[str]] | None:
    """The write a human approved for this run, read off the case, never the message,
    and the approval's index in the case's `action_log`.

    The message only claims a decision, and anything that may send to the agent queue
    or invoke this function can claim one. The approval routes record the decision on
    the case before they queue it (`decisions.py`), so the run is approved only when the
    case's latest decision is that approval, it is recent, no run has used it yet, it was
    made by a named person, and the write it names by digest is still in the case's
    traces. The grant carries that write, and the run checks lift the amount gate for it alone.

    Also returned: the `action_log` indices of every unused approval of that write at the
    end of the log, and their distinct approvers, which the run checks count against a pack's
    dual-approval limit.
    """
    if not (_human_approved(payload) and case_id and TABLE_NAME):
        return None
    item = (
        dynamodb.Table(TABLE_NAME)
        .get_item(
            Key=to_case_key(case_id),
            ProjectionExpression="action_log, agent_traces",
            ConsistentRead=True,
        )
        .get("Item")
        or {}
    )
    decisions = [
        (i, e)
        for i, e in enumerate(item.get("action_log") or [])
        if isinstance(e, dict) and str(e.get("action", "")).startswith("human_")
    ]
    index, entry = decisions[-1] if decisions else (-1, {})
    digest = entry.get("approved_write")
    try:
        at = datetime.fromisoformat(str(entry.get("timestamp"))).timestamp()
    except ValueError:
        at = None
    # A small negative age is clock skew between this function and the route.
    if (
        entry.get("action") != "human_approved"
        or not entry.get("actor")
        or not isinstance(digest, str)
        or at is None
        or not -60 <= now - at <= _APPROVAL_MAX_AGE_SECONDS
    ):
        logger.warning(f"Run for {case_id} claims an approval the case does not record")
        return None
    if entry.get("consumed_by_run"):
        logger.warning(
            f"Approval on {case_id} was already used by run {entry['consumed_by_run']}"
        )
        return None
    # A chosen option's write is on the entry, copied by the decision route as the
    # person saw it; anything else is found in the traces by its digest.
    if "option_write" in entry:
        write = entry["option_write"]
        if write_digest(write) != digest:
            logger.warning(
                f"Approval on {case_id} names an option write it does not hold"
            )
            return None
    else:
        write = proposed_write_with_digest(item.get("agent_traces"), digest)
    if not isinstance(write, dict):
        logger.warning(f"Approval on {case_id} names a write its traces do not hold")
        return None
    fields = {
        str(f["name"]): str(f.get("proposed", ""))
        for f in write.get("fields") or []
        if isinstance(f, dict) and f.get("name")
    }
    approvals = approvals_of(item.get("action_log"), digest)
    approvers = sorted({str(e["actor"]) for _, e in approvals if e.get("actor")})
    return (
        {
            "digest": digest,
            "op": write.get("op"),
            "service": write.get("service"),
            "entity": write.get("entity"),
            "fields": fields,
        },
        [i for i, _ in approvals] or [index],
        approvers,
    )


def _consume_approval(case_id: str, index: int, run_id: str) -> bool:
    """Mark the approval at `action_log[index]` used by `run_id`; False if a run
    already has. Conditional, so two deliveries racing for one approval get one run.
    The log is append-only, so the index still names the entry `_approved_write` read.
    """
    try:
        dynamodb.Table(TABLE_NAME).update_item(
            Key=to_case_key(case_id),
            UpdateExpression=f"SET action_log[{index}].consumed_by_run = :run",
            ConditionExpression=(
                f"action_log[{index}].#a = :approved"
                f" AND attribute_not_exists(action_log[{index}].consumed_by_run)"
            ),
            ExpressionAttributeNames={"#a": "action"},
            ExpressionAttributeValues={":run": run_id, ":approved": "human_approved"},
        )
    except ClientError as error:
        if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        logger.warning(f"Approval on {case_id} was already used by another run")
        return False
    return True


def _case_pack(case_id: str) -> str | None:
    item = (
        dynamodb.Table(TABLE_NAME)
        .get_item(
            Key=to_case_key(case_id),
            ProjectionExpression="pack_id",
        )
        .get("Item", {})
    )
    pack = item.get("pack_id")
    return str(pack) if pack else None


def _grant_item(
    payload: dict,
    run_id: str,
    *,
    attended: bool,
    now: int,
    caller: str = "",
    secret_sha256: str = "",
) -> dict:
    """The run grant the Gateway run checks check for this run (`gateway_run_checks.py`).

    `packs` is a sorted list rather than a string set: DynamoDB refuses an empty set, and
    an empty scope is a real value here (it refuses every case and process-type tool).
    """
    case_id = try_normalize_case_id(payload.get("case_id") or "") or ""
    approval = _approved_write(payload, case_id, now)
    # Consumed as the grant is built, just before it is written: an approval authorizes
    # one run. A grant that then fails to write loses it, and the case needs a fresh one.
    approved_write = (
        approval[0]
        if approval and all(_consume_approval(case_id, i, run_id) for i in approval[1])
        else None
    )
    item = {
        "run_id": run_id,
        "client_id": caller or AGENT_CLIENT_ID,
        "secret_sha256": secret_sha256,
        "approved": approved_write is not None,
        "expires_at": now + _GRANT_TTL_SECONDS,
    }
    if approved_write:
        item["approved_write"] = approved_write
        item["approvers"] = approval[2]
    if case_id:
        item["case_id"] = case_id
    # `POST /runs` authorized it (the caller may author the pack); the run checks refuse
    # every write on this grant, whatever tools the agent loaded.
    if payload.get("rehearse"):
        item["rehearsal"] = True
    # A conversation turn (`_handle_direct`): the run checks refuse the same tools.
    elif payload.get("ask") is True:
        item["ask"] = True
    if attended:
        # `POST /runs` resolved this from the caller's verified group claim. Absent means
        # the entitlement floor is inert, so the grant is unrestricted too.
        if isinstance(payload.get("visible_packs"), list):
            item["packs"] = sorted({str(p) for p in payload["visible_packs"]})
    else:
        # Unattended, whatever the trigger says: the run may touch only its own case's
        # pack. No case or an unknown pack is an empty scope, which refuses every case
        # and process-type tool rather than leaving them open.
        pack = _case_pack(case_id) if case_id else None
        item["packs"] = [pack] if pack else []
    return item


def _write_grant(
    payload: dict,
    run_id: str,
    *,
    attended: bool,
    caller: str = "",
    secret_sha256: str = "",
) -> None:
    """Put the run grant. Raises on failure, which stops the run before it starts."""
    if not RUN_GRANTS_TABLE or not AGENT_CLIENT_ID:
        raise RuntimeError(
            "RUN_GRANTS_TABLE and AGENT_CLIENT_ID are both required; refusing to start "
            "a run whose grant could never match"
        )
    item = _grant_item(
        payload,
        run_id,
        attended=attended,
        now=int(time.time()),
        caller=caller,
        secret_sha256=secret_sha256,
    )
    dynamodb.Table(RUN_GRANTS_TABLE).put_item(Item=item)


def _delete_grant(run_id: str, secret_sha256: str) -> None:
    """Drop the run's grant once the run is over, so its secret cannot be replayed.

    Keyed by `run_id` alone, and conditional on this run's secret hash, so it can never
    remove another run's grant. Never raises: the TTL is the backstop for a failure.
    """
    try:
        dynamodb.Table(RUN_GRANTS_TABLE).delete_item(
            Key={"run_id": run_id},
            ConditionExpression="secret_sha256 = :h",
            ExpressionAttributeValues={":h": secret_sha256},
        )
    except Exception as exc:  # noqa: BLE001 - the grant expires on its own
        logger.warning(f"Could not delete run grant run={run_id}: {type(exc).__name__}")


def _invoke_agent(
    payload: dict,
    *,
    subject: str = "",
    context=None,
    session_id: str = "",
    run_id: str = "",
    attended: bool = False,
    user_token: str = "",
) -> dict:
    """Invoke AgentCore over AG-UI with a Cognito bearer token and the acting subject.

    Returns the run's terminal outcome so the caller can decide whether the case was
    actually processed. Raises on transport failure or on exceeding the run budget.
    """
    deadline = _run_deadline(context)
    region = os.environ.get("AWS_REGION", "us-east-1")
    agent_arn = ssm.get_parameter(Name=RUNTIME_ARN_PARAMETER)["Parameter"]["Value"]
    escaped = urllib.parse.quote(agent_arn, safe="")
    url = f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{escaped}/invocations?qualifier=DEFAULT"

    # `write`, and only `write`: this token starts a run that posts to SAP.
    token = get_delegating_token()
    # One case, one Runtime session. Memory keys on (actor_id, session_id), so a
    # per-delivery id puts every poller run and ticket-action callback for a case in its
    # own partition and the agent resumes nothing it just did. The direct path already
    # passes the case-derived id and the run id its claim published; this derives the
    # same value for the queued path. A run with no resolvable case (an unparseable
    # id) has nothing to be continuous with, so it still gets a disposable id.
    if not session_id:
        case_session = try_normalize_case_id(payload.get("case_id") or "")
        session_id = (
            to_runtime_session_id(case_session)
            if case_session
            else f"sqs-{int(time.time())}-{uuid.uuid4().hex}"
        )
    run_id = run_id or str(uuid.uuid4())
    # Before the Runtime call, and not inside a try: a grant that fails to write means
    # the run would be refused at the Gateway on its first gated call, so it must not
    # start at all.
    # A forwarded person's token is the Gateway bearer for this run, so the grant
    # names that person rather than the agent's client.
    grant_secret, grant_digest = _grant_secret()
    _write_grant(
        payload,
        run_id,
        attended=attended,
        caller=person_key(user_token) if user_token else "",
        secret_sha256=grant_digest,
    )

    # The Runtime serves the AG-UI contract, so the request body must be a well-formed
    # RunAgentInput. The ERP fields travel in forwardedProps.erpPayload, which is where
    # the agent reads them from; the prompt is also placed in the AG-UI message history.
    # The agent rewrites the last user message with its own built prompt, so this
    # content is a readable reference rather than the instruction itself.
    content = (
        payload.get("prompt")
        or f"Process ERP exception case {payload.get('case_id', '')}"
    ).strip()
    agui_input = {
        "threadId": session_id,
        "runId": run_id,
        # RunAgentInput requires the key; nothing reads it back, so it carries no
        # duplicate of the caseId/trigger already in forwardedProps.erpPayload.
        "state": None,
        "messages": [
            {
                "id": f"input-{run_id}",
                "role": "user",
                "content": content,
            }
        ],
        "tools": [],
        "context": [],
        "forwardedProps": {"erpPayload": payload},
    }

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
    }
    # The acting human's OIDC subject. auth.py turns it into the Memory actorId, the
    # audit initiator and the trace, and trusts it because it can only reach the
    # container from a machine-credentialed caller: the Runtime's authorizer accepts
    # the machine client alone and its header allowlist is what lets this through at
    # all (both in business_operations/runtime.py). Absent, not empty — auth.py rejects a
    # blank value, and a robot-initiated run with no human behind it should resolve to
    # the machine subject as it always has.
    if subject:
        headers["X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Identity"] = subject
    if user_token:
        headers[USER_TOKEN_HEADER] = user_token
    headers[RUN_GRANT_HEADER] = grant_secret

    req = urllib.request.Request(
        url,
        data=json.dumps(agui_input).encode(),
        method="POST",
        headers=headers,
    )
    # urllib's timeout is a SOCKET timeout applied per blocking read, not a budget for
    # the whole run — a stream that keeps producing never trips it. The agent emits a
    # keepalive comment every 15s, so silence beyond a few heartbeats means the stream
    # is dead, and that is what this value detects. The total budget is enforced
    # separately against the Lambda's own remaining time.
    try:
        with urllib.request.urlopen(  # nosec B310 — URL built from trusted config  # nosemgrep: dynamic-urllib-use-detected
            req, timeout=_IDLE_TIMEOUT_SECONDS
        ) as resp:
            status = resp.status
            try:
                byte_count, terminal = _drain_stream(resp, deadline=deadline)
            except TimeoutError:
                # `InvokeAgentRuntime` does not cancel the container when this end gives
                # up — measured live: the runtime kept running and applying side
                # effects for minutes after the invoker abandoned it, with no
                # cancellation attempted. Without this call, nobody watches that.
                _stop_orphaned_session(agent_arn, session_id, run_id)
                raise
    finally:
        _delete_grant(run_id, grant_digest)

    # HTTP 200 covers both a completed and a failed run, so the terminal event is the
    # only signal that separates them. The caller acts on it.
    logger.info(
        f"Agent response: {status} ({byte_count} bytes) run={run_id} terminal={terminal}"
    )
    return {"terminal": terminal}


def _stop_orphaned_session(agent_arn: str, session_id: str, run_id: str) -> None:
    """Best-effort: tell AgentCore to end the run this Lambda is about to abandon.

    Closing our end of the socket does not stop the container — it keeps running and
    applying side effects nobody is watching. This is a separate API call, so it can
    fail independently of the timeout it is responding to; either way the caller's
    TimeoutError is what gets raised, not this.

    Over the same OAuth bearer token `_invoke_agent` uses, not IAM/SigV4 — confirmed
    live: this Runtime's inbound authorizer is the custom JWT authorizer configured
    in `business_operations/runtime.py`, and `StopRuntimeSession` enforces the *same*
    method the Runtime was configured with. A role holding the IAM action was still
    refused with "Authorization method mismatch ... ensure your request uses the
    matching method (OAuth or SigV4)".
    """
    region = os.environ.get("AWS_REGION", "us-east-1")
    escaped = urllib.parse.quote(agent_arn, safe="")
    url = (
        f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{escaped}"
        "/stopruntimesession?qualifier=DEFAULT"
    )
    req = urllib.request.Request(
        url,
        method="POST",
        headers={
            # Same scope as the invoke it is cleaning up after — `StopRuntimeSession`
            # enforces the same method and the same authorizer as `invocations`.
            "Authorization": f"Bearer {get_delegating_token()}",
            "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
        },
    )
    try:
        with urllib.request.urlopen(  # nosec B310 — URL built from trusted config  # nosemgrep: dynamic-urllib-use-detected
            req, timeout=10
        ):
            pass
        logger.warning(f"Stopped orphaned session {session_id!r} for run={run_id}")
    except Exception as error:  # noqa: BLE001 — best-effort; the timeout still wins
        logger.error(
            f"Could not stop orphaned session {session_id!r} for run={run_id}: {error}"
        )


def _run_deadline(context) -> float:
    """A monotonic deadline for the whole run, derived from the Lambda's own budget.

    Taken from the invocation context rather than a constant so it tracks the
    configured timeout instead of duplicating it, and leaves margin to record a
    status and return a batch failure before the runtime kills the process.
    """
    remaining_ms = None
    if context is not None and hasattr(context, "get_remaining_time_in_millis"):
        try:
            remaining_ms = context.get_remaining_time_in_millis()
        except Exception:  # nosec B110 — a missing budget falls back below  # noqa: BLE001
            remaining_ms = None
    if not remaining_ms:
        return time.monotonic() + _FALLBACK_RUN_BUDGET_SECONDS
    budget = max(
        remaining_ms / 1000.0 - _DEADLINE_MARGIN_SECONDS, _MIN_RUN_BUDGET_SECONDS
    )
    return time.monotonic() + budget


def _note_terminal(line: str, seen: set[str]) -> None:
    """Record a terminal event if `line` is an SSE `data:` line carrying one.

    By the event's `type`, never by a substring: model output and echoed SAP fields
    can hold the text "RUN_ERROR", and matching it anywhere failed every run of a
    case whose record did.
    """
    if not line.startswith("data:"):
        return
    try:
        event = json.loads(line[5:])
    except ValueError:
        return
    if not isinstance(event, dict):
        return
    kind = event.get("type")
    if kind in ("RUN_ERROR", "RUN_FINISHED"):
        seen.add(kind)
    if kind == "RUN_ERROR" and event.get("code") in _DELIBERATE_STOP_CODES:
        seen.add(event["code"])


def _drain_stream(resp, *, deadline: float) -> tuple[int, str]:
    """Consume the AG-UI stream, reporting size and terminal outcome.

    Nothing downstream replays the events, so the body is scanned line by line as it
    arrives and discarded rather than buffered: an agent response can reach hundreds
    of megabytes. Only the unfinished last line is kept, so an event split across two
    reads is still read whole, and a line too long to be a terminal event is dropped.

    Returns (bytes read, terminal outcome: "RUN_FINISHED" / "RUN_ERROR" /
    a value from _DELIBERATE_STOP_CODES / "none").
    """
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    byte_count = 0
    seen: set[str] = set()
    pending = ""
    # Inside a line already dropped as too long: its rest is dropped too.
    overlong = False

    while True:
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"agent run exceeded its budget after {byte_count} bytes; "
                f"terminal event not yet seen"
            )
        # read1, not read: http.client's read(amt) is fill-or-EOF — it blocks until
        # that many bytes have accumulated, so on a low-volume SSE stream the deadline
        # check above ran exactly once, at t≈0. read1 returns after one underlying
        # read, which is what gives this loop a deadline at all.
        chunk = resp.read1(_READ_CHUNK_BYTES)
        if not chunk:
            break
        byte_count += len(chunk)
        *lines, pending = (pending + decoder.decode(chunk)).split("\n")
        for line in lines:
            if overlong:
                overlong = False
                continue
            _note_terminal(line.rstrip("\r"), seen)
        if len(pending) > _MAX_TERMINAL_LINE_CHARS:
            pending, overlong = "", True
    if not overlong:
        _note_terminal((pending + decoder.decode(b"", final=True)).rstrip("\r"), seen)

    # A deliberate stop is a RUN_ERROR subtype, and a failure outranks a completion:
    # a stream reporting both is not a success.
    terminal = next(
        (code for code in _DELIBERATE_STOP_CODES if code in seen),
        "RUN_ERROR"
        if "RUN_ERROR" in seen
        else "RUN_FINISHED"
        if "RUN_FINISHED" in seen
        else "none",
    )
    return byte_count, terminal
