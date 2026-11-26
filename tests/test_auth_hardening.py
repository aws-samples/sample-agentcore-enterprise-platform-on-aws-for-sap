# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Synthesized Cognito behavior for workshop and enterprise brokered identity."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from stacks.auth_stack import AuthStack

ACCOUNT = "111111111111"
REGION = "us-east-1"


def _synth_auth(
    idp_type: str = "cognito",
    idp_config: dict[str, str] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    app = cdk.App()
    stack = AuthStack(
        app,
        f"Auth-{idp_type.replace('_', '-')}",
        project_name="agentcore",
        environment="test",
        idp_type=idp_type,
        idp_mode="brokered",
        idp_config=idp_config,
        env=cdk.Environment(account=ACCOUNT, region=REGION),
        **kwargs,
    )
    return Template.from_stack(stack).to_json()


def _resources(
    template: dict[str, Any],
    resource_type: str,
) -> list[dict[str, Any]]:
    return [
        resource
        for resource in template["Resources"].values()
        if resource["Type"] == resource_type
    ]


def _user_client(
    template: dict[str, Any],
    client_name: str,
) -> dict[str, Any]:
    return next(
        resource["Properties"]
        for resource in _resources(template, "AWS::Cognito::UserPoolClient")
        if resource["Properties"]["ClientName"] == client_name
    )


def test_cognito_only_keeps_native_auth_and_closes_signup_by_default() -> None:
    template = _synth_auth()
    user_pool = _resources(template, "AWS::Cognito::UserPool")[0]["Properties"]

    # Self-registration is off unless identity.self_sign_up opens it.
    assert user_pool["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"] is True

    app_client = _user_client(template, "agentcore-test-app-client")
    web_client = _user_client(template, "agentcore-test-web-client")

    assert app_client["SupportedIdentityProviders"] == ["COGNITO"]
    assert web_client["SupportedIdentityProviders"] == ["COGNITO"]
    assert "ALLOW_USER_PASSWORD_AUTH" in app_client["ExplicitAuthFlows"]
    assert "ALLOW_USER_SRP_AUTH" in app_client["ExplicitAuthFlows"]
    assert "ALLOW_USER_SRP_AUTH" in web_client["ExplicitAuthFlows"]


def test_self_sign_up_can_be_opened_without_an_idp() -> None:
    # identity.self_sign_up: true. A self-registered account is then a
    # signed-in user to every use case on the shared pool.
    template = _synth_auth(self_sign_up=True)
    user_pool = _resources(template, "AWS::Cognito::UserPool")[0]["Properties"]
    assert user_pool["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"] is False


def test_self_sign_up_setting_reaches_the_stack() -> None:
    from infra_utils.platform_config import (
        PlatformConfig,
        apply_environment_overrides,
        to_env,
    )

    assert to_env(PlatformConfig())["SELF_SIGN_UP"] == "false"
    on = PlatformConfig.model_validate({"identity": {"self_sign_up": True}})
    assert to_env(on)["SELF_SIGN_UP"] == "true"
    overridden = apply_environment_overrides(PlatformConfig(), {"SELF_SIGN_UP": "true"})
    assert overridden.identity.self_sign_up is True
    app_source = (Path(__file__).resolve().parents[1] / "app.py").read_text()
    assert 'cfg("self_sign_up", "SELF_SIGN_UP", "false")' in app_source
    assert "self_sign_up=self_sign_up," in app_source


def test_user_clients_register_no_localhost_callback_by_default() -> None:
    # Cognito accepts plain-HTTP localhost, so any local process listening on
    # the port could otherwise finish the code flow against the real pool.
    template = _synth_auth()
    for name in ("agentcore-test-app-client", "agentcore-test-web-client"):
        client = _user_client(template, name)
        assert "CallbackURLs" not in client and "LogoutURLs" not in client
        assert client["AllowedOAuthFlowsUserPoolClient"] is False
    domain = _resources(template, "AWS::Cognito::UserPoolDomain")[0]["Properties"]
    assert domain["ManagedLoginVersion"] == 2
    assert not _resources(template, "AWS::Cognito::ManagedLoginBranding")


def test_explicit_callback_urls_are_registered() -> None:
    template = _synth_auth(
        callback_urls=["https://app.example.com/cb"],
        logout_urls=["https://app.example.com"],
    )
    client = _user_client(template, "agentcore-test-web-client")
    assert client["CallbackURLs"] == ["https://app.example.com/cb"]
    # Managed login shows no page to a client without a branding style.
    clients = {
        logical
        for logical, r in template["Resources"].items()
        if r["Type"] == "AWS::Cognito::UserPoolClient"
        and r["Properties"].get("CallbackURLs")
    }
    brandings = [
        r["Properties"]
        for r in _resources(template, "AWS::Cognito::ManagedLoginBranding")
    ]
    assert len(clients) == 2
    assert {b["ClientId"]["Ref"] for b in brandings} == clients
    assert all(b["UseCognitoProvidedValues"] is True for b in brandings)


@pytest.mark.parametrize("field", ["localhost_callbacks", "managed_login"])
def test_the_removed_login_settings_are_refused(field: str) -> None:
    from pydantic import ValidationError

    from infra_utils.platform_config import PlatformConfig

    with pytest.raises(ValidationError, match=field):
        PlatformConfig.model_validate({"identity": {field: True}})


@pytest.mark.parametrize(
    ("idp_type", "idp_config", "provider_name"),
    [
        (
            "entra_id",
            {
                "tenant_id": "22222222-3333-4444-5555-666666666666",
                "client_id": "entra-client",
                "client_secret_name": "agentcore/entra-secret",
            },
            "EntraID",
        ),
        (
            "okta",
            {
                "issuer_url": "https://example.okta.com/oauth2/default",
                "client_id": "okta-client",
                "client_secret_name": "agentcore/okta-secret",
            },
            "Okta",
        ),
        (
            "ping",
            {
                "issuer_url": "https://auth.example.com",
                "client_id": "ping-client",
                "client_secret_name": "agentcore/ping-secret",
            },
            "PingIdentity",
        ),
    ],
)
def test_brokered_enterprise_idp_is_the_only_user_sign_in_provider(
    idp_type: str,
    idp_config: dict[str, str],
    provider_name: str,
) -> None:
    template = _synth_auth(idp_type, idp_config)
    user_pool = _resources(template, "AWS::Cognito::UserPool")[0]["Properties"]

    assert user_pool["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"] is True

    for client_name in (
        "agentcore-test-app-client",
        "agentcore-test-web-client",
    ):
        client = _user_client(template, client_name)
        assert client["SupportedIdentityProviders"] == [provider_name]
        assert "COGNITO" not in client["SupportedIdentityProviders"]
        assert "ALLOW_USER_PASSWORD_AUTH" not in client["ExplicitAuthFlows"]
        assert "ALLOW_USER_SRP_AUTH" not in client["ExplicitAuthFlows"]


@pytest.mark.parametrize(
    ("idp_type", "idp_config", "missing_key"),
    [
        (
            "entra_id",
            {
                "client_id": "entra-client",
                "client_secret_name": "agentcore/entra-secret",
            },
            "idp_tenant_id",
        ),
        (
            "okta",
            {
                "client_id": "okta-client",
                "client_secret_name": "agentcore/okta-secret",
            },
            "idp_issuer_url",
        ),
        (
            "ping",
            {
                "client_id": "ping-client",
                "client_secret_name": "agentcore/ping-secret",
            },
            "idp_issuer_url",
        ),
    ],
)
def test_incomplete_enterprise_idp_config_fails_closed(
    idp_type: str,
    idp_config: dict[str, str],
    missing_key: str,
) -> None:
    with pytest.raises(ValueError, match=missing_key):
        _synth_auth(idp_type, idp_config)


@pytest.mark.parametrize("idp_type", ["cognito", "entra_id", "okta", "ping"])
def test_public_web_client_uses_authorization_code_flow_for_pkce(
    idp_type: str,
) -> None:
    configs = {
        "cognito": None,
        "entra_id": {
            "tenant_id": "22222222-3333-4444-5555-666666666666",
            "client_id": "entra-client",
            "client_secret_name": "agentcore/entra-secret",
        },
        "okta": {
            "issuer_url": "https://example.okta.com/oauth2/default",
            "client_id": "okta-client",
            "client_secret_name": "agentcore/okta-secret",
        },
        "ping": {
            "issuer_url": "https://auth.example.com",
            "client_id": "ping-client",
            "client_secret_name": "agentcore/ping-secret",
        },
    }
    template = _synth_auth(
        idp_type,
        configs[idp_type],
        callback_urls=["https://app.example.com/cb"],
        logout_urls=["https://app.example.com"],
    )
    web_client = _user_client(template, "agentcore-test-web-client")

    assert web_client["GenerateSecret"] is False
    assert web_client["AllowedOAuthFlows"] == ["code"]
    assert web_client["AllowedOAuthFlowsUserPoolClient"] is True


def _ssm(template: dict[str, Any]) -> dict[str, Any]:
    return {
        r["Properties"]["Name"].rsplit("/auth/", 1)[1]: r["Properties"]["Value"]
        for r in _resources(template, "AWS::SSM::Parameter")
    }


def test_brokered_idp_provider_name_and_domain_url_are_published() -> None:
    # A use case's own client copies the platform's provider list from these.
    entra = _ssm(
        _synth_auth(
            "entra_id",
            {"tenant_id": "t", "client_id": "c", "client_secret_name": "s"},
        )
    )
    assert entra["idp-provider-name"] == "EntraID"
    assert entra["domain-url"] == (
        f"https://agentcore-test-{ACCOUNT}.auth.{REGION}.amazoncognito.com"
    )
    cognito_only = _ssm(_synth_auth())
    assert "idp-provider-name" not in cognito_only
    assert "domain-url" in cognito_only
