# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Imports the demo layer's lib at synth, as a use case requiring a layer may."""

import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk import aws_ssm as ssm
from demo_layer import LABEL

from infra_utils.use_case_context import UseCaseContext


def build(app, ctx: UseCaseContext, config: dict) -> None:
    stack = cdk.Stack(app, f"{ctx.prefix}-uc-needs-demo", env=ctx.cdk_env)
    ssm.StringParameter(
        stack,
        "Out",
        parameter_name=f"{ctx.ssm_prefix}/use-cases/needs-demo/label",
        string_value=LABEL,
    )
    if config.get("wide_grant"):
        iam.Role.from_role_arn(
            stack,
            "GatewayRole",
            ssm.StringParameter.value_for_string_parameter(
                stack, f"{ctx.ssm_prefix}/gateway/role-arn"
            ),
        ).add_to_principal_policy(
            iam.PolicyStatement(actions=["s3:GetObject"], resources=["*"])
        )
