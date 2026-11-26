# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""RuntimeStack is the one core class a use case may build its own runtime with.

What a use case needs beyond the platform's own runtimes: an explicit protocol
(its build context is not an agent-code/ pattern), extra allowlisted request
headers, and the role and CfnRuntime to extend. Defaults must leave the
platform's runtimes exactly as they were.
"""

from __future__ import annotations

import json
from pathlib import Path

import aws_cdk as cdk
import pytest
import yaml
from app_synth import synth_app
from aws_cdk.assertions import Template

from stacks.runtime_stack import RuntimeStack

REPO = Path(__file__).resolve().parents[1]

ENV = cdk.Environment(account="111111111111", region="us-east-1")
ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_pool"


def _stack(tmp_path, **kw) -> RuntimeStack:
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    return RuntimeStack(
        cdk.App(),
        "proj-dev-uc-tenant-runtime",
        project_name="proj",
        environment="dev",
        component_name="tenant-agent",
        source_dir=str(tmp_path),
        cognito_issuer_url=ISSUER,
        cognito_allowed_clients=["tenant-machine"],
        env=ENV,
        **kw,
    )


def _runtime(stack) -> dict:
    template = Template.from_stack(stack).to_json()
    return next(
        r["Properties"]
        for r in template["Resources"].values()
        if r["Type"] == "AWS::BedrockAgentCore::Runtime"
    )


def test_defaults_are_unchanged(tmp_path):
    props = _runtime(_stack(tmp_path))
    assert props["ProtocolConfiguration"] == "HTTP"
    assert props["RequestHeaderConfiguration"] == {
        "RequestHeaderAllowlist": ["Authorization"]
    }


def test_explicit_protocol_and_extra_headers(tmp_path):
    header = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Identity"
    props = _runtime(_stack(tmp_path, protocol="AGUI", extra_request_headers=[header]))
    assert props["ProtocolConfiguration"] == "AGUI"
    assert props["RequestHeaderConfiguration"]["RequestHeaderAllowlist"] == [
        "Authorization",
        header,
    ]
    assert "AuthorizerConfiguration" in props


def test_an_unknown_protocol_fails_the_synth(tmp_path):
    with pytest.raises(ValueError, match="protocol"):
        _stack(tmp_path, protocol="GRPC")


def test_role_and_runtime_are_exposed(tmp_path):
    stack = _stack(tmp_path)
    assert stack.runtime_role.role_name  # an iam.Role a use case can extend
    stack.runtime.add_property_override("Description", "tenant runtime")
    assert _runtime(stack)["Description"] == "tenant runtime"


def _retention(stack) -> list[dict]:
    template = Template.from_stack(stack).to_json()
    return [
        r["Properties"]
        for r in template["Resources"].values()
        if r["Type"] == "Custom::LogRetention"
    ]


def test_the_runtimes_own_log_group_gets_the_platforms_retention(tmp_path):
    # AgentCore creates /aws/bedrock-agentcore/runtimes/<id>-DEFAULT with none.
    # CodeBuild creates /aws/codebuild/<project> on the first build with none either.
    groups = {
        json.dumps(p["LogGroupName"]): p["RetentionInDays"]
        for p in _retention(_stack(tmp_path, log_retention_days=90))
    }
    (name,) = [g for g in groups if "/aws/bedrock-agentcore/runtimes/" in g]
    assert "-DEFAULT" in name and "AgentRuntimeId" in name
    (build,) = [g for g in groups if "/aws/codebuild/" in g]
    assert "-build-" in build
    assert set(groups.values()) == {90}
    assert {p["RetentionInDays"] for p in _retention(_stack(tmp_path))} == {30}


def test_an_unsupported_retention_fails_the_synth(tmp_path):
    with pytest.raises(ValueError, match="log_retention_days"):
        _stack(tmp_path, log_retention_days=7)


def test_bytecode_beside_the_source_leaves_the_asset_hash_alone(tmp_path):
    # The source asset's key reaches the build trigger, which rebuilds on any change.
    def asset_keys(stack) -> str:
        return json.dumps(
            Template.from_stack(stack).to_json()["Resources"]["BuildTriggerCR"]
        )

    clean = asset_keys(_stack(tmp_path))
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "agent.cpython-313.pyc").write_bytes(b"\0")
    (tmp_path / ".DS_Store").write_bytes(b"\0")
    (tmp_path / ".ruff_cache").mkdir()
    (tmp_path / ".ruff_cache" / "CACHEDIR.TAG").write_text("x")
    assert asset_keys(_stack(tmp_path)) == clean


def test_app_gives_every_runtime_the_platform_retention():
    config = yaml.safe_load((REPO / "presets/greenfield.yaml").read_text())
    config.setdefault("agents", {})["a2a"] = True
    config.setdefault("observability", {})["log_retention_days"] = 90
    result, out = synth_app(yaml.safe_dump(config))
    assert result.returncode == 0, result.stderr[-2000:]
    runtimes, retained = 0, []
    for t in out.glob("*.template.json"):
        resources = json.loads(t.read_text())["Resources"].values()
        runtimes += sum(
            r["Type"] == "AWS::BedrockAgentCore::Runtime" for r in resources
        )
        retained += [
            r["Properties"]["RetentionInDays"]
            for r in resources
            if r["Type"] == "Custom::LogRetention"
            and "/aws/bedrock-agentcore/runtimes/" in json.dumps(r["Properties"])
        ]
    # The orchestrator, code-agent and research-agent.
    assert runtimes >= 3
    assert retained == [90] * runtimes
