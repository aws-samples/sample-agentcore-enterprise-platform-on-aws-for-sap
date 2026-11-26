# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""gateway.people: a second Gateway for people, in its own stack, off by default.

What a deploy must create for a remote MCP client to connect (the clients were measured
by hand on the agent Gateway first): an authorization-code client with a secret and
short tokens, its managed-login style, the clients alone in the people Gateway's
allowed clients, a resource server named exactly that Gateway's URL, and the people
interceptor on REQUEST. The agent Gateway admits no remote client.
"""

from __future__ import annotations

import json
from pathlib import Path

import aws_cdk as cdk
import pytest
import yaml
from aws_cdk.assertions import Template
from pydantic import ValidationError

from infra_utils import platform_config as pc
from infra_utils import synth_guards
from stacks.gateway_stack import GatewayStack
from stacks.people_gateway_stack import PeopleGatewayStack

ENV = cdk.Environment(account="111111111111", region="us-east-1")
QUICK = {"name": "quick", "regions": ["us-east-1"]}
PEOPLE = {"people": {"enabled": True, "remote_clients": [QUICK]}}


def _app() -> cdk.App:
    # No bundling: the interceptor's wheels are pip's, and not what is tested here.
    return cdk.App(context={"aws:cdk:bundling-stacks": []})


def _people(remote_clients=None, providers=None, **kw) -> dict:
    stack = PeopleGatewayStack(
        _app(),
        "People",
        project_name="proj",
        environment="dev",
        cognito_issuer_url="https://issuer.example.com",
        user_pool_id="us-east-1_pool",
        remote_clients=remote_clients
        or [{"name": "quick", "callback_urls": ["https://cb.example/q"]}],
        remote_client_providers=providers,
        env=ENV,
        **kw,
    )
    return Template.from_stack(stack).to_json()


def _of(template, kind):
    return [
        r["Properties"] for r in template["Resources"].values() if r["Type"] == kind
    ]


def _config(**gateway):
    return pc.PlatformConfig.model_validate({"gateway": gateway})


# ── configuration ──


def test_off_by_default():
    cfg = pc.PlatformConfig()
    assert cfg.gateway.people.enabled is False
    assert "agentcore-workshop-dev-people-gateway" not in cfg.expected_stacks()


def test_on_adds_one_stack_after_the_agent_gateway():
    stacks = _config(**PEOPLE).expected_stacks()
    i = stacks.index("agentcore-workshop-dev-gateway")
    assert stacks[i + 1] == "agentcore-workshop-dev-people-gateway"


def test_the_old_key_is_refused_with_where_it_moved():
    with pytest.raises(ValidationError, match="gateway.people.remote_clients"):
        _config(remote_clients=[QUICK])


def test_enabled_needs_a_client_and_clients_need_enabled():
    with pytest.raises(ValidationError, match="at least one"):
        _config(people={"enabled": True})
    with pytest.raises(ValidationError, match="enabled is false"):
        _config(people={"remote_clients": [QUICK]})


@pytest.mark.parametrize(
    "entry",
    [
        {"name": "quick"},
        {"name": "quick", "regions": ["not-a-region"]},
        {"name": "quick", "regions": ["us-east-1"], "callbacks": ["https://x"]},
        {"name": "copilot-studio"},
        {"name": "copilot-studio", "callbacks": ["https://evil.example/redirect"]},
        {"name": "copilot-studio", "regions": ["us-east-1"]},
        {"name": "other", "regions": ["us-east-1"]},
    ],
)
def test_bad_entries_are_refused(entry):
    with pytest.raises(ValidationError):
        _config(people={"enabled": True, "remote_clients": [entry]})


def test_copilot_takes_its_connector_redirect_or_the_placeholder():
    cfg = _config(
        people={
            "enabled": True,
            "remote_clients": [
                {
                    "name": "copilot-studio",
                    "callbacks": [
                        pc.COPILOT_REDIRECT,
                        pc.COPILOT_REDIRECT + "/abc-123",
                    ],
                }
            ],
        }
    )
    assert cfg.gateway.people.remote_clients[0].callback_urls()[1].endswith("/abc-123")


def test_names_are_unique():
    with pytest.raises(ValidationError, match="unique"):
        _config(people={"enabled": True, "remote_clients": [QUICK, QUICK]})


def test_cedar_is_not_required():
    assert _config(**PEOPLE).gateway.people.enabled


def test_refused_in_direct_mode():
    with pytest.raises(ValidationError, match="brokered"):
        pc.PlatformConfig.model_validate(
            {
                "gateway": PEOPLE,
                "identity": {
                    "idp": "entra_id",
                    "mode": "direct",
                    "tenant_id": "t",
                    "client_id": "c",
                    "client_secret_name": "s",
                },
                "deployment": {"platform_account": "123456789012"},
            }
        )


def test_production_needs_the_acknowledgement():
    with pytest.raises(ValidationError) as e:
        pc.PlatformConfig.model_validate(
            {"gateway": PEOPLE, "deployment": {"mode": "production"}}
        )
    assert "allow_in_production" in str(e.value)
    acknowledged = {
        "people": {
            "enabled": True,
            "remote_clients": [{**QUICK, "allow_in_production": True}],
        }
    }
    with pytest.raises(ValidationError) as e:  # the other production controls
        pc.PlatformConfig.model_validate(
            {"gateway": acknowledged, "deployment": {"mode": "production"}}
        )
    assert "remote client" not in str(e.value)


# ── the stack ──


def test_the_client_is_short_lived_and_secret_kept_in_cognito():
    t = _people()
    (client,) = _of(t, "AWS::Cognito::UserPoolClient")
    assert client["GenerateSecret"] is True
    assert client["AllowedOAuthFlows"] == ["code"]
    assert client["CallbackURLs"] == ["https://cb.example/q"]
    assert set(client["AllowedOAuthScopes"]) == {"openid", "email", "profile"}
    assert client["EnableTokenRevocation"] is True
    assert client["SupportedIdentityProviders"] == ["COGNITO"]
    assert client["AccessTokenValidity"] == 15 and client["IdTokenValidity"] == 15
    assert client["RefreshTokenValidity"] == 8 * 60
    assert client["TokenValidityUnits"]["RefreshToken"] == "minutes"
    assert client["RefreshTokenRotation"]["Feature"] == "ENABLED"
    (branding,) = _of(t, "AWS::Cognito::ManagedLoginBranding")
    assert branding["UseCognitoProvidedValues"] is True
    params = [p["Name"] for p in _of(t, "AWS::SSM::Parameter")]
    assert "/proj/dev/people-gateway/remote-clients/quick/client-id" in params
    assert "ClientSecret" not in json.dumps(t)


def test_a_brokered_idp_is_the_only_sign_in():
    t = _people(providers=["EntraID"])
    (client,) = _of(t, "AWS::Cognito::UserPoolClient")
    assert client["SupportedIdentityProviders"] == ["EntraID"]


def test_gate_1_admits_only_the_remote_clients():
    t = _people(
        [
            {"name": "quick", "callback_urls": ["https://a/cb"]},
            {"name": "copilot-studio", "callback_urls": ["https://b/cb"]},
        ]
    )
    (gw,) = _of(t, "AWS::BedrockAgentCore::Gateway")
    jwt = gw["AuthorizerConfiguration"]["CustomJWTAuthorizer"]
    assert jwt["DiscoveryUrl"] == (
        "https://issuer.example.com/.well-known/openid-configuration"
    )
    clients = {
        k: r
        for k, r in t["Resources"].items()
        if r["Type"] == "AWS::Cognito::UserPoolClient"
    }
    assert sorted(c["Ref"] for c in jwt["AllowedClients"]) == sorted(clients)
    assert "AllowedAudience" not in jwt
    # No policy engine: the people Gateway carries only people targets.
    assert "PolicyEngineConfiguration" not in gw
    assert not _of(t, "AWS::BedrockAgentCore::PolicyEngine")


def test_the_token_is_issued_for_this_gateway_alone():
    t = _people()
    (rs,) = _of(t, "AWS::Cognito::UserPoolResourceServer")
    assert rs["Identifier"] == {"Fn::GetAtt": ["PeopleGateway", "GatewayUrl"]}
    assert rs["Scopes"] == [
        {"ScopeName": "people", "ScopeDescription": "Use the people Gateway"}
    ]


def test_gate_2_is_on_every_request_with_the_token_by_default():
    t = _people()
    (gw,) = _of(t, "AWS::BedrockAgentCore::Gateway")
    (ic,) = gw["InterceptorConfigurations"]
    assert ic["InterceptionPoints"] == ["REQUEST"]
    assert ic["InputConfiguration"]["PassRequestHeaders"] is True
    (fn,) = [
        f
        for f in _of(t, "AWS::Lambda::Function")
        if f.get("FunctionName") == "proj-dev-people-interceptor"
    ]
    env = fn["Environment"]["Variables"]
    assert env["ISSUER"] == "https://issuer.example.com"
    assert env["RESOURCE_PARAMETER"] == "/proj/dev/people-gateway/url"
    assert env["CLIENT_NAMES"] == "quick"
    assert "CLAIMS_PATH" not in env


def test_claims_from_the_gateways_input_turn_the_headers_off():
    t = _people(claims_path="mcp.gatewayRequest.context.claims")
    (gw,) = _of(t, "AWS::BedrockAgentCore::Gateway")
    assert gw["InterceptorConfigurations"][0]["InputConfiguration"] == {
        "PassRequestHeaders": False
    }
    (fn,) = [
        f
        for f in _of(t, "AWS::Lambda::Function")
        if f.get("FunctionName") == "proj-dev-people-interceptor"
    ]
    assert fn["Environment"]["Variables"]["CLAIMS_PATH"] == (
        "mcp.gatewayRequest.context.claims"
    )


@pytest.mark.parametrize(
    "path", ["mcp.gatewayRequest.headers", "mcp.gatewayRequest.body.x"]
)
def test_claims_are_never_read_from_what_the_client_writes(path):
    with pytest.raises(ValueError, match="client writes"):
        _people(claims_path=path)


def test_the_role_is_this_gateways_and_invokes_only_the_interceptor():
    t = _people()
    roles = {
        k: r["Properties"]
        for k, r in t["Resources"].items()
        if r["Type"] == "AWS::IAM::Role"
        and "bedrock-agentcore"
        in json.dumps(r["Properties"]["AssumeRolePolicyDocument"])
    }
    ((role_id, role),) = roles.items()
    (statement,) = role["AssumeRolePolicyDocument"]["Statement"]
    assert statement["Condition"]["ArnLike"]["aws:SourceArn"].endswith(
        ":gateway/proj-dev-people-gateway-*"
    )
    policies = [p for p in _of(t, "AWS::IAM::Policy") if {"Ref": role_id} in p["Roles"]]
    actions = [
        (s["Action"], json.dumps(s["Resource"]))
        for p in policies
        for s in p["PolicyDocument"]["Statement"]
    ]
    assert actions == [("lambda:InvokeFunction", actions[0][1])]
    assert "PeopleInterceptor" in actions[0][1]


def test_publishes_what_a_use_case_attaches_by():
    t = _people()
    params = {p["Name"] for p in _of(t, "AWS::SSM::Parameter")}
    for key in ("url", "id", "role-arn"):
        assert f"/proj/dev/people-gateway/{key}" in params


def test_refusals_are_counted():
    (mf,) = _of(_people(), "AWS::Logs::MetricFilter")
    assert (
        mf["MetricTransformations"][0]["MetricNamespace"] == "proj-dev/people-gateway"
    )
    assert mf["MetricTransformations"][0]["MetricName"] == "Refusals"


def test_the_people_role_is_held_to_a_use_cases_own_resources():
    scope = synth_guards.Scope(prefix="p-e", ssm_prefix="/p/e")
    template = {
        "Resources": {
            "Grant": {
                "Type": "AWS::IAM::Policy",
                "Properties": {
                    "Roles": ["p-e-people-gateway-role"],
                    "PolicyDocument": {
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": "lambda:InvokeFunction",
                                "Resource": "*",
                            }
                        ]
                    },
                },
            }
        }
    }
    assert synth_guards._shared_role({"uc": template}, scope)


def test_the_agent_gateway_takes_no_remote_client():
    stack = GatewayStack(
        cdk.App(),
        "Gw",
        project_name="proj",
        environment="dev",
        cognito_issuer_url="https://issuer.example.com",
        cognito_allowed_clients=["platform-app"],
        user_pool_id="us-east-1_pool",
        env=ENV,
    )
    t = Template.from_stack(stack).to_json()
    assert not _of(t, "AWS::Cognito::UserPoolClient")
    assert not _of(t, "AWS::Cognito::UserPoolResourceServer")
    (gw,) = _of(t, "AWS::BedrockAgentCore::Gateway")
    assert gw["AuthorizerConfiguration"]["CustomJWTAuthorizer"]["AllowedClients"] == [
        "platform-app"
    ]


# ── gateway.people.issuer: the organisation's own issuer ──

ISSUER = {
    "discovery_url": "https://idp.example.com/.well-known/openid-configuration",
    "clients": ["example-client"],
    "audience": "gateway",
    "groups": {"from": "claim", "claim": "groups"},
}


def _issuer(**over) -> dict:
    return {**ISSUER, **over}


def _people_with(issuer: dict, **gateway):
    return _config(people={"enabled": True, "issuer": issuer, **gateway})


def test_an_issuer_stands_in_for_the_remote_clients():
    cfg = _people_with(_issuer())
    assert cfg.gateway.people.issuer.groups.source == "claim"
    assert "agentcore-workshop-dev-people-gateway" in cfg.expected_stacks()


@pytest.mark.parametrize(
    "discovery_url",
    [
        "http://idp.example.com/.well-known/openid-configuration",
        "https://idp.example.com/oauth2/token",
        "https://idp.example.com/.well-known/openid-configuration?x=1",
        "idp.example.com/.well-known/openid-configuration",
    ],
)
def test_a_discovery_url_that_is_not_https_well_known_is_refused(discovery_url):
    with pytest.raises(ValidationError, match="discovery_url"):
        _people_with(_issuer(discovery_url=discovery_url))


def test_an_rfc_8414_discovery_url_is_taken():
    url = "https://idp.example.com/.well-known/oauth-authorization-server/tenant"
    assert _people_with(_issuer(discovery_url=url)).gateway.people.issuer


@pytest.mark.parametrize("clients", [[], [""], ["a", "a"], ["a,b"], [" a"]])
def test_bad_clients_are_refused(clients):
    with pytest.raises(ValidationError, match="clients"):
        _people_with(_issuer(clients=clients))


def test_an_empty_audience_is_refused():
    with pytest.raises(ValidationError, match="audience"):
        _people_with(_issuer(audience=""))


def test_groups_from_a_claim_need_the_claim():
    with pytest.raises(ValidationError, match="groups.claim"):
        _people_with(_issuer(groups={"from": "claim"}))


def test_groups_from_the_pool_need_a_verified_email():
    with pytest.raises(ValidationError, match="email_is_verified_by_issuer"):
        _people_with(_issuer(groups={"from": "pool"}))
    ok = _issuer(groups={"from": "pool", "email_is_verified_by_issuer": True})
    assert _people_with(ok).gateway.people.issuer.groups.source == "pool"


def test_an_issuer_and_remote_clients_are_refused_together():
    # One authorizer takes one discovery URL (CreateGateway's CustomJWTAuthorizer).
    with pytest.raises(ValidationError, match="not both"):
        _people_with(_issuer(), remote_clients=[QUICK])


def test_an_issuer_needs_the_gateway_on():
    with pytest.raises(ValidationError, match="enabled is false"):
        _config(people={"issuer": _issuer()})


def test_an_unknown_issuer_key_is_refused():
    with pytest.raises(ValidationError):
        _people_with(_issuer(client_secret="x"))


#: The platform's own direct-mode fixture: this file names no identity provider.
DIRECT_FIXTURE = Path(__file__).parent / "fixtures" / "contract" / "direct.yaml"


def _direct_raw(issuer) -> dict:
    raw = yaml.safe_load(DIRECT_FIXTURE.read_text())
    raw["gateway"] = {"people": {"enabled": True, "issuer": issuer}}
    return raw


def _direct(issuer):
    return pc.PlatformConfig.model_validate(_direct_raw(issuer))


def test_an_issuer_is_allowed_in_direct_mode():
    assert _direct(_issuer()).gateway.people.issuer


def test_pool_groups_are_refused_in_direct_mode():
    with pytest.raises(ValidationError, match="from: pool"):
        _direct(_issuer(groups={"from": "pool", "email_is_verified_by_issuer": True}))


def test_production_needs_the_issuers_acknowledgement():
    with pytest.raises(ValidationError, match="issuer"):
        pc.PlatformConfig.model_validate(
            {
                "gateway": {"people": {"enabled": True, "issuer": _issuer()}},
                "deployment": {"mode": "production"},
            }
        )
    with pytest.raises(ValidationError) as e:  # the other production controls
        pc.PlatformConfig.model_validate(
            {
                "gateway": {
                    "people": {
                        "enabled": True,
                        "issuer": _issuer(allow_in_production=True),
                    }
                },
                "deployment": {"mode": "production"},
            }
        )
    assert "allow_in_production" not in str(e.value)


def _issuer_stack(issuer=None, **kw) -> dict:
    model = pc.PeopleIssuer.model_validate(issuer or ISSUER)
    stack = PeopleGatewayStack(
        _app(),
        "People",
        project_name="proj",
        environment="dev",
        cognito_issuer_url="https://issuer.example.com",
        user_pool_id=kw.pop("user_pool_id", ""),
        remote_clients=[],
        issuer=model.model_dump(by_alias=True),
        env=ENV,
        **kw,
    )
    return Template.from_stack(stack).to_json()


def _interceptor_env(t) -> dict:
    (fn,) = [
        f
        for f in _of(t, "AWS::Lambda::Function")
        if f.get("FunctionName") == "proj-dev-people-interceptor"
    ]
    return fn["Environment"]["Variables"]


def test_gate_1_takes_the_issuers_discovery_url_and_clients_for_the_gateway_audience():
    t = _issuer_stack()
    (gw,) = _of(t, "AWS::BedrockAgentCore::Gateway")
    assert gw["AuthorizerConfiguration"]["CustomJWTAuthorizer"] == {
        "DiscoveryUrl": ISSUER["discovery_url"],
        "AllowedClients": ["example-client"],
    }


def test_gate_1_pins_a_literal_audience():
    t = _issuer_stack(_issuer(audience="api://people"))
    (gw,) = _of(t, "AWS::BedrockAgentCore::Gateway")
    assert gw["AuthorizerConfiguration"]["CustomJWTAuthorizer"] == {
        "DiscoveryUrl": ISSUER["discovery_url"],
        "AllowedAudience": ["api://people"],
    }


def test_an_issuer_creates_no_cognito_client_or_resource_server():
    t = _issuer_stack()
    assert not _of(t, "AWS::Cognito::UserPoolClient")
    assert not _of(t, "AWS::Cognito::UserPoolResourceServer")
    assert not _of(t, "AWS::Cognito::ManagedLoginBranding")
    params = {p["Name"] for p in _of(t, "AWS::SSM::Parameter")}
    assert not [p for p in params if "/remote-clients/" in p]
    for key in (
        "url",
        "id",
        "role-arn",
        "issuer/discovery-url",
        "issuer/clients",
        "issuer/audience",
    ):
        assert f"/proj/dev/people-gateway/{key}" in params


def test_the_interceptor_is_told_the_issuers_checks():
    env = _interceptor_env(_issuer_stack())
    assert env["DISCOVERY_URL"] == ISSUER["discovery_url"]
    assert env["ALLOWED_CLIENTS"] == "example-client"
    assert env["AUDIENCE"] == "gateway"
    assert env["GROUPS_FROM"] == "claim" and env["GROUPS_CLAIM"] == "groups"
    assert env["GROUP_MAP"] == "{}"
    for cognito_only in ("ISSUER", "CLIENT_NAMES", "USER_POOL_ID"):
        assert cognito_only not in env


def test_the_group_map_reaches_the_interceptor():
    env = _interceptor_env(
        _issuer_stack(group_map={"idp-authors": ["business-operations-authors"]})
    )
    assert json.loads(env["GROUP_MAP"]) == {
        "idp-authors": ["business-operations-authors"]
    }


def _interceptor_statements(t) -> list[dict]:
    roles = [
        k
        for k, r in t["Resources"].items()
        if r["Type"] == "AWS::IAM::Role" and k.startswith("PeopleInterceptorRole")
    ]
    return [
        s
        for p in _of(t, "AWS::IAM::Policy")
        if any({"Ref": r} in p["Roles"] for r in roles)
        for s in p["PolicyDocument"]["Statement"]
    ]


def test_only_pool_groups_give_the_interceptor_its_pool_lookup():
    claim = json.dumps(_interceptor_statements(_issuer_stack()))
    assert "cognito-idp" not in claim
    pool = _issuer_stack(
        _issuer(groups={"from": "pool", "email_is_verified_by_issuer": True}),
        user_pool_id="us-east-1_pool",
    )
    (lookup,) = [
        s for s in _interceptor_statements(pool) if "cognito-idp" in json.dumps(s)
    ]
    assert sorted(lookup["Action"]) == [
        "cognito-idp:AdminListGroupsForUser",
        "cognito-idp:ListUsers",
    ]
    assert "userpool/us-east-1_pool" in json.dumps(lookup["Resource"])
    assert _interceptor_env(pool)["USER_POOL_ID"] == "us-east-1_pool"


def test_pool_groups_need_the_pool():
    with pytest.raises(ValueError, match="user pool"):
        _issuer_stack(
            _issuer(groups={"from": "pool", "email_is_verified_by_issuer": True})
        )


def test_the_stack_refuses_both_or_neither():
    with pytest.raises(ValueError, match="not both"):
        _people(issuer=pc.PeopleIssuer.model_validate(ISSUER).model_dump(by_alias=True))
    with pytest.raises(ValueError, match="not both"):
        PeopleGatewayStack(
            _app(),
            "P",
            project_name="proj",
            environment="dev",
            cognito_issuer_url="https://issuer.example.com",
            user_pool_id="",
            remote_clients=[],
            env=ENV,
        )


def test_direct_mode_synthesizes_a_people_gateway_with_an_issuer():
    from app_synth import synth_app

    result, out = synth_app(yaml.safe_dump(_direct_raw(ISSUER)))
    assert result.returncode == 0, result.stderr[-3000:]
    t = json.loads(
        (out / "agentcore-workshop-dev-people-gateway.template.json").read_text()
    )
    (gw,) = _of(t, "AWS::BedrockAgentCore::Gateway")
    authorizer = gw["AuthorizerConfiguration"]["CustomJWTAuthorizer"]
    assert authorizer["DiscoveryUrl"] == ISSUER["discovery_url"]
    assert not _of(t, "AWS::Cognito::UserPoolClient")
