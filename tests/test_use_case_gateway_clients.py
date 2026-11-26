# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Use-case clients on the shared Gateway: own scopes, and only while enabled.

A use case declares gateway_clients in its manifest; the gateway stack creates
them on the platform pool (Cognito generates the ids, so a manifest cannot
carry them) and adds them to allowedClients. Two guards: every scope sits on
the use case's own resource server, never agentcore/invoke, and a disabled use
case's client leaves the list.
"""

from __future__ import annotations

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from infra_utils import platform_config as pc
from stacks.gateway_stack import GatewayStack

ENV = cdk.Environment(account="111111111111", region="us-east-1")

TENANT = pc.UseCaseManifest.model_validate(
    {
        "name": "tenant-a",
        "owner": "x",
        "summary": "declares a gateway client",
        "platform_interface": pc.PLATFORM_INTERFACE_VERSION,
        "requires": ["auth", "gateway"],
        "stacks": ["uc-tenant-a"],
        "gateway_clients": [{"name": "machine", "scopes": ["read", "write"]}],
    }
)


@pytest.fixture
def with_tenant(monkeypatch):
    real = pc.discover_use_cases()
    monkeypatch.setattr(pc, "discover_use_cases", lambda: {**real, "tenant-a": TENANT})


def _gateway(use_case_clients) -> dict:
    app = cdk.App()
    stack = GatewayStack(
        app,
        "Gw",
        project_name="proj",
        environment="dev",
        cognito_issuer_url="https://issuer.example.com",
        cognito_allowed_clients=["platform-app", "platform-m2m"],
        user_pool_id="us-east-1_pool",
        use_case_clients=use_case_clients,
        env=ENV,
    )
    return Template.from_stack(stack).to_json()


def _resources(template, kind):
    return [r for r in template["Resources"].values() if r["Type"] == kind]


def _allowed_clients(template) -> list:
    gw = _resources(template, "AWS::BedrockAgentCore::Gateway")[0]
    return gw["Properties"]["AuthorizerConfiguration"]["CustomJWTAuthorizer"][
        "AllowedClients"
    ]


def test_only_enabled_use_cases_contribute_clients(with_tenant):
    disabled = pc.PlatformConfig.model_validate({"use_cases": {"hello-platform": {}}})
    assert disabled.use_case_gateway_clients() == {}
    enabled = pc.PlatformConfig.model_validate({"use_cases": {"tenant-a": {}}})
    assert enabled.use_case_gateway_clients() == {
        "tenant-a": {"machine": ["read", "write"]}
    }


def test_a_disabled_use_cases_client_is_absent_from_allowed_clients(with_tenant):
    disabled = pc.PlatformConfig.model_validate({"use_cases": {}})
    template = _gateway(disabled.use_case_gateway_clients())
    assert _allowed_clients(template) == ["platform-app", "platform-m2m"]
    assert not _resources(template, "AWS::Cognito::UserPoolClient")

    enabled = pc.PlatformConfig.model_validate({"use_cases": {"tenant-a": {}}})
    template = _gateway(enabled.use_case_gateway_clients())
    clients = _allowed_clients(template)
    assert clients[:2] == ["platform-app", "platform-m2m"]
    assert len(clients) == 3 and isinstance(clients[2], dict)  # a Ref to the new client


def test_use_case_scopes_are_its_own_never_agentcore_invoke():
    template = _gateway({"tenant-a": {"machine": ["read", "write"]}})
    (server,) = _resources(template, "AWS::Cognito::UserPoolResourceServer")
    assert server["Properties"]["Identifier"] == "uc-tenant-a"
    (client,) = _resources(template, "AWS::Cognito::UserPoolClient")
    scopes = client["Properties"]["AllowedOAuthScopes"]
    assert scopes == ["uc-tenant-a/read", "uc-tenant-a/write"]
    assert client["Properties"]["AllowedOAuthFlows"] == ["client_credentials"]


def test_client_id_and_secret_name_are_published():
    template = _gateway({"tenant-a": {"machine": ["read"]}})
    names = {
        r["Properties"]["Name"] for r in _resources(template, "AWS::SSM::Parameter")
    }
    base = "/proj/dev/gateway/use-case-clients/tenant-a/machine"
    assert {f"{base}/client-id", f"{base}/client-secret-name"} <= names


@pytest.mark.parametrize("scope", ["agentcore/invoke", "invoke/x", "Read", ""])
def test_a_manifest_cannot_name_a_full_or_foreign_scope(scope):
    with pytest.raises(ValueError):
        pc.GatewayClient.model_validate({"name": "machine", "scopes": [scope]})


def test_clients_need_the_pool_and_the_gateway():
    with pytest.raises(ValueError, match="requires"):
        pc.UseCaseManifest.model_validate(
            {**TENANT.model_dump(), "requires": ["gateway"]}
        )


_DIRECT = {
    "idp": "entra_id",
    "mode": "direct",
    "tenant_id": "3f2b1c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
    "client_id": "9a8b7c6d-1234-4abc-9def-000111222333",
    "client_secret_name": "agentcore/idp-client-secret",
}
_APP = {"client_id": "11111111-2222-4333-8444-555555555555", "client_secret_name": "s"}


def test_direct_mode_needs_an_app_for_every_declared_client(with_tenant):
    with pytest.raises(ValueError, match="use_case_clients"):
        pc.PlatformConfig.model_validate(
            {"identity": _DIRECT, "use_cases": {"tenant-a": {}}}
        )
    with pytest.raises(ValueError, match="use_case_clients"):
        pc.PlatformConfig.model_validate(
            {
                "identity": {
                    **_DIRECT,
                    "use_case_clients": {"tenant-a": {"machine": _APP, "extra": _APP}},
                },
                "use_cases": {"tenant-a": {}},
            }
        )
    ok = pc.PlatformConfig.model_validate(
        {
            "deployment": {"platform_account": "111122223333"},
            "identity": {
                **_DIRECT,
                "use_case_clients": {"tenant-a": {"machine": _APP}},
            },
            "use_cases": {"tenant-a": {}},
        }
    )
    assert ok.use_case_client_apps() == {"tenant-a": {"machine": _APP}}


def test_brokered_mode_refuses_declared_apps(with_tenant):
    with pytest.raises(ValueError, match="direct"):
        pc.PlatformConfig.model_validate(
            {
                "identity": {"use_case_clients": {"tenant-a": {"machine": _APP}}},
                "use_cases": {"tenant-a": {}},
            }
        )


def test_direct_mode_publishes_declared_apps_and_creates_no_client():
    app = cdk.App()
    stack = GatewayStack(
        app,
        "Gw",
        project_name="proj",
        environment="dev",
        cognito_issuer_url="https://login.microsoftonline.com/t/v2.0",
        cognito_allowed_clients=[],
        allowed_audience=["platform-app"],
        use_case_client_apps={"tenant-a": {"machine": _APP}},
        env=ENV,
    )
    template = Template.from_stack(stack).to_json()
    assert not _resources(template, "AWS::Cognito::UserPoolClient")
    params = {
        r["Properties"]["Name"]: r["Properties"]["Value"]
        for r in _resources(template, "AWS::SSM::Parameter")
    }
    base = "/proj/dev/gateway/use-case-clients/tenant-a/machine"
    assert params[f"{base}/client-id"] == _APP["client_id"]
    assert params[f"{base}/client-secret-name"] == "s"
    authorizer = _resources(template, "AWS::BedrockAgentCore::Gateway")[0][
        "Properties"
    ]["AuthorizerConfiguration"]["CustomJWTAuthorizer"]
    assert authorizer["AllowedAudience"] == ["platform-app"]
    assert "AllowedClients" not in authorizer


def test_the_gateway_role_trusts_only_this_deployments_gateway():
    roles = [
        r
        for r in _resources(_gateway({}), "AWS::IAM::Role")
        if r["Properties"].get("RoleName") == "proj-dev-gateway-role"
    ]
    (statement,) = roles[0]["Properties"]["AssumeRolePolicyDocument"]["Statement"]
    assert statement["Principal"] == {"Service": "bedrock-agentcore.amazonaws.com"}
    assert statement["Condition"] == {
        "StringEquals": {"aws:SourceAccount": "111111111111"},
        "ArnLike": {
            "aws:SourceArn": "arn:aws:bedrock-agentcore:us-east-1:111111111111"
            ":gateway/proj-dev-gateway-*"
        },
    }


def _role_statements(template) -> list[dict]:
    return [
        st
        for p in _resources(template, "AWS::IAM::Policy")
        for st in p["Properties"]["PolicyDocument"]["Statement"]
    ]


@pytest.mark.parametrize("token_exchange", [False, True])
def test_token_exchange_grants_are_the_platforms_and_direct_only(token_exchange):
    app = cdk.App()
    stack = GatewayStack(
        app,
        "Gw",
        project_name="proj",
        environment="dev",
        cognito_issuer_url="https://login.microsoftonline.com/t/v2.0",
        cognito_allowed_clients=[],
        allowed_audience=["platform-app"],
        token_exchange=token_exchange,
        env=ENV,
    )
    statements = {
        st.get("Sid"): st
        for st in _role_statements(Template.from_stack(stack).to_json())
    }
    vault = statements.get("TokenExchangeVault")
    if not token_exchange:
        assert vault is None
        return
    # Measured on the OBO spike: the directory itself must be on the vault call.
    assert (
        "arn:aws:bedrock-agentcore:us-east-1:111111111111:workload-identity-directory/default"
        in vault["Resource"]
    )
    assert statements["TokenExchangeWorkloadToken"]["Action"] == [
        "bedrock-agentcore:GetWorkloadAccessToken",
        "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
    ]
