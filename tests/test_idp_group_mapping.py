# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Brokered-IdP groups become Cognito groups only through an explicit mapping.

Federated users carry no Cognito groups, so a use case's group-gated routes
never match under a brokered IdP. The pre token generation trigger adds the
groups a mapping names for the user's IdP values, and nothing else: unmapped
values are dropped, existing groups survive, native users are left alone, and
with no mapping there is no trigger at all.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from infra_utils import platform_config as pc
from infra_utils.idp_group_guard import idp_group_write_violations
from stacks.auth_stack import AuthStack
from stacks.gateway_stack import GatewayStack

HANDLER = (
    Path(__file__).resolve().parents[1] / "tools" / "pre_token_groups" / "handler.py"
)
_spec = importlib.util.spec_from_file_location("pre_token_groups", HANDLER)
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)

GROUP_MAP = {"g-approvers": ["tenant-a-approver"], "g-authors": ["tenant-a-author"]}
ENTRA = {"tenant_id": "t", "client_id": "c", "client_secret_name": "s"}


def _event(idp_groups, existing=None, provider="EntraID"):
    attributes = {"sub": "u"}
    if idp_groups is not None:
        attributes["custom:idp_groups"] = idp_groups
    if provider:
        attributes["identities"] = json.dumps([{"providerName": provider}])
    return {
        "version": "1",
        "triggerSource": "TokenGeneration_HostedAuth",
        "request": {
            "userAttributes": attributes,
            "groupConfiguration": {
                "groupsToOverride": existing or [],
                "iamRolesToOverride": ["arn:aws:iam::1:role/r"] if existing else [],
            },
        },
        "response": {"claimsOverrideDetails": None},
    }


def _run(monkeypatch, event, group_map=GROUP_MAP):
    monkeypatch.setenv("GROUP_MAP", json.dumps(group_map))
    monkeypatch.setenv("PROVIDER_NAME", "EntraID")
    return hook.handler(event, None)


def _groups(event):
    details = event["response"]["claimsOverrideDetails"]
    return details and details["groupOverrideDetails"]["groupsToOverride"]


@pytest.mark.parametrize(
    "raw",
    [
        '["g-approvers","g-unknown"]',
        "[g-approvers, g-unknown]",
        "g-approvers,g-unknown",
    ],
)
def test_mapped_values_add_groups_and_unmapped_values_drop(monkeypatch, raw):
    assert _groups(_run(monkeypatch, _event(raw))) == ["tenant-a-approver"]


def test_existing_cognito_groups_and_roles_are_kept(monkeypatch):
    out = _run(monkeypatch, _event('["g-authors"]', existing=["platform-ops"]))
    details = out["response"]["claimsOverrideDetails"]["groupOverrideDetails"]
    assert details["groupsToOverride"] == ["platform-ops", "tenant-a-author"]
    assert details["iamRolesToOverride"] == ["arn:aws:iam::1:role/r"]


def test_an_idp_value_cannot_mint_a_group_name(monkeypatch):
    # The value is a real group name, but not a key of the mapping.
    assert _groups(_run(monkeypatch, _event('["tenant-a-approver"]'))) is None


def test_no_mapping_changes_nothing(monkeypatch):
    event = _event('["g-approvers"]', existing=["platform-ops"])
    assert _run(monkeypatch, event, group_map={}) == _event(
        '["g-approvers"]', existing=["platform-ops"]
    )


def test_native_user_with_the_attribute_gets_nothing(monkeypatch):
    # A pool user can write custom:idp_groups itself; only the IdP may set it.
    assert _groups(_run(monkeypatch, _event('["g-approvers"]', provider=None))) is None
    other = _event('["g-approvers"]', provider="Okta")
    assert _groups(_run(monkeypatch, other)) is None


def test_missing_or_empty_attribute_changes_nothing(monkeypatch):
    for raw in (None, "", "[]"):
        assert _groups(_run(monkeypatch, _event(raw))) is None


# ── synth ──


def _synth(idp_type="entra_id", **kwargs):
    app = cdk.App()
    stack = AuthStack(
        app,
        "Auth",
        project_name="proj",
        environment="dev",
        idp_type=idp_type,
        idp_config=ENTRA if idp_type != "cognito" else None,
        env=cdk.Environment(account="111111111111", region="us-east-1"),
        **kwargs,
    )
    return Template.from_stack(stack).to_json()


def _of(template, kind):
    return [
        r["Properties"] for r in template["Resources"].values() if r["Type"] == kind
    ]


def _hook(template):
    return [
        f
        for f in _of(template, "AWS::Lambda::Function")
        if f.get("FunctionName") == "proj-dev-pre-token-groups"
    ]


def test_no_mapping_means_no_trigger_and_no_attribute():
    template = _synth()
    pool = _of(template, "AWS::Cognito::UserPool")[0]
    assert "LambdaConfig" not in pool
    assert not any(a["Name"] == "idp_groups" for a in pool.get("Schema", []))
    assert not _hook(template)


def test_mapping_wires_attribute_claim_and_v1_trigger():
    template = _synth(idp_group_claim="groups", idp_group_map=GROUP_MAP)
    pool = _of(template, "AWS::Cognito::UserPool")[0]
    attr = next(a for a in pool["Schema"] if a["Name"] == "idp_groups")
    assert attr["Mutable"] is True
    assert "PreTokenGeneration" in pool["LambdaConfig"]
    provider = _of(template, "AWS::Cognito::UserPoolIdentityProvider")[0]
    assert provider["AttributeMapping"]["custom:idp_groups"] == "groups"
    env = _hook(template)[0]["Environment"]["Variables"]
    assert json.loads(env["GROUP_MAP"]) == GROUP_MAP
    assert env["PROVIDER_NAME"] == "EntraID"


def test_claim_alone_maps_the_attribute_without_a_trigger():
    template = _synth(idp_group_claim="roles")
    assert "LambdaConfig" not in _of(template, "AWS::Cognito::UserPool")[0]
    provider = _of(template, "AWS::Cognito::UserPoolIdentityProvider")[0]
    assert provider["AttributeMapping"]["custom:idp_groups"] == "roles"


def test_mapping_without_an_external_idp_is_refused():
    with pytest.raises(ValueError, match="external IdP"):
        _synth(idp_type="cognito", idp_group_claim="groups")


# ── config ──

TENANT = pc.UseCaseManifest.model_validate(
    {
        "name": "tenant-a",
        "owner": "x",
        "summary": "s",
        "platform_interface": pc.PLATFORM_INTERFACE_VERSION,
        "stacks": ["uc-tenant-a"],
    }
)


@pytest.fixture
def with_tenant(monkeypatch):
    real = pc.discover_use_cases()
    monkeypatch.setattr(pc, "discover_use_cases", lambda: {**real, "tenant-a": TENANT})


def _config(group_map, use_cases=("tenant-a",), **identity):
    return pc.PlatformConfig.model_validate(
        {
            "identity": {
                "idp": "entra_id",
                "tenant_id": "11111111-2222-3333-4444-555555555555",
                "client_id": "c",
                "client_secret_name": "s",
                "group_claim": "groups",
                "group_map": group_map,
                **identity,
            },
            "deployment": {"platform_account": "123456789012"},
            "use_cases": {name: {} for name in use_cases},
        }
    )


def test_only_enabled_use_cases_contribute(with_tenant):
    mapping = {"tenant-a": {"g-approvers": ["tenant-a-approver"]}}
    assert _config(mapping).idp_group_map() == {"g-approvers": ["tenant-a-approver"]}
    assert _config(mapping, use_cases=()).idp_group_map() == {}


@pytest.mark.parametrize("group", ["approver", "tenant-b-approver", "tenant-a- x"])
def test_a_use_case_names_only_its_own_groups(with_tenant, group):
    with pytest.raises(ValueError, match="tenant-a-<group>"):
        _config({"tenant-a": {"g": [group]}})


def test_group_map_needs_a_claim_and_a_brokered_idp(with_tenant):
    with pytest.raises(ValueError, match="needs identity.group_claim"):
        _config({"tenant-a": {"g": ["tenant-a-x"]}}, group_claim="")
    with pytest.raises(ValueError, match="brokered external IdP"):
        pc.IdentityConfig.model_validate({"group_claim": "groups"})


# ── nobody writes custom:idp_groups for themselves ──
# Pre token generation reruns on refresh, so a user who could write the
# attribute could grant themselves any mapped group.


def _client(**props):
    return {
        "S": {
            "Resources": {
                "C": {"Type": "AWS::Cognito::UserPoolClient", "Properties": props}
            }
        }
    }


SAFE = {
    "WriteAttributes": ["custom:idp_groups"],
    "ExplicitAuthFlows": ["ALLOW_REFRESH_TOKEN_AUTH"],
    "RefreshTokenValidity": 720,
    "TokenValidityUnits": {"RefreshToken": "minutes"},
}


def test_guard_refuses_a_writing_client_that_issues_self_service_tokens():
    assert idp_group_write_violations(_client(**SAFE)) == []
    assert idp_group_write_violations(
        _client(**SAFE, AllowedOAuthScopes=["openid", "aws.cognito.signin.user.admin"])
    )
    assert idp_group_write_violations(
        _client(**{**SAFE, "ExplicitAuthFlows": ["ALLOW_USER_SRP_AUTH"]})
    )
    # Unset flows are Cognito's default, which includes SRP.
    assert idp_group_write_violations(_client(WriteAttributes=["custom:idp_groups"]))


def test_guard_refuses_a_writing_client_that_refreshes_past_12_hours():
    """Refresh keeps the attribute, so an IdP group removal waits for it to expire."""
    no_validity = {k: v for k, v in SAFE.items() if "Validity" not in k}
    assert idp_group_write_violations(_client(**no_validity))  # Cognito's 30 days
    assert idp_group_write_violations(_client(**{**SAFE, "RefreshTokenValidity": 721}))
    # Unset units are days.
    assert idp_group_write_violations(_client(**no_validity, RefreshTokenValidity=1))
    assert (
        idp_group_write_violations(
            _client(
                **no_validity,
                RefreshTokenValidity=12,
                TokenValidityUnits={"RefreshToken": "hours"},
            )
        )
        == []
    )


def test_guard_ignores_clients_that_cannot_write_the_attribute():
    # Unset WriteAttributes means standard attributes only.
    assert (
        idp_group_write_violations(
            _client(AllowedOAuthScopes=["aws.cognito.signin.user.admin"])
        )
        == []
    )
    assert idp_group_write_violations(_client(WriteAttributes=["email"])) == []


def test_platform_clients_pass_the_guard_and_only_user_clients_write_it():
    template = _synth(idp_group_claim="groups", idp_group_map=GROUP_MAP)
    assert idp_group_write_violations({"Auth": template}) == []
    writes = {
        c["ClientName"]: c.get("WriteAttributes")
        for c in _of(template, "AWS::Cognito::UserPoolClient")
    }
    # Federated sign-in needs write access to every mapped attribute.
    assert sorted(writes["proj-dev-app-client"]) == [
        "custom:idp_groups",
        "email",
        "name",
    ]
    assert sorted(writes["proj-dev-web-client"]) == [
        "custom:idp_groups",
        "email",
        "name",
    ]
    assert writes["proj-dev-m2m-client-v2"] is None
    refresh = {
        c["ClientName"]: (c.get("RefreshTokenValidity"), c.get("TokenValidityUnits"))
        for c in _of(template, "AWS::Cognito::UserPoolClient")
    }
    for user_client in ("proj-dev-app-client", "proj-dev-web-client"):
        assert refresh[user_client] == (720, {"RefreshToken": "minutes"})


def test_without_a_claim_the_platform_clients_are_unchanged():
    template = _synth()
    assert all(
        "WriteAttributes" not in c
        for c in _of(template, "AWS::Cognito::UserPoolClient")
    )


def test_use_case_gateway_clients_cannot_write_it():
    app = cdk.App()
    stack = GatewayStack(
        app,
        "Gw",
        project_name="proj",
        environment="dev",
        cognito_issuer_url="https://issuer.example.com",
        cognito_allowed_clients=["a"],
        user_pool_id="us-east-1_pool",
        use_case_clients={"tenant-a": {"machine": ["read"]}},
        env=cdk.Environment(account="111111111111", region="us-east-1"),
    )
    clients = _of(Template.from_stack(stack).to_json(), "AWS::Cognito::UserPoolClient")
    assert clients and all("WriteAttributes" not in c for c in clients)
