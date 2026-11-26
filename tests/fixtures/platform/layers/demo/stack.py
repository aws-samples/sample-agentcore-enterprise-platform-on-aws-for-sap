# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""A layer stack for tests: a parameter, and with `violate` one finding for every guard."""

import aws_cdk as cdk
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_iam as iam
from aws_cdk import aws_ssm as ssm

from infra_utils.use_case_context import UseCaseContext


def wide_grant(stack: cdk.Stack, ctx: UseCaseContext) -> None:
    """Grant the shared Gateway role a wildcard, which the shared-role guard refuses."""
    role = iam.Role.from_role_arn(
        stack,
        "GatewayRole",
        ssm.StringParameter.value_for_string_parameter(
            stack, f"{ctx.ssm_prefix}/gateway/role-arn"
        ),
    )
    role.add_to_principal_policy(
        iam.PolicyStatement(actions=["s3:GetObject"], resources=["*"])
    )


def build(app, ctx: UseCaseContext, config: dict) -> None:
    stack = cdk.Stack(app, f"{ctx.prefix}-layer-demo", env=ctx.cdk_env)
    ssm.StringParameter(
        stack,
        "Out",
        parameter_name=f"{ctx.ssm_prefix}/layers/demo/seen",
        string_value="demo",
    )
    if not config.get("violate"):
        return
    wide_grant(stack, ctx)
    iam.Role(
        stack, "Own", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com")
    ).add_to_policy(iam.PolicyStatement(actions=["kms:Decrypt"], resources=["*"]))
    cdk.CfnOutput(
        stack, "Leak", value=cdk.SecretValue.secrets_manager("s").unsafe_unwrap()
    )
    cognito.CfnUserPoolClient(
        stack, "Writer", user_pool_id="pool", write_attributes=["custom:idp_groups"]
    )
    # The test's config gives the SAP layer's check the `sap-` prefix.
    agentcore.CfnGatewayTarget(
        stack,
        "Squatter",
        name="sap-squatter",
        gateway_identifier="g",
        target_configuration={"mcp": {}},
    )
