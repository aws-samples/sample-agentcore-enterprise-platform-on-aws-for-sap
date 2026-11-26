# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""While an emergency stop stands, nothing new starts: one test per path that starts a
run, each refusing with the stop's reason and recording nothing."""

from __future__ import annotations

import json
from unittest import mock

import pytest
import test_business_operations_api_invoker as ti
import test_business_operations_api_runs as tr
import test_business_operations_pipeline  # noqa: F401 - puts pipeline/ on the path
from test_business_operations_api import USERS, _cases
from test_business_operations_api_entitlement import _MAP, CASE, WORKERS, _enqueue
from test_business_operations_api_invoker import invoker  # noqa: F401 - fixture
from test_business_operations_api_runs import runs  # noqa: F401 - fixture

STOP = {
    "stands": True,
    "stopped_at": "2026-11-21T10:15:02+00:00",
    "stopped_by": "op@example.com (sub-1)",
    "reason": "SAP shows releases no case explains",
}


@pytest.fixture
def standing(monkeypatch):
    import emergency_stop

    monkeypatch.setattr(emergency_stop, "standing", lambda *a, **k: dict(STOP))
    return emergency_stop


def _refused(status, body):
    assert status == 409, body
    assert body["error"].startswith("An emergency stop stands: SAP shows releases")
    assert body["emergency_stop"]["stopped_by"] == "op@example.com (sub-1)"


@pytest.mark.parametrize(
    "start",
    [
        {"case_id": tr.CASE, "prompt": "p", "intent": "process"},
        {"case_id": tr.CASE, "prompt": "why held?"},
        {"prompt": "brief me", "session_id": tr.BROWSER_SESSION},
    ],
)
def test_every_start_on_post_runs_is_refused_and_claims_nothing(
    runs,  # noqa: F811
    standing,
    start,
):
    with mock.patch.object(runs, "claim_live_run") as claim:
        _refused(*tr._call(runs, "/runs", start, groups=(USERS, tr.WORKERS)))
    claim.assert_not_called()
    runs.lambda_client.invoke.assert_not_called()


def test_start_runs_is_refused_and_queues_nothing(monkeypatch, standing):
    mod, _table, sqs = _cases(
        monkeypatch, _MAP, items={CASE: {"case_id": CASE, "pack_id": "ap"}}
    )
    monkeypatch.setenv("CASE_WORKER_GROUPS", WORKERS)
    _refused(*_enqueue(mod, {"case_id": CASE}, groups=(USERS, "ap-clerk", WORKERS)))
    assert not sqs.sent


def test_a_decision_is_refused_before_anything_is_recorded(monkeypatch, standing):
    from test_business_operations_api import _reply

    mod, table, sqs = _cases(
        monkeypatch,
        {"ap": {}},
        items={
            CASE: {"case_id": CASE, "pack_id": "ap", "status": "awaiting_human_input"}
        },
    )
    response = _reply(mod, "approved", groups=(USERS,))
    _refused(response["statusCode"], json.loads(response["body"]))
    assert not sqs.sent
    assert not table.updates


def test_a_ticket_action_is_refused_before_anything_is_recorded(monkeypatch, standing):
    import test_business_operations_api_tickets as tt

    for k, v in {
        "API_ALLOWED_CLIENTS": tt.CONSOLE,
        "USERS_GROUP": USERS,
        "TICKETS_TABLE_NAME": "tickets",
        "CASES_TABLE_NAME": "cases",
        "ENTITLEMENT_MAP": json.dumps(tt._MAP),
        "AGENT_QUEUE_URL": "q",
    }.items():
        monkeypatch.setenv(k, v)
    with mock.patch("boto3.resource"), mock.patch("boto3.client"):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "bo_api_tickets_stop", tt.UC / "api" / "tickets" / "index.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    mod.QUEUE_URL = "q"
    event = tt._event(
        "POST",
        "/tickets/{id}/action",
        groups=(USERS,),
        pathParameters={"id": "TKT-1"},
        body=json.dumps({"action": "approved"}),
    )
    response = mod.handler(event, None)
    _refused(response["statusCode"], json.loads(response["body"]))
    mod.table.get_item.assert_not_called()
    mod.sqs.send_message.assert_not_called()


def test_the_producers_gate_queues_nothing(monkeypatch, standing):
    import case_store

    monkeypatch.setattr(case_store, "get_trigger_mode", lambda: "auto")
    monkeypatch.setattr(case_store, "get_pack_mode", lambda p: "auto")
    monkeypatch.setattr(case_store, "QUEUE_URL", "q")
    sent = []
    monkeypatch.setattr(case_store, "send", lambda *a, **k: sent.append(a))
    gate = case_store.Gate()
    assert not gate.allows("finance_ops")
    assert not case_store.enqueue("ap-x", "finance_ops", "poller", gate)
    assert not sent


def test_the_sweeper_and_the_operator_rerun_refuse(monkeypatch, standing):
    import batch_runner
    import operator_rerun

    monkeypatch.setattr(batch_runner, "QUEUE_URL", "q")
    out = batch_runner.handler({}, None)
    assert out["skipped"] == "emergency-stop"

    table = mock.MagicMock()
    table.get_item.return_value = {"Item": {"status": "error"}}
    monkeypatch.setattr(operator_rerun, "QUEUE_URL", "q")
    monkeypatch.setattr(operator_rerun, "get_cases_table", lambda: table)
    sent = []
    monkeypatch.setattr(operator_rerun, "send", lambda *a, **k: sent.append(a))

    class Ctx:
        aws_request_id = "r"

    out = operator_rerun.handler({"case_id": "ap-" + "a" * 26}, Ctx())
    assert out["queued"] is False and "emergency stop stands" in out["error"]
    assert not sent


def test_the_invoker_holds_a_queued_message_and_marks_its_case(
    invoker,  # noqa: F811
    monkeypatch,
    standing,
):
    response, table, sent, claim = ti._queued(invoker, monkeypatch, ["detected"])
    assert response == ti.FAIL and not sent and not claim.called
    (mark,) = table.update_item.call_args_list
    assert mark.kwargs["UpdateExpression"] == "SET held_by_stop = :t"
    assert mark.kwargs["ExpressionAttributeValues"] == {":t": STOP["stopped_at"]}


def test_a_held_message_is_not_dropped_as_a_redelivery_once_cleared(
    invoker,  # noqa: F811
    monkeypatch,
):
    # A reply to a parked case, held once by a stop: its second delivery runs.
    monkeypatch.setattr(invoker, "_held", lambda *a: True)
    _, _table, sent, _ = ti._queued(
        invoker, monkeypatch, ["awaiting_human_input"], receive_count=2
    )
    assert sent
    # Not held: the guard drops a redelivery into a parked case, as before.
    monkeypatch.setattr(invoker, "_held", lambda *a: False)
    _, _, sent, _ = ti._queued(
        invoker, monkeypatch, ["awaiting_human_input"], receive_count=2
    )
    assert not sent


def test_a_direct_start_releases_its_claim_and_runs_nothing(
    invoker,  # noqa: F811
    monkeypatch,
    standing,
):
    sent, release = ti._direct(invoker, monkeypatch)
    assert not sent and release.called


def test_settings_say_whether_the_stop_stands_from_its_parameter(monkeypatch):
    import importlib.util

    import emergency_stop
    import test_business_operations_api_config as tc

    for k, v in {
        "API_ALLOWED_CLIENTS": tc.CONSOLE,
        "USERS_GROUP": USERS,
        "CONFIG_TABLE": "cfg",
        "CONTACTS_JSON": "{}",
    }.items():
        monkeypatch.setenv(k, v)
    with mock.patch("boto3.resource"), mock.patch("boto3.client"):
        spec = importlib.util.spec_from_file_location(
            "bo_api_config_stop", tc.UC / "api" / "config" / "index.py"
        )
        config = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(config)
    config.table = mock.MagicMock()
    config.table.scan.return_value = {"Items": []}
    config.s3 = mock.MagicMock()
    config.table.get_item.return_value = {
        "Item": {"namespace": "deployment", "config_key": "last-stop", "runs_total": 1}
    }
    monkeypatch.setenv("ENTITLEMENT_MAP", json.dumps({"finance_ops": {}}))
    for state, stands in (
        (dict(STOP), True),
        ({**STOP, "stands": False, "cleared_by": "x", "cleared_at": "t2"}, False),
    ):
        monkeypatch.setattr(emergency_stop, "read", lambda *a, s=state: s)
        last = tc._body(tc._get(config, tc.SETTINGS))["last_stop"]
        assert last["stands"] is stands and last["runs_total"] == 1
        assert ("cleared_by" in last) is (not stands)


def test_ops_pulls_and_clears_the_stop_through_the_stop_function(monkeypatch, capsys):
    import sys

    sys.path.insert(0, str(ti.UC))
    import ops

    calls = []
    monkeypatch.setattr(ops, "_own_function", lambda key: (f"fn-{key}", "us-west-2"))
    monkeypatch.setattr(ops, "_caller", lambda region: "arn:aws:sts::1:assumed/x")

    def invoke(fn, region, payload, seconds):
        calls.append((fn, payload))
        stop = {**STOP, "runs": [], "runs_total": 0, "complete": True}
        return {"ok": True, "stop": stop}

    monkeypatch.setattr(ops, "_invoke", invoke)
    assert ops.emergency_stop(["SAP", "looks", "wrong", "--yes"]) == 0
    assert ops.emergency_stop(["--clear", "fixed", "--yes"]) == 0
    assert calls == [
        (
            "fn-stop-function",
            {
                "action": "stop",
                "reason": "SAP looks wrong",
                "actor": "arn:aws:sts::1:assumed/x",
            },
        ),
        (
            "fn-stop-function",
            {"action": "clear", "reason": "fixed", "actor": "arn:aws:sts::1:assumed/x"},
        ),
    ]
    assert ops.emergency_stop(["--yes"]) == 2, "a reason is required"
