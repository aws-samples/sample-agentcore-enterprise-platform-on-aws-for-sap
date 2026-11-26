# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The agent's paths to a person: `send_notification` and the sample ticket tools.

Two Gateway targets, each a Lambda the shared Gateway role may invoke and nothing
more of ours:

- `business-operations-notification` (`send_notification`), over one channel per
  deployment (`notification.channel`): SES, or the sample ticketing table. Outbound
  only; this use case receives no email. Every message is screened by this use
  case's own guardrail first (`PII_ENTITIES`), in every Cedar mode.
- `business-operations-tickets` (`demo_create_ticket`, `demo_get_ticket`), only
  while `api.sample_ticketing` is on. Like the tickets API beside it, it is a
  SAMPLE: a stand-in for a real ITSM, to replace before production.

A dead channel is the one failure the agent cannot report (it is told not to
announce an outage over the outage), so the tool emits `NotificationFailure` and
an alarm on it publishes to the platform's ops topic (`alarm_topic: true` in the
manifest).
"""

from __future__ import annotations

from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda

from business_operations import alarms as uc_alarms
from business_operations import names, persistence
from business_operations.config import BusinessOperationsConfig
from business_operations.data import Data
from infra_utils.guardrail_policy import published_version
from infra_utils.use_case_context import UseCaseContext
from sap_kit.functions import gateway_lambda_target, uc_function

UC = Path(__file__).resolve().parents[1]
NAME = names.NAME
#: What a notification may never carry. Bank and tax numbers are routine AP
#: correspondence, so they are not here.
PII_ENTITIES = (
    "US_SOCIAL_SECURITY_NUMBER",
    "CREDIT_DEBIT_CARD_NUMBER",
    "AWS_ACCESS_KEY",
    "AWS_SECRET_KEY",
    "PASSWORD",
)

_PRIORITY = {
    "Type": "string",
    "Description": "low, medium (the default), high or critical. High and critical page someone.",
}
_CASE_ID = {
    "Type": "string",
    "Description": (
        "Case ID for correlation, exactly as given. Pass it here only, never in "
        "subject or body."
    ),
}
NOTIFICATION_TOOLS = [
    {
        "Name": "send_notification",
        "Description": (
            "Send a notification to a person through this deployment's channel (an "
            "email, or a ticket in the sample ticketing system). Use for "
            "confirmations, summaries and hand-offs. The recipient is an email address."
        ),
        "InputSchema": {
            "Type": "object",
            "Properties": {
                "recipient": {
                    "Type": "string",
                    "Description": "Email address to notify",
                },
                "subject": {
                    "Type": "string",
                    "Description": "Subject line. NEVER put the case ID in it; pass it in case_id.",
                },
                "body": {
                    "Type": "string",
                    "Description": (
                        "Plain-text body. NEVER put the case ID in it: the guardrail "
                        "scores it as an AWS key and refuses the message. Pass it in "
                        "case_id and the platform appends a reference line on delivery."
                    ),
                },
                "case_id": _CASE_ID,
                "priority": _PRIORITY,
            },
            "Required": ["recipient", "subject", "body"],
        },
    }
]

_FIELD = {
    "Type": "object",
    "Properties": {
        "name": {"Type": "string", "Description": "SAP field name"},
        "current": {
            "Type": "string",
            "Description": (
                "The value in SAP today. Only include it if you read this value in "
                "this run; omit it rather than guessing."
            ),
        },
        "proposed": {"Type": "string", "Description": "The value you intend to write"},
    },
    "Required": ["name", "proposed"],
}
TICKET_TOOLS = [
    {
        "Name": "demo_create_ticket",
        "Description": (
            "Create a ticket in the sample ticketing system (a stand-in for "
            "ServiceNow or similar). Use when a case needs a human's approval or "
            "answer, or work assigned to a team. Returns the ticket with its ID."
        ),
        "InputSchema": {
            "Type": "object",
            "Properties": {
                "title": {
                    "Type": "string",
                    "Description": "Short summary of the ticket",
                },
                "description": {
                    "Type": "string",
                    "Description": "Detailed description of the issue or request",
                },
                "priority": {
                    "Type": "string",
                    "Description": "high, medium (the default) or low",
                },
                "assigned_to": {
                    "Type": "string",
                    "Description": "Person or team to assign the ticket to",
                },
                "case_id": {
                    "Type": "string",
                    "Description": "The case this ticket is about, exactly as given",
                },
                "category": {
                    "Type": "string",
                    "Description": "e.g. approval_request, exception, inquiry",
                },
                "response_type": {
                    "Type": "string",
                    "Description": (
                        "'approval' (the default) for an approve or deny decision, "
                        "'free_text' when you need a written answer"
                    ),
                },
                "proposed_write": {
                    "Type": "object",
                    "Description": (
                        "The SAP write you are asking permission to make. Supply it "
                        "whenever the ticket asks for approval of a write; omit it when "
                        "you are only asking a question. The console renders it as a "
                        "before/after diff and checks your 'current' values against "
                        "what this run read."
                    ),
                    "Properties": {
                        "op": {
                            "Type": "string",
                            "Description": "update, create or function_import",
                        },
                        "service": {
                            "Type": "string",
                            "Description": "OData service name, e.g. API_SUPPLIERINVOICE_PROCESS_SRV",
                        },
                        "entity": {
                            "Type": "string",
                            "Description": "Entity set, or the function name for function_import",
                        },
                        "key": {
                            "Type": "string",
                            "Description": "Key values of the record, slash-joined, e.g. 5105600000/2026",
                        },
                        "fields": {
                            "Type": "array",
                            "Description": "One entry per field the write changes; a function_import lists its parameters.",
                            "Items": _FIELD,
                        },
                    },
                    "Required": ["op", "fields"],
                },
            },
            "Required": ["title", "description"],
        },
    },
    {
        "Name": "demo_get_ticket",
        "Description": "Get one ticket from the sample ticketing system by its ID.",
        "InputSchema": {
            "Type": "object",
            "Properties": {
                "ticket_id": {"Type": "string", "Description": "e.g. TKT-A1B2C3D4"}
            },
            "Required": ["ticket_id"],
        },
    },
]


def create(
    stack: cdk.Stack,
    ctx: UseCaseContext,
    config: BusinessOperationsConfig,
    data: Data,
    *,
    gateway_id: str,
    gateway_role: iam.IRole,
    layer: _lambda.ILayerVersion,
    persist: bedrock.CfnGuardrail | None = None,
) -> dict[str, agentcore.CfnGatewayTarget]:
    """The targets, by name, for policies.create."""
    cfg = config.notification
    channel = config.notification_channel
    prefix = ctx.prefix

    def function(id: str, source: str, env: dict[str, str]) -> _lambda.Function:
        return uc_function(
            stack,
            id,
            ctx,
            UC / source,
            handler="handler.handler",
            layers=[layer],  # case_key, the one case-identity codec
            timeout=30,
            environment=env,
        )

    def target(id: str, name: str, fn: _lambda.IFunction, tools: list[dict]):
        return gateway_lambda_target(
            stack,
            id,
            name=name,
            fn=fn,
            tools=tools,
            gateway_id=gateway_id,
            gateway_role=gateway_role,
        )

    guardrail = bedrock.CfnGuardrail(
        stack,
        "NotificationGuardrail",
        name=names.guardrail_name(ctx.project, ctx.environment, "mail"),
        description=f"{NAME}: what send_notification may never carry",
        blocked_input_messaging="Blocked by the notification guardrail.",
        blocked_outputs_messaging="Blocked by the notification guardrail.",
        sensitive_information_policy_config=bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty(
            pii_entities_config=[
                bedrock.CfnGuardrail.PiiEntityConfigProperty(type=e, action="BLOCK")
                for e in PII_ENTITIES
            ]
        ),
    )
    namespace = f"{prefix}/{NAME}"
    env = {
        "NOTIFICATION_CHANNEL": channel,
        "METRICS_NAMESPACE": namespace,
        "GUARDRAIL_ID": guardrail.attr_guardrail_id,
        "GUARDRAIL_VERSION": published_version(guardrail).attr_version,
    }
    if channel == "ses":
        env["NOTIFICATION_SENDER"] = cfg.ses_sender_email or ""
        env["NOTIFICATION_RECIPIENT_DOMAINS"] = ",".join(cfg.recipient_domains)
    else:
        env["TICKETS_TABLE"] = data.tickets.table_name
    notify = function("NotificationFunction", "notification", env)
    persistence.grant(persist, notify)
    notify.add_to_role_policy(
        iam.PolicyStatement(
            actions=["bedrock:ApplyGuardrail"],
            resources=[guardrail.attr_guardrail_arn],
        )
    )
    if channel == "ses" and cfg.ses_sender_email:
        # Sends as the one configured address; nothing to grant without one.
        notify.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ses:SendEmail"],
                resources=[f"arn:aws:ses:{stack.region}:{stack.account}:identity/*"],
                conditions={"StringEquals": {"ses:FromAddress": cfg.ses_sender_email}},
            )
        )
    elif channel == "tickets":
        data.tickets.grant(notify, "dynamodb:PutItem")
        data.grant_key(notify)
    if channel == "ses" and not cfg.ses_sender_email:
        cdk.Annotations.of(stack).add_warning_v2(
            f"{NAME}:no-ses-sender",
            "notification.ses_sender_email is unset: every send_notification fails "
            "and raises the notification-channel alarm until it is set and verified. "
            "Without a sender the channel defaults to tickets only while "
            "api.sample_ticketing is on and channel is not set to ses.",
        )

    alarm = cloudwatch.Alarm(
        stack,
        "NotificationChannelAlarm",
        alarm_description=(
            f"{prefix} {NAME}: the notification channel failed. The agent cannot reach "
            "a person on it; affected cases are parked for manual review."
        ),
        metric=cloudwatch.Metric(
            namespace=namespace,
            metric_name="NotificationFailure",
            statistic="Sum",
            period=cdk.Duration.minutes(5),
        ),
        threshold=0,
        comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        evaluation_periods=1,
        treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
    )
    topic = uc_alarms.topic(stack, ctx)
    alarm.add_alarm_action(cw_actions.SnsAction(topic))
    alarm.add_ok_action(cw_actions.SnsAction(topic))

    targets = {
        names.NOTIFICATION_TARGET: target(
            "NotificationTarget", names.NOTIFICATION_TARGET, notify, NOTIFICATION_TOOLS
        )
    }
    if config.api.sample_ticketing:
        tickets = function(
            "TicketToolFunction",
            "tickets_tool",
            {
                "TICKETS_TABLE": data.tickets.table_name,
                "CASES_TABLE": data.cases.table_name,
            },
        )
        persistence.grant(persist, tickets)
        data.tickets.grant(tickets, "dynamodb:PutItem", "dynamodb:GetItem")
        data.cases.grant(tickets, "dynamodb:UpdateItem")
        data.grant_key(tickets)
        targets[names.TICKETS_TARGET] = target(
            "TicketsTarget", names.TICKETS_TARGET, tickets, TICKET_TOOLS
        )
    return targets
