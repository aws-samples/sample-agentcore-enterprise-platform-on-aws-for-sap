# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Start and stop an agent run on behalf of the browser.

    POST /runs       claim `live_run`, then hand the run to agent_invoker
    POST /runs/stop  end the Runtime session, then release the claim

A start's `intent` decides whether the run may write. `ask`, the default, is a
conversation turn: the agent reads what it can read today and is given no tool that
changes SAP, a case, a ticket or sends mail, and its grant carries `ask` so the run checks
refuse those tools too. `process` is Process on a case, the SOP run, and needs the
case's pack's approvers. A client that sends nothing new therefore cannot write.

The browser POSTs here and watches the
run's channel; the invocation is asynchronous because a run outlives an API Gateway
request by minutes.

Separate from the cases Lambda because this role invokes the agent and reads the
delegating client's secret, and folding it into the CRUD Lambda would hand every case
read both.

`INVOKER_FUNCTION_NAME` is empty until the invoker is wired; until then a start
answers 503. The runtime's ARN is read from SSM (`RUNTIME_ARN_PARAMETER`) per stop,
because the runtime stack deploys after this one; while it is absent a stop releases
the claim without reaching a runtime.
"""

import json
import logging
import os
import uuid

import boto3
import emergency_stop
import registry
import runtime_session
from api_http import HttpError, field, json_body, respond, route
from case_key import (
    RUNTIME_SESSION_MIN_LENGTH,
    RUNTIME_SESSION_PREFIX,
    CaseKeyError,
    to_case_key,
    to_runtime_session_id,
)
from case_status import SETTLED_STATUSES
from delegating_token import get_delegating_token
from entitlement import (
    CASE_FORBIDDEN,
    CASE_MISSING,
    CASE_NOT_A_CASE_WORKER,
    CallerEntitlement,
    caller_identity,
    case_pack_verdict,
)
from live_run_claim import (
    AlreadyRunning,
    CaseSettled,
    MissingCase,
    claim_live_run,
    release_live_run,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE_NAME = os.environ["CASES_TABLE"]
INVOKER_FUNCTION_NAME = os.environ.get("INVOKER_FUNCTION_NAME", "")
RUNTIME_ARN_PARAMETER = os.environ.get("RUNTIME_ARN_PARAMETER", "")
# Direct mode with an obo SAP system: the caller's own token goes with the run, so the
# agent reaches the Gateway as this person (business_operations/identity.py).
FORWARD_USER_TOKEN = os.environ.get("FORWARD_USER_TOKEN") == "true"
REGION = os.environ.get("AWS_REGION", "us-east-1")

lambda_client = boto3.client("lambda")
dynamodb = boto3.resource("dynamodb")

STOP_TIMEOUT_SECONDS = 10

# Checked before the claim, because `claim_live_run` writes the prompt into the case
# item: DynamoDB rejects an item over 400KB with a ValidationException, which is not a
# failed condition and would surface a bad request as a 500. Well under that ceiling
# and under the 256KB async-invoke payload limit, since the item also carries the
# case's own attributes and its appended `agent_traces`. Generous for a chat turn
# (~8k words); raise it if a real prompt ever needs more.
MAX_PROMPT_BYTES = 32_000


#: Body of every entitlement refusal, worded exactly as `cases_api` words its own and
#: for the same reason: it names no pack the caller *would* be allowed, because an
#: enumerable list of pack names is the one thing a probing client wants back.
_FORBIDDEN = {"error": "Not entitled to this pack"}
_NOT_A_CASE_WORKER = "Only this pack's case workers may start the agent on its cases"

#: `intent`: a conversation turn, which reads only, or Process, which may write.
ASK = "ask"
PROCESS = "process"
INTENTS = (ASK, PROCESS)


def _entitlement_gate(
    case_key: dict | None, caller: CallerEntitlement, *, drive: bool = False
) -> None:
    """Pass if this caller may drive a run here, else raise the 403/404.

    Starting a run on a case is a write to it and reads its whole state back through the
    agent, so it cannot be looser than `GET /cases/{id}` — a caller who may not read a
    case must not be able to make the agent read it aloud. The verdict, and the
    404-before-403 ordering, come from the shared module so the two routes cannot drift.

    A case-less conversation has no pack to check *here*, so the only question left is
    whether the caller may see anything at all. Refusing a caller entitled to nothing
    matches `GET /cases` with no `?pack=`; a caller with any pack keeps the plain chat
    turn. That is not the whole answer, though, because a chat turn can still name a
    case in its prompt text and the runtime will resolve it — which is why `_start`
    stamps `visible_packs` into the invocation and the runtime enforces it there.
    """
    if case_key is None:
        if not caller.visible_packs:
            raise HttpError(403, _FORBIDDEN["error"])
        return
    verdict = case_pack_verdict(
        dynamodb.Table(TABLE_NAME), case_key, caller, drive=drive
    )
    if verdict == CASE_MISSING:
        raise HttpError(404, "Case not found")
    if verdict == CASE_FORBIDDEN:
        raise HttpError(403, _FORBIDDEN["error"])
    if verdict == CASE_NOT_A_CASE_WORKER:
        raise HttpError(403, _NOT_A_CASE_WORKER)


def _rehearsal_gate(body: dict, caller: CallerEntitlement) -> None:
    """Authorize a rehearsal, or raise the 400/403 to answer with.

    A rehearsal runs a pack that is not serving anything yet — it may still be **incomplete**,
    with `<AWAITING_SURVEY>` in its `source` — so the caller names the `process_type`
    directly and the pack is resolved from it. `validate_packs.py` forbids two packs
    claiming one process type, so that resolution is unambiguous.

    Gated on **authoring**, not on visibility: this is the pack's author checking their own
    prompt, SOPs and field mapping before an operator sees them. Both gates apply, in the
    order and wording the pack-authoring route uses — the deployment-wide floor first,
    so a caller who clears nothing learns nothing about which packs exist.

    A `case_id` is refused rather than ignored: a case-less run is what makes a rehearsal
    claim no `live_run` and write nothing back to any case row (`_cases_table` in
    `agent/utils/case_persistence.py` no-ops without a case id). Withholding the
    side-effecting *tools* is a separate, client-side filter — see `build_tool_filters`.
    """
    if body.get("case_id"):
        raise HttpError(
            400,
            "A rehearsal runs no case",
            detail="Send `prompt` and `process_type` with a `session_id`; a "
            "rehearsal must not write to a case row.",
        )
    process_type = (field(body, "process_type") or "").strip()
    if not process_type:
        raise HttpError(400, "process_type is required to rehearse")
    pack_id = registry.process_type_pack_map(include_incomplete=True).get(process_type)
    if not pack_id:
        raise HttpError(400, f"No pack maps process_type {process_type}")
    if not caller.clears_author_floor():
        raise HttpError(
            403,
            "Not a pack author",
            detail="Your groups are not in this deployment's `sop_author_groups`.",
        )
    if not caller.may_author(pack_id):
        raise HttpError(
            403,
            f"You may not rehearse {pack_id}",
            detail="Rehearsing a pack runs its prompt and its SOPs, so it needs "
            "the same grant as authoring them — `author_groups`, declared per pack "
            "under `entitlement`.",
        )


def _browser_session_id(body: dict) -> str:
    """The caller's own session id for a case-less run, or "" if it is unusable.

    Length alone is not enough. `to_runtime_session_id` mints
    `RUNTIME_SESSION_PREFIX + case_id`, right-padded to RUNTIME_SESSION_MIN_LENGTH —
    which a real case reaches exactly, so a caller could hand us a case's own session
    id, run inside that case's AgentCore Memory session and pollute its history, and
    never pass the `live_run` claim that a case-bearing request has to win. That
    namespace belongs to the case-bearing path; anything in it is refused here.
    """
    session_id = (field(body, "session_id") or "").strip()
    if len(session_id) < RUNTIME_SESSION_MIN_LENGTH:
        return ""
    if session_id.startswith(RUNTIME_SESSION_PREFIX):
        return ""
    return session_id


@route
def handler(event):
    body = json_body(event)
    caller = CallerEntitlement.resolve(event)
    if event.get("resource", "").endswith("/stop"):
        return respond(*_stop(body, caller), event)
    # Subject only: this route never shows a name to anyone, so the display field has
    # no consumer here. Empty when there is no authorizer at all, which `_start`
    # degrades to the machine identity rather than failing.
    _display, subject = caller_identity(event)
    return respond(*_start(body, subject, caller, user_token=_user_token(event)), event)


def _user_token(event: dict) -> str:
    """The bearer the authorizer already verified, when it is forwarded at all."""
    if not FORWARD_USER_TOKEN:
        return ""
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    raw = str(headers.get("authorization") or "")
    return raw[7:] if raw.lower().startswith("bearer ") else raw


def _start(
    body: dict,
    subject: str,
    caller: CallerEntitlement,
    *,
    user_token: str = "",
) -> tuple[int, dict]:
    """`(status, body)` for a start."""
    prompt = (field(body, "prompt") or "").strip()
    if not prompt:
        raise HttpError(400, "prompt is required")
    if len(prompt.encode()) > MAX_PROMPT_BYTES:
        raise HttpError(400, "prompt is too large")

    # Ahead of everything else, because a rehearsal changes what the rest of this function
    # may do: no case, and its own gate in place of `_entitlement_gate`.
    rehearse = bool(field(body, "rehearse", bool))
    intent = _intent(body, rehearse)
    if rehearse:
        _rehearsal_gate(body, caller)

    raw_case_id = field(body, "case_id") or ""
    if raw_case_id:
        try:
            case_key = to_case_key(raw_case_id)
        except CaseKeyError:
            raise HttpError(400, "Invalid case_id") from None
        # Normalized once, here: the claim key, the session id and the forwarded
        # payload all come off this, so padding in the caller's id cannot claim one key
        # and be reported under another.
        case_id = case_key["case_id"]
        session_id = to_runtime_session_id(case_id)
    else:
        # The case-less conversation. There is no case row, so nothing to claim and
        # nothing to resume on reload; the browser's own session id keys the channel
        # it is already watching.
        case_key = None
        case_id = ""
        session_id = _browser_session_id(body)
        if not session_id:
            raise HttpError(400, "session_id is required when case_id is absent")

    # Ahead of the claim, because the claim writes this caller's prompt into the case
    # item — a refusal after it would have already stored their text on a case they may
    # not touch, and left the case marked busy. Processing a case takes the pack's
    # approvers, because the run checks pass a write at or under the limit for whoever started
    # the run. Asking about it needs only sight of it, as stopping a run (below) does:
    # the run reads what `GET /cases/{id}` already shows and writes nothing.
    _entitlement_gate(case_key, caller, drive=intent == PROCESS)
    # Every start, a question and a rehearsal included: nothing new starts while an
    # emergency stop stands (`emergency_stop.py`).
    _stop_gate()

    if not INVOKER_FUNCTION_NAME:
        return 503, {"error": "Agent invoker not configured"}

    run_id = str(uuid.uuid4())
    if case_key is not None:
        try:
            claim_live_run(
                dynamodb.Table(TABLE_NAME),
                case_key,
                run_id=run_id,
                # Stamped, not accepted: the client does not get to describe its own
                # run as a poller or a webhook in the audit trail.
                trigger="manual",
                prompt=prompt,
                # A settled case's decision is recorded, and its SAP write may be
                # posted: Process would work it twice. A question reads only.
                refuse=SETTLED_STATUSES if intent == PROCESS else frozenset(),
            )
        except CaseSettled as settled:
            return 409, {
                "error": f"This case is settled as {settled.status}, so Process "
                "does not run on it again. Ask about it instead.",
                "status": settled.status,
            }
        except AlreadyRunning as running:
            # Not an error. The browser attaches to the run already on this case —
            # which is also what makes a double-click on Process harmless. The id can
            # be empty when the losing write could not read the prior claim; omit it
            # rather than name a run the browser would then watch for.
            held = {"run_id": running.run_id} if running.run_id else {}
            return 409, {"error": "already_running", **held}
        except MissingCase:
            return 404, {"error": "Case not found"}

    try:
        lambda_client.invoke(
            FunctionName=INVOKER_FUNCTION_NAME,
            InvocationType="Event",
            Payload=json.dumps(
                {
                    "case_id": case_id,
                    "prompt": prompt,
                    "process_type": field(body, "process_type") or "",
                    "run_id": run_id,
                    "session_id": session_id,
                    "subject": subject,
                    # An authorization input, stamped here and never taken from the
                    # client, so no caller can widen their own scope. Never empty:
                    # `_entitlement_gate` already refused a caller entitled to nothing.
                    "visible_packs": sorted(caller.visible_packs),
                    # Also an authorization input, and only present once
                    # `_rehearsal_gate` has passed: the runtime withholds every
                    # side-effecting tool on it, and nothing downstream re-checks it.
                    **({"rehearse": True} if rehearse else {}),
                    # The invoker reads any intent but `process` as a question, so
                    # a rehearsal needs none and sends none.
                    **({} if rehearse else {"intent": intent}),
                    # Beside the payload, never in it: the invoker sends it as a
                    # header, so it never reaches the prompt, the trace or Memory.
                    **({"user_token": user_token} if user_token else {}),
                }
            ).encode(),
        )
    except Exception as error:  # noqa: BLE001 - logged by class, never echoed
        # The claim is already written, and nothing else will ever clear it: no agent
        # was started to release it.
        logger.error(f"[RUN] could not hand off run {run_id}: {error}")
        if case_key is not None:
            release_live_run(dynamodb.Table(TABLE_NAME), case_key, run_id=run_id)
        return 502, {"error": "Could not start run"}

    # `rehearsal` is echoed only when it is true, and `intent` otherwise, so a client can
    # tell which kind of run it started.
    return 202, {
        "run_id": run_id,
        "session_id": session_id,
        **({"rehearsal": True} if rehearse else {"intent": intent}),
    }


def _stop_gate() -> None:
    if stop := emergency_stop.standing():
        raise HttpError(
            409,
            emergency_stop.refusal(stop),
            emergency_stop=emergency_stop.public(stop),
        )


def _intent(body: dict, rehearse: bool) -> str:
    """The start's `intent`, `ask` when absent, or raise the 400 to answer with."""
    raw = body.get("intent")
    intent = ASK if raw is None else raw
    if intent not in INTENTS:
        raise HttpError(400, 'intent must be "ask" or "process"')
    if intent == PROCESS:
        if rehearse:
            raise HttpError(400, "A rehearsal is not a process run")
        if not body.get("case_id"):
            raise HttpError(
                400,
                "Process needs a case_id",
                detail="Process runs a case's SOP; a conversation without a case asks.",
            )
    return intent


def _stop(body: dict, caller: CallerEntitlement) -> tuple[int, dict]:
    """`(status, body)` for a stop."""
    raw_case_id = field(body, "case_id") or ""
    if raw_case_id:
        try:
            case_key = to_case_key(raw_case_id)
        except CaseKeyError:
            raise HttpError(400, "Invalid case_id") from None
        session_id = to_runtime_session_id(case_key["case_id"])
    else:
        case_key = None
        session_id = _browser_session_id(body)
        if not session_id:
            raise HttpError(400, "case_id or session_id is required")

    # Same floor as the start route: an unentitled caller must not be able to cut short
    # a run on a case they cannot see, which is a denial of service on someone else's work.
    _entitlement_gate(case_key, caller)

    table = dynamodb.Table(TABLE_NAME)
    run_id = ""
    if case_key is not None:
        try:
            # Consistent, because the claim being released here can be milliseconds
            # old: an eventually-consistent read that misses it leaves `run_id` empty,
            # skips the release below, and leaves the case busy for a full stale window
            # — the exact failure the stop route exists to prevent. Projected, because
            # the case item also carries its whole appended `agent_traces` list.
            response = table.get_item(
                Key=case_key, ConsistentRead=True, ProjectionExpression="live_run"
            )
            run_id = ((response.get("Item") or {}).get("live_run") or {}).get(
                "run_id", ""
            )
        except Exception as error:  # noqa: BLE001 - logged by class, never echoed
            # Guarded because the session stop below is the half the user actually
            # asked for, and a failed read must not cost them that too. With no run_id
            # there is nothing safe to release — `release_live_run` is conditioned on it
            # precisely so a release cannot un-run a later run — so the claim falls
            # back to expiring on its own after the stale window.
            logger.warning(f"[RUN] could not read live_run for {case_key}: {error}")

    # Both halves are load-bearing. Skipping the session stop leaves the agent running,
    # and its next unconditional `SET live_run` would put the case straight back into
    # the state the release below just cleared. Skipping the release leaves the UI busy
    # for a full stale window after the user asked it to stop.
    stopped = _stop_runtime_session(session_id)
    if case_key is not None and run_id:
        release_live_run(table, case_key, run_id=run_id)
    # A segment already in flight can land after the release and re-write
    # live_run. Small window, self-healing after one stale interval; a per-run
    # cancelled flag the agent checks is the upgrade path if it ever bites.
    #
    # Reported rather than assumed: on a refused stop the agent is still running, and what
    # the browser needs to know is that the case is about to look busy again.
    return 202, {"stopped": stopped}


def _stop_runtime_session(session_id: str) -> bool:
    """Ask the Runtime to abandon this session, reporting whether it agreed
    (`runtime_session.stop`, best-effort)."""
    return runtime_session.stop(
        session_id,
        runtime_arn_parameter=RUNTIME_ARN_PARAMETER,
        region=REGION,
        token=lambda: get_delegating_token(),
        timeout=STOP_TIMEOUT_SECONDS,
    )
