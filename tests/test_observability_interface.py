# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""What a use case can find out about the platform's observability and key.

The ops topic (its own stack) is created when the platform alarms or a use case
asks for it, and published to SSM; under production it is encrypted with the platform CMK
whose policy lets CloudWatch publish (alias/aws/sns cannot). Span retention is
set only when configured, because those log groups are account-wide.
"""

from __future__ import annotations

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from pydantic import ValidationError

from infra_utils import platform_config as pc
from stacks.observability_stack import ObservabilityStack, OpsTopicStack
from stacks.security_stack import SecurityStack

ENV = cdk.Environment(account="111111111111", region="us-east-1")


def _obs(app=None, **kw) -> dict:
    app = app or cdk.App()
    stack = ObservabilityStack(
        app, "Obs", project_name="proj", environment="dev", env=ENV, **kw
    )
    return Template.from_stack(stack).to_json()


def _of(template: dict, kind: str) -> list[dict]:
    return [r for r in template["Resources"].values() if r["Type"] == kind]


def _params(template: dict) -> set[str]:
    return {r["Properties"]["Name"] for r in _of(template, "AWS::SSM::Parameter")}


def _topic(app=None, **kw) -> dict:
    app = app or cdk.App()
    stack = OpsTopicStack(
        app, "Ops", project_name="proj", environment="dev", env=ENV, **kw
    )
    return Template.from_stack(stack).to_json()


def test_the_observability_stack_has_no_topic_of_its_own():
    for t in (_obs(), _obs(enable_alarms=True)):
        assert not _of(t, "AWS::SNS::Topic")
        assert not any("alarm-topic" in p or "ops-topic" in p for p in _params(t))


def test_alarms_send_to_the_ops_topic_read_from_ssm():
    t = _obs(enable_alarms=True)
    assert "/proj/dev/ops-topic/arn" in {
        p.get("Default") for p in t["Parameters"].values()
    }


def test_the_ops_topic_stack_publishes_an_unencrypted_topic_by_default():
    t = _topic()
    assert len(_of(t, "AWS::SNS::Topic")) == 1
    assert "/proj/dev/ops-topic/arn" in _params(t)
    assert "KmsMasterKeyId" not in _of(t, "AWS::SNS::Topic")[0]["Properties"]
    assert "TopicName" not in _of(t, "AWS::SNS::Topic")[0]["Properties"]


def test_production_topic_uses_the_platform_key_and_lets_cloudwatch_in():
    app = cdk.App()
    sec = SecurityStack(app, "Sec", project_name="proj", environment="dev", env=ENV)
    t = _topic(app, key=sec.kms_key)
    assert "KmsMasterKeyId" in _of(t, "AWS::SNS::Topic")[0]["Properties"]
    key = _of(Template.from_stack(sec).to_json(), "AWS::KMS::Key")[0]
    statements = key["Properties"]["KeyPolicy"]["Statement"]
    assert any(
        s.get("Principal") == {"Service": "cloudwatch.amazonaws.com"}
        and set(s["Action"]) == {"kms:Decrypt", "kms:GenerateDataKey*"}
        and s["Condition"]["StringEquals"]["aws:SourceAccount"] == ENV.account
        for s in statements
    )
    assert "/proj/dev/security/kms-key-arn" in _params(
        Template.from_stack(sec).to_json()
    )


def test_production_security_alerts_use_the_platform_key_and_let_eventbridge_in():
    app = cdk.App()
    sec = SecurityStack(app, "Sec", project_name="proj", environment="dev", env=ENV)
    t = _obs(app, enable_traceability=True, alerts_key=sec.kms_key)
    (alerts,) = _of(t, "AWS::SNS::Topic")
    assert "KmsMasterKeyId" in alerts["Properties"]
    key = _of(Template.from_stack(sec).to_json(), "AWS::KMS::Key")[0]
    # Only for this account's rules, on the key and on the topic alike.
    assert [
        s["Condition"]
        for s in key["Properties"]["KeyPolicy"]["Statement"]
        if s.get("Principal") == {"Service": "events.amazonaws.com"}
    ] == [{"StringEquals": {"aws:SourceAccount": ENV.account}}]
    (policy,) = _of(t, "AWS::SNS::TopicPolicy")
    (publish,) = policy["Properties"]["PolicyDocument"]["Statement"]
    assert publish["Condition"] == {"StringEquals": {"aws:SourceAccount": ENV.account}}
    # Outside production the topic stays unencrypted: EventBridge cannot use alias/aws/sns.
    (plain,) = _of(_obs(enable_traceability=True), "AWS::SNS::Topic")
    assert "KmsMasterKeyId" not in plain["Properties"]


def _retention_calls(template: dict) -> list[str]:
    return [
        str(r["Properties"].get("Create"))
        for r in _of(template, "Custom::AWS")
        if "putRetentionPolicy" in str(r["Properties"].get("Create"))
    ]


def test_span_retention_untouched_unless_set():
    assert _retention_calls(_obs()) == []
    calls = _retention_calls(_obs(span_retention_days=90))
    assert len(calls) == 2
    assert any("aws/spans" in c for c in calls)
    assert any("/aws/application-signals/data" in c for c in calls)
    assert all('"retentionInDays":90' in c for c in calls)


def test_span_retention_config():
    assert pc.ObservabilityConfig().span_retention_days is None
    cfg = pc.PlatformConfig.model_validate(
        {"observability": {"span_retention_days": 731}}
    )
    assert pc.to_env(cfg)["SPAN_RETENTION_DAYS"] == "731"
    assert "SPAN_RETENTION_DAYS" not in pc.to_env(pc.PlatformConfig())
    with pytest.raises(ValidationError):
        pc.ObservabilityConfig(span_retention_days=45)
    with pytest.raises(ValidationError):
        pc.ObservabilityConfig(span_retention_days=30, transaction_search=False)


def test_manifest_alarm_topic_creates_the_topic(monkeypatch):
    tenant = pc.UseCaseManifest.model_validate(
        {
            "name": "tenant-a",
            "owner": "x",
            "summary": "s",
            "platform_interface": pc.PLATFORM_INTERFACE_VERSION,
            "stacks": ["uc-tenant-a"],
            "alarm_topic": True,
        }
    )
    real = pc.discover_use_cases()
    monkeypatch.setattr(pc, "discover_use_cases", lambda: {**real, "tenant-a": tenant})
    assert not pc.PlatformConfig().alarm_topic_needed()
    assert pc.PlatformConfig.model_validate(
        {"use_cases": {"tenant-a": {}}}
    ).alarm_topic_needed()
    assert (
        "agentcore-workshop-dev-ops-topic"
        in pc.PlatformConfig.model_validate(
            {"use_cases": {"tenant-a": {}}}
        ).expected_stacks()
    )
    assert not any("ops-topic" in s for s in pc.PlatformConfig().expected_stacks())


def test_production_vpc_logs_its_flows_and_development_does_not():
    from stacks.networking_stack import NetworkingStack

    def flow_logs(retain: bool) -> list[dict]:
        stack = NetworkingStack(
            cdk.App(),
            "Net",
            project_name="proj",
            environment="dev",
            env=ENV,
            retain_data=retain,
            log_retention_days=90,
        )
        t = Template.from_stack(stack).to_json()
        groups = {
            k for k, r in t["Resources"].items() if r["Type"] == "AWS::Logs::LogGroup"
        }
        logs = _of(t, "AWS::EC2::FlowLog")
        for f in logs:
            assert f["Properties"]["TrafficType"] == "ALL"
            assert f["Properties"]["LogGroupName"]["Ref"] in groups
        return logs

    assert len(flow_logs(True)) == 1
    assert flow_logs(False) == []


def test_a_kms_key_disable_or_deletion_alerts_the_security_topic():
    t = _obs(enable_traceability=True)
    (rule,) = [
        r
        for r in _of(t, "AWS::Events::Rule")
        if r["Properties"]["EventPattern"].get("source") == ["aws.kms"]
    ]
    assert rule["Properties"]["EventPattern"]["detail"]["eventName"] == [
        "DisableKey",
        "ScheduleKeyDeletion",
    ]
    (topic,) = [k for k, r in t["Resources"].items() if r["Type"] == "AWS::SNS::Topic"]
    assert rule["Properties"]["Targets"][0]["Arn"] == {"Ref": topic}


def test_a_guardrail_content_intervention_alarms():
    (a,) = [
        r["Properties"]
        for r in _of(_obs(enable_alarms=True), "AWS::CloudWatch::Alarm")
        if r["Properties"]["MetricName"] == "InvocationsIntervened"
    ]
    # The dimension set read off a live account: Operation alone, or with a policy type.
    assert a["Namespace"] == "AWS/Bedrock/Guardrails"
    assert {d["Name"]: d["Value"] for d in a["Dimensions"]} == {
        "Operation": "ApplyGuardrail",
        "GuardrailPolicyType": "ContentPolicy",
    }
