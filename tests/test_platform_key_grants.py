# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""A use case may use the platform CMK only through a service (kms:ViaService).

security/kms-key-arn is published so a use case can encrypt its own tables and
buckets with the platform key. The key policy trusts the account, so the IAM
grant is the whole control, and app.py refuses one that would let a use-case
role call kms:Decrypt on the key directly. Synthesized, so this checks the
shapes CDK really emits for an SSM-imported key.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_ssm as ssm
from aws_cdk.assertions import Template

from infra_utils.shared_role_grants import foreign_key_grant_violations

ENV = cdk.Environment(account="111111111111", region="us-east-1")


def _use_case(grant) -> list[str]:
    app = cdk.App()
    stack = cdk.Stack(app, "proj-dev-uc-tenant", env=ENV)
    key = kms.Key.from_key_arn(
        stack,
        "PlatformKey",
        ssm.StringParameter.value_for_string_parameter(
            stack, "/proj/dev/security/kms-key-arn"
        ),
    )
    role = iam.Role(
        stack, "Fn", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com")
    )
    grant(stack, role, key)
    return foreign_key_grant_violations(
        {stack.stack_name: Template.from_stack(stack).to_json()}
    )


def _via(*services: str) -> dict:
    return {
        "StringEquals": {
            "kms:ViaService": [f"{s}.{cdk.Aws.REGION}.amazonaws.com" for s in services]
        }
    }


def test_a_bare_grant_on_the_platform_key_is_refused():
    assert _use_case(lambda s, role, key: key.grant_decrypt(role))


def test_a_grant_through_dynamodb_and_s3_passes():
    def grant(stack, role, key):
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["kms:Decrypt", "kms:GenerateDataKey*"],
                resources=[key.key_arn],
                conditions=_via("dynamodb", "s3"),
            )
        )

    assert _use_case(grant) == []


def test_another_service_or_a_wildcard_service_is_refused():
    def other(stack, role, key):
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["kms:Decrypt"],
                resources=[key.key_arn],
                conditions=_via("dynamodb", "ec2"),
            )
        )

    def wildcard(stack, role, key):
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["kms:Decrypt"],
                resources=[key.key_arn],
                conditions={"StringLike": {"kms:ViaService": "*.amazonaws.com"}},
            )
        )

    assert _use_case(other)
    assert _use_case(wildcard)


def test_kms_on_every_key_is_refused():
    def grant(stack, role, key):
        role.add_to_policy(iam.PolicyStatement(actions=["kms:*"], resources=["*"]))

    assert _use_case(grant)


def test_the_use_cases_own_key_is_its_own_business():
    def grant(stack, role, key):
        kms.Key(stack, "Own").grant_encrypt_decrypt(role)

    assert _use_case(grant) == []
