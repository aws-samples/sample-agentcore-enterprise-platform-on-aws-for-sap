# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""What a live Gateway needs before its policy engine and interceptor work.

Each of these deployed cleanly and then failed on the first real call:

1. CreatePolicy rejects a permit with an open ``resource`` ("a wildcard
   resource was detected"), and policy names are unique per account and
   Region, so the shipped permit is pinned to the Gateway and prefixed.
2. Attaching the engine makes the Gateway call GetPolicyEngine and probe
   AuthorizeAction as its own role; the interceptor is invoked as that role
   too, not through the Lambda resource permission.

The interceptor's third live fix, forwarding the request body alone, is
tests/test_interceptor_extensions.py's.
"""

from __future__ import annotations

import json

import aws_cdk as cdk
from aws_cdk.assertions import Template

from stacks.gateway_stack import GatewayStack


def _template() -> dict:
    app = cdk.App()
    stack = GatewayStack(
        app,
        "Gw",
        project_name="proj",
        environment="dev",
        cognito_issuer_url="https://issuer.example.com",
        cognito_allowed_clients=["client"],
        tool_configs={
            "sample-tool": {
                "source_dir": "tools/sample_tool",
                "tool_schema": [
                    {
                        "Name": "text_analysis_tool",
                        "Description": "d",
                        "InputSchema": {"Type": "object"},
                    }
                ],
            }
        },
        enable_egress_filter=True,
        enable_cedar=True,
        env=cdk.Environment(account="111111111111", region="us-east-1"),
    )
    return Template.from_stack(stack).to_json()


def _of_type(template: dict, kind: str) -> dict:
    return {k: r for k, r in template["Resources"].items() if r["Type"] == kind}


def _role_actions(template: dict) -> set[str]:
    actions = set()
    for policy in _of_type(template, "AWS::IAM::Policy").values():
        for stmt in policy["Properties"]["PolicyDocument"]["Statement"]:
            a = stmt["Action"]
            actions.update(a if isinstance(a, list) else [a])
    return actions


def test_permit_is_pinned_to_the_gateway_named_per_deployment_and_after_targets():
    t = _template()
    (policy,) = _of_type(t, "AWS::BedrockAgentCore::Policy").values()
    assert policy["Properties"]["Name"] == "proj_dev_PermitReadTools"
    statement = json.dumps(policy["Properties"]["Definition"])
    assert "resource == AgentCore::Gateway::" in statement
    assert "GatewayArn" in statement
    targets = _of_type(t, "AWS::BedrockAgentCore::GatewayTarget")
    assert targets and set(targets) <= set(policy.get("DependsOn", []))


def test_gateway_role_can_read_its_engine_and_invoke_its_interceptor_first():
    t = _template()
    assert {
        "bedrock-agentcore:GetPolicyEngine",
        "bedrock-agentcore:AuthorizeAction",
        "bedrock-agentcore:PartiallyAuthorizeActions",
        "lambda:InvokeFunction",
    } <= _role_actions(t)
    (gateway,) = _of_type(t, "AWS::BedrockAgentCore::Gateway").values()
    role_policies = {
        k
        for k, r in _of_type(t, "AWS::IAM::Policy").items()
        if k.startswith("GatewayRole")
    }
    assert role_policies and role_policies <= set(gateway.get("DependsOn", []))
