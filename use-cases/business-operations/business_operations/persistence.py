# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The persistence guardrail: allowed in the model, masked in what is stored or sent.

Created only under `security.require_guardrails`, which `deployment.mode:
production` requires, so a stack either masks every write `agent/utils/persist_mask.py`
covers or none. Its own guardrail rather than the runtime's: that one blocks SSNs
and refuses topics, and a refused trace write would lose the trace instead of
masking it. This one only anonymizes, so the call fails only when Amazon Bedrock does.
"""

from __future__ import annotations

from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_ssm as ssm

from business_operations import names
from infra_utils.guardrail_policy import published_version
from infra_utils.use_case_context import UseCaseContext

#: Anonymized wherever a run stores or sends text. Not the AWS key types: the
#: guardrail reads a derived case id as an access key, and every trace carries one.
ENTITIES = (
    "US_BANK_ACCOUNT_NUMBER",
    "US_BANK_ROUTING_NUMBER",
    "INTERNATIONAL_BANK_ACCOUNT_NUMBER",
    "SWIFT_CODE",
    "US_INDIVIDUAL_TAX_IDENTIFICATION_NUMBER",
    "US_SOCIAL_SECURITY_NUMBER",
    "CREDIT_DEBIT_CARD_NUMBER",
)
PARAM = f"use-cases/{names.NAME}/persist-guardrail-id"
VERSION_PARAM = f"use-cases/{names.NAME}/persist-guardrail-version"


def create(stack, ctx: UseCaseContext) -> bedrock.CfnGuardrail | None:
    if not ctx.require_guardrails:
        return None
    guardrail = bedrock.CfnGuardrail(
        stack,
        "PersistGuardrail",
        name=names.guardrail_name(ctx.project, ctx.environment, "persist"),
        description=f"{names.NAME}: masked in what a run stores or sends",
        blocked_input_messaging="Blocked by the persistence guardrail.",
        blocked_outputs_messaging="Blocked by the persistence guardrail.",
        sensitive_information_policy_config=bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty(
            pii_entities_config=[
                bedrock.CfnGuardrail.PiiEntityConfigProperty(type=e, action="ANONYMIZE")
                for e in ENTITIES
            ]
        ),
    )
    ssm.StringParameter(
        stack,
        "Out-persist-guardrail-id",
        parameter_name=f"{ctx.ssm_prefix}/{PARAM}",
        string_value=guardrail.attr_guardrail_id,
    )
    ssm.StringParameter(
        stack,
        "Out-persist-guardrail-version",
        parameter_name=f"{ctx.ssm_prefix}/{VERSION_PARAM}",
        string_value=published_version(guardrail).attr_version,
    )
    return guardrail


def grant(guardrail: bedrock.CfnGuardrail | None, *fns: _lambda.Function) -> None:
    """Give each function the guardrail, or nothing when the stack has none."""
    if guardrail is None:
        return
    for fn in fns:
        fn.add_environment("PERSIST_GUARDRAIL_ID", guardrail.attr_guardrail_id)
        fn.add_environment(
            "PERSIST_GUARDRAIL_VERSION", published_version(guardrail).attr_version
        )
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:ApplyGuardrail"],
                resources=[guardrail.attr_guardrail_arn],
            )
        )
