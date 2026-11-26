# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""business-operations API: POST /runs and POST /runs/stop.

The claim is taken before the invoker is handed the run, the entitlement gate lands
before the claim (which writes the caller's prompt into the case), and every fault is
a CORS-bearing 500 that logs a class and a code, never a message.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import urllib.error
from unittest import mock

import pytest
from botocore.exceptions import ClientError
from case_key import derive_case_id, to_runtime_session_id
from case_status import SETTLED_STATUSES
from live_run_claim import AlreadyRunning, CaseSettled, MissingCase
from test_business_operations_api import CONSOLE, UC, USERS, _event

CASE = derive_case_id("ap", "ap", {"SupplierInvoice": "5100001976"})
ORIGIN = "https://app.test"
BROWSER_SESSION = "browser-session-0000000000000000x"
AUTHORS = "business-operations-authors"
WORKERS = "business-operations-case-workers"


@pytest.fixture
def runs(monkeypatch):
    for k, v in {
        "API_ALLOWED_CLIENTS": CONSOLE,
        "USERS_GROUP": USERS,
        "CASES_TABLE": "cases",
        "INVOKER_FUNCTION_NAME": "invoker",
        "RUNTIME_ARN_PARAMETER": "/p/dev/runtimes/business-operations/arn",
        "CORS_ALLOWED_ORIGINS": ORIGIN,
        "ENTITLEMENT_MAP": json.dumps({"ap": {}, "tax": {}}),
        "AWS_REGION": "us-east-1",
        "CASE_WORKER_GROUPS": WORKERS,
    }.items():
        monkeypatch.setenv(k, v)
    with mock.patch("boto3.client"), mock.patch("boto3.resource"):
        spec = importlib.util.spec_from_file_location(
            "bo_api_runs", UC / "api" / "runs" / "index.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    mod.lambda_client = mock.MagicMock()
    _case(mod, pack_id="ap", live_run={"run_id": "run-9"})
    return mod


def _case(mod, **attrs):
    table = mod.dynamodb.Table.return_value
    table.get_item.return_value = {"Item": attrs} if attrs else {}
    return table


def _req(resource, body, groups=(USERS,), **claims):
    event = _event("POST", resource, groups=groups, body=json.dumps(body))
    event["headers"] = {"origin": ORIGIN}
    event["requestContext"]["authorizer"]["claims"].update(claims)
    return event


def _call(mod, resource, body, **kw):
    response = mod.handler(_req(resource, body, **kw), None)
    return response["statusCode"], json.loads(response["body"])


def _payload(mod) -> dict:
    return json.loads(mod.lambda_client.invoke.call_args.kwargs["Payload"])


# ── start ──


def test_a_claimed_run_is_handed_to_the_invoker_asynchronously(runs):
    with mock.patch.object(runs, "claim_live_run") as claim:
        status, body = _call(
            runs, "/runs", {"case_id": CASE, "prompt": "why held?", "process_type": "h"}
        )
    assert status == 202
    assert body["run_id"] == claim.call_args.kwargs["run_id"]
    assert claim.call_args.args[1] == {"case_id": CASE}
    # Stamped, not accepted from the client.
    assert claim.call_args.kwargs["trigger"] == "manual"
    invoke = runs.lambda_client.invoke.call_args.kwargs
    assert (invoke["FunctionName"], invoke["InvocationType"]) == ("invoker", "Event")
    assert _payload(runs) == {
        "case_id": CASE,
        "prompt": "why held?",
        "process_type": "h",
        "run_id": body["run_id"],
        # Memory partitions on this; a per-request id would split the conversation.
        "session_id": to_runtime_session_id(CASE),
        "subject": "sub-1",
        "visible_packs": ["ap", "tax"],
        # No `intent` in the body: a question, which the invoker runs reading only.
        "intent": "ask",
    }
    assert body["session_id"] == to_runtime_session_id(CASE)
    assert body["intent"] == "ask"
    assert "rehearsal" not in body


def test_the_subject_and_pack_set_are_never_taken_from_the_body(runs):
    with mock.patch.object(runs, "claim_live_run"):
        _call(
            runs,
            "/runs",
            {
                "case_id": CASE,
                "prompt": "p",
                "subject": "attacker",
                "visible_packs": ["hr"],
                "trigger": "poller",
            },
            **{"cognito:username": "alice"},
        )
    payload = _payload(runs)
    assert payload["subject"] == "sub-1"  # `sub`, not the username, not the body
    assert payload["visible_packs"] == ["ap", "tax"]


@pytest.mark.parametrize("held,expected", [("run-0", {"run_id": "run-0"}), ("", {})])
def test_a_run_already_in_flight_is_a_409_naming_it_when_it_can(runs, held, expected):
    with mock.patch.object(runs, "claim_live_run", side_effect=AlreadyRunning(held)):
        status, body = _call(runs, "/runs", {"case_id": CASE, "prompt": "p"})
    assert (status, body) == (409, {"error": "already_running", **expected})
    runs.lambda_client.invoke.assert_not_called()


def test_a_missing_case_is_a_404(runs):
    with mock.patch.object(runs, "claim_live_run", side_effect=MissingCase("gone")):
        assert _call(runs, "/runs", {"case_id": CASE, "prompt": "p"})[0] == 404


@pytest.mark.parametrize(
    "body",
    [
        {"case_id": "../etc/passwd", "prompt": "p"},
        {"case_id": CASE, "prompt": "   "},
        {"case_id": CASE, "prompt": "x" * 32_001},
        {"prompt": "hello", "session_id": "short"},
        # A case's own session id: a case-less run cannot borrow its Memory.
        {"prompt": "hello", "session_id": to_runtime_session_id(CASE)},
    ],
)
def test_a_bad_request_is_400_before_the_claim(runs, body):
    with mock.patch.object(runs, "claim_live_run") as claim:
        assert _call(runs, "/runs", body)[0] == 400
    claim.assert_not_called()
    runs.lambda_client.invoke.assert_not_called()


def test_a_case_less_run_uses_the_browsers_session_and_claims_nothing(runs):
    with mock.patch.object(runs, "claim_live_run") as claim:
        status, body = _call(
            runs, "/runs", {"prompt": "hi", "session_id": BROWSER_SESSION}
        )
    assert (status, body["session_id"]) == (202, BROWSER_SESSION)
    claim.assert_not_called()
    assert _payload(runs)["case_id"] == ""


def test_a_failed_handover_releases_the_claim(runs):
    runs.lambda_client.invoke.side_effect = RuntimeError("throttled")
    with (
        mock.patch.object(runs, "claim_live_run"),
        mock.patch.object(runs, "release_live_run") as release,
    ):
        assert _call(runs, "/runs", {"case_id": CASE, "prompt": "p"})[0] == 502
    assert release.call_args.args[1] == {"case_id": CASE}


def test_without_an_invoker_a_start_is_503_and_claims_nothing(runs, monkeypatch):
    monkeypatch.setattr(runs, "INVOKER_FUNCTION_NAME", "")
    with mock.patch.object(runs, "claim_live_run") as claim:
        assert _call(runs, "/runs", {"case_id": CASE, "prompt": "p"})[0] == 503
    claim.assert_not_called()


# ── the entitlement gate ──


@pytest.fixture
def closed(runs, monkeypatch):
    monkeypatch.setenv(
        "ENTITLEMENT_MAP",
        json.dumps({"ap": {"groups": ["ap-clerk"]}, "tax": {"groups": ["tax-team"]}}),
    )
    return runs


def test_an_unentitled_case_is_403_before_the_claim(closed):
    with mock.patch.object(closed, "claim_live_run") as claim:
        assert _call(closed, "/runs", {"case_id": CASE, "prompt": "p"})[0] == 403
    claim.assert_not_called()
    closed.lambda_client.invoke.assert_not_called()


def test_an_entitled_caller_starts_and_carries_only_their_packs(closed):
    with mock.patch.object(closed, "claim_live_run") as claim:
        status, _ = _call(
            closed,
            "/runs",
            {"case_id": CASE, "prompt": "p"},
            groups=(USERS, "ap-clerk"),
        )
    assert status == 202 and claim.called
    assert _payload(closed)["visible_packs"] == ["ap"]


def test_a_reader_may_ask_about_a_case_but_not_process_it(runs, monkeypatch):
    approvers = {"ap": {"groups": ["ap-clerk", "ap-mgr"], "approve_groups": ["ap-mgr"]}}
    monkeypatch.setenv("ENTITLEMENT_MAP", json.dumps(approvers))
    process = {"case_id": CASE, "prompt": "p", "intent": "process"}
    with mock.patch.object(runs, "claim_live_run") as claim:
        # Neither a reader nor an approver starts a run: that is a case worker's.
        for groups in ((USERS, "ap-clerk"), (USERS, "ap-mgr")):
            status, body = _call(runs, "/runs", process, groups=groups)
            assert status == 403 and "case workers" in body["error"], groups
        claim.assert_not_called()
        assert (
            _call(runs, "/runs", process, groups=(USERS, "ap-clerk", WORKERS))[0] == 202
        )
        assert _payload(runs)["intent"] == "process"
        # Asking about the case needs only sight of it: the turn writes nothing.
        ask = {"case_id": CASE, "prompt": "why held?"}
        assert _call(runs, "/runs", ask, groups=(USERS, "ap-clerk"))[0] == 202
        assert _payload(runs)["intent"] == "ask"
    # And the case-less conversation, whose grant names no case to write.
    chat = {"prompt": "hi", "session_id": BROWSER_SESSION}
    assert _call(runs, "/runs", chat, groups=(USERS, "ap-clerk"))[0] == 202


# ── intent ──


def test_process_is_handed_over_as_process_and_echoed(runs):
    with mock.patch.object(runs, "claim_live_run"):
        status, body = _call(
            runs,
            "/runs",
            {"case_id": CASE, "prompt": "p", "intent": "process"},
            groups=(USERS, WORKERS),
        )
    assert (status, body["intent"]) == (202, "process")
    assert _payload(runs)["intent"] == "process"


def test_process_on_a_settled_case_is_a_409_and_a_question_is_not(runs):
    process = {"case_id": CASE, "prompt": "p", "intent": "process"}
    settled = CaseSettled("complete")
    with mock.patch.object(runs, "claim_live_run", side_effect=settled) as claim:
        status, body = _call(runs, "/runs", process, groups=(USERS, WORKERS))
    assert (status, body) == (
        409,
        {
            "error": "This case is settled as complete, so Process does not run "
            "on it again. Ask about it instead.",
            "status": "complete",
        },
    )
    assert claim.call_args.kwargs["refuse"] == SETTLED_STATUSES
    runs.lambda_client.invoke.assert_not_called()
    # A question reads only, so a settled case still takes one.
    with mock.patch.object(runs, "claim_live_run") as claim:
        assert _call(runs, "/runs", {"case_id": CASE, "prompt": "why?"})[0] == 202
    assert claim.call_args.kwargs["refuse"] == frozenset()


def test_the_briefing_and_a_case_less_message_are_questions(runs):
    status, body = _call(
        runs, "/runs", {"prompt": "brief me", "session_id": BROWSER_SESSION}
    )
    assert (status, body["intent"]) == (202, "ask")
    assert _payload(runs)["intent"] == "ask"


@pytest.mark.parametrize(
    "body,error",
    [
        (
            {"case_id": CASE, "prompt": "p", "intent": "write"},
            'intent must be "ask" or "process"',
        ),
        (
            {"case_id": CASE, "prompt": "p", "intent": 1},
            'intent must be "ask" or "process"',
        ),
        ({"case_id": CASE, "prompt": "p", "intent": None}, None),
        (
            {"prompt": "p", "session_id": BROWSER_SESSION, "intent": "process"},
            "Process needs a case_id",
        ),
        (
            {"prompt": "p", "intent": "process", "rehearse": True, "process_type": "x"},
            "A rehearsal is not a process run",
        ),
    ],
)
def test_an_unusable_intent_is_400_before_the_claim(runs, body, error):
    with mock.patch.object(runs, "claim_live_run") as claim:
        status, reply = _call(runs, "/runs", body)
    if error is None:  # an explicit null is the default, a question
        assert (status, _payload(runs)["intent"]) == (202, "ask")
        return
    assert (status, reply["error"]) == (400, error)
    claim.assert_not_called()
    runs.lambda_client.invoke.assert_not_called()


def test_a_case_the_gate_cannot_find_is_404_read_consistently(closed):
    table = _case(closed)
    with mock.patch.object(closed, "claim_live_run") as claim:
        status, _ = _call(
            closed,
            "/runs",
            {"case_id": CASE, "prompt": "p"},
            groups=(USERS, "ap-clerk"),
        )
    assert status == 404
    claim.assert_not_called()
    assert table.get_item.call_args.kwargs["ConsistentRead"] is True


@pytest.mark.parametrize("groups,status", [((USERS,), 403), ((USERS, "ap-clerk"), 202)])
def test_a_case_less_run_needs_some_pack(closed, groups, status):
    body = {"prompt": "hi", "session_id": BROWSER_SESSION}
    assert _call(closed, "/runs", body, groups=groups)[0] == status


@pytest.mark.parametrize(
    "groups,status,released", [((USERS,), 403, False), ((USERS, "ap-clerk"), 202, True)]
)
def test_stopping_a_run_is_floored_too(closed, groups, status, released):
    with (
        mock.patch.object(closed, "_stop_runtime_session", return_value=True) as stop,
        mock.patch.object(closed, "release_live_run") as release,
    ):
        assert (
            _call(closed, "/runs/stop", {"case_id": CASE}, groups=groups)[0] == status
        )
    assert stop.called is released and release.called is released


# ── stop ──


def test_stop_ends_the_session_then_releases_the_claim_it_read(runs):
    calls = []
    table = _case(runs, pack_id="ap", live_run={"run_id": "run-9"})
    with (
        mock.patch.object(
            runs,
            "_stop_runtime_session",
            side_effect=lambda s: calls.append("stop") or True,
        ),
        mock.patch.object(
            runs, "release_live_run", side_effect=lambda *a, **k: calls.append(k)
        ),
    ):
        status, body = _call(runs, "/runs/stop", {"case_id": CASE})
    assert (status, body) == (202, {"stopped": True})
    # The agent's next `SET live_run` would undo a release that landed first.
    assert calls == ["stop", {"run_id": "run-9"}]
    assert table.get_item.call_args.kwargs["ConsistentRead"] is True


def test_a_failed_case_read_still_stops_the_session_and_releases_nothing(runs):
    table = _case(runs, pack_id="ap")
    reads = iter([{"Item": {"pack_id": "ap"}}])

    def get_item(**kw):
        if "live_run" in kw.get("ProjectionExpression", ""):
            raise RuntimeError("throttled")
        return next(reads)

    table.get_item.side_effect = get_item
    with (
        mock.patch.object(runs, "_stop_runtime_session", return_value=True) as stop,
        mock.patch.object(runs, "release_live_run") as release,
    ):
        assert _call(runs, "/runs/stop", {"case_id": CASE})[0] == 202
    assert stop.called and not release.called


def _ssm(mod, monkeypatch):
    ssm = mock.MagicMock()
    monkeypatch.setattr(mod.boto3, "client", lambda *a, **k: ssm)
    return ssm


def test_a_refused_runtime_stop_is_reported_and_still_released(runs, monkeypatch):
    _ssm(runs, monkeypatch).get_parameter.return_value = {
        "Parameter": {"Value": "arn:aws:bedrock-agentcore:us-east-1:1:runtime/agent"}
    }
    with (
        mock.patch.object(runs, "get_delegating_token", return_value="tok"),
        mock.patch.object(
            runs.runtime_session.urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("down"),
        ),
        mock.patch.object(runs, "release_live_run") as release,
    ):
        status, body = _call(runs, "/runs/stop", {"case_id": CASE})
    assert (status, body) == (202, {"stopped": False})
    assert release.called


def test_a_stop_failure_never_logs_the_bearer_token(runs, caplog, monkeypatch):
    _ssm(runs, monkeypatch).get_parameter.side_effect = ValueError(
        "Invalid header value 'Bearer super-secret-token'"
    )
    with caplog.at_level(logging.WARNING), mock.patch.object(runs, "release_live_run"):
        assert _call(runs, "/runs/stop", {"case_id": CASE})[0] == 202
    assert "super-secret-token" not in caplog.text
    assert "ValueError" in caplog.text


def test_stop_needs_something_to_stop(runs):
    assert _call(runs, "/runs/stop", {})[0] == 400


# ── faults ──


@pytest.mark.parametrize(
    "fault,logged,hidden",
    [
        (
            ClientError(
                {
                    "Error": {
                        "Code": "ProvisionedThroughputExceededException",
                        "Message": "slow",
                    }
                },
                "UpdateItem",
            ),
            ["ClientError", "ProvisionedThroughputExceededException"],
            "slow",
        ),
        (ValueError("bad key"), ["ValueError"], "bad key"),
    ],
)
def test_an_infra_fault_is_a_500_that_carries_cors_and_logs_no_message(
    runs, caplog, fault, logged, hidden
):
    runs.dynamodb.Table.return_value.update_item.side_effect = fault
    with caplog.at_level(logging.ERROR):
        response = runs.handler(_req("/runs", {"case_id": CASE, "prompt": "p"}), None)
    assert response["statusCode"] == 500
    assert json.loads(response["body"]) == {"error": "Internal server error"}
    assert response["headers"]["Access-Control-Allow-Origin"] == ORIGIN
    assert all(word in caplog.text for word in logged)
    assert hidden not in caplog.text


def test_a_non_dict_error_member_still_answers_500(runs, caplog):
    fault = ValueError("bad key")
    fault.response = {"Error": "throttled"}
    runs.dynamodb.Table.return_value.update_item.side_effect = fault
    with caplog.at_level(logging.ERROR):
        response = runs.handler(_req("/runs", {"case_id": CASE, "prompt": "p"}), None)
    assert response["statusCode"] == 500
    assert "throttled" not in caplog.text


# ── rehearsal ──

_REHEARSAL = {
    "rehearse": True,
    "prompt": "vendor 100214 is $412 over",
    "process_type": "invoice_mismatch",
    "session_id": BROWSER_SESSION,
}


def _rehearse(mod, monkeypatch, extra=None, mapping=None, groups=(USERS, AUTHORS)):
    monkeypatch.setenv("SOP_AUTHOR_GROUPS", AUTHORS)
    seen = {}

    def pack_map(include_incomplete=False):
        seen["include_incomplete"] = include_incomplete
        return {"invoice_mismatch": "ap"} if include_incomplete else {}

    monkeypatch.setattr(mod.registry, "process_type_pack_map", mapping or pack_map)
    status, body = _call(mod, "/runs", {**_REHEARSAL, **(extra or {})}, groups=groups)
    return status, body, seen


def test_a_rehearsal_runs_case_less_reaches_an_incomplete_pack_and_says_so(
    runs, monkeypatch
):
    with mock.patch.object(runs, "claim_live_run") as claim:
        status, body, seen = _rehearse(runs, monkeypatch)
    assert (status, body["rehearsal"]) == (202, True)
    assert seen["include_incomplete"] is True
    claim.assert_not_called()
    payload = _payload(runs)
    assert (payload["rehearse"], payload["case_id"]) == (True, "")
    # A rehearsal is its own read-only run, and never a process run.
    assert "intent" not in payload and "intent" not in body


def test_a_rehearsal_needs_the_deployment_floor(runs, monkeypatch):
    status, body, _ = _rehearse(runs, monkeypatch, groups=(USERS,))
    assert (status, body["error"]) == (403, "Not a pack author")
    runs.lambda_client.invoke.assert_not_called()


def test_a_rehearsal_needs_the_packs_own_authoring_grant(runs, monkeypatch):
    monkeypatch.setenv(
        "ENTITLEMENT_MAP", json.dumps({"ap": {"author_groups": ["ap-lead"]}})
    )
    status, body, _ = _rehearse(runs, monkeypatch)
    assert (status, body["error"]) == (403, "You may not rehearse ap")
    runs.lambda_client.invoke.assert_not_called()


@pytest.mark.parametrize(
    "extra,mapping,error",
    [
        (
            {},
            lambda include_incomplete=False: {},
            "No pack maps process_type invoice_mismatch",
        ),
        ({"process_type": "  "}, None, "process_type is required to rehearse"),
        ({"case_id": CASE}, None, "A rehearsal runs no case"),
    ],
)
def test_a_rehearsal_that_cannot_be_resolved_is_400(
    runs, monkeypatch, extra, mapping, error
):
    with mock.patch.object(runs, "claim_live_run") as claim:
        status, body, _ = _rehearse(runs, monkeypatch, extra, mapping)
    assert (status, body["error"]) == (400, error)
    claim.assert_not_called()
    runs.lambda_client.invoke.assert_not_called()


@pytest.mark.parametrize(
    "body",
    [
        {"prompt": 123},
        {"prompt": "p", "session_id": 5},
        {"prompt": "p", "rehearse": True, "process_type": ["x"]},
        {"prompt": "p", "rehearse": "yes"},
    ],
)
def test_a_field_of_the_wrong_type_is_400_not_500(runs, body):
    status, reply = _call(runs, "/runs", body)
    assert status == 400 and "must be" in reply["error"]
