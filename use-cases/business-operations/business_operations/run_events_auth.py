# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The run-event channel's subscribe check: `run_events_auth/` on every subscribe.

The channel's Cognito mode proves only that a pool user signed in through the console
client (under the platform's direct mode, its OIDC mode: that an Entra token was
addressed to the platform app, which every app in the tenant can obtain). On a shared pool that is any user any tenant creates, or anyone who
self-registers, so the namespace's `onSubscribe` handler is this function, which holds
the subscriber to the API's own floor (the users group) and to the pack the channel
names. See `run_events_auth/index.py` for the channel grammar.
"""

from __future__ import annotations

from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_appsync as appsync
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda

from business_operations import ASSET_EXCLUDE, identity
from business_operations.api import ADMINS_GROUP, USERS_GROUP
from infra_utils.use_case_context import UseCaseContext
from sap_kit.functions import uc_function

SRC = Path(__file__).resolve().parents[1]


def handler_config(
    stack: cdk.Stack,
    ctx: UseCaseContext,
    events: appsync.EventApi,
    *,
    console_client_id: str,
    registry_bucket: str,
    kms_key_arn: str | None,
) -> appsync.HandlerConfig:
    """The subscribe authorizer, as the `runs` namespace's direct Lambda handler."""
    layer = _lambda.LayerVersion(
        stack,
        "RunEventsAuthLayer",
        # The API's own entitlement and registry modules, not a copy of either.
        code=_lambda.Code.from_asset(
            str(SRC / "api" / "layer"),
            follow_symlinks=cdk.SymlinkFollowMode.ALWAYS,
            exclude=ASSET_EXCLUDE,
        ),
        compatible_runtimes=[_lambda.Runtime.PYTHON_3_13],
    )
    fn = uc_function(
        stack,
        "RunEventsAuth",
        ctx,
        SRC / "run_events_auth",
        layers=[layer],
        timeout=10,
        environment={
            "API_ALLOWED_CLIENTS": console_client_id,
            "USERS_GROUP": USERS_GROUP,
            "ADMINS_GROUP": ADMINS_GROUP,
            "REGISTRY_BUCKET": registry_bucket,
            **(
                {"GROUP_CLAIMS": identity.GROUP_CLAIMS_DIRECT}
                if identity.direct(ctx)
                else {}
            ),
        },
    )
    bucket = f"arn:{stack.partition}:s3:::{registry_bucket}"
    fn.add_to_role_policy(
        iam.PolicyStatement(actions=["s3:GetObject"], resources=[f"{bucket}/*"])
    )
    if kms_key_arn:
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["kms:Decrypt"],
                resources=[kms_key_arn],
                conditions={
                    "StringEquals": {
                        "kms:ViaService": f"s3.{stack.region}.amazonaws.com"
                    }
                },
            )
        )
    return appsync.HandlerConfig(
        direct=True,
        data_source=events.add_lambda_data_source("RunEventsAuthSource", fn),
        lambda_invoke_type=appsync.LambdaInvokeType.REQUEST_RESPONSE,
    )
