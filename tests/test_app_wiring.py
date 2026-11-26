# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""What app.py hands the core stacks from platform.yaml, read off synthesized templates.

Each case synthesizes a contract fixture (shared with the other synth tests through
app_synth) and checks the resource the wiring produces, so a test fails when the
argument is wired wrong rather than when app.py is reworded.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from app_synth import synth_app

from infra_utils.platform_config import PlatformConfig

REPO = Path(__file__).resolve().parents[1]
CONTRACT = REPO / "tests" / "fixtures" / "contract"
HELLO_SAP = CONTRACT / "hello-sap.yaml"


def _resources(config: Path | str, stack_suffix: str, type_: str) -> list[dict]:
    result, out = synth_app(config)
    assert result.returncode == 0, result.stderr[-2000:]
    (path,) = out.glob(f"*-{stack_suffix}.template.json")
    resources = json.loads(path.read_text())["Resources"].values()
    return [r["Properties"] for r in resources if r["Type"] == type_]


def _client_names(config) -> set[str]:
    return {
        p.get("ClientName", "")
        for p in _resources(config, "gateway", "AWS::Cognito::UserPoolClient")
    }


def without_use_cases(config: Path) -> str:
    """The fixture with every use case turned off and nothing else changed."""
    raw = yaml.safe_load(config.read_text())
    raw.pop("use_cases")
    return yaml.safe_dump(raw)


def test_the_gateway_gets_clients_from_enabled_use_cases_only():
    on = _client_names(HELLO_SAP)
    off = _client_names(without_use_cases(HELLO_SAP))
    assert any("hello-sap" in n for n in on)
    assert not any("hello-sap" in n for n in off)


def _sids(config) -> set[str]:
    return {
        st.get("Sid")
        for p in _resources(config, "gateway", "AWS::IAM::Policy")
        for st in p["PolicyDocument"]["Statement"]
    }


def test_the_gateway_may_exchange_tokens_in_direct_mode_only():
    assert "TokenExchangeVault" in _sids(CONTRACT / "direct.yaml")
    assert "TokenExchangeVault" not in _sids(CONTRACT / "usecase.yaml")


def test_the_auth_stack_gets_the_group_map_of_enabled_use_cases_only():
    config = {
        "deployment": {"platform_account": "111111111111"},
        "identity": {
            "idp": "entra_id",
            "tenant_id": "11111111-2222-3333-4444-555555555555",
            "client_id": "c",
            "client_secret_name": "s",
            "group_claim": "groups",
            "group_map": {
                "hello-platform": {"g1": ["hello-platform-readers"]},
                "hello-sap": {"g2": ["hello-sap-x"]},
            },
        },
        "use_cases": {"hello-platform": {}},
    }
    (trigger,) = [
        p
        for p in _resources(yaml.safe_dump(config), "auth", "AWS::Lambda::Function")
        if "GROUP_MAP" in p.get("Environment", {}).get("Variables", {})
    ]
    assert json.loads(trigger["Environment"]["Variables"]["GROUP_MAP"]) == {
        "g1": ["hello-platform-readers"]
    }


def test_pattern_none_deploys_a_use_case_without_the_sample_orchestrator():
    config = yaml.safe_load(HELLO_SAP.read_text())
    config["agents"] = {"pattern": "none"}
    result, out = synth_app(yaml.safe_dump(config))
    assert result.returncode == 0, result.stderr[-2000:]
    built = {t.name.removesuffix(".template.json") for t in out.glob("*.template.json")}
    assert built == set(PlatformConfig.model_validate(config).expected_stacks())
    assert "agentcore-workshop-dev-uc-hello-sap" in built
    assert not any(name.endswith("-runtime-orchestrator") for name in built)


def test_a_migration_needs_the_orchestrator():
    config = yaml.safe_load((REPO / "presets" / "migration.yaml").read_text())
    config["agents"] = {**config.get("agents", {}), "pattern": "none"}
    with pytest.raises(ValueError, match="pattern none builds no orchestrator"):
        PlatformConfig.model_validate(config)


def test_the_security_alert_covers_rerouting_and_rewriting_changes():
    # A rewritten Cedar permit, a repointed Gateway target or a new runtime image changes
    # what the agent may do as much as a Gateway change, so each must reach the alert.
    (rule,) = [
        r
        for r in _resources(
            CONTRACT / "production.yaml",
            "observability",
            "AWS::Events::Rule",
        )
        if r["EventPattern"]["source"] == ["aws.bedrock-agentcore"]
    ]
    names = set(rule["EventPattern"]["detail"]["eventName"])
    assert {
        "UpdatePolicy",
        "CreateGatewayTarget",
        "UpdateGatewayTarget",
        "DeleteGatewayTarget",
        "UpdateAgentRuntime",
    } <= names


def test_the_security_alert_covers_the_interceptors_code_and_configuration():
    # The interceptor runs every tenant's checks and names the role their trust rests
    # on: a code or environment change to it must reach the alert.
    (rule,) = [
        r
        for r in _resources(
            CONTRACT / "production.yaml",
            "observability",
            "AWS::Events::Rule",
        )
        if r["EventPattern"]["source"] == ["aws.lambda"]
    ]
    detail = rule["EventPattern"]["detail"]
    assert {
        "UpdateFunctionCode20150331v2",
        "UpdateFunctionConfiguration20150331v2",
    } <= set(detail["eventName"])
    names = detail["requestParameters"]["functionName"]
    assert any(isinstance(n, str) and n.endswith("-egress-interceptor") for n in names)
    assert {"wildcard"} == {k for n in names if isinstance(n, dict) for k in n}


def test_app_synthesizes_without_a_platform_yaml():
    result, out = synth_app(REPO / "no-such-platform.yaml")
    assert result.returncode == 0, result.stderr[-2000:]
    assert any(out.glob("*.template.json"))
