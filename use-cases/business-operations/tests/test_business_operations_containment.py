# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""business-operations: automatic containment. An anomaly alarm going to ALARM sets
the trigger mode to `manual`, once, never to anything wider, and never over a person's later
choice; and the rule, the function and its role, as synthesized."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest
from app_synth import synth_app

REPO = Path(__file__).resolve().parents[3]
UC = REPO / "use-cases" / "business-operations"
sys.path.insert(0, str(UC))

MAIN = "agentcore-workshop-dev-uc-business-operations"
PARAM = "/agentcore-workshop/dev/use-cases/business-operations/autonomy/trigger-mode"
FIRED = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


class FakeSsm:
    """One parameter store: what was written, and when."""

    class exceptions:
        class ParameterNotFound(Exception):
            pass

    def __init__(self, value: str | None, modified: datetime = FIRED):
        self.params = {} if value is None else {PARAM: (value, modified)}
        self.puts: list[dict] = []

    def get_parameter(self, Name):
        if Name not in self.params:
            raise self.exceptions.ParameterNotFound()
        value, modified = self.params[Name]
        return {"Parameter": {"Value": value, "LastModifiedDate": modified}}

    def put_parameter(self, **kw):
        self.puts.append(kw)
        self.params[kw["Name"]] = (kw["Value"], datetime.now(timezone.utc))


@pytest.fixture
def contain(monkeypatch):
    monkeypatch.setenv("TRIGGER_MODE_PARAMETER", PARAM)
    monkeypatch.setenv("TOPIC_ARN", "arn:aws:sns:us-east-1:1:ops")
    with mock.patch("boto3.client"):
        spec = importlib.util.spec_from_file_location(
            "bo_containment", UC / "containment" / "index.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

    def run(ssm: FakeSsm, state="ALARM", at=FIRED):
        mod._ssm = ssm
        event = {
            # EventBridge cuts `time` to the second; the alarm's state keeps milliseconds.
            "time": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "detail": {
                "alarmName": "p-dev-RunCheckRefusalAlarm",
                "state": {
                    "value": state,
                    "reason": "Threshold Crossed",
                    "timestamp": at.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "+0000",
                },
            },
        }
        return mod.contain(event)

    run.mod = mod
    return run


def test_an_alarm_sets_auto_to_manual_and_says_why(contain):
    ssm = FakeSsm("auto", FIRED - timedelta(days=1))
    assert contain(ssm)["contained"]
    mode, record = ssm.puts
    assert (mode["Name"], mode["Value"]) == (PARAM, "manual")
    assert "RunCheckRefusalAlarm" in mode["Description"]
    assert record["Name"] == f"{PARAM}-last-changed-by"
    assert record["Value"].startswith("manual set by automatic containment")
    ((_, kw),) = contain.mod._sns.publish.call_args_list
    assert (
        "RunCheckRefusalAlarm" in kw["Message"]
        and "trigger-mode set auto" in kw["Message"]
    )


@pytest.mark.parametrize("value", ["auto", "manual", "yolo", None])
def test_it_never_writes_anything_but_manual(contain, value):
    ssm = FakeSsm(value, FIRED - timedelta(hours=1))
    contain(ssm)
    assert {p["Value"] for p in ssm.puts if p["Name"] == PARAM} <= {"manual"}


def test_a_second_delivery_of_the_same_alarm_does_nothing(contain):
    ssm = FakeSsm("auto", FIRED - timedelta(days=1))
    assert contain(ssm)["contained"]
    writes = len(ssm.puts)
    assert not contain(ssm)["contained"]
    assert len(ssm.puts) == writes


@pytest.mark.parametrize("value", ["manual", None])
def test_already_manual_or_unset_is_left_alone(contain, value):
    ssm = FakeSsm(value)
    assert contain(ssm) == {
        "contained": False,
        "why": "the trigger mode is already manual",
    }
    assert ssm.puts == []


def test_an_operator_who_sets_auto_after_the_alarm_keeps_it(contain):
    # Contained, then a person looked and set auto again; the same alarm's event,
    # delivered late, must not take it back.
    ssm = FakeSsm("auto", FIRED + timedelta(minutes=5))
    result = contain(ssm)
    assert not result["contained"] and "after" in result["why"]
    assert ssm.puts == []


def test_auto_set_earlier_in_the_alarms_own_second_is_still_contained(contain):
    # Seen live: auto written at :51.4, the alarm fired at :51.9, event `time` :51.
    ssm = FakeSsm("auto", FIRED + timedelta(milliseconds=400))
    assert contain(ssm, at=FIRED + timedelta(milliseconds=900))["contained"]


def test_only_an_alarm_transition_contains(contain):
    ssm = FakeSsm("auto", FIRED - timedelta(days=1))
    assert not contain(ssm, state="OK")["contained"]
    assert ssm.puts == []


# ── As synthesized ──


@pytest.fixture(scope="module")
def resources() -> dict:
    result, out = synth_app(UC / "fixtures/contract/business-operations.yaml")
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads((out / f"{MAIN}.template.json").read_text())["Resources"]


def _logical(resources: dict, type_: str, prefix: str) -> str:
    (found,) = [
        k for k, r in resources.items() if r["Type"] == type_ and k.startswith(prefix)
    ]
    return found


def test_the_rule_sends_the_trigger_alarms_alarm_state_to_the_function(resources):
    from business_operations import containment

    rule = resources[_logical(resources, "AWS::Events::Rule", "ContainmentRule")]
    pattern = rule["Properties"]["EventPattern"]
    assert pattern["detail-type"] == ["CloudWatch Alarm State Change"]
    assert pattern["detail"] == {"state": {"value": ["ALARM"]}}
    own = [r["Fn::GetAtt"][0] for r in pattern["resources"] if "Fn::GetAtt" in r]
    assert sorted(o.rstrip("0123456789ABCDEF") for o in own) == sorted(
        containment.TRIGGERS
    )
    platform = [r for r in pattern["resources"] if "Fn::GetAtt" not in r]
    assert "guardrail-content-interventions" in json.dumps(platform)
    (target,) = rule["Properties"]["Targets"]
    fn = _logical(resources, "AWS::Lambda::Function", "Containment")
    assert target["Arn"] == {"Fn::GetAtt": [fn, "Arn"]}


def test_the_platform_alarm_it_names_is_the_one_the_platform_creates():
    from business_operations import containment

    source = (REPO / "stacks" / "observability_stack.py").read_text()
    for name in containment.PLATFORM_TRIGGERS:
        assert f'"{name}"' in source


def test_its_role_holds_the_switch_and_the_topic_and_nothing_else(resources):
    fn = resources[_logical(resources, "AWS::Lambda::Function", "Containment")]
    role = fn["Properties"]["Role"]["Fn::GetAtt"][0]
    statements = [
        s
        for r in resources.values()
        if r["Type"] == "AWS::IAM::Policy" and {"Ref": role} in r["Properties"]["Roles"]
        for s in r["Properties"]["PolicyDocument"]["Statement"]
    ]
    granted = {}
    for s in statements:
        actions = s["Action"] if isinstance(s["Action"], list) else [s["Action"]]
        for a in actions:
            granted.setdefault(a, []).append(json.dumps(s["Resource"]))
    xray = {"xray:PutTraceSegments", "xray:PutTelemetryRecords"}
    assert set(granted) - xray == {
        "ssm:GetParameter",
        "ssm:PutParameter",
        "sns:Publish",
    }
    (read,) = granted["ssm:GetParameter"]
    assert read.endswith('autonomy/trigger-mode"]]}')
    assert sorted(w.rsplit("/", 1)[1] for w in granted["ssm:PutParameter"]) == [
        'trigger-mode"]]}',
        'trigger-mode-last-changed-by"]]}',
    ]
    (topic,) = granted["sns:Publish"]
    assert "opstopicarn" in topic  # the platform topic, read from SSM


def test_the_run_check_refusal_alarm_counts_this_use_cases_interceptor_refusals(
    resources,
):
    f = resources[
        _logical(resources, "AWS::Logs::MetricFilter", "RunCheckRefusalFilter")
    ]
    props = f["Properties"]
    assert (
        props["LogGroupName"] == "/aws/lambda/agentcore-workshop-dev-egress-interceptor"
    )
    assert "business_operations_check" in props["FilterPattern"]
    (t,) = props["MetricTransformations"]
    assert t["MetricName"] == "RunCheckRefusal"


# ── ops.py break-glass ──


class FakeLambda:
    def __init__(self):
        self.calls: list[tuple] = []

    def list_event_source_mappings(self, FunctionName):
        arn = "arn:aws:sqs:us-east-1:1:p-dev-agent.fifo"
        return {"EventSourceMappings": [{"UUID": "u-1", "EventSourceArn": arn}]}

    def update_event_source_mapping(self, UUID, Enabled):
        self.calls.append(("mapping", UUID, Enabled))

    def put_function_concurrency(self, FunctionName, ReservedConcurrentExecutions):
        self.calls.append(("concurrency", ReservedConcurrentExecutions))

    def delete_function_concurrency(self, FunctionName):
        self.calls.append(("concurrency", None))


@pytest.fixture
def glass(monkeypatch):
    import boto3
    import ops

    lam, modes = FakeLambda(), []
    monkeypatch.setattr(ops, "_own_function", lambda key: ("p-dev-invoker", "r"))
    monkeypatch.setattr(ops, "trigger_mode", lambda args: modes.append(args) or 0)
    monkeypatch.setattr(boto3, "client", lambda *a, **kw: lam)
    return ops, lam, modes


def test_break_glass_sets_manual_then_pauses_the_queue_then_zeroes_the_invoker(glass):
    ops, lam, modes = glass
    assert ops.main(["break-glass", "--yes"]) == 0
    assert modes == [["set", "manual"]]
    assert lam.calls == [("mapping", "u-1", False), ("concurrency", 0)]


def test_undo_resumes_the_invoker_and_leaves_the_trigger_mode_manual(glass):
    ops, lam, modes = glass
    assert ops.main(["break-glass", "--undo", "--yes"]) == 0
    assert modes == []
    assert lam.calls == [("concurrency", None), ("mapping", "u-1", True)]
