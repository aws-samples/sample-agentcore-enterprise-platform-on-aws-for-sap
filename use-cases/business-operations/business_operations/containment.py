# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Automatic containment: an anomaly alarm going to ALARM sets the trigger mode to `manual`.

The pattern is the CloudWatch-alarm-to-EventBridge-to-Lambda remediation AWS documents
(re:Post "How do I use EventBridge to create a custom response for a CloudWatch
alarm?", and aws-samples/aws-cdk-examples `python/ec2-alarms-to-opsitem`): a rule on
the alarm's `CloudWatch Alarm State Change` event, matched by alarm ARN and the
ALARM state, invokes a function. Here the function writes the one switch that stops
unattended runs from starting (`containment/index.py`) and tells the ops topic why.

The triggers are the alarms that say a run is misbehaving rather than that something
it depends on is down: the run checks refusing this use case's calls, the cost per run,
runs dead-lettered, and the platform's guardrail content-filter alarm. The last is
account-wide, so a prompt attack on any tenant's traffic contains this one too; that
errs toward manual. It exists only while the platform's `observability.alarms` is on.

Its role may read and write the trigger-mode parameter and write its attribution
record beside it, and publish to the ops topic. Nothing else.
"""

from __future__ import annotations

from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda

from business_operations import alarms as uc_alarms
from business_operations.audit_alerts import TRIGGER_MODE_PATH
from business_operations.names import NAME
from infra_utils.use_case_context import UseCaseContext
from sap_kit.functions import uc_function

SRC = Path(__file__).resolve().parents[1] / "containment"
#: This stack's alarms (alarms.py) that contain.
TRIGGERS = ("RunCheckRefusalAlarm", "AgentCostAlarm", "DlqAlarm")
#: The platform's alarms that contain, named `<prefix>-<name>` (stacks/observability_stack.py).
PLATFORM_TRIGGERS = ("guardrail-content-interventions",)
ACTOR_SUFFIX = "-last-changed-by"


def function(stack: cdk.Stack, ctx: UseCaseContext) -> _lambda.Function:
    param = f"{ctx.ssm_prefix}/use-cases/{NAME}/{TRIGGER_MODE_PATH}"
    fn = uc_function(
        stack,
        "Containment",
        ctx,
        SRC,
        timeout=10,
        environment={
            "TOPIC_ARN": uc_alarms.topic(stack, ctx).topic_arn,
            "TRIGGER_MODE_PARAMETER": param,
        },
    )
    uc_alarms.grant_publish(stack, ctx, fn)

    def arn(name: str) -> str:
        return stack.format_arn(
            service="ssm", resource="parameter", resource_name=name.lstrip("/")
        )

    fn.add_to_role_policy(
        iam.PolicyStatement(
            actions=["ssm:GetParameter", "ssm:PutParameter"], resources=[arn(param)]
        )
    )
    fn.add_to_role_policy(
        iam.PolicyStatement(
            actions=["ssm:PutParameter"], resources=[arn(param + ACTOR_SUFFIX)]
        )
    )
    return fn


def rule(
    stack: cdk.Stack,
    ctx: UseCaseContext,
    fn: _lambda.Function,
    alarms: list[cw.Alarm],
) -> events.Rule:
    watched = [a.alarm_arn for a in alarms if a.node.id in TRIGGERS]
    watched += [
        stack.format_arn(
            service="cloudwatch",
            resource="alarm",
            resource_name=f"{ctx.prefix}-{name}",
            arn_format=cdk.ArnFormat.COLON_RESOURCE_NAME,
        )
        for name in PLATFORM_TRIGGERS
    ]
    return events.Rule(
        stack,
        "ContainmentRule",
        description=f"{NAME}: an anomaly alarm went to ALARM, so contain the agent",
        event_pattern=events.EventPattern(
            source=["aws.cloudwatch"],
            detail_type=["CloudWatch Alarm State Change"],
            resources=watched,
            detail={"state": {"value": ["ALARM"]}},
        ),
        targets=[targets.LambdaFunction(fn)],
    )
