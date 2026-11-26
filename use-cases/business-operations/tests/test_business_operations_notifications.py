# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""business-operations: send_notification, the sample ticket tools, and their stack.

The SES adapter's allowlist and the guardrail screen are the notification's own
exfiltration controls, in every Cedar mode, and a dead channel is the one failure the
agent cannot report, so both halves of the failure split (request: named back, no
alarm; channel: metric) are pinned here, along with what the stack wires around them.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import aws_cdk as cdk
import boto3
import pytest
from aws_cdk.assertions import Template

from infra_utils.platform_config import load_platform_config
from infra_utils.use_case_context import UseCaseContext

REPO = Path(__file__).resolve().parents[3]
UC = REPO / "use-cases" / "business-operations"
sys.path.insert(0, str(UC / "api" / "layer" / "python"))
sys.path.insert(0, str(UC))

CASE = "ap-" + "a" * 26
TOOL = "business-operations-notification___send_notification"


def _ctx(tool=TOOL):
    ctx = MagicMock()
    ctx.client_context.custom = {"bedrockAgentCoreToolName": tool}
    return ctx


def _load(source: str, monkeypatch, env: dict):
    env = {"GUARDRAIL_ID": "g-1", **env}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(boto3, "resource", MagicMock())
    monkeypatch.setattr(boto3, "client", MagicMock())
    spec = importlib.util.spec_from_file_location(
        f"bo_{source}", UC / source / "handler.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if hasattr(mod, "screen"):
        guardrail = MagicMock()
        guardrail.apply_guardrail.return_value = {"action": "NONE"}
        mod._clients["bedrock-runtime"] = guardrail
    return mod


@pytest.fixture
def ses(monkeypatch):
    monkeypatch.delenv("NOTIFICATION_RECIPIENT_DOMAINS", raising=False)
    mod = _load(
        "notification",
        monkeypatch,
        {"NOTIFICATION_CHANNEL": "ses", "NOTIFICATION_SENDER": "agent@example.com"},
    )
    client = MagicMock()
    client.send_email.return_value = {"MessageId": "m-1"}
    mod._clients["ses"] = client
    return mod


def _emf(capsys) -> dict | None:
    for line in capsys.readouterr().out.splitlines():
        if line.startswith("{") and '"_aws"' in line:
            return json.loads(line)
    return None


def _send(mod, **args):
    return mod.handler({"subject": "s", "body": "b", **args}, _ctx())


def test_a_missing_sender_is_a_channel_failure_that_alarms(ses, monkeypatch, capsys):
    monkeypatch.setenv("NOTIFICATION_SENDER", " ")
    out = _send(ses, recipient="buyer@example.com")
    assert "notification.ses_sender_email" in out["error"]
    assert "unavailable" in out["error"]  # the prompt's cue to park, not retry
    payload = _emf(capsys)
    assert payload["NotificationFailure"] == 1
    assert payload["FailureKind"] == "NotConfigured"
    # The alarm queries the dimensionless set.
    assert [] in payload["_aws"]["CloudWatchMetrics"][0]["Dimensions"]
    ses._clients["ses"].send_email.assert_not_called()


@pytest.mark.parametrize(
    "recipient", ["attacker@evil.example", "attacker@notexample.com", "not-an-email"]
)
def test_an_off_allowlist_recipient_is_refused_without_an_alarm(ses, recipient, capsys):
    out = _send(ses, recipient=recipient)
    assert out["error"].startswith("Notification failed:")
    assert _emf(capsys) is None
    ses._clients["ses"].send_email.assert_not_called()


def test_the_sender_domain_and_its_subdomains_are_the_default(ses):
    for recipient in ("buyer@example.com", "ap@eu.example.com"):
        assert "content" in _send(ses, recipient=recipient)
    sent = ses._clients["ses"].send_email.call_args.kwargs
    assert sent["Source"] == "SAP Agent <agent@example.com>"


def test_configured_domains_replace_the_default(ses, monkeypatch):
    monkeypatch.setenv("NOTIFICATION_RECIPIENT_DOMAINS", " Supplier.test , @other.test")
    assert "content" in _send(ses, recipient="x@supplier.test")
    assert "error" in _send(ses, recipient="buyer@example.com")


def test_a_channel_exception_alarms_and_stays_opaque(ses, capsys):
    ses._clients["ses"].send_email.side_effect = RuntimeError("arn:aws:secret")
    out = _send(ses, recipient="buyer@example.com")
    assert out["error"] == "Notification channel unavailable: RuntimeError"
    assert _emf(capsys)["FailureKind"] == "RuntimeError"


def test_the_case_reference_is_appended_once_and_only_for_a_real_case_id(ses):
    assert ses.with_case_reference("Paid.", CASE) == f"Paid.\n\n---\nCase ID: {CASE}\n"
    once = ses.with_case_reference("Paid.", CASE)
    assert ses.with_case_reference(once, CASE) == once
    for hostile in (
        "x\n\nAKIAIOSFODNN7EXAMPLE",
        "ap-abc\nCase ID: y",
        "ap-" + "a" * 500,
    ):
        assert ses.with_case_reference("Paid.", hostile) == "Paid."
    _send(ses, recipient="buyer@example.com", case_id=CASE)
    body = ses._clients["ses"].send_email.call_args.kwargs["Message"]["Body"]
    assert body["Text"]["Data"].count(CASE) == 1


def test_a_message_carrying_pii_never_leaves(ses, capsys):
    guardrail = ses._clients["bedrock-runtime"]
    guardrail.apply_guardrail.return_value = {
        "action": "GUARDRAIL_INTERVENED",
        "assessments": [
            {
                "sensitiveInformationPolicy": {
                    "piiEntities": [
                        {"type": "US_SOCIAL_SECURITY_NUMBER", "action": "BLOCKED"}
                    ]
                }
            }
        ],
    }
    out = _send(
        ses, recipient="buyer@example.com", body="SSN 123-45-6789", case_id=CASE
    )
    assert "US_SOCIAL_SECURITY_NUMBER" in out["error"]
    assert out["error"].startswith("Notification failed:")  # a request error
    assert not ses._clients["ses"].send_email.called and _emf(capsys) is None
    # Scored as the agent wrote it, before the case reference the guardrail would
    # read as an AWS key is appended; and as content leaving, not a prompt.
    call = guardrail.apply_guardrail.call_args.kwargs
    assert call["source"] == "OUTPUT" and call["guardrailIdentifier"] == "g-1"
    texts = [c["text"]["text"] for c in call["content"]]
    assert texts == ["buyer@example.com", "s", "SSN 123-45-6789"]


def test_a_guardrail_that_cannot_answer_sends_nothing(ses, monkeypatch, capsys):
    ses._clients["bedrock-runtime"].apply_guardrail.side_effect = RuntimeError("x")
    out = _send(ses, recipient="buyer@example.com")
    assert out["error"] == "Notification channel unavailable: RuntimeError"
    assert not ses._clients["ses"].send_email.called
    assert _emf(capsys)["FailureKind"] == "RuntimeError"
    monkeypatch.setattr(ses, "GUARDRAIL_ID", "")
    assert (
        "no notification guardrail"
        in _send(ses, recipient="buyer@example.com")["error"]
    )


def test_a_persistence_guardrail_that_cannot_answer_sends_nothing(ses, monkeypatch):
    import persist_mask

    monkeypatch.setenv("PERSIST_GUARDRAIL_ID", "gr-persist")
    monkeypatch.setattr(
        persist_mask,
        "_client",
        MagicMock(apply_guardrail=MagicMock(side_effect=RuntimeError("x"))),
    )
    out = _send(ses, recipient="buyer@example.com")
    assert "persistence guardrail" in out["error"]
    assert not ses._clients["ses"].send_email.called


def test_an_unknown_priority_is_a_request_error(ses, capsys):
    out = _send(ses, recipient="buyer@example.com", priority="urgent")
    assert "priority" in out["error"] and _emf(capsys) is None


def test_a_direct_invoke_is_turned_away(ses):
    out = ses.handler({"recipient": "buyer@example.com"}, object())
    assert "Gateway" in out["error"]


def test_the_tickets_channel_files_an_fyi_not_an_approval(monkeypatch):
    mod = _load(
        "notification",
        monkeypatch,
        {"NOTIFICATION_CHANNEL": "tickets", "TICKETS_TABLE": "t"},
    )
    table = MagicMock()
    mod._clients["dynamodb"] = MagicMock(Table=lambda name: table)
    out = mod.handler(
        {"recipient": "ap", "subject": "s", "body": "b", "case_id": CASE}, _ctx()
    )
    assert json.loads(out["content"][0]["text"])["channel"] == "tickets"
    item = table.put_item.call_args.kwargs["Item"]
    assert item["response_type"] == "free_text"
    assert item["case_id"] == CASE


# ── the ticket tools ──


@pytest.fixture
def tickets(monkeypatch):
    mod = _load("tickets_tool", monkeypatch, {"TICKETS_TABLE": "t", "CASES_TABLE": "c"})
    tables = {"t": MagicMock(), "c": MagicMock()}
    mod._dynamodb = MagicMock(Table=lambda name: tables[name])
    return mod, tables


def _tool(name):
    return _ctx(f"business-operations-tickets___{name}")


def test_create_ticket_links_the_case_and_drops_a_bad_case_id(tickets):
    mod, tables = tickets
    mod.handler({"title": "Approve", "case_id": CASE}, _tool("demo_create_ticket"))
    item = tables["t"].put_item.call_args.kwargs["Item"]
    assert item["created_by"] == "agent" and item["response_type"] == "approval"
    assert tables["c"].update_item.call_args.kwargs["Key"] == {"case_id": CASE}

    tables["c"].reset_mock()
    mod.handler({"title": "x", "case_id": "not a case"}, _tool("demo_create_ticket"))
    assert tables["t"].put_item.call_args.kwargs["Item"]["case_id"] == ""
    tables["c"].update_item.assert_not_called()


@pytest.mark.parametrize("response_type", ["approval", "free_text"])
def test_create_ticket_stamps_its_kind_on_the_case(tickets, response_type):
    # The case list tells "Needs approval" from "Needs a reply" by this, without
    # reading each case's ticket.
    mod, tables = tickets
    mod.handler(
        {"title": "Ask", "case_id": CASE, "response_type": response_type},
        _tool("demo_create_ticket"),
    )
    call = tables["c"].update_item.call_args.kwargs
    assert "ticket_response_type = :rt" in call["UpdateExpression"]
    assert call["ExpressionAttributeValues"][":rt"] == response_type


def test_create_ticket_refuses_a_bad_response_type_and_no_title(tickets):
    mod, tables = tickets
    assert "error" in mod.handler(
        {"title": "x", "response_type": "yes"}, _tool("demo_create_ticket")
    )
    assert "error" in mod.handler({"title": " "}, _tool("demo_create_ticket"))
    tables["t"].put_item.assert_not_called()


def test_the_run_checks_hold_a_ticket_to_the_runs_packs():
    from business_operations_check import run_checks

    assert run_checks.gated("business-operations-tickets___demo_create_ticket")
    assert run_checks.gated("business-operations-tickets___demo_get_ticket")


# ── config and stack ──


def test_the_tickets_channel_needs_sample_ticketing():
    from business_operations import config as uc_config

    with pytest.raises(ValueError, match="sample_ticketing"):
        uc_config.load(
            {"notification": {"channel": "tickets"}, "api": {"sample_ticketing": False}}
        )
    with pytest.raises(ValueError, match="not an email"):
        uc_config.load({"notification": {"ses_sender_email": "nope"}})


@pytest.mark.parametrize(
    "notification, ticketing, channel",
    [
        ({}, True, "tickets"),  # no sender: SES setup is manual, tickets work today
        ({"ses_sender_email": "a@corp.example"}, True, "ses"),
        ({"channel": "ses"}, True, "ses"),  # chosen explicitly: stays a failure
        ({}, False, "ses"),  # nowhere else to send: channel failure + synth warning
    ],
)
def test_the_channel_defaults_to_tickets_without_a_sender(
    notification, ticketing, channel
):
    from business_operations import config as uc_config

    cfg = uc_config.load(
        {"notification": notification, "api": {"sample_ticketing": ticketing}}
    )
    assert cfg.notification_channel == channel


def _stack(settings: dict | None = None) -> dict:
    spec = importlib.util.spec_from_file_location("bo_stack_n", UC / "stack.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    from business_operations import config as uc_config

    stack = mod.BusinessOperationsStack(
        cdk.App(),
        "p-dev-uc-business-operations",
        ctx=UseCaseContext(
            project="p",
            environment="dev",
            platform=load_platform_config(
                REPO
                / "use-cases/business-operations/fixtures/contract/business-operations.yaml"
            ),
        ),
        config=uc_config.load(settings),
        env=cdk.Environment(account="111111111111", region="us-east-1"),
    )
    return Template.from_stack(stack).to_json()


def _of(t: dict, kind: str) -> dict:
    return {k: r for k, r in t["Resources"].items() if r["Type"] == kind}


def _target_names(t: dict) -> set[str]:
    return {
        r["Properties"]["Name"]
        for r in _of(t, "AWS::BedrockAgentCore::GatewayTarget").values()
    }


def _policies(t: dict) -> dict:
    return _of(t, "AWS::BedrockAgentCore::Policy")


def test_default_stack_attaches_both_targets_and_alarms_to_the_ops_topic():
    t = _stack()
    assert {"business-operations-notification", "business-operations-tickets"} <= (
        _target_names(t)
    )
    created = _policies(t)
    for p in ("Policynotifications", "Policyticketscreate"):
        assert p in created
    # Cedar copies neither the recipient allowlist nor the PII check.
    assert {"Policynotificationegress", "Policypiiguardrail"}.isdisjoint(created)
    envs = [
        f["Properties"].get("Environment", {}).get("Variables", {})
        for f in _of(t, "AWS::Lambda::Function").values()
    ]
    assert {"NOTIFICATION_CHANNEL": "tickets"}.items() <= next(
        e for e in envs if "NOTIFICATION_CHANNEL" in e
    ).items()
    alarm = next(iter(_of(t, "AWS::CloudWatch::Alarm").values()))["Properties"]
    assert alarm["Threshold"] == 0
    topic = alarm["AlarmActions"][0]["Ref"]
    assert t["Parameters"][topic]["Default"] == "/p/dev/ops-topic/arn"
    assert not [
        s
        for p in _of(t, "AWS::IAM::Policy").values()
        for s in p["Properties"]["PolicyDocument"]["Statement"]
        if "ses:SendEmail" in json.dumps(s)
    ]


def test_the_notification_function_screens_with_its_own_guardrail():
    """Provisioned with the target, PII set to BLOCK, and callable only by the
    notification function's own role: nothing is granted to the shared Gateway role."""
    t = _stack()
    (guardrail,) = _of(t, "AWS::Bedrock::Guardrail").values()
    pii = guardrail["Properties"]["SensitiveInformationPolicyConfig"][
        "PiiEntitiesConfig"
    ]
    assert {e["Type"] for e in pii} == {
        "US_SOCIAL_SECURITY_NUMBER",
        "CREDIT_DEBIT_CARD_NUMBER",
        "AWS_ACCESS_KEY",
        "AWS_SECRET_KEY",
        "PASSWORD",
    }
    assert {e["Action"] for e in pii} == {"BLOCK"}
    (logical_id,) = _of(t, "AWS::Bedrock::Guardrail")
    env = next(
        f["Properties"]["Environment"]["Variables"]
        for f in _of(t, "AWS::Lambda::Function").values()
        if "NOTIFICATION_CHANNEL"
        in f["Properties"].get("Environment", {}).get("Variables", {})
    )
    assert env["GUARDRAIL_ID"] == {"Fn::GetAtt": [logical_id, "GuardrailId"]}
    grants = [
        (p["Properties"]["Roles"], s)
        for p in _of(t, "AWS::IAM::Policy").values()
        for s in p["Properties"]["PolicyDocument"]["Statement"]
        if "bedrock:ApplyGuardrail" in json.dumps(s.get("Action"))
        or "InvokeGuardrailChecks" in json.dumps(s.get("Action"))
    ]
    assert len(grants) == 1
    roles, statement = grants[0]
    assert statement["Resource"] == {"Fn::GetAtt": [logical_id, "GuardrailArn"]}
    assert "NotificationFunction" in json.dumps(roles)


def test_a_sender_pins_ses():
    t = _stack({"notification": {"ses_sender_email": "agent@corp.example"}})
    ses = [
        s
        for p in _of(t, "AWS::IAM::Policy").values()
        for s in p["Properties"]["PolicyDocument"]["Statement"]
        if s.get("Action") == "ses:SendEmail"
    ]
    assert ses[0]["Condition"] == {
        "StringEquals": {"ses:FromAddress": "agent@corp.example"}
    }


def test_no_sample_ticketing_means_no_ticket_target_or_statements():
    t = _stack({"api": {"sample_ticketing": False}})
    assert "business-operations-tickets" not in _target_names(t)
    assert "Policyticketscreate" not in _policies(t)
