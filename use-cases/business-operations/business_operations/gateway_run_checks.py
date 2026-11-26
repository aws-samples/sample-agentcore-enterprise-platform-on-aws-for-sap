# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The run checks' wiring: what the interceptor check (business_operations_check) may read.

The check runs inside the platform's interceptor, as the platform's interceptor
role, so it gets no environment of its own. It reads one parameter this stack
publishes, `use-cases/business-operations/run-checks`, trusting only a version
CloudFormation wrote, and this module grants the role
that parameter and the few reads the check makes, each on this use case's own
resources (the synth refuses anything else on a shared role).
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk import aws_ssm as ssm
from sap_layer import names as sap_names
from sap_layer.access import trusted_description

from business_operations import names
from business_operations.data import Data
from infra_utils.use_case_context import UseCaseContext

CHECK_PATH = f"use-cases/{names.NAME}/business_operations_check"
#: Its own targets, and the SAP layer's, where it governs this use case's writes.
PREFIXES = (f"{names.NAME}-", sap_names.TARGET_PREFIX)


def require_extension(platform) -> None:
    """Refuse a synth that enables this use case without its run checks in the interceptor.

    Without them nothing ties a SAP write to a run or holds it to the approval limit.
    """
    for e in platform.gateway.interceptor_extensions:
        if (
            e.path.rstrip("/") == CHECK_PATH
            and set(PREFIXES) <= set(e.target_prefixes)
            and e.request_headers
        ):
            return
    raise ValueError(
        f"use_cases.{names.NAME} needs its interceptor check. Add to platform.yaml:\n"
        "gateway:\n"
        "  interceptor_extensions:\n"
        f"    - path: {CHECK_PATH}\n"
        f"      target_prefixes: [{', '.join(PREFIXES)}]\n"
        "      request_headers: true"
    )


def attach(
    stack: cdk.Stack,
    ctx: UseCaseContext,
    data: Data,
    *,
    sap_callers: list[str],
    agent_callers: list[str],
) -> ssm.StringParameter:
    """`sap_callers` are the apps this use case's SAP claim names (business_operations/sap.py);
    `agent_callers` the ones of those that run the agent, whose SAP reads need a run grant."""
    ssm_prefix = ctx.ssm_prefix
    cedar = ctx.platform.security.cedar
    role = iam.Role.from_role_arn(
        stack,
        "InterceptorRole",
        ctx.ssm_value(stack, "gateway/interceptor-role-arn"),
    )
    parameter = ssm.StringParameter(
        stack,
        "Out-run-checks",
        parameter_name=f"{ssm_prefix}/use-cases/{names.NAME}/run-checks",
        # The check trusts only what CloudFormation wrote; this nonce is how
        # `deploy.sh layer run sap rewrite <stack>` has it write a tampered value again.
        description=trusted_description(stack),
        string_value=stack.to_json_string(
            {
                "run_grants_table": data.run_grants.table_name,
                "cases_table": data.cases.table_name,
                "tickets_table": data.tickets.table_name,
                "config_table": data.config.table_name,
                "registry_bucket": data.registry.bucket_name,
                # On a SAP target the run checks decide for these apps only: any other
                # caller is another use case's, and the SAP layer's check already
                # held it to that use case's claim.
                "sap_callers": sap_callers,
                # The agent's own client (and the console app when a person's token is
                # forwarded): their SAP reads, unlike the operator reader's, need a run
                # grant. A SAP caller not listed here reads as before.
                "agent_callers": agent_callers,
                # Not read by the run checks: verify.py reads it to know what Cedar refuses.
                "cedar_enforced": cedar.enabled and cedar.mode == "ENFORCE",
            }
        ),
    )
    # GetParameter and GetParameterHistory (the check's writer check), on this one
    # parameter.
    parameter.grant_read(role)
    data.run_grants.grant(role, "dynamodb:GetItem")
    data.cases.grant(role, "dynamodb:GetItem")
    data.tickets.grant(role, "dynamodb:GetItem")
    data.config.grant(role, "dynamodb:GetItem")
    role.add_to_principal_policy(
        iam.PolicyStatement(
            actions=["s3:GetObject"],
            resources=[data.registry.arn_for_objects("registry.json")],
        )
    )
    data.grant_key(role)
    return parameter
