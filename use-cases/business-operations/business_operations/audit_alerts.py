# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The audit-change alerts: a trigger-mode change (the switch or a pack's mode), a `registry.json` write, a SOP write by
anyone but the pack composer, and a change to either bucket's policy.
Each goes to the platform's ops topic, naming the call, the resource and the principal
(`audit_alert/index.py`).

The SOP bucket's policy denies writes to everyone but its writer, so a SOP written
by anyone else must follow a policy change; the policy change is the alert that comes
first, and it is a management event the trail does log.

The flip is an SSM write, so an EventBridge rule matches its CloudTrail event, the way
the SAP layer watches its own parameters; it fires only while a trail logs management
events in this Region (the platform's security stack, or an organization trail). The
registry and the SOPs are S3 objects, whose writes are data events no trail here
logs, so the buckets notify the same function directly.
"""

from __future__ import annotations

from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_notifications as s3n

from business_operations import alarms
from business_operations.data import Data
from business_operations.names import NAME, trigger_mode_parameters
from infra_utils.use_case_context import UseCaseContext
from sap_kit.functions import uc_function

SRC = Path(__file__).resolve().parents[1] / "audit_alert"
#: Under `<ssm_prefix>/use-cases/<name>/`; `production-check` looks for a rule naming it.
TRIGGER_MODE_PATH = "autonomy/trigger-mode"
SSM_WRITES = ["PutParameter", "DeleteParameter", "DeleteParameters"]


def trigger_mode_pattern(
    param: str, packs: str | None = None, stop: str | None = None
) -> dict:
    """Any write of the trigger-mode parameter itself, of any pack's mode under
    `packs` (`<…>/autonomy/packs`), or of the emergency stop `stop`, by anyone."""
    names = [
        param,
        *([stop] if stop else []),
        *([{"prefix": f"{packs}/"}] if packs else []),
    ]
    return {
        "source": ["aws.ssm"],
        "detail_type": ["AWS API Call via CloudTrail"],
        "detail": {
            "eventSource": ["ssm.amazonaws.com"],
            "eventName": SSM_WRITES,
            "$or": [
                {"requestParameters": {"name": names}},
                {"requestParameters": {"names": names}},
            ],
        },
    }


BUCKET_POLICY_WRITES = ["PutBucketPolicy", "DeleteBucketPolicy"]


def bucket_policy_pattern(buckets: list[str]) -> dict:
    """Any write of these buckets' policies, by anyone, a deploy's included."""
    return {
        "source": ["aws.s3"],
        "detail_type": ["AWS API Call via CloudTrail"],
        "detail": {
            "eventSource": ["s3.amazonaws.com"],
            "eventName": BUCKET_POLICY_WRITES,
            "requestParameters": {"bucketName": buckets},
        },
    }


def create(
    stack: cdk.Stack,
    ctx: UseCaseContext,
    data: Data,
    sop_writers: list[_lambda.Function],
) -> _lambda.Function:
    topic = alarms.topic(stack, ctx)
    fn = uc_function(
        stack,
        "AuditAlert",
        ctx,
        SRC,
        timeout=10,
        environment={
            "TOPIC_ARN": topic.topic_arn,
            "SOPS_BUCKET": data.sops.bucket_name,
            "SOP_WRITER_ROLE_IDS": cdk.Fn.join(
                ",", [w.role.node.default_child.attr_role_id for w in sop_writers]
            ),
        },
    )
    alarms.grant_publish(stack, ctx, fn)

    param = f"{ctx.ssm_prefix}/use-cases/{NAME}/{TRIGGER_MODE_PATH}"
    events.Rule(
        stack,
        "TriggerModeChangeRule",
        description=(
            f"{NAME}: the deployment's trigger mode, a pack's mode or the emergency stop was written"
        ),
        event_pattern=events.EventPattern(
            **trigger_mode_pattern(
                param,
                trigger_mode_parameters(ctx.ssm_prefix)["packs"],
                trigger_mode_parameters(ctx.ssm_prefix)["stop"],
            )
        ),
        targets=[targets.LambdaFunction(fn)],
    )
    events.Rule(
        stack,
        "PackBucketPolicyRule",
        description=f"{NAME}: the SOP or registry bucket's policy was written",
        event_pattern=events.EventPattern(
            **bucket_policy_pattern([data.sops.bucket_name, data.registry.bucket_name])
        ),
        targets=[targets.LambdaFunction(fn)],
    )
    dest = s3n.LambdaDestination(fn)
    registry_key = s3.NotificationKeyFilter(prefix="registry.json")
    for kind in (s3.EventType.OBJECT_CREATED, s3.EventType.OBJECT_REMOVED):
        data.registry.add_event_notification(kind, dest, registry_key)
        data.sops.add_event_notification(kind, dest)
    return fn
