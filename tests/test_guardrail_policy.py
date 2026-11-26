# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The guardrails' PII policy: configurable, with a production floor config cannot lower.

The Gateway egress guardrail takes per-entity overrides from platform.yaml; a
runtime takes a whole policy. Under deployment.mode production both keep the
prompt-attack filter and SSN, card and AWS-credential BLOCK.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import get_args

import aws_cdk as cdk
import botocore
import pytest
import yaml
from aws_cdk.assertions import Template
from pydantic import ValidationError

from infra_utils.guardrail_policy import (
    CREDENTIAL_FLOOR,
    PII_FLOOR,
    PiiAction,
    PiiEntityType,
    baseline_policy,
    floor_violations,
    published_version,
)
from infra_utils.platform_config import PlatformConfig
from infra_utils.policy_loader import load_control
from stacks.gateway_stack import GatewayStack
from stacks.runtime_stack import RuntimeStack

REPO = Path(__file__).resolve().parents[1]
ENV = cdk.Environment(account="111111111111", region="us-east-1")


def _entities(policy: dict) -> dict[str, str]:
    return {
        e["Type"]: e["Action"]
        for e in policy["SensitiveInformationPolicyConfig"]["PiiEntitiesConfig"]
    }


def test_entity_names_match_the_bedrock_service_model():
    model = next(Path(botocore.__file__).parent.glob("data/bedrock/*/service-2.json*"))
    opener = gzip.open if model.suffix == ".gz" else open
    shapes = json.load(opener(model))["shapes"]
    assert set(get_args(PiiEntityType)) == set(shapes["GuardrailPiiEntityType"]["enum"])
    assert set(get_args(PiiAction)) == set(
        shapes["GuardrailSensitiveInformationAction"]["enum"]
    )


def test_default_is_the_artifact_unchanged():
    assert baseline_policy() == load_control("guardrail.egress-default")


def test_overrides_replace_and_add_entities():
    entities = _entities(baseline_policy({"NAME": "NONE", "PASSWORD": "BLOCK"}))
    assert "NAME" not in entities  # NONE: not screened at all
    assert entities["PASSWORD"] == "BLOCK"
    assert entities["ADDRESS"] == "ANONYMIZE"


def test_the_floor_wins_over_overrides():
    policy = baseline_policy(
        {"US_SOCIAL_SECURITY_NUMBER": "NONE"}, floor=True, runtime=True
    )
    assert all(_entities(policy)[t] == "BLOCK" for t in PII_FLOOR)
    assert floor_violations(policy) == []


def test_a_runtime_blocks_credentials_in_what_it_says_not_what_it_is_given():
    # The detector reads some case ids as a secret key: on input it refused the case.
    entities = {
        e["Type"]: e
        for e in baseline_policy(floor=True, runtime=True)[
            "SensitiveInformationPolicyConfig"
        ]["PiiEntitiesConfig"]
    }
    for t in CREDENTIAL_FLOOR:
        assert entities[t]["InputEnabled"] is False
        assert entities[t]["OutputAction"] == "BLOCK"
    # The Gateway's tool data carries case ids as bare values: no credential floor there.
    egress = _entities(baseline_policy(floor=True))
    assert not set(CREDENTIAL_FLOOR) & set(egress)
    assert all(egress[t] == "BLOCK" for t in PII_FLOOR)


def test_floor_violations_name_what_is_missing():
    policy = load_control("guardrail.egress-default")
    assert floor_violations(policy) == [
        "AWS_ACCESS_KEY at BLOCK on output",
        "AWS_SECRET_KEY at BLOCK on output",
    ]
    policy.pop("ContentPolicyConfig")
    assert "a PROMPT_ATTACK filter at InputStrength HIGH" in floor_violations(policy)


# ── platform.yaml validation ──


def _production(**security) -> dict:
    raw = yaml.safe_load((REPO / "presets" / "production.yaml").read_text())
    raw["security"].update(security)
    return raw


def test_production_preset_validates_with_overrides_above_the_floor():
    config = PlatformConfig.model_validate(
        _production(egress_pii_entities={"NAME": "NONE", "ADDRESS": "NONE"})
    )
    assert config.security.egress_pii_entities["NAME"] == "NONE"


@pytest.mark.parametrize("entity", PII_FLOOR)
@pytest.mark.parametrize("action", ["ANONYMIZE", "NONE"])
def test_production_refuses_lowering_the_floor(entity, action):
    with pytest.raises(ValidationError, match=f"keep {entity} at BLOCK"):
        PlatformConfig.model_validate(_production(egress_pii_entities={entity: action}))


def test_workshop_may_lower_anything():
    config = PlatformConfig.model_validate(
        {"security": {"egress_pii_entities": {"US_SOCIAL_SECURITY_NUMBER": "NONE"}}}
    )
    assert config.security.egress_pii_entities == {"US_SOCIAL_SECURITY_NUMBER": "NONE"}


def test_an_unknown_entity_type_is_refused():
    with pytest.raises(ValidationError):
        PlatformConfig.model_validate(
            {"security": {"egress_pii_entities": {"SSN": "BLOCK"}}}
        )


# ── synthesized guardrails ──


def _guardrail(stack: cdk.Stack) -> dict:
    template = Template.from_stack(stack).to_json()
    return next(
        r["Properties"]
        for r in template["Resources"].values()
        if r["Type"] == "AWS::Bedrock::Guardrail"
    )


def _gateway(**kw) -> GatewayStack:
    return GatewayStack(
        cdk.App(),
        "Gw",
        project_name="proj",
        environment="dev",
        cognito_issuer_url="https://issuer.example.com",
        cognito_allowed_clients=["client"],
        enable_egress_filter=True,
        env=ENV,
        **kw,
    )


def test_gateway_guardrail_defaults_to_the_artifact():
    artifact = load_control("guardrail.egress-default")
    assert (
        _guardrail(_gateway())["SensitiveInformationPolicyConfig"]
        == artifact["SensitiveInformationPolicyConfig"]
    )


def test_gateway_guardrail_takes_overrides_and_the_floor():
    props = _guardrail(
        _gateway(egress_pii_entities={"NAME": "NONE"}, guardrail_floor=True)
    )
    assert all(_entities(props)[t] == "BLOCK" for t in PII_FLOOR)
    assert "NAME" not in _entities(props)  # NONE: not screened


def _runtime(tmp_path, **kw) -> RuntimeStack:
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    return RuntimeStack(
        cdk.App(),
        "proj-dev-uc-tenant-runtime",
        project_name="proj",
        environment="dev",
        component_name="tenant-agent",
        source_dir=str(tmp_path),
        require_guardrails=True,
        env=ENV,
        **kw,
    )


def _has_deny(stack: RuntimeStack) -> bool:
    template = Template.from_stack(stack).to_json()
    return any(
        s.get("Sid") == "DenyUngovernedInference"
        for r in template["Resources"].values()
        if r["Type"] == "AWS::IAM::Policy"
        for s in r["Properties"]["PolicyDocument"]["Statement"]
    )


def test_runtime_takes_its_own_policy_and_keeps_the_deny(tmp_path):
    stack = _runtime(
        tmp_path,
        guardrail_policy=baseline_policy(
            {"NAME": "NONE", "ADDRESS": "NONE"}, floor=True, runtime=True
        ),
        guardrail_floor=True,
    )
    assert "ADDRESS" not in _entities(_guardrail(stack))  # NONE: not screened
    assert _has_deny(stack)


def test_production_runtime_baseline_carries_the_floor(tmp_path):
    assert floor_violations(_guardrail(_runtime(tmp_path, guardrail_floor=True))) == []


@pytest.mark.parametrize(
    "policy",
    [
        baseline_policy({"US_SOCIAL_SECURITY_NUMBER": "ANONYMIZE"}),
        {**baseline_policy(floor=True), "ContentPolicyConfig": {"FiltersConfig": []}},
    ],
    ids=["pii-floor", "prompt-attack"],
)
def test_production_refuses_a_runtime_policy_below_the_floor(tmp_path, policy):
    with pytest.raises(ValueError, match="production floor"):
        _runtime(tmp_path, guardrail_policy=policy, guardrail_floor=True)


# ── Every consumer runs on a published version, republished when the policy changes ──


def _version(**overrides) -> dict:
    from aws_cdk import aws_bedrock as bedrock

    stack = cdk.Stack(cdk.App(), "G", env=ENV)
    props = {
        "name": "g",
        "blocked_input_messaging": "in",
        "blocked_outputs_messaging": "out",
    } | overrides
    guardrail = bedrock.CfnGuardrail(stack, "Guardrail", **props)
    assert published_version(guardrail) is published_version(guardrail)
    (version,) = (
        Template.from_stack(stack)
        .find_resources("AWS::Bedrock::GuardrailVersion")
        .values()
    )
    return version


def test_a_policy_change_republishes_the_version():
    first = _version()
    assert first["UpdateReplacePolicy"] == "Retain"
    assert first["Properties"]["Description"].startswith("Policy sha256:")
    assert _version() == first
    assert (
        _version(blocked_outputs_messaging="other")["Properties"]["Description"]
        != first["Properties"]["Description"]
    )


def _consumers(template: dict) -> dict[str, object]:
    """Every *GUARDRAIL_VERSION environment value in a template, by resource."""
    found = {}
    for logical_id, r in template["Resources"].items():
        props = r.get("Properties", {})
        env = props.get("Environment", {}).get("Variables") or props.get(
            "EnvironmentVariables"
        )
        for key, value in (env or {}).items():
            if key.endswith("GUARDRAIL_VERSION"):
                found[f"{logical_id}.{key}"] = value
    return found


def test_the_gateway_and_runtime_consumers_name_the_published_version(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    stacks = [
        _gateway(),
        RuntimeStack(
            cdk.App(),
            "Rt",
            project_name="proj",
            environment="dev",
            component_name="agent",
            source_dir=str(tmp_path),
            require_guardrails=True,
            env=ENV,
        ),
    ]
    for stack in stacks:
        template = Template.from_stack(stack).to_json()
        versions = {
            k
            for k, r in template["Resources"].items()
            if r["Type"] == "AWS::Bedrock::GuardrailVersion"
        }
        consumers = _consumers(template)
        assert consumers, stack.stack_name
        for where, value in consumers.items():
            assert value["Fn::GetAtt"][0] in versions, (where, value)
