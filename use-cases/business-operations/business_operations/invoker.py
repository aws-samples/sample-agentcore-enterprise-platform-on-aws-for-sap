# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The agent invoker: the one Lambda that starts a run on the agent runtime.

`POST /runs` claims the case and invokes it asynchronously, and the agent queue is its
second caller. Before every run it writes the run grant the Gateway run checks
check, `{run_id, client_id, case_id, packs, approved, rehearsal, expires_at}`, naming
the client the agent calls the Gateway as, and the agent sends
`x-audit-correlation-id=run_id` on every Gateway call. Without the grant the run checks refuse
every case, process-type and SAP write call. It is also the only way to a probe grant:
verify.py invokes it with `{"verify_grant": {...}}` for a two-minute grant naming the
`reader` client.

It reaches the runtime as the delegating client, the only client the runtime admits,
and finds the runtime's ARN in SSM per run, because the runtime stack deploys after
this one.
"""

from __future__ import annotations

from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_ssm as ssm

from business_operations import names
from business_operations.api import Api
from business_operations.data import Data
from business_operations.runtime import AGENT_CLIENT
from infra_utils.use_case_context import UseCaseContext
from sap_kit.functions import uc_function

SRC = Path(__file__).resolve().parents[1] / "invoker"
#: The handler's run budget derives from this (invoker/index.py).
TIMEOUT_SECONDS = 900


def create(
    stack: cdk.Stack, ctx: UseCaseContext, data: Data, api: Api
) -> _lambda.Function:
    ssm_prefix = ctx.ssm_prefix
    runtime_arn = f"{ssm_prefix}/runtimes/{names.NAME}/arn"
    stop_parameter = names.trigger_mode_parameters(ssm_prefix)["stop"]

    fn = uc_function(
        stack,
        "AgentInvoker",
        ctx,
        SRC,
        layers=[api.layer],
        timeout=TIMEOUT_SECONDS,
        # `POST /runs` invokes it asynchronously, and Lambda retries a crashed async
        # invoke twice: a run, and its SAP writes, replayed. SQS deliveries are not
        # affected; the queue's own redrive covers them.
        retry_attempts=0,
        environment={
            "CASES_TABLE": data.cases.table_name,
            "RUN_GRANTS_TABLE": data.run_grants.table_name,
            # The client the agent calls the Gateway as; the run checks match the grant on it.
            "AGENT_CLIENT_ID": ctx.ssm_value(
                stack, f"gateway/use-case-clients/{names.NAME}/{AGENT_CLIENT}/client-id"
            ),
            # The one client a probe grant may name (verify.py).
            "VERIFY_CLIENT_ID": ctx.ssm_value(
                stack, f"gateway/use-case-clients/{names.NAME}/reader/client-id"
            ),
            "RUNTIME_ARN_PARAMETER": runtime_arn,
            # While an emergency stop stands no run starts (`emergency_stop.py`).
            "EMERGENCY_STOP_PARAMETER": stop_parameter,
            **api.delegating_env,
        },
    )
    fn.add_to_role_policy(
        iam.PolicyStatement(
            actions=["ssm:GetParameter"],
            resources=[
                stack.format_arn(
                    service="ssm",
                    resource="parameter",
                    resource_name=stop_parameter.lstrip("/"),
                )
            ],
        )
    )
    data.grant_cases(fn, "GetItem", "UpdateItem")
    # DeleteItem so a finished run takes its grant with it; the TTL is the backstop.
    data.run_grants.grant(fn, "dynamodb:PutItem", "dynamodb:DeleteItem")
    # The run checks trust a grant, so only this function may mint one, whatever IAM grants
    # elsewhere in the account. verify.py asks it for its probe grants.
    data.run_grants.add_to_resource_policy(
        iam.PolicyStatement(
            sid="OnlyTheInvokerWritesGrants",
            effect=iam.Effect.DENY,
            principals=[iam.AnyPrincipal()],
            actions=[
                "dynamodb:PutItem",
                "dynamodb:UpdateItem",
                "dynamodb:BatchWriteItem",
                "dynamodb:PartiQLInsert",
                "dynamodb:PartiQLUpdate",
            ],
            # A table's own policy cannot name its ARN (CDK: circular reference).
            resources=["*"],
            conditions={"ArnNotEquals": {"aws:PrincipalArn": fn.role.role_arn}},
        )
    )
    # Revoking is the invoker's, as a run ends, and the emergency stop's, which ends
    # every run at once. A delete cannot mint or widen a grant.
    stop = api.functions["stop"]
    data.run_grants.add_to_resource_policy(
        iam.PolicyStatement(
            sid="OnlyTheInvokerAndTheStopRevokeGrants",
            effect=iam.Effect.DENY,
            principals=[iam.AnyPrincipal()],
            actions=["dynamodb:DeleteItem", "dynamodb:PartiQLDelete"],
            resources=["*"],
            conditions={
                "ArnNotEquals": {
                    "aws:PrincipalArn": [fn.role.role_arn, stop.role.role_arn]
                }
            },
        )
    )
    data.run_grants.grant(stop, "dynamodb:Scan", "dynamodb:DeleteItem")
    data.grant_key(stop)
    api.delegating_secret.grant_read(fn)
    runtime_param = stack.format_arn(
        service="ssm", resource="parameter", resource_name=runtime_arn.lstrip("/")
    )
    fn.add_to_role_policy(
        iam.PolicyStatement(actions=["ssm:GetParameter"], resources=[runtime_param])
    )

    # Where the eval runner finds it (evals/run_regression.py), rather than a pinned name.
    ssm.StringParameter(
        stack,
        "Out-invoker-function",
        parameter_name=f"{ssm_prefix}/use-cases/{names.NAME}/invoker-function",
        string_value=fn.function_name,
    )

    runs = api.functions["runs"]
    runs.add_environment("INVOKER_FUNCTION_NAME", fn.function_name)
    runs.add_environment("RUNTIME_ARN_PARAMETER", runtime_arn)
    fn.grant_invoke(runs)
    runs.add_to_role_policy(
        iam.PolicyStatement(actions=["ssm:GetParameter"], resources=[runtime_param])
    )
    stop.add_environment("RUN_GRANTS_TABLE", data.run_grants.table_name)
    stop.add_environment("RUNTIME_ARN_PARAMETER", runtime_arn)
    stop.add_to_role_policy(
        iam.PolicyStatement(actions=["ssm:GetParameter"], resources=[runtime_param])
    )
    return fn
