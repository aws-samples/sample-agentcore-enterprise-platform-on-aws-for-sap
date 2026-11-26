# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""A use case extends the shared Gateway role only with grants on its own resources.

The Gateway publishes its id, role ARN and policy-engine id so a use case can
attach to it from its own stack. That role serves every use case, so app.py
refuses any use-case grant on it that names a wildcard, a literal ARN or a
resource the use case does not define. Synthesized, so this checks the shapes
CDK really emits (grant_invoke's `<arn>:*`, the SSM-resolved role import).
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_ssm as ssm
from aws_cdk.assertions import Template

from infra_utils.shared_role_grants import shared_role_grant_violations
from stacks.gateway_stack import GatewayStack

ENV = cdk.Environment(account="111111111111", region="us-east-1")
ROLE_PARAM = "/proj/dev/gateway/role-arn"


def _use_case(grant) -> dict:
    app = cdk.App()
    stack = cdk.Stack(app, "proj-dev-uc-tenant", env=ENV)
    role = iam.Role.from_role_arn(
        stack,
        "GatewayRole",
        ssm.StringParameter.value_for_string_parameter(stack, ROLE_PARAM),
    )
    fn = _lambda.Function(
        stack,
        "Target",
        runtime=_lambda.Runtime.PYTHON_3_13,
        handler="index.handler",
        code=_lambda.Code.from_inline("def handler(e, c): return e"),
    )
    grant(stack, role, fn)
    return {stack.stack_name: Template.from_stack(stack).to_json()}


def _check(templates: dict) -> list[str]:
    return shared_role_grant_violations(
        templates,
        role_names=["proj-dev-gateway-role"],
        role_arn_parameters=[ROLE_PARAM],
    )


def test_a_grant_on_the_use_cases_own_lambda_passes():
    # grant_invoke emits [fn.Arn, fn.Arn + ":*"]: the qualifier wildcard sits
    # under an ARN the use case owns, so it is not a wildcard resource.
    assert _check(_use_case(lambda s, role, fn: fn.grant_invoke(role))) == []


def test_a_wildcard_grant_is_refused():
    def grant(stack, role, fn):
        role.add_to_principal_policy(
            iam.PolicyStatement(actions=["lambda:InvokeFunction"], resources=["*"])
        )

    findings = _check(_use_case(grant))
    assert findings and '"*"' in findings[0]


def test_no_action_may_take_a_wildcard_on_the_shared_role():
    # A guardrail check on `*` was once allowed for a Cedar `when guardrails`
    # statement; nothing needs it now, so it is a wildcard like any other.
    def grant(stack, role, fn):
        role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeGuardrailChecks"], resources=["*"]
            )
        )

    assert _check(_use_case(grant))


def test_a_literal_arn_is_refused():
    def grant(stack, role, fn):
        role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=["arn:aws:lambda:us-east-1:111111111111:function:other"],
            )
        )

    assert _check(_use_case(grant))


def test_a_foreign_resource_by_parameter_is_refused():
    def grant(stack, role, fn):
        other = ssm.StringParameter.value_for_string_parameter(
            stack, "/proj/dev/runtimes/orchestrator/arn"
        )
        role.add_to_principal_policy(
            iam.PolicyStatement(actions=["bedrock-agentcore:*"], resources=[other])
        )

    assert _check(_use_case(grant))


def test_grants_on_the_use_cases_own_roles_are_not_this_guards_business():
    def grant(stack, role, fn):
        fn.add_to_role_policy(
            iam.PolicyStatement(actions=["xray:PutTraceSegments"], resources=["*"])
        )

    assert _check(_use_case(grant)) == []


def test_a_role_named_literally_is_still_caught():
    app = cdk.App()
    stack = cdk.Stack(app, "proj-dev-uc-tenant", env=ENV)
    role = iam.Role.from_role_name(stack, "GatewayRole", "proj-dev-gateway-role")
    role.add_to_principal_policy(
        iam.PolicyStatement(actions=["s3:GetObject"], resources=["*"])
    )
    assert _check({stack.stack_name: Template.from_stack(stack).to_json()})


def test_deny_statements_are_allowed_on_anything():
    def grant(stack, role, fn):
        role.add_to_principal_policy(
            iam.PolicyStatement(
                effect=iam.Effect.DENY, actions=["s3:*"], resources=["*"]
            )
        )

    assert _check(_use_case(grant)) == []


def _gateway(**kw) -> dict:
    app = cdk.App()
    stack = GatewayStack(
        app,
        "Gw",
        project_name="proj",
        environment="dev",
        cognito_issuer_url="https://issuer.example.com",
        cognito_allowed_clients=["client"],
        env=ENV,
        **kw,
    )
    return Template.from_stack(stack).to_json()


def _parameter_names(template: dict) -> set[str]:
    return {
        r["Properties"]["Name"]
        for r in template["Resources"].values()
        if r["Type"] == "AWS::SSM::Parameter"
    }


def test_gateway_publishes_id_and_role_arn():
    names = _parameter_names(_gateway())
    assert {"/proj/dev/gateway/id", "/proj/dev/gateway/role-arn"} <= names
    assert "/proj/dev/gateway/policy-engine-id" not in names


def test_policy_engine_id_is_published_only_with_cedar():
    assert "/proj/dev/gateway/policy-engine-id" in _parameter_names(
        _gateway(enable_cedar=True)
    )


KEY_PARAM = "/proj/dev/security/kms-key-arn"


def _key_grant(conditions):
    def grant(stack, role, fn):
        key = ssm.StringParameter.value_for_string_parameter(stack, KEY_PARAM)
        role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=["kms:Decrypt"], resources=[key], conditions=conditions
            )
        )

    return grant


def _check_keys(templates: dict) -> list[str]:
    return shared_role_grant_violations(
        templates,
        role_names=["proj-dev-gateway-role"],
        role_arn_parameters=[ROLE_PARAM],
        key_arn_parameters=(KEY_PARAM,),
    )


def test_the_platform_key_through_a_service_passes():
    # The shared role reads the use case's own encrypted table; the table grant
    # is the control, the key grant only lets DynamoDB decrypt for it.
    via = {"StringEquals": {"kms:ViaService": "dynamodb.us-east-1.amazonaws.com"}}
    assert _check_keys(_use_case(_key_grant(via))) == []


def test_the_platform_key_without_a_service_is_refused():
    assert _check_keys(_use_case(_key_grant(None)))
    # And only when the caller names the key parameter at all.
    via = {"StringEquals": {"kms:ViaService": "dynamodb.us-east-1.amazonaws.com"}}
    assert _check(_use_case(_key_grant(via)))


def test_two_stacks_naming_one_inline_policy_on_one_role_collide():
    from infra_utils.shared_role_grants import inline_policy_name_collisions

    def stack(name: str) -> dict:
        return {
            "Parameters": {"Role": {"Default": "/p/dev/gateway/role-arn"}},
            "Resources": {
                "P": {
                    "Type": "AWS::IAM::Policy",
                    "Properties": {"PolicyName": name, "Roles": [{"Ref": "Role"}]},
                }
            },
        }

    assert inline_policy_name_collisions({"a": stack("X"), "b": stack("X")}) == [
        "X on /p/dev/gateway/role-arn: a/P, b/P"
    ]
    assert inline_policy_name_collisions({"a": stack("X"), "b": stack("Y")}) == []
