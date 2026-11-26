# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""business-operations API: POST /deployment/stop, the emergency stop.

Admins only. The switch goes to `manual` every time, every unexpired grant is revoked,
each run with a case has its session stopped, its claim released and its case parked
when it was `processing`, and each step happens whatever the one before it managed.
"""

from __future__ import annotations

import importlib.util
import json
import time
from unittest import mock

import pytest
from botocore.exceptions import ClientError
from case_key import derive_case_id, to_runtime_session_id
from test_business_operations_api import CONSOLE, UC, USERS, _event

ADMINS = "business-operations-admins"
PREFIX = "/p/dev/use-cases/business-operations/autonomy"
CASE = derive_case_id("ap", "ap", {"SupplierInvoice": "5100001976"})
OTHER = derive_case_id("ap", "ap", {"SupplierInvoice": "5100001977"})


@pytest.fixture
def api(monkeypatch):
    for k, v in {
        "API_ALLOWED_CLIENTS": CONSOLE,
        "USERS_GROUP": USERS,
        "ADMINS_GROUP": ADMINS,
        "TRIGGER_MODE_PREFIX": PREFIX,
        "CASES_TABLE": "cases",
        "CONFIG_TABLE": "cfg",
        "RUN_GRANTS_TABLE": "grants",
        "RUNTIME_ARN_PARAMETER": "/p/dev/runtimes/business-operations/arn",
        "AWS_REGION": "us-east-1",
        "EMERGENCY_STOP_PARAMETER": f"{PREFIX}/emergency-stop",
        "AGENT_QUEUE_MAPPING_ID": "mapping-1",
    }.items():
        monkeypatch.setenv(k, v)
    with mock.patch("boto3.client"), mock.patch("boto3.resource"):
        spec = importlib.util.spec_from_file_location(
            "bo_api_stop", UC / "api" / "stop" / "index.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    tables = {n: mock.MagicMock(name=n) for n in ("cases", "cfg", "grants")}
    mod.dynamodb = mock.MagicMock()
    mod.dynamodb.Table.side_effect = tables.__getitem__
    mod.ssm = mock.MagicMock()
    mod.lambda_client = mock.MagicMock()
    tables["grants"].scan.return_value = {
        "Items": [
            {"run_id": "run-a", "case_id": CASE},
            {"run_id": "run-b"},  # a conversation turn: no case
            {"run_id": "run-c", "case_id": OTHER},
        ]
    }
    mod.tables = tables
    mod.sessions = []

    def session_stop(session_id, **_):
        mod.sessions.append(session_id)
        return True

    monkeypatch.setattr(mod.runtime_session, "stop", session_stop)
    monkeypatch.setattr(mod, "release_live_run", lambda *a, **k: True)
    return mod


def _post(api, body, groups=(USERS, ADMINS)):
    event = _event("POST", "/deployment/stop", groups=groups, body=json.dumps(body))
    response = api.handler(event, None)
    return response["statusCode"], json.loads(response["body"])


def _conditional():
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
    )


def test_only_an_admin_may_stop_and_a_refusal_changes_nothing(api):
    status, body = _post(api, {"reason": "x"}, groups=(USERS,))
    assert status == 403 and "administrator" in body["error"]
    api.ssm.put_parameter.assert_not_called()
    api.tables["grants"].delete_item.assert_not_called()


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"reason": ""},
        {"reason": "   "},
        {"reason": "a\nb"},
        {"reason": "x" * 501},
        {"reason": 5},
    ],
)
def test_a_stop_needs_a_reason_on_one_line(api, body):
    status, answer = _post(api, body)
    assert (status, answer.get("field")) == (400, "reason")
    api.ssm.put_parameter.assert_not_called()


def test_a_stop_sets_manual_revokes_every_grant_and_stops_each_case_run(api):
    # The second case is not `processing`: released, not parked.
    api.tables["cases"].update_item.side_effect = [None, _conditional()]
    status, body = _post(api, {"reason": "  SAP shows releases no case explains "})
    assert status == 200
    stop = body["stop"]
    # The stop stands first, so nothing starts while the runs are swept.
    stands = api.ssm.put_parameter.call_args_list[0].kwargs
    assert stands["Name"] == f"{PREFIX}/emergency-stop"
    assert json.loads(stands["Value"]) == {
        "stands": True,
        "stopped_at": stop["stopped_at"],
        "stopped_by": "op@example.com (sub-1)",
        "reason": "SAP shows releases no case explains",
    }
    # The switch, always written, so its audit-change alert fires for every stop.
    first = api.ssm.put_parameter.call_args_list[1].kwargs
    assert first["Name"] == f"{PREFIX}/trigger-mode" and first["Value"] == "manual"
    assert "op@example.com (sub-1) (emergency stop)" == first["Description"]
    # The agent queue's consumer paused, so queued work waits.
    api.lambda_client.update_event_source_mapping.assert_called_once_with(
        UUID="mapping-1", Enabled=False
    )
    assert stop["stands"] is True and stop["queue_held"] is True
    actor = api.ssm.put_parameter.call_args_list[2].kwargs
    assert actor["Name"] == f"{PREFIX}/trigger-mode-last-changed-by"
    assert "(emergency stop)" in actor["Value"]
    revoked = {
        c.kwargs["Key"]["run_id"]
        for c in api.tables["grants"].delete_item.call_args_list
    }
    assert revoked == {"run-a", "run-b", "run-c"}
    assert sorted(api.sessions) == sorted(
        to_runtime_session_id(c) for c in (CASE, OTHER)
    )
    assert stop["reason"] == "SAP shows releases no case explains"
    assert stop["stopped_by"] == "op@example.com (sub-1)"
    assert stop["trigger_mode_set"] is True and stop["complete"] is True
    runs = {r["run_id"]: r for r in stop["runs"]}
    assert stop["runs_total"] == 3
    assert runs["run-b"] == {
        "run_id": "run-b",
        "case_id": None,
        "session_stopped": False,
        "released": False,
        "parked": False,
    }
    parked = sorted(r["parked"] for r in (runs["run-a"], runs["run-c"]))
    assert parked == [False, True]
    assert all(
        runs[r]["session_stopped"] and runs[r]["released"] for r in ("run-a", "run-c")
    )
    # The park is conditioned on `processing` and names the stop.
    park = api.tables["cases"].update_item.call_args_list[0].kwargs
    assert park["ConditionExpression"] == "#s = :processing"
    values = park["ExpressionAttributeValues"]
    assert values[":parked"] == "manual_review_required"
    assert "Stopped by an emergency stop: SAP shows releases" in values[":r"]
    # Recorded where GET /deployment/settings reads it.
    item = api.tables["cfg"].put_item.call_args.kwargs["Item"]
    assert (item["namespace"], item["config_key"]) == ("deployment", "last-stop")
    assert item["runs_total"] == 3


def test_only_unexpired_grants_are_listed(api):
    _post(api, {"reason": "r"})
    scan = api.tables["grants"].scan.call_args.kwargs
    attr, now = scan["FilterExpression"].get_expression()["values"]
    assert attr.name == "expires_at" and abs(now - time.time()) < 5


def test_a_failed_switch_write_still_stops_every_run(api):
    api.ssm.put_parameter.side_effect = RuntimeError("throttled")
    status, body = _post(api, {"reason": "r"})
    assert status == 200 and body["stop"]["trigger_mode_set"] is False
    assert api.tables["grants"].delete_item.call_count == 3
    assert len(api.sessions) == 2


def test_out_of_time_still_revokes_and_says_it_is_incomplete(api, monkeypatch):
    def slow(session_id, **_):
        time.sleep(0.5)
        return True

    monkeypatch.setattr(api.runtime_session, "stop", slow)
    record = api.stop("r", "op", budget=0.05)
    assert record["complete"] is False
    assert api.tables["grants"].delete_item.call_count == 3
    assert not any(r["session_stopped"] for r in record["runs"])


def test_no_grants_table_still_sets_manual(api, monkeypatch):
    monkeypatch.setattr(api, "RUN_GRANTS_TABLE", "")
    status, body = _post(api, {"reason": "r"})
    assert status == 200 and body["stop"]["runs"] == []
    assert body["stop"]["trigger_mode_set"] is True


def test_the_record_lists_at_most_100_runs_and_counts_them_all(api):
    api.tables["grants"].scan.return_value = {
        "Items": [{"run_id": f"run-{i:03}"} for i in range(150)]
    }
    record = api.stop("r", "op")
    assert record["runs_total"] == 150 and len(record["runs"]) == 100


def test_only_post(api):
    event = _event("GET", "/deployment/stop", groups=(USERS, ADMINS))
    assert api.handler(event, None)["statusCode"] == 405


# ── the stop stands until cleared ──


def _standing(api, **extra):
    state = {
        "stands": True,
        "stopped_at": "2026-11-21T10:15:02+00:00",
        "stopped_by": "op@example.com (sub-1)",
        "reason": "r",
        **extra,
    }
    api.ssm.get_parameter.return_value = {"Parameter": {"Value": json.dumps(state)}}
    return state


def _clear(api, body, groups=(USERS, ADMINS)):
    event = _event(
        "POST", "/deployment/stop/clear", groups=groups, body=json.dumps(body)
    )
    response = api.dispatch(event, None)
    return response["statusCode"], json.loads(response["body"])


def test_a_clear_ends_the_stop_resumes_the_queue_and_leaves_the_trigger_mode_alone(api):
    _standing(api)
    api.tables["cfg"].update_item.return_value = {
        "Attributes": {
            "namespace": "deployment",
            "config_key": "last-stop",
            "runs_total": 2,
        }
    }
    status, body = _clear(api, {"reason": "fixed in v15"})
    assert status == 200, body
    stop = body["stop"]
    assert stop["stands"] is False and stop["clear_reason"] == "fixed in v15"
    assert stop["cleared_by"] == "op@example.com (sub-1)" and stop["runs_total"] == 2
    (written,) = api.ssm.put_parameter.call_args_list
    assert written.kwargs["Name"] == f"{PREFIX}/emergency-stop"
    assert json.loads(written.kwargs["Value"])["stands"] is False
    # Never the switch: the trigger mode stays manual.
    assert not any(
        "trigger-mode" in c.kwargs["Name"] for c in api.ssm.put_parameter.call_args_list
    )
    api.lambda_client.update_event_source_mapping.assert_called_once_with(
        UUID="mapping-1", Enabled=True
    )
    api.tables["grants"].delete_item.assert_not_called()


def _in_use():
    return ClientError(
        {"Error": {"Code": "ResourceInUseException"}}, "UpdateEventSourceMapping"
    )


def test_a_clear_waits_for_a_mapping_still_pausing_then_resumes_it(api, monkeypatch):
    # A clear seconds after a stop meets the mapping mid-Disabling.
    waits = []
    monkeypatch.setattr(api.time, "sleep", waits.append)
    _standing(api)
    api.lambda_client.update_event_source_mapping.side_effect = [
        _in_use(),
        _in_use(),
        {},
    ]
    status, body = _clear(api, {"reason": "fixed in v15"})
    assert status == 200, body
    assert body["stop"]["queue_resumed"] is True
    assert api.lambda_client.update_event_source_mapping.call_count == 3
    assert waits == [api.QUEUE_MAPPING_WAIT_SECONDS] * 2


def test_a_mapping_that_never_settles_reports_the_queue_not_resumed(api, monkeypatch):
    monkeypatch.setattr(api.time, "sleep", lambda _: None)
    _standing(api)
    api.lambda_client.update_event_source_mapping.side_effect = _in_use()
    status, body = _clear(api, {"reason": "fixed in v15"})
    assert status == 200, body
    assert body["stop"]["queue_resumed"] is False
    assert (
        api.lambda_client.update_event_source_mapping.call_count
        == api.QUEUE_MAPPING_TRIES
    )


def test_a_clear_needs_a_standing_stop_an_admin_and_a_reason(api):
    api.ssm.get_parameter.side_effect = ClientError(
        {"Error": {"Code": "ParameterNotFound"}}, "GetParameter"
    )
    assert _clear(api, {"reason": "x"})[0] == 409
    api.ssm.get_parameter.side_effect = None
    _standing(api, stands=False)
    assert _clear(api, {"reason": "x"}) == (409, {"error": "No emergency stop stands"})
    _standing(api)
    assert _clear(api, {"reason": "x"}, groups=(USERS,))[0] == 403
    assert _clear(api, {})[0] == 400
    api.ssm.put_parameter.assert_not_called()


def test_an_unwritten_clear_says_the_stop_still_stands(api):
    _standing(api)
    api.ssm.put_parameter.side_effect = RuntimeError("throttled")
    status, body = _clear(api, {"reason": "x"})
    assert status == 502 and "still stands" in body["error"]
    api.lambda_client.update_event_source_mapping.assert_not_called()


def test_an_operator_with_iam_stops_and_clears_through_the_same_function(api):
    out = api.dispatch({"action": "stop", "reason": "r", "actor": "arn:aws:sts::1:x"})
    assert out["ok"] and out["stop"]["stopped_by"] == "arn:aws:sts::1:x (IAM)"
    assert api.tables["grants"].delete_item.call_count == 3
    _standing(api)
    api.ssm.put_parameter.reset_mock()
    out = api.dispatch({"action": "clear", "reason": "ok", "actor": "arn:aws:sts::1:x"})
    assert out["ok"] and out["stop"]["stands"] is False
    assert api.dispatch({"action": "drop", "reason": "r"})["ok"] is False
    assert api.dispatch({"action": "stop", "reason": ""})["ok"] is False


# ── emergency_stop, as every gate reads it ──


def test_an_absent_stop_is_none_and_an_unreadable_one_stands(monkeypatch):
    import emergency_stop

    ssm = mock.MagicMock()
    monkeypatch.setenv(emergency_stop.PARAMETER_ENV, "/p")
    ssm.get_parameter.side_effect = ClientError(
        {"Error": {"Code": "ParameterNotFound"}}, "GetParameter"
    )
    assert emergency_stop.standing(ssm) is None
    ssm.get_parameter.side_effect = RuntimeError("throttled")
    assert emergency_stop.standing(ssm)["unreadable"] is True
    ssm.get_parameter.side_effect = None
    for raw, stands in (("not json", True), ("[]", True), ('{"stands": false}', False),
                        ('{"stands": true, "reason": "r"}', True)):  # fmt: skip
        ssm.get_parameter.return_value = {"Parameter": {"Value": raw}}
        assert (emergency_stop.standing(ssm) is not None) is stands, raw
    monkeypatch.setenv(emergency_stop.PARAMETER_ENV, "")
    assert emergency_stop.standing(ssm) is None


def test_the_refusal_names_the_stop():
    import emergency_stop

    state = {"stands": True, "reason": "SAP shows releases", "stopped_by": "op",
             "stopped_at": "t", "runs": ["x"]}  # fmt: skip
    body = emergency_stop.refusal_body(state)
    assert body["error"] == (
        "An emergency stop stands: SAP shows releases (op, t). Nothing starts until "
        "an admin clears it."
    )
    assert "runs" not in body["emergency_stop"]
