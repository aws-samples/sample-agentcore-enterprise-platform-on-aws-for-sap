# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The SAP layer: its config, the server's env-var contract, its stack and its check."""

from __future__ import annotations

import base64
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import aws_cdk as cdk
import pytest
import yaml

from infra_utils.use_case_context import UseCaseContext

REPO = Path(__file__).resolve().parents[3]
LAYER = REPO / "layers" / "sap"
sys.path.insert(0, str(LAYER / "lib"))
sys.path.insert(0, str(LAYER))

import sap_check
from sap_check import decide as check_decide
from sap_layer import access, names
from sap_layer.config import (
    IMAGE_REGISTRY_ACCOUNTS,
    SapConfig,
    SapSystem,
    image_registry_account,
)
from test_layers import synth, template

from sap_kit.names import RUNTIME_NAME_MAX

_spec = importlib.util.spec_from_file_location("sap_layer_stack", LAYER / "stack.py")
layer_stack = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(layer_stack)
runtime_env, static_tools = layer_stack.runtime_env, layer_stack.static_tools

# Every variable the AWS for SAP MCP Server reads, copied from its Configuration
# Reference (docs.aws.amazon.com/mcp-sap/latest/awsforsapmcp/configuration-reference.html,
# read 2026-11-02), plus MCP_SERVER_SAP_OAUTH_PROVIDER, the name its validation
# rule 3, AWS's template and the earlier system use where the table says
# MCP_SERVER_OAUTH_PROVIDER. Re-read the page when the pinned image tag moves.
REFERENCE_VARS = {
    "MCP_SERVER_SAP_BASE_URL",
    "MCP_SERVER_SAP_OAUTH_FLOW",
    "MCP_SERVER_BASIC_AUTH_SECRET_NAME",
    "MCP_SERVER_OAUTH_PROVIDER",
    "MCP_SERVER_SAP_OAUTH_PROVIDER",
    "MCP_SERVER_SAP_OAUTH_SCOPES",
    "MCP_SERVER_APP_CALLBACK_URL",
    "MCP_SERVER_NAME",
    "MCP_SERVER_LOG_LEVEL",
    "MCP_SERVER_REGION",
    "MCP_SERVER_SAP_SYSTEM",
    "MCP_SERVER_SAP_CLIENT_NUMBER",
    "MCP_SERVER_READ_ENABLED",
    "MCP_SERVER_WRITE_ENABLED",
    "MCP_SERVER_CREATE_ENABLED",
    "MCP_SERVER_UPDATE_ENABLED",
    "MCP_SERVER_DELETE_ENABLED",
    "MCP_SERVER_FUNCTION_IMPORT_ENABLED",
    "MCP_SERVER_CUSTOM_CATALOG_BUCKET",
    "MCP_SERVER_USE_SAP_CATALOG",
    "MCP_SERVER_ALLOWED_SERVICE_PREFIXES",
    "MCP_SERVER_ODATA_VERSION",
    "MCP_SERVER_ALLOWED_GROUP_PREFIXES",
    "MCP_SERVER_CUSTOM_CATALOG_ODATA_VERSION",
    "MCP_SERVER_SERVICE_HINTS_S3_URL",
    "MCP_SERVER_SESSION_VERIFY",
}

BASIC = {
    "system_role": "sandbox",
    "base_url": "https://sap.example.com/sap/opu/odata/sap/",
    "secret_name": "x/sap/s4",
}
M2M_SAP = {
    **BASIC,
    "flow": "m2m-sap",
    "token_url": "https://sap.example.com/sap/bc/sec/oauth2/token",
    "scopes": ["ZAPI_SALES_ORDER_SRV_0001", "ZAPI_X_0001"],
}


# ── Config ──


def test_a_basic_system_needs_a_url_and_a_secret():
    SapSystem(**BASIC)
    with pytest.raises(ValueError, match="needs secret_name"):
        SapSystem(system_role="sandbox", base_url=BASIC["base_url"])
    with pytest.raises(ValueError, match="https"):
        SapSystem(**{**BASIC, "base_url": "http://sap.example.com/"})


def test_the_old_landscape_key_names_its_new_name():
    old = {k: v for k, v in BASIC.items() if k != "system_role"}
    with pytest.raises(ValueError, match="`landscape` is now `system_role`"):
        SapSystem(**old, landscape="sandbox")


def test_m2m_flows_need_their_endpoint_and_scopes():
    SapSystem(**M2M_SAP)
    with pytest.raises(ValueError, match="scopes, token_url"):
        SapSystem(**BASIC, flow="m2m-sap")
    with pytest.raises(ValueError, match="discovery_url"):
        SapSystem(**BASIC, flow="m2m-oidc", scopes=["x/.default"])


def test_user_federation_is_not_a_flow():
    for flow in ("user-federation", "USER_FEDERATION", "ON_BEHALF_OF_TOKEN_EXCHANGE"):
        with pytest.raises(ValueError):
            SapSystem(**BASIC, flow=flow)


OBO = {
    **BASIC,
    "flow": "obo",
    "scopes": ["api://sap-app/SAP.Access"],
    "gateway_scopes": ["api://mcp-app/.default"],
}


def test_obo_needs_both_hops_scopes_and_writes_nothing_by_default():
    system = SapSystem(**OBO)
    assert system.writes == []
    assert SapSystem(**BASIC).writes == ["create", "update", "function_import"]
    assert SapSystem(**OBO, writes=["update"]).writes == ["update"]
    with pytest.raises(ValueError, match="needs gateway_scopes"):
        SapSystem(**{**OBO, "gateway_scopes": []})
    with pytest.raises(ValueError, match="for flow obo only"):
        SapSystem(**BASIC, gateway_scopes=["x"])


def test_delete_cannot_be_enabled():
    with pytest.raises(ValueError):
        SapSystem(**BASIC, writes=["create", "delete"])


def test_every_system_says_what_role_it_has_with_no_default():
    for system in (
        {k: v for k, v in BASIC.items() if k != "system_role"},
        {"runtime_arn": "arn:aws:bedrock-agentcore:us-west-2:111111111111:runtime/x"},
    ):
        with pytest.raises(ValueError, match="system_role is required: sandbox, dev"):
            SapSystem(**system)
    with pytest.raises(ValueError):
        SapSystem(**{**BASIC, "system_role": "prod"})
    for system_role in ("sandbox", "development", "quality", "production"):
        assert (
            SapSystem(**{**BASIC, "system_role": system_role}).system_role
            == system_role
        )


def test_bring_your_own_takes_a_runtime_arn_only():
    SapSystem(
        system_role="production",
        runtime_arn="arn:aws:bedrock-agentcore:us-west-2:111111111111:runtime/x-1",
    )
    with pytest.raises(ValueError, match="not an AgentCore runtime ARN"):
        SapSystem(system_role="production", runtime_arn="https://example.com/mcp")


def test_system_names_and_the_image_tag():
    SapConfig(systems={"s4-prod": BASIC})
    with pytest.raises(ValueError, match="lowercase"):
        SapConfig(systems={"S4_prod": BASIC})
    with pytest.raises(ValueError, match="at most 16"):
        SapConfig(systems={"s" * 17: BASIC})
    assert SapConfig(image_tag="latest").image_ref == ":latest"


def test_runtime_names_fit_and_differ_per_deployment():
    name = names.sap_runtime_name("abo", "hotel", "s" * 16)
    assert len(name) <= RUNTIME_NAME_MAX
    assert name != names.sap_runtime_name("abo", "alpha", "s" * 16)


def test_role_name_is_the_templates_shape():
    # The image repository admits pulls by this name only; the id is the
    # template's UniqueId rule.
    role = names.sap_role_name("abo", "hotel", "s" * 16)
    prefix, role_id = role[:35], role[35:]
    assert prefix == "AWSForSAP-MCP-Server-ExecutionRole-"
    assert re.fullmatch(r"[a-z0-9]{1,8}", role_id)
    assert role == names.sap_role_name("abo", "hotel", "s" * 16)
    assert role != names.sap_role_name("abo", "hotel", "s4")
    assert role != names.sap_role_name("abo", "alpha", "s" * 16)


def test_registry_account_per_region():
    # From each Region's public template; re-read them when AWS adds a Region.
    assert IMAGE_REGISTRY_ACCOUNTS == {
        "us-east-1": "754248135873",
        "us-east-2": "287871536835",
        "us-west-2": "659880308466",
        "ca-central-1": "002904791290",
        "sa-east-1": "890876258295",
        "eu-central-1": "968077439207",
        "eu-west-1": "288507842876",
        "eu-west-2": "398664673480",
        "eu-west-3": "544994275650",
        "eu-south-1": "177258422004",
        "eu-south-2": "167667035049",
        "eu-north-1": "457644036255",
        "ap-south-1": "252303747309",
        "ap-southeast-1": "846898690245",
        "ap-southeast-2": "595260646035",
        "ap-northeast-1": "581351185905",
        "ap-northeast-2": "847283080198",
    }
    with pytest.raises(ValueError, match="not published in me-central-1"):
        image_registry_account("me-central-1")


# ── The env-var contract ──


def test_every_variable_we_set_is_one_the_server_reads():
    for system in (
        SapSystem(**BASIC),
        SapSystem(**M2M_SAP, service_prefixes=["Z"]),
        SapSystem(**OBO),
        SapSystem(**BASIC, odata_version="v4", service_prefixes=["*"]),
        SapSystem(**BASIC, odata_version="mixed", catalog_bucket=CATALOG),
    ):
        env = runtime_env(system, region="us-west-2", provider_name="p")
        assert set(env) <= REFERENCE_VARS, set(env) - REFERENCE_VARS


def test_basic_env_names_the_secret_and_never_enables_delete():
    env = runtime_env(SapSystem(**BASIC, client="100"), region="r", provider_name=None)
    assert env["MCP_SERVER_SAP_OAUTH_FLOW"] == "BASIC"
    assert env["MCP_SERVER_BASIC_AUTH_SECRET_NAME"] == "x/sap/s4"
    assert env["MCP_SERVER_SAP_CLIENT_NUMBER"] == "100"
    assert env["MCP_SERVER_REGION"] == "r"
    assert env["MCP_SERVER_DELETE_ENABLED"] == "false"
    assert env["MCP_SERVER_SESSION_VERIFY"] == "true"
    assert "MCP_SERVER_SAP_OAUTH_PROVIDER" not in env


def test_no_writes_turns_the_master_switch_off():
    # The server refuses WRITE_ENABLED=true with no operation enabled.
    env = runtime_env(SapSystem(**BASIC, writes=[]), region="r", provider_name=None)
    assert env["MCP_SERVER_WRITE_ENABLED"] == "false"
    assert {
        env[f"MCP_SERVER_{op}_ENABLED"]
        for op in ("CREATE", "UPDATE", "FUNCTION_IMPORT")
    } == {"false"}


def test_m2m_env_names_the_provider_and_comma_joins_scopes():
    env = runtime_env(SapSystem(**M2M_SAP), region="r", provider_name="prov")
    assert env["MCP_SERVER_SAP_OAUTH_FLOW"] == "M2M"
    assert env["MCP_SERVER_SAP_OAUTH_PROVIDER"] == "prov"
    assert env["MCP_SERVER_SAP_OAUTH_SCOPES"] == "ZAPI_SALES_ORDER_SRV_0001,ZAPI_X_0001"
    assert "MCP_SERVER_BASIC_AUTH_SECRET_NAME" not in env


def test_obo_env_asks_the_provider_for_an_on_behalf_of_exchange():
    env = runtime_env(SapSystem(**OBO), region="r", provider_name="prov")
    assert env["MCP_SERVER_SAP_OAUTH_FLOW"] == "ON_BEHALF_OF_TOKEN_EXCHANGE"
    assert env["MCP_SERVER_SAP_OAUTH_PROVIDER"] == "prov"
    assert env["MCP_SERVER_SAP_OAUTH_SCOPES"] == "api://sap-app/SAP.Access"
    assert env["MCP_SERVER_WRITE_ENABLED"] == "false"


CATALOG = "awsforsap-mcp-server-catalog/dev"
_ODATA_VARS = (
    "MCP_SERVER_ODATA_VERSION",
    "MCP_SERVER_CUSTOM_CATALOG_BUCKET",
    "MCP_SERVER_CUSTOM_CATALOG_ODATA_VERSION",
)


def _odata_env(**kw) -> dict:
    env = runtime_env(SapSystem(**BASIC, **kw), region="r", provider_name=None)
    return {k: env[k] for k in _ODATA_VARS if k in env}


def test_odata_v2_is_the_servers_default_and_sets_nothing():
    # Unset, so every template from before the setting synthesizes unchanged.
    assert _odata_env() == _odata_env(odata_version="v2") == {}


def test_odata_v4_sets_the_version_and_needs_a_prefix_filter():
    assert _odata_env(odata_version="v4", service_prefixes=["*"]) == {
        "MCP_SERVER_ODATA_VERSION": "V4"
    }
    with pytest.raises(ValueError, match="v4 needs service_prefixes"):
        SapSystem(**BASIC, odata_version="v4")


def test_odata_mixed_reads_each_versions_service_from_a_custom_catalog():
    assert _odata_env(odata_version="mixed", catalog_bucket=CATALOG) == {
        "MCP_SERVER_CUSTOM_CATALOG_BUCKET": CATALOG,
        "MCP_SERVER_CUSTOM_CATALOG_ODATA_VERSION": "MIXED",
    }
    with pytest.raises(ValueError, match="mixed needs catalog_bucket"):
        SapSystem(**BASIC, odata_version="mixed")
    # A catalog on a V2 system is the server's default catalog version.
    assert _odata_env(catalog_bucket=CATALOG) == {
        "MCP_SERVER_CUSTOM_CATALOG_BUCKET": CATALOG
    }


@pytest.mark.parametrize(
    "kw, why",
    [
        ({"odata_version": "V4"}, "odata_version"),
        ({"odata_version": "v3"}, "odata_version"),
        ({"catalog_bucket": "s3://awsforsap-mcp-server-x"}, "without s3://"),
        ({"catalog_bucket": "my-catalog"}, "awsforsap-mcp-server-"),
    ],
)
def test_odata_settings_refuse_what_the_server_does_not_take(kw, why):
    with pytest.raises(ValueError, match=why):
        SapSystem(**BASIC, **kw)


def test_odata_settings_are_not_for_a_server_the_layer_does_not_build():
    with pytest.raises(ValueError, match="runtime_arn server's owner"):
        SapSystem(
            system_role="sandbox",
            runtime_arn="arn:aws:bedrock-agentcore:us-west-2:111111111111:runtime/x",
            writes=[],
            odata_version="mixed",
            catalog_bucket=CATALOG,
        )


def test_obo_static_tools_are_the_reads_plus_enabled_writes():
    listed = names.LISTED_READ_TOOLS
    assert sorted(t["name"] for t in static_tools(SapSystem(**OBO))) == sorted(listed)
    with_update = static_tools(SapSystem(**OBO, writes=["update"]))
    assert {t["name"] for t in with_update} == {*listed, "odata_update"}
    assert all(set(t) >= {"name", "description", "inputSchema"} for t in with_update)


def test_the_gateway_only_policy_denies_every_invoke_but_the_gateway_roles():
    allow, deny = layer_stack.gateway_only_policy("arn:rt", "arn:gw")["Statement"]
    assert allow["Effect"] == "Allow" and allow["Principal"] == {"AWS": "arn:gw"}
    assert deny["Effect"] == "Deny" and deny["Principal"] == {"AWS": "*"}
    assert deny["Action"] == layer_stack.RUNTIME_INVOKE_ACTIONS
    assert "bedrock-agentcore:InvokeAgentRuntime" in deny["Action"]
    assert deny["Condition"] == {"ArnNotEquals": {"aws:PrincipalArn": "arn:gw"}}
    # AgentCore refuses a policy whose Resource is not the runtime it is attached to.
    assert allow["Resource"] == deny["Resource"] == "arn:rt"


def test_create_only_names_keep_the_first_owners_seed_forever():
    # The runtime, role and providers were first created by business-operations.
    # These values are deployed; a change replaces every runtime and role.
    assert names.HASH_SEED == "business-operations"
    assert names.sap_runtime_name("abo", "hotel", "s4") == "bo_sap_s4_9c792ad7"
    assert names.sap_role_name("abo", "hotel", "s4") == (
        "AWSForSAP-MCP-Server-ExecutionRole-e9f1ad47"
    )
    assert names.sap_runtime_name("abo", "sapmig", "s4") == "bo_sap_s4_cf14b258"
    assert names.sap_role_name("abo", "sapmig", "s4").endswith("-79d0f801")


def test_sap_system_names_cap_at_16():
    assert names.SAP_SYSTEM_MAX == 16
    with pytest.raises(ValueError, match="at most 16 fit"):
        names.sap_target("s" * 17)


# ── Access: the operator's ceiling ──

SYSTEMS = {"s4": BASIC, "ecc": {**BASIC, "type": "ECC", "writes": ["update"]}}


def test_an_access_row_for_a_use_case_that_is_not_installed_fails_at_config_load():
    # The manifest's use_case_maps: [access] has the platform check the row's name.
    from infra_utils.platform_config import PlatformConfig

    with pytest.raises(
        ValueError, match=r"  - layers\.sap\.access\.no-such-use-case\n"
    ):
        PlatformConfig.model_validate(
            {"layers": {"sap": {"access": {"no-such-use-case": {"read": "*"}}}}}
        )


def test_the_check_leaves_every_other_target_alone_without_a_read():
    assert check_decide.TARGET_PREFIXES == (
        names.TARGET_PREFIX,
        names.LEGACY_TARGET_PREFIX,
    )

    def unread():
        raise AssertionError("read for another use case's call")

    for tool in ("another-use-case___odata_delete", "x___odata_read"):
        assert (
            check_decide.decide(
                tool=tool, settings=unread, claims=unread, caller=unread
            )
            is None
        )


def test_access_names_only_systems_and_writes_that_exist():
    SapConfig(
        systems=SYSTEMS, access={"uc-a": {"read": ["s4"], "write": {"ecc": ["update"]}}}
    )
    with pytest.raises(ValueError, match="unknown system"):
        SapConfig(systems=SYSTEMS, access={"uc-a": {"read": ["nope"]}})
    with pytest.raises(ValueError, match="not on in layers.sap.systems.ecc.writes"):
        SapConfig(systems=SYSTEMS, access={"uc-a": {"write": {"ecc": ["create"]}}})
    # A direct caller signs in as the service account: Basic or a token, never obo.
    cfg = SapConfig(systems={"m": M2M_SAP}, access={"uc-a": {"bypass_gateway": ["m"]}})
    assert cfg.direct_systems() == ["m"]
    assert cfg.systems["m"].direct_sign_in() == {
        "flow": "m2m-sap",
        "token_url": M2M_SAP["token_url"],
        "discovery_url": None,
        "scopes": M2M_SAP["scopes"],
    }
    assert SapSystem(**BASIC).direct_sign_in() == {}
    with pytest.raises(ValueError, match="must be a basic, m2m-sap or m2m-oidc"):
        SapConfig(systems={"o": OBO}, access={"uc-a": {"bypass_gateway": ["o"]}})


UNATTENDED = {
    "flow": "m2m-sap",
    "secret_name": "x/sap/oauth",
    "token_url": "https://sap.example.com:44301/sap/bc/sec/oauth2/token",
    "scopes": ["ZAPI_X_0001"],
}


def test_unattended_signs_direct_callers_in_whatever_the_servers_flow():
    # Absent: a direct caller signs in as the system's server does.
    assert SapSystem(**M2M_SAP).direct_secret_name == "x/sap/s4"
    for own in (BASIC, M2M_SAP, OBO):
        system = SapSystem(**own, unattended=UNATTENDED)
        assert system.flow == own.get("flow", "basic")  # the server's, unchanged
        assert system.direct_secret_name == "x/sap/oauth"
        assert system.direct_sign_in() == {
            "flow": "m2m-sap",
            "token_url": UNATTENDED["token_url"],
            "discovery_url": None,
            "scopes": ["ZAPI_X_0001"],
        }
    # Basic is its default: a password of its own, under a token-flow server.
    basic = SapSystem(**M2M_SAP, unattended={"secret_name": "x/sap/basic"})
    assert basic.direct_sign_in() == {}
    assert basic.direct_secret_name == "x/sap/basic"
    # An obo system may be called directly only with one.
    obo = {**OBO, "unattended": UNATTENDED}
    cfg = SapConfig(systems={"o": obo}, access={"uc-a": {"bypass_gateway": ["o"]}})
    assert cfg.direct_systems() == ["o"]
    with pytest.raises(ValueError, match="or have an unattended block"):
        SapConfig(systems={"o": OBO}, access={"uc-a": {"bypass_gateway": ["o"]}})
    # It is named, so the layer can warn that SAP no longer authorizes the person.
    assert cfg.obo_direct_systems() == ["o"]
    basic = SapConfig(systems={"b": BASIC}, access={"uc-a": {"bypass_gateway": ["b"]}})
    assert basic.obo_direct_systems() == []


@pytest.mark.parametrize(
    "unattended, why",
    [
        ({**UNATTENDED, "flow": "obo"}, "unattended.flow"),
        ({"flow": "m2m-sap", "secret_name": "u"}, "m2m-sap needs scopes, token_url"),
        ({"flow": "m2m-oidc", "secret_name": "u", "scopes": ["s"]}, "discovery_url"),
        ({"flow": "basic"}, "unattended.secret_name"),
        ({**UNATTENDED, "token_url": "http://sap.example.com/t"}, "https"),
        ({**UNATTENDED, "gateway_scopes": ["x"]}, "gateway_scopes"),
    ],
)
def test_unattended_is_checked_like_a_systems_own_sign_in(unattended, why):
    with pytest.raises(ValueError, match=why):
        SapSystem(**BASIC, unattended=unattended)


def test_bypass_gateway_has_no_old_name():
    s4 = {"s4": SYSTEMS["s4"]}
    cfg = SapConfig(systems=s4, access={"uc-a": {"bypass_gateway": ["s4"]}})
    assert cfg.direct_systems() == ["s4"]
    with pytest.raises(ValueError, match="direct"):
        SapConfig(systems=s4, access={"uc-a": {"direct": ["s4"]}})


def test_a_star_is_every_system_and_every_write_it_has_on():
    cfg = SapConfig(systems=SYSTEMS, access={"uc-a": {"read": "*", "write": "*"}})
    assert cfg.reads("uc-a") == ["ecc", "s4"]
    assert cfg.writes("uc-a") == {
        "ecc": ["update"],
        "s4": ["create", "update", "function_import"],
    }
    assert cfg.reads("not-named") == [] and cfg.writes("not-named") == {}


# ── Check: the claim gate (decide.py) ──

SETTINGS = {
    "cedar_enforced": False,
    "targets": {names.sap_target("s4"): "s4", names.sap_target("ecc"): "ecc"},
    "ceilings": {
        "uc-a": {"read": ["s4", "ecc"], "write": {"s4": ["update"]}},
        "uc-b": {"read": ["s4"], "write": {}},
    },
}
CLAIM_A = {
    "use_case": "uc-a",
    "callers": ["app-a"],
    "read": ["s4"],
    "write": {"s4": ["update"]},
    "governed_by": "uc_a_check",
}
CLAIM_B = {
    "use_case": "uc-b",
    "callers": ["app-b"],
    "read": ["s4"],
    "write": {},
    "governed_by": None,
}


def _decide(tool: str, caller: str, *, claims=(CLAIM_A, CLAIM_B), settings=SETTINGS):
    return check_decide.decide(
        tool=tool,
        settings=lambda: settings,
        claims=lambda: list(claims),
        caller=lambda: caller,
    )


def _tool(system: str, name: str) -> str:
    return f"{names.sap_target(system)}___{name}"


def _never():
    raise AssertionError("read when the call's name alone decides")


def test_an_unclaimed_client_is_refused_a_read_and_a_write():
    for name in ("odata_read", "odata_update"):
        reason = _decide(_tool("s4", name), "someone-else")
        assert reason == "no use case claims SAP access for this client"


def test_a_claimed_client_reads_and_writes_what_it_claims():
    assert _decide(_tool("s4", "odata_read"), "app-a") is None
    assert _decide(_tool("s4", "find_sap_services"), "app-a") is None
    assert _decide(_tool("s4", "odata_update"), "app-a") is None


def test_a_claim_covers_only_its_systems_and_operations():
    assert "does not claim read on SAP system 'ecc'" in _decide(
        _tool("ecc", "odata_read"), "app-a"
    )
    assert "does not claim create" in _decide(_tool("s4", "odata_create"), "app-a")


def test_the_ceiling_caps_a_claim_that_names_more():
    greedy = {**CLAIM_B, "write": {"s4": ["update"]}, "governed_by": "x"}
    assert "does not claim update" in _decide(
        _tool("s4", "odata_update"), "app-b", claims=[greedy]
    )
    outside = {**CLAIM_A, "use_case": "uc-z"}
    assert "does not claim read" in _decide(
        _tool("s4", "odata_read"), "app-a", claims=[outside]
    )


def test_odata_delete_is_refused_for_everyone_before_any_read():
    reason = check_decide.decide(
        tool=_tool("s4", "odata_delete"), settings=_never, claims=_never, caller=_never
    )
    assert reason.startswith("odata_delete is not available")


def test_an_unknown_target_or_tool_is_refused():
    assert "not a SAP target" in _decide(
        f"{names.TARGET_PREFIX}zz___odata_read", "app-a"
    )
    assert "not a SAP tool" in _decide(_tool("s4", "odata_merge"), "app-a")


def test_a_client_two_use_cases_claim_is_refused():
    both = {**CLAIM_B, "callers": ["app-a"]}
    assert "one owner" in _decide(
        _tool("s4", "odata_read"), "app-a", claims=[CLAIM_A, both]
    )


def test_an_ungoverned_write_passes_only_while_cedar_enforces():
    ungoverned = {**CLAIM_A, "governed_by": None}
    reason = _decide(_tool("s4", "odata_update"), "app-a", claims=[ungoverned])
    assert "only while Cedar enforces" in reason
    enforced = {**SETTINGS, "cedar_enforced": True}
    assert (
        _decide(
            _tool("s4", "odata_update"), "app-a", claims=[ungoverned], settings=enforced
        )
        is None
    )
    # A read needs no governing check.
    assert _decide(_tool("s4", "odata_read"), "app-a", claims=[ungoverned]) is None


def test_the_check_knows_the_tools_the_layer_names():
    assert (
        check_decide.READ_TOOLS
        == set(names.SAP_READ_TOOLS)
        == set(names.LISTED_READ_TOOLS)
    )
    assert check_decide.WRITE_OPS == {t: op for op, t in names.WRITE_TOOLS.items()}
    assert set(check_decide.WRITE_OPS) == set(names.SAP_WRITE_TOOLS)


# ── Check: its reads (__init__.py) ──


def _bearer(claims: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"Bearer h.{body}.s"


# How GetParameterHistory names a stack deploy's write (measured on a development deployment).
CLOUDFORMATION = (
    "arn:aws:sts::111111111111:assumed-role/"
    "cdk-hnb659fds-cfn-exec-role-111111111111-us-west-2/AWSCloudFormation"
)


class _Ssm:
    """get_parameter_history over a dict; a value starting with `!` is that error."""

    def __init__(self, values: dict):
        import botocore.exceptions

        self.values = values
        self.writers: dict[str, str] = {}
        self.calls = 0

        class exceptions:
            ClientError = botocore.exceptions.ClientError

            class ParameterNotFound(botocore.exceptions.ClientError):
                pass

        self.exceptions = exceptions

    def get_parameter_history(self, Name, MaxResults, NextToken=None):
        self.calls += 1
        v = self.values.get(Name)
        if v is None:
            raise self.exceptions.ParameterNotFound(
                {"Error": {"Code": "ParameterNotFound"}}, "GetParameterHistory"
            )
        if isinstance(v, str) and v.startswith("!"):
            raise self.exceptions.ClientError(
                {"Error": {"Code": v[1:]}}, "GetParameterHistory"
            )
        writer = self.writers.get(Name, CLOUDFORMATION)
        # Two pages, oldest first: the latest version is the one that counts.
        if NextToken is None:
            old = {"Version": 1, "Value": "{}", "LastModifiedUser": CLOUDFORMATION}
            return {"Parameters": [old], "NextToken": "t"}
        latest = {"Version": 2, "Value": json.dumps(v), "LastModifiedUser": writer}
        return {"Parameters": [latest]}


@pytest.fixture
def reads(monkeypatch):
    monkeypatch.setenv("SSM_PREFIX", "/p/e")
    monkeypatch.setenv(
        "CFN_EXEC_ROLE_ARN",
        "arn:aws:iam::111111111111:role/cdk-hnb659fds-cfn-exec-role-111111111111-us-west-2",
    )
    monkeypatch.setattr(sap_check, "_cache", None)
    fake = _Ssm(
        {
            "/p/e/layers/sap/check": SETTINGS,
            "/p/e/layers/sap/claims/uc-a": CLAIM_A,
        }
    )
    monkeypatch.setattr(sap_check, "_ssm", fake)
    return fake


def _call(tool: str, claims: dict) -> str | None:
    return sap_check.check(
        {"params": {"name": tool}}, {"Authorization": _bearer(claims)}
    )


def test_the_check_keys_on_client_id_or_azp(reads):
    assert _call(_tool("s4", "odata_update"), {"client_id": "app-a"}) is None
    # A person's Entra token: the calling app is its azp, the console app.
    assert (
        _call(_tool("s4", "odata_read"), {"azp": "app-a", "scp": "x", "oid": "1"})
        is None
    )
    refused = _call(_tool("s4", "odata_read"), {"client_id": "app-b"})
    assert refused.startswith("no use case claims SAP access for this client.")


def test_the_check_names_the_run_a_call_carries(reads, caplog):
    headers = {"Authorization": _bearer({"client_id": "app-a"})}
    with caplog.at_level("INFO"):
        sap_check.check({"params": {"name": _tool("s4", "odata_delete")}}, headers)
        sap_check.check(
            {"params": {"name": _tool("s4", "odata_delete")}},
            {**headers, "X-Audit-Correlation-Id": "run-1"},
        )
    assert [r.getMessage() for r in caplog.records] == [
        'interceptor run {"run_id": "run-1"}'
    ]


def test_an_absent_or_unreadable_claim_claims_nothing(reads):
    # uc-b has no claim parameter; with another use case denied, uc-a still works.
    reads.values["/p/e/layers/sap/check"] = {
        **SETTINGS,
        "ceilings": {**SETTINGS["ceilings"], "uc-c": {"read": ["s4"], "write": {}}},
    }
    reads.values["/p/e/layers/sap/claims/uc-c"] = "!AccessDeniedException"
    assert _call(_tool("s4", "odata_read"), {"client_id": "app-a"}) is None


@pytest.mark.parametrize(
    "writer",
    [
        "arn:aws:sts::111111111111:assumed-role/Admin/someone",
        # Its own session name, but not CloudFormation's role.
        "arn:aws:sts::111111111111:assumed-role/Admin/AWSCloudFormation",
        # CloudFormation's role in another account or Region.
        CLOUDFORMATION.replace("-111111111111-us-west-2", "-222222222222-us-west-2"),
        CLOUDFORMATION.replace("us-west-2", "us-east-1"),
        # Another account's own execution role, and another bootstrap qualifier's.
        CLOUDFORMATION.replace("111111111111", "222222222222"),
        CLOUDFORMATION.replace("hnb659fds", "other"),
    ],
)
def test_a_claim_no_stack_deploy_wrote_claims_nothing(reads, writer):
    reads.writers["/p/e/layers/sap/claims/uc-a"] = writer
    refused = _call(_tool("s4", "odata_read"), {"client_id": "app-a"})
    assert refused.startswith("no use case claims SAP access for this client.")


def test_settings_no_stack_deploy_wrote_refuse_every_call(reads):
    reads.writers["/p/e/layers/sap/check"] = "arn:aws:iam::111111111111:user/x"
    with pytest.raises(sap_check.Untrusted, match="last written by"):
        _call(_tool("s4", "odata_read"), {"client_id": "app-a"})


def test_any_other_failure_refuses_by_raising(reads):
    reads.values["/p/e/layers/sap/claims/uc-a"] = "!ThrottlingException"
    with pytest.raises(Exception, match="ThrottlingException"):
        _call(_tool("s4", "odata_read"), {"client_id": "app-a"})


def test_a_claim_in_another_use_cases_parameter_is_refused(reads):
    reads.values["/p/e/layers/sap/claims/uc-a"] = {**CLAIM_A, "use_case": "uc-b"}
    with pytest.raises(ValueError, match="another use case"):
        _call(_tool("s4", "odata_read"), {"client_id": "app-b"})


def test_a_token_naming_no_app_is_refused(reads):
    with pytest.raises(ValueError, match="no client_id or azp"):
        _call(_tool("s4", "odata_read"), {"sub": "x"})


def test_reads_are_kept_for_the_ttl(reads, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(sap_check.time, "monotonic", lambda: now[0])
    _call(_tool("s4", "odata_read"), {"client_id": "app-a"})
    first = reads.calls
    _call(_tool("s4", "odata_read"), {"client_id": "app-a"})
    assert reads.calls == first
    now[0] += sap_check.TTL_SECONDS
    _call(_tool("s4", "odata_read"), {"client_id": "app-a"})
    assert reads.calls > first


# ── Synth ──

PREFIX = "agentcore-workshop-dev"
LAYER_STACK = f"{PREFIX}-layer-sap"
CHECK = {
    "path": "layers/sap/sap_check",
    "target_prefixes": [names.TARGET_PREFIX],
    "request_headers": True,
}


def _config(sap: dict, **extra) -> dict:
    return {
        "security": {"cedar": {"enabled": True}, **extra.pop("security", {})},
        "gateway": {"interceptor_extensions": [CHECK, *extra.pop("extensions", [])]},
        "layers": {"sap": sap},
        **extra,
    }


def _layer(tmp_path: Path, sap: dict, **extra) -> dict:
    # tests/fixtures installs uc-a and uc-b, the use cases the access rows name.
    result = synth(
        tmp_path, _config(sap, **extra), use_cases=LAYER / "tests" / "fixtures"
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return template(tmp_path, LAYER_STACK)["Resources"]


def _of(resources: dict, kind: str) -> list[dict]:
    return [
        r["Properties"]
        for r in resources.values()
        if r["Type"] == f"AWS::BedrockAgentCore::{kind}"
    ]


def _param(resources: dict, suffix: str):
    (value,) = [
        r["Properties"]["Value"]
        for r in resources.values()
        if r["Type"] == "AWS::SSM::Parameter"
        and r["Properties"]["Name"].endswith(f"/layers/sap/{suffix}")
    ]
    return value


def test_synth_builds_one_runtime_and_target_per_system(tmp_path):
    resources = _layer(
        tmp_path,
        {
            "systems": {"s4": BASIC, "s4m": M2M_SAP},
            "access": {"uc-a": {"read": "*", "write": "*"}},
        },
    )
    runtimes = _of(resources, "Runtime")
    assert len(runtimes) == 2
    for rt in runtimes:
        uri = rt["AgentRuntimeArtifact"]["ContainerConfiguration"]["ContainerUri"]
        assert uri.endswith(":latest") and "754248135873" in uri
        assert "AuthorizerConfiguration" not in rt  # SigV4 inbound
        assert rt["NetworkConfiguration"] == {"NetworkMode": "PUBLIC"}
    # The endpoint is the runtime's ARN URL-encoded, with every token resolved:
    # an encoded placeholder ("%24%7BToken") reaches the Gateway as a bad URL
    # and fails as "Authorization error".
    endpoint = json.dumps(
        _of(resources, "GatewayTarget")[0]["TargetConfiguration"]["Mcp"]["McpServer"]
    )
    assert "Token" not in endpoint and "%3Aruntime%2F" in endpoint
    targets = sorted(t["Name"] for t in _of(resources, "GatewayTarget"))
    assert targets == [names.sap_target("s4"), names.sap_target("s4m")]
    # Each runtime refuses every principal but the Gateway's role, whatever IAM
    # allows elsewhere in the account; and the target is created only once it does.
    rt_policies = _of(resources, "ResourcePolicy")
    assert len(rt_policies) == 2
    for p in rt_policies:
        text = json.dumps(p["Policy"])
        assert "DenyEveryoneElse" in text and "ArnNotEquals" in text
        assert "aws:PrincipalArn" in text and "gatewayrolearn" in text
    for key, r in resources.items():
        if r["Type"] == "AWS::BedrockAgentCore::GatewayTarget":
            assert any(d.startswith("RuntimePolicy") for d in r["DependsOn"]), key
    (provider,) = _of(resources, "OAuth2CredentialProvider")
    custom = provider["Oauth2ProviderConfigInput"]["CustomOauth2ProviderConfig"]
    assert custom["ClientSecretConfig"] == {
        "SecretId": "x/sap/s4",
        "JsonKey": "clientSecret",
    }
    assert "ClientSecret" not in custom
    # The runtime role reads its own secret, never another's, and mints tokens
    # only from its own provider.
    policies = {
        k: r["Properties"]
        for k, r in resources.items()
        if r["Type"] == "AWS::IAM::Policy"
    }
    text = json.dumps(policies)
    assert "secret:x/sap/s4-??????" in text and "bedrock-agentcore*" not in text
    (grant,) = [
        st
        for p in policies.values()
        for st in p["PolicyDocument"]["Statement"]
        if "bedrock-agentcore:GetResourceOauth2Token" in json.dumps(st["Action"])
    ]
    assert '"*"' not in json.dumps(grant["Resource"])
    assert "token-vault/default" in json.dumps(grant["Resource"])
    # No Cedar statement: each use case permits its own clients.
    assert not _of(resources, "Policy")


def test_policies_never_take_the_names_the_retained_copies_have(tmp_path):
    # An inline policy's name is its logical id. The migration leaves the use case
    # stack's copies attached (RolePolicy-<system>, SapGatewayRole's) until these
    # replace them, so the same name would overwrite, then delete, the new one.
    resources = _layer(tmp_path, {"systems": {"s4": BASIC}})
    names_ = {
        r["Properties"]["PolicyName"]
        for r in resources.values()
        if r["Type"] == "AWS::IAM::Policy"
    }
    assert not [
        n
        for n in names_
        if n.startswith(("RolePolicys4", "SapGatewayRole", "GatewayRole"))
    ]
    runtime = next(r for k, r in resources.items() if k == "Runtimes4")
    assert runtime["DependsOn"] == [
        n for n in names_ if n.startswith("LayerRolePolicys4")
    ]


def test_the_runtime_log_group_gets_the_platforms_retention(tmp_path):
    resources = _layer(tmp_path, {"systems": {"s4": BASIC}})
    (name,) = [
        json.dumps(r["Properties"]["LogGroupName"])
        for r in resources.values()
        if r["Type"] == "Custom::LogRetention"
    ]
    assert "-DEFAULT" in name and "Runtimes4" in name


def test_synth_publishes_systems_connections_and_the_checks_settings(tmp_path):
    resources = _layer(
        tmp_path,
        {
            "systems": {"s4": {**BASIC, "client": "100"}, "s4m": M2M_SAP},
            "access": {
                "uc-a": {
                    "read": ["s4"],
                    "write": {"s4": ["update"]},
                    "bypass_gateway": ["s4"],
                }
            },
        },
    )
    systems = json.loads(_param(resources, "systems"))
    assert systems["s4"]["target"] == names.sap_target("s4")
    assert names.sap_target("s4") + "___odata_update" in systems["s4"]["tools"]
    assert not [t for t in systems["s4"]["tools"] if "delete" in t]
    assert json.loads(_param(resources, "systems/s4/connection")) == {
        "base_url": BASIC["base_url"],
        "client": "100",
        "secret_name": "x/sap/s4",
    }
    # Only a system some use case may call directly has its connection published.
    assert not [
        r
        for r in resources.values()
        if "s4m/connection" in json.dumps(r.get("Properties", {}))
    ]
    settings = json.loads(_param(resources, "check"))
    assert settings == {
        "cedar_enforced": False,
        "targets": {names.sap_target("s4"): "s4", names.sap_target("s4m"): "s4m"},
        "ceilings": {"uc-a": {"read": ["s4"], "write": {"s4": ["update"]}}},
    }
    # The interceptor's role reads the settings, the layer's own parameter.
    grants = [
        r["Properties"]
        for r in resources.values()
        if r["Type"] == "AWS::IAM::Policy"
        and "interceptorrolearn" in json.dumps(r["Properties"]["Roles"])
    ]
    assert grants and "Outcheck" in json.dumps(grants)


TOKEN_44300 = "https://sap.example.com:44300/sap/bc/sec/oauth2/token"


def test_synth_in_the_platform_vpc_and_with_no_systems(tmp_path):
    resources = _layer(
        tmp_path,
        {
            "systems": {"s4": BASIC, "s4m": {**M2M_SAP, "token_url": TOKEN_44300}},
            "access": {"uc-a": {"bypass_gateway": ["s4", "s4m"]}},
        },
        security={"networking": True},
    )
    assert {
        r["NetworkConfiguration"]["NetworkMode"] for r in _of(resources, "Runtime")
    } == {"VPC"}
    (sg,) = [r for r in resources.values() if r["Type"] == "AWS::EC2::SecurityGroup"]
    # A direct caller fetches a token flow's token itself, so its port is opened too.
    egress = sg["Properties"]["SecurityGroupEgress"]
    assert [e["FromPort"] for e in egress] == [443, 44300]
    # A direct caller of the use case's own reaches SAP through the same group.
    assert "SapSecurityGroup" in json.dumps(
        _param(resources, "egress-security-group-id")
    )

    # The shell the migration deploys first: no system, and still the parameters
    # a moved runtime and target reference, which a refactor cannot add.
    (tmp_path / "shell").mkdir()
    result = synth(tmp_path / "shell", _config({"systems": {}}))
    assert result.returncode == 0, result.stderr[-2000:]
    shell = template(tmp_path / "shell", LAYER_STACK)
    assert [r["Type"] for r in shell["Resources"].values()] == ["AWS::SSM::Parameter"]
    defaults = {p.get("Default") for p in shell["Parameters"].values()}
    assert {
        "/agentcore-workshop/dev/gateway/id",
        "/agentcore-workshop/dev/gateway/role-arn",
    } <= defaults


def test_synth_publishes_and_opens_the_unattended_sign_in(tmp_path):
    resources = _layer(
        tmp_path,
        {
            "systems": {"s4": {**BASIC, "unattended": UNATTENDED}},
            "access": {"uc-a": {"bypass_gateway": ["s4"]}},
        },
        security={"networking": True},
    )
    assert json.loads(_param(resources, "systems/s4/connection")) == {
        "base_url": BASIC["base_url"],
        "client": None,
        "secret_name": "x/sap/oauth",
        **{k: UNATTENDED[k] for k in ("flow", "token_url", "scopes")},
        "discovery_url": None,
    }
    (sg,) = [r for r in resources.values() if r["Type"] == "AWS::EC2::SecurityGroup"]
    egress = sg["Properties"]["SecurityGroupEgress"]
    assert [e["FromPort"] for e in egress] == [443, 44301]
    # The server still signs in with the system's own flow and secret.
    (rt,) = _of(resources, "Runtime")
    env = rt["EnvironmentVariables"]
    assert env["MCP_SERVER_SAP_OAUTH_FLOW"] == "BASIC"
    assert env["MCP_SERVER_BASIC_AUTH_SECRET_NAME"] == "x/sap/s4"


def test_the_image_may_be_pinned_by_digest(tmp_path):
    digest = "sha256:" + "a1" * 32
    sap = {"image_tag": digest, "systems": {"s4": BASIC}}
    (rt,) = _of(_layer(tmp_path, sap, region="us-west-2"), "Runtime")
    uri = rt["AgentRuntimeArtifact"]["ContainerConfiguration"]["ContainerUri"]
    assert uri == f"659880308466.dkr.ecr.us-west-2.amazonaws.com/aws-sap-mcp@{digest}"
    assert SapConfig().image_ref == ":latest"
    assert (
        SapConfig(image_tag="1.1.0-958").image_ref == ":1.1.0-958"
    )  # a pin still works
    with pytest.raises(ValueError, match="not a sha256 digest"):
        SapConfig(image_tag="sha256:abc")


def _production_layer(tmp_path: Path, sap: dict):
    import aws_cdk as cdk

    from infra_utils.platform_config import PlatformConfig

    platform = PlatformConfig.model_validate(
        {
            "project": "acme",
            "environment": "prd",
            "region": "us-west-2",
            "security": {"cedar": {"enabled": True}},
            "gateway": {"interceptor_extensions": [CHECK]},
        }
    )
    ctx = UseCaseContext(
        "acme",
        "prd",
        platform,
        production=True,
        region="us-west-2",
        cdk_env=cdk.Environment(account="111111111111", region="us-west-2"),
    )
    return layer_stack.build(cdk.App(outdir=str(tmp_path / "cdk.out")), ctx, sap)


def test_production_refuses_the_latest_image(tmp_path):
    # The runtime holds the SAP credentials: production runs a server someone pinned.
    with pytest.raises(ValueError, match="image_tag is latest"):
        _production_layer(tmp_path, {"systems": {"s4": BASIC}})
    _production_layer(tmp_path, {"image_tag": "1.1.0-1042", "systems": {"s4": BASIC}})
    # No system, no runtime, nothing to pin.
    _production_layer(tmp_path, {})


def test_verify_in_production_fails_when_no_banner_was_read():
    spec = importlib.util.spec_from_file_location("sap_verify", LAYER / "verify.py")
    verify = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verify)
    assert "rt-1" in verify.unread_banner_failure(["rt-1"], "production")
    assert verify.unread_banner_failure(["rt-1"], "workshop") is None
    assert verify.unread_banner_failure([], "production") is None


def test_synth_pulls_from_the_regions_registry_and_refuses_an_unlisted_region(tmp_path):
    sap = {"systems": {"s4": BASIC}}
    (rt,) = _of(_layer(tmp_path, sap, region="us-west-2"), "Runtime")
    uri = rt["AgentRuntimeArtifact"]["ContainerConfiguration"]["ContainerUri"]
    assert uri.startswith("659880308466.dkr.ecr.us-west-2.amazonaws.com/aws-sap-mcp:")
    (tmp_path / "x").mkdir()
    result = synth(tmp_path / "x", _config(sap, region="me-central-1"))
    assert result.returncode != 0
    assert "not published in me-central-1" in result.stderr


def test_a_layer_with_systems_refuses_to_synth_without_its_check(tmp_path):
    config = _config({"systems": {"s4": BASIC}})
    config["gateway"]["interceptor_extensions"] = []
    result = synth(tmp_path, config)
    assert result.returncode != 0 and "needs its interceptor check" in result.stderr
    # A prefix wider than the SAP targets would refuse every other tenant's calls.
    (tmp_path / "wide").mkdir()
    config["gateway"]["interceptor_extensions"] = [{**CHECK, "target_prefixes": ["s"]}]
    result = synth(tmp_path / "wide", config)
    assert result.returncode != 0 and "needs its interceptor check" in result.stderr


def test_an_obo_system_called_directly_warns_at_synth(tmp_path):
    import aws_cdk as cdk
    import aws_cdk.assertions

    from infra_utils.platform_config import PlatformConfig

    platform = PlatformConfig.model_validate(
        {
            "project": "acme",
            "environment": "dev",
            "region": "us-west-2",
            "deployment": {"platform_account": "111111111111"},
            "identity": DIRECT,
            "security": {"cedar": {"enabled": True}},
            "gateway": {"interceptor_extensions": [CHECK]},
        }
    )
    ctx = UseCaseContext(
        "acme",
        "dev",
        platform,
        region="us-west-2",
        cdk_env=cdk.Environment(account="111111111111", region="us-west-2"),
    )
    sap = {
        "systems": {"sb2": {**OBO, "unattended": UNATTENDED}},
        "access": {"uc-a": {"bypass_gateway": ["sb2"]}},
    }
    stack = layer_stack.build(cdk.App(outdir=str(tmp_path / "cdk.out")), ctx, sap)
    aws_cdk.assertions.Annotations.from_stack(stack).has_warning(
        "*", aws_cdk.assertions.Match.string_like_regexp("SAP system sb2 .*read-only")
    )


def _obo_stack(tmp_path: Path, identity: dict) -> dict:
    """The layer stack alone with an obo system, without the rest of app.py."""
    import aws_cdk as cdk
    import aws_cdk.assertions

    from infra_utils.platform_config import PlatformConfig

    platform = PlatformConfig.model_validate(
        {
            "project": "acme",
            "environment": "dev",
            "region": "us-west-2",
            "deployment": {"platform_account": "111111111111"},
            "identity": identity,
            "security": {"cedar": {"enabled": True}},
            "gateway": {"interceptor_extensions": [CHECK]},
        }
    )
    app = cdk.App(outdir=str(tmp_path / "cdk.out"))
    ctx = UseCaseContext(
        "acme",
        "dev",
        platform,
        region="us-west-2",
        cdk_env=cdk.Environment(account="111111111111", region="us-west-2"),
    )
    stack = layer_stack.build(app, ctx, {"systems": {"sb2": OBO, "s4": BASIC}})
    return aws_cdk.assertions.Template.from_stack(stack).to_json()["Resources"]


DIRECT = {
    "idp": "entra_id",
    "mode": "direct",
    "tenant_id": "tenant-1",
    "client_id": "platform-app",
    "client_secret_name": "abo/idp-secret",
}


def test_synth_obo_exchanges_at_the_gateway_and_again_at_the_server(tmp_path):
    resources = _obo_stack(tmp_path, DIRECT)
    by_name = {t["Name"]: t for t in _of(resources, "GatewayTarget")}
    obo = by_name[names.sap_target("sb2")]
    (cred,) = obo["CredentialProviderConfigurations"]
    assert cred["CredentialProviderType"] == "OAUTH"
    oauth = cred["CredentialProvider"]["OauthCredentialProvider"]
    assert oauth["GrantType"] == "TOKEN_EXCHANGE"
    assert oauth["Scopes"] == ["api://mcp-app/.default"]
    server = obo["TargetConfiguration"]["Mcp"]["McpServer"]
    # Listing needs a user token, so the tool list is static, never synchronized.
    assert "ListingMode" not in server
    listed = json.loads(server["McpToolSchema"]["InlinePayload"])["tools"]
    assert "odata_read" in {t["name"] for t in listed}
    basic = by_name[names.sap_target("s4")]
    assert basic["CredentialProviderConfigurations"][0]["CredentialProviderType"] == (
        "GATEWAY_IAM_ROLE"
    )

    providers = {p["Name"]: p for p in _of(resources, "OAuth2CredentialProvider")}
    hop1 = providers[names.sap_gateway_provider_name("acme", "dev", "sb2")]
    ms1 = hop1["Oauth2ProviderConfigInput"]["MicrosoftOauth2ProviderConfig"]
    assert hop1["CredentialProviderVendor"] == "MicrosoftOauth2"
    assert ms1["ClientId"] == "platform-app" and ms1["TenantId"] == "tenant-1"
    assert (
        "abo/idp-secret" in ms1["ClientSecret"] and "{{resolve:" in ms1["ClientSecret"]
    )
    hop2 = providers[names.sap_provider_name("acme", "dev", "sb2")]
    ms2 = hop2["Oauth2ProviderConfigInput"]["MicrosoftOauth2ProviderConfig"]
    assert ms2["ClientSecretConfig"] == {
        "SecretId": "x/sap/s4",
        "JsonKey": "clientSecret",
    }
    assert "ClientSecret" not in ms2
    # The Gateway role's exchange grants are the platform's (token_exchange in its
    # gateway stack): app.py refuses a vault-wide grant on that role.
    assert not [
        st
        for p in resources.values()
        if p["Type"] == "AWS::IAM::Policy"
        for st in p["Properties"]["PolicyDocument"]["Statement"]
        if "GatewayProvidersb2" in json.dumps(st["Resource"])
    ]
    runtimes = _of(resources, "Runtime")
    (obo_rt,) = [r for r in runtimes if "AuthorizerConfiguration" in r]
    jwt = obo_rt["AuthorizerConfiguration"]["CustomJWTAuthorizer"]
    assert jwt["DiscoveryUrl"] == (
        "https://login.microsoftonline.com/tenant-1/v2.0/.well-known/openid-configuration"
    )
    assert jwt["AllowedAudience"] == [
        "{{resolve:secretsmanager:x/sap/s4:SecretString:clientId}}"
    ]
    # Only hop one's own app, the platform app, may present one: not any tenant
    # client consented to the server app's scope.
    assert jwt["CustomClaims"] == [
        {
            "InboundTokenClaimName": "azp",
            "InboundTokenClaimValueType": "STRING",
            "AuthorizingClaimMatchValue": {
                "ClaimMatchOperator": "EQUALS",
                "ClaimMatchValue": {"MatchValueString": "platform-app"},
            },
        }
    ]
    # Only the SigV4 runtime carries the Gateway-only resource policy.
    assert len(_of(resources, "ResourcePolicy")) == 1


def test_synth_refuses_obo_without_direct_entra(tmp_path):
    with pytest.raises(ValueError, match="needs identity.mode direct"):
        _obo_stack(tmp_path, {"idp": "cognito"})


# ── A use case that is not a business process agent (layers/sap/tests/fixtures/vendor-lookup) ──


def test_a_twenty_line_use_case_claims_and_permits_its_sap_reads(tmp_path):
    result = synth(
        tmp_path,
        _config(
            {
                "systems": {"s4": BASIC},
                "access": {"vendor-lookup": {"read": ["s4"]}},
            },
            use_cases={"vendor-lookup": {}},
        ),
        use_cases=LAYER / "tests" / "fixtures",
    )
    assert result.returncode == 0, result.stderr[-2000:]
    uc = f"{PREFIX}-uc-vendor-lookup"
    resources = template(tmp_path, uc)["Resources"]
    (claim,) = [
        r["Properties"]
        for r in resources.values()
        if r["Type"] == "AWS::SSM::Parameter"
        and r["Properties"]["Name"].endswith("/claims/vendor-lookup")
    ]
    # The caller is a token (the client id the platform publishes), so the claim is a
    # Join; CDK's JSON drops the null governed_by, which the check reads as None.
    head, ref, tail = claim["Value"]["Fn::Join"][1]
    assert head == '{"use_case":"vendor-lookup","callers":["'
    assert "vendorlookupagentclientid" in ref["Ref"]
    assert tail == '"],"read":["s4"],"write":{}}'
    (permit,) = _of(resources, "Policy")
    assert permit["Name"].startswith("sap_s4_read_")
    statement = json.dumps(permit["Definition"]["Cedar"]["Statement"])
    assert '\\"uc-vendor-lookup/read\\"' in statement and "odata_read" in statement
    assert "odata_update" not in statement
    (fn,) = [
        r["Properties"]
        for r in resources.values()
        if r["Type"] == "AWS::Lambda::Function"
    ]
    assert (
        fn["Environment"]["Variables"]["TOOL"]
        == names.sap_target("s4") + "___odata_read"
    )
    deps = json.loads((tmp_path / "cdk.out" / "manifest.json").read_text())[
        "artifacts"
    ][uc]["dependencies"]
    assert LAYER_STACK in deps


def test_a_use_case_outside_its_ceiling_fails_the_synth(tmp_path):
    result = synth(
        tmp_path,
        _config(
            {"systems": {"s4": BASIC}, "access": {"vendor-lookup": {"read": []}}},
            use_cases={"vendor-lookup": {}},
        ),
        use_cases=LAYER / "tests" / "fixtures",
    )
    assert result.returncode != 0
    assert "outside layers.sap.access.vendor-lookup.read" in result.stderr


def test_each_owner_grants_the_interceptor_role_under_its_own_policy_name(tmp_path):
    # Measured on a development deployment: one policy name in two stacks fails the second deploy
    # ("Policy resource was already managed by another stack").
    result = synth(
        tmp_path,
        _config(
            {"systems": {"s4": BASIC}, "access": {"vendor-lookup": {"read": ["s4"]}}},
            use_cases={"vendor-lookup": {}},
        ),
        use_cases=LAYER / "tests" / "fixtures",
    )
    assert result.returncode == 0, result.stderr[-2000:]

    def names_(stack):
        return {
            r["Properties"]["PolicyName"]
            for r in template(tmp_path, stack)["Resources"].values()
            if r["Type"] == "AWS::IAM::Policy"
            and "interceptorrolearn" in json.dumps(r["Properties"]["Roles"])
        }

    layer, uc = names_(LAYER_STACK), names_(f"{PREFIX}-uc-vendor-lookup")
    assert layer and uc and not layer & uc


def test_a_custom_catalog_is_readable_by_its_runtime_only(tmp_path):
    mixed = {**BASIC, "odata_version": "mixed", "catalog_bucket": CATALOG}
    resources = _layer(tmp_path, {"systems": {"s4": mixed, "s4b": BASIC}})
    grants = {
        k: [
            st["Resource"]
            for st in r["Properties"]["PolicyDocument"]["Statement"]
            if st["Action"] == "s3:GetObject"
        ]
        for k, r in resources.items()
        if k.startswith("LayerRolePolicy")
    }
    assert sorted(map(len, grants.values())) == [0, 1]
    assert f"{CATALOG}/catalog.json" in json.dumps(grants)


def test_the_moved_resources_keep_their_first_stacks_cost_tag(tmp_path):
    # A stack refactor moves a resource only with its properties unchanged, tags too.
    resources = _layer(tmp_path, {"systems": {"s4": BASIC}})
    runtime_tags = resources["Runtimes4"]["Properties"]["Tags"]
    role_tags = {
        t["Key"]: t["Value"] for t in resources["Roles42B79830B"]["Properties"]["Tags"]
    }
    assert runtime_tags["Component"] == role_tags["Component"] == names.LEGACY_COMPONENT
    policy = next(r for k, r in resources.items() if k.startswith("LayerRolePolicy"))
    assert "Tags" not in policy["Properties"]  # IAM policies take no tags


# ── ops.py ──

_ops_spec = importlib.util.spec_from_file_location("sap_layer_ops", LAYER / "ops.py")
layer_ops = importlib.util.module_from_spec(_ops_spec)
_ops_spec.loader.exec_module(layer_ops)


def test_secret_spec_basic_and_m2m():
    cfg = SapConfig(systems={"s4": BASIC, "ecc": M2M_SAP})
    assert layer_ops.secret_spec(cfg, ["s4"]) == ("x/sap/s4", ("username", "password"))
    assert layer_ops.secret_spec(cfg, ["ecc"]) == (
        "x/sap/s4",
        ("clientId", "clientSecret"),
    )
    cfg = SapConfig(systems={"o": {**OBO, "unattended": UNATTENDED}})
    assert layer_ops.secret_spec(cfg, ["o", "unattended"]) == (
        "x/sap/oauth",
        ("clientId", "clientSecret"),
    )


@pytest.mark.parametrize(
    "args, why",
    [
        (["nope"], "no SAP system 'nope'"),
        (["byo"], "someone else deployed"),
        ([], "usage"),
        (["byo", "other"], "usage"),
        (["s4", "unattended"], "has no unattended block"),
    ],
)
def test_secret_spec_refuses(args, why):
    arn = "arn:aws:bedrock-agentcore:us-west-2:111111111111:runtime/x"
    byo = {"system_role": "sandbox", "runtime_arn": arn}
    cfg = SapConfig(systems={"byo": byo, "s4": BASIC})
    with pytest.raises(ValueError, match=why):
        layer_ops.secret_spec(cfg, args)


def test_secrets_refuses_without_a_terminal(monkeypatch, capsys):
    import io

    monkeypatch.setenv("LAYER_CONFIG", json.dumps({"systems": {"s4": BASIC}}))
    monkeypatch.setattr(sys, "stdin", io.StringIO("user\npass\n"))
    assert layer_ops.secrets(["s4"]) == 1
    assert "terminal" in capsys.readouterr().err


# ── The direct callers' clients (lib/sap_client) ──


@pytest.fixture
def sap_odata(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    sys.path.insert(0, str(LAYER / "lib" / "sap_client"))
    import sap_odata

    return sap_odata


def test_a_locked_po_is_retried_and_a_refusal_is_not(sap_odata, monkeypatch):
    from unittest import mock

    monkeypatch.setattr(sap_odata.time, "sleep", lambda s: None)
    posts = []

    def request(self, method, path, body=None, headers=None):
        if method == "GET":
            return 200, {"X-CSRF-Token": "t"}, ""
        posts.append(path)
        return answers.pop(0)

    monkeypatch.setattr(sap_odata._Session, "request", request)
    sap = mock.Mock(base_url="https://sap.example.com", client=None)

    answers = [(400, {}, "ME/006 already processing"), (201, {}, '{"d": {"X": 1}}')]
    assert sap_odata.post(sap, "SRV", "A_X", {}) == {"X": 1}
    assert len(posts) == 2

    posts.clear()
    answers = [(400, {}, "Supplier blocked")]
    with pytest.raises(RuntimeError, match="Supplier blocked"):
        sap_odata.post(sap, "SRV", "A_X", {})
    assert len(posts) == 1


def _adt(columns: dict) -> str:
    cols = "".join(
        f'<dataPreview:columns><dataPreview:metadata dataPreview:name="{name}"/>'
        "<dataPreview:dataSet>"
        + "".join(
            f"<dataPreview:data>{v}</dataPreview:data>" if v else "<dataPreview:data/>"
            for v in values
        )
        + "</dataPreview:dataSet></dataPreview:columns>"
        for name, values in columns.items()
    )
    return (
        f'<dataPreview:tableData xmlns:dataPreview="x">{cols}</dataPreview:tableData>'
    )


# MARV as a sandbox S/4HANA had it on 2026-11-04: MM open for 2026/08 and, backposting, 07.
MARV = {
    "LFGJA": ["2026"], "LFMON": ["08"], "VMGJA": ["2026"], "VMMON": ["07"],
    "XRUEM": ["X"], "OPVAR": ["1710"], "XKALE": ["X"],
}  # fmt: skip
T001B_WIDE = {
    "FRYE1": ["2000"], "FRPE1": ["001"], "TOYE1": ["2030"], "TOPE1": ["012"],
    "FRYE2": ["2000"], "FRPE2": ["013"], "TOYE2": ["2030"], "TOPE2": ["016"],
}  # fmt: skip


def _periods(sap_odata, monkeypatch, marv: dict, t001b: dict) -> list[str]:
    queries = []

    def request(self, method, path, body=None, headers=None):
        if method == "GET":
            return 400, {"X-CSRF-Token": "t"}, ""
        queries.append(body.decode())
        assert headers["X-CSRF-Token"] == "t"
        return 200, {}, _adt(marv if "MARV" in queries[-1] else t001b)

    monkeypatch.setattr(sap_odata._Session, "request", request)
    return queries


def test_the_open_posting_date_is_the_latest_one_mm_and_fi_both_allow(
    sap_odata, monkeypatch
):
    from datetime import date
    from unittest import mock

    sap = mock.Mock(base_url="https://sap.example.com", client=None, oauth=None)
    queries = _periods(sap_odata, monkeypatch, MARV, T001B_WIDE)
    # FI open 2000-2030 is not what limits it; MM's 2026/08 is.
    assert sap_odata.open_posting_date(sap, "1710", date(2026, 11, 4)) == "2026-08-31"
    assert "BUKRS = '1710'" in queries[0] and "BUKRS = '1710'" in queries[1]
    assert sap_odata.open_posting_date(sap, "1710", date(2026, 8, 10)) == "2026-08-10"
    assert sap_odata.open_posting_date(sap, "1710", date(2026, 7, 31)) == "2026-07-31"
    with pytest.raises(RuntimeError, match="no period open"):
        sap_odata.open_posting_date(sap, "1710", date(2026, 6, 30))
    # FI closed for 08: only 07 is left. Interval 2 spans years like SAP compares it.
    fi = {**T001B_WIDE, "TOYE1": ["2026"], "TOPE1": ["007"]}
    fi |= {"FRYE2": ["2026"], "TOYE2": ["2026"]}
    _periods(sap_odata, monkeypatch, MARV, fi)
    assert sap_odata.open_posting_date(sap, "1710", date(2026, 11, 4)) == "2026-07-31"
    # No backposting: 07 is not open in MM.
    _periods(sap_odata, monkeypatch, {**MARV, "XRUEM": [""]}, T001B_WIDE)
    with pytest.raises(RuntimeError, match="no period open"):
        sap_odata.open_posting_date(sap, "1710", date(2026, 7, 31))
    _periods(sap_odata, monkeypatch, {**MARV, "XKALE": [""]}, T001B_WIDE)
    with pytest.raises(RuntimeError, match="calendar months"):
        sap_odata.open_posting_date(sap, "1710")
    _periods(sap_odata, monkeypatch, {}, T001B_WIDE)
    with pytest.raises(RuntimeError, match="no MM period"):
        sap_odata.open_posting_date(sap, "1710")
    with pytest.raises(RuntimeError, match="not a company code"):
        sap_odata.open_posting_date(sap, "17' OR '1")
    _periods(sap_odata, monkeypatch, {**MARV, "OPVAR": ["1' OR '1"]}, T001B_WIDE)
    with pytest.raises(RuntimeError, match="posting period variant"):
        sap_odata.open_posting_date(sap, "1710")


def test_the_open_period_is_not_read_through_adt_on_a_token_flow(
    sap_odata, monkeypatch
):
    # A client-credentials token has no scope for ADT: SAP answered 403 live.
    from unittest import mock

    reached = mock.Mock(side_effect=AssertionError("reached SAP"))
    monkeypatch.setattr(sap_odata._Session, "request", reached)
    sap = mock.Mock(base_url="https://sap.example.com", oauth={"flow": "m2m-sap"})
    with pytest.raises(RuntimeError, match="signs in with m2m-sap.*needs Basic"):
        sap_odata.open_posting_date(sap, "1710")
    reached.assert_not_called()


@pytest.mark.parametrize(
    "tool",
    [
        f"{names.sap_target('s4')}___odata_read",
        f"{names.sap_target('s4')}___",
        f"{names.LEGACY_TARGET_PREFIX}s4___odata_read",
    ],
)
def test_no_sap_tool_may_skip_the_egress_response_scan(tool):
    platform = SimpleNamespace(
        security=SimpleNamespace(
            egress_unscanned_responses=["uc-sops___load_sop", tool]
        )
    )
    with pytest.raises(ValueError, match="names SAP tools"):
        access.require_check(platform, SapConfig())


def test_the_check_may_keep_the_targets_first_prefix_while_they_are_renamed(tmp_path):
    config = _config({"systems": {"s4": BASIC}})
    config["gateway"]["interceptor_extensions"] = [
        {**CHECK, "target_prefixes": [names.TARGET_PREFIX, names.LEGACY_TARGET_PREFIX]}
    ]
    result = synth(tmp_path, config)
    assert result.returncode == 0, result.stderr[-2000:]


# ── Nobody but CloudFormation may write the layer's parameters ──

SSM_WRITES = {
    "ssm:putparameter",
    "ssm:deleteparameter",
    "ssm:deleteparameters",
    "ssm:labelparameterversion",
}


def _flatten(value, resources: dict) -> str:
    """A policy Resource as an fnmatch pattern; a Ref to a parameter is its name."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and "Fn::Join" in value:
        sep, parts = value["Fn::Join"]
        return sep.join(_flatten(p, resources) for p in parts)
    if isinstance(value, dict) and "Ref" in value:
        if value["Ref"].startswith("AWS::"):
            return "*"
        r = resources.get(value["Ref"], {})
        if r.get("Type") == "AWS::SSM::Parameter":
            return r["Properties"]["Name"].lstrip("/")
    # Any other token resolves to something we cannot see: assume it covers.
    return "*"


def _writes_on_layer_prefix(templates: dict[str, dict], prefix: str) -> list[str]:
    from fnmatch import fnmatchcase

    probe = f"arn:aws:ssm:us-east-1:111111111111:parameter{prefix}/layers/sap/claims/x"
    found = []
    for stack, t in templates.items():
        resources = t.get("Resources", {})
        for lid, r in resources.items():
            if r["Type"] not in {
                "AWS::IAM::Policy",
                "AWS::IAM::ManagedPolicy",
                "AWS::IAM::Role",
            }:
                continue
            props = r.get("Properties", {})
            docs = [props.get("PolicyDocument")] + [
                p.get("PolicyDocument") for p in props.get("Policies") or []
            ]
            for doc in filter(None, docs):
                for st in doc.get("Statement", []):
                    if st.get("Effect") != "Allow":
                        continue
                    actions = st.get("Action", [])
                    actions = [actions] if isinstance(actions, str) else actions
                    if not any(
                        a.lower() in SSM_WRITES
                        or fnmatchcase("ssm:putparameter", a.lower())
                        or a == "*"
                        for a in actions
                    ):
                        continue
                    res = st.get("Resource", [])
                    res = res if isinstance(res, list) else [res]
                    if any(fnmatchcase(probe, _flatten(x, resources)) for x in res):
                        found.append(f"{stack}/{lid}: {actions} on {res}")
    return found


def assert_no_role_writes_the_layers_parameters(tmp_path: Path, config: dict) -> None:
    """No role the synthesized stacks define may write the layer's SSM parameters, and the
    write alert's DLQ is on the platform CMK exactly when the deployment is production."""
    # CloudFormation's own deploy role is the bootstrap's, defined in no template here.
    result = synth(tmp_path, config)
    assert result.returncode == 0, result.stderr[-2000:]
    templates = {
        p.name: json.loads(p.read_text())
        for p in (tmp_path / "cdk.out").glob("*.template.json")
    }
    prefix = f"/{config['project']}/{config['environment']}"
    assert _writes_on_layer_prefix(templates, prefix) == []
    (layer,) = [
        t for n, t in templates.items() if n.endswith("-layer-sap.template.json")
    ]
    (dlq,) = [
        r["Properties"]
        for k, r in layer["Resources"].items()
        if r["Type"] == "AWS::SQS::Queue" and k.startswith("WriteAlertDlq")
    ]
    production = (config.get("deployment") or {}).get("mode") == "production"
    assert ("KmsMasterKeyId" in dlq) is production


CONTRACT = REPO / "tests/fixtures/contract"


def _hello_sap(production: bool) -> dict:
    """The hello-sap fixture, or the same use case and layer on the production fixture."""
    hello = yaml.safe_load((CONTRACT / "hello-sap.yaml").read_text())
    if not production:
        return hello
    config = yaml.safe_load((CONTRACT / "production.yaml").read_text())
    config["gateway"]["interceptor_extensions"] = hello["gateway"][
        "interceptor_extensions"
    ]
    # production refuses latest
    config["layers"] = {"sap": {**hello["layers"]["sap"], "image_tag": "1.1.0-1042"}}
    config["use_cases"] = hello["use_cases"]
    return config


@pytest.mark.parametrize("production", [False, True])
def test_no_role_the_stacks_define_may_write_the_layers_parameters(
    tmp_path, production
):
    assert_no_role_writes_the_layers_parameters(tmp_path, _hello_sap(production))


def test_the_write_check_finds_a_grant_that_covers_the_prefix():
    policy = {
        "Type": "AWS::IAM::Policy",
        "Properties": {
            "PolicyDocument": {
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": "ssm:Put*",
                        "Resource": "arn:aws:ssm:*:*:parameter/p/e/*",
                    }
                ]
            }
        },
    }
    assert _writes_on_layer_prefix({"s": {"Resources": {"P": policy}}}, "/p/e")


def test_the_alert_fires_on_every_writer_but_cloudformation():
    stack = cdk.Stack(
        cdk.App(), "S", env=cdk.Environment(account="111111111111", region="us-west-2")
    )
    writer = stack.resolve(layer_stack.cloudformation_writer(stack))
    # The very session the check trusts, nothing wider.
    assert writer == {
        "Fn::Sub": CLOUDFORMATION.replace("arn:aws:", "arn:${AWS::Partition}:")
    }
    pattern = layer_stack.write_alert_pattern("/p/e/layers/sap", "w")["detail"]
    assert pattern["userIdentity"]["arn"] == [{"anything-but": ["w"]}]
    assert set(pattern["eventName"]) >= {
        "PutParameter",
        "DeleteParameter",
        "DeleteParameters",
    }
    assert pattern["$or"][0]["requestParameters"]["name"] == [
        {"prefix": "/p/e/layers/sap/"}
    ]


def test_the_rewrite_verb_bumps_the_parameter_the_stacks_declare():
    assert layer_ops.NONCE == access.REWRITE_NONCE


def test_the_alert_names_the_call_parameter_and_principal_never_the_value(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    spec = importlib.util.spec_from_file_location(
        "sap_write_alert", LAYER / "write_alert" / "index.py"
    )
    alert = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(alert)

    text = alert.message(
        {
            "eventName": "PutParameter",
            "eventTime": "t",
            "userIdentity": {"arn": "arn:aws:sts::1:assumed-role/Admin/x"},
            "requestParameters": {
                "name": "/p/e/layers/sap/claims/u",
                "value": "SECRET",
            },
        }
    )
    assert "PutParameter on /p/e/layers/sap/claims/u" in text
    assert "assumed-role/Admin/x" in text and "SECRET" not in text
    bare = alert.message({"eventName": "PutParameter", "requestParameters": None})
    assert "None" not in bare.splitlines()[0]


def test_verify_fails_when_the_live_cedar_mode_is_not_the_configured_one():
    spec = importlib.util.spec_from_file_location("sap_verify", LAYER / "verify.py")
    verify = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verify)
    enforce = {"policyEngineConfiguration": {"mode": "ENFORCE"}}
    log_only = {"policyEngineConfiguration": {"mode": "LOG_ONLY"}}
    assert verify.cedar_mismatch({"cedar_enforced": True}, enforce) is None
    assert "LOG_ONLY" in verify.cedar_mismatch({"cedar_enforced": True}, log_only)
    assert "unset" in verify.cedar_mismatch({"cedar_enforced": True}, {})
    # Settings that do not lean on Cedar have nothing to be wrong about.
    assert verify.cedar_mismatch({"cedar_enforced": False}, log_only) is None


def test_verify_reads_the_newest_certificate_banner():
    spec = importlib.util.spec_from_file_location("sap_verify", LAYER / "verify.py")
    verify = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verify)
    on, off = "||    Session Verify: True    ||", "||    Session Verify: False    ||"
    assert verify.session_verify([off, on]) is True
    assert verify.session_verify([on, off]) is False
    assert verify.session_verify([]) is None


def test_an_undelivered_write_alert_lands_in_a_dlq_that_alarms(tmp_path):
    resources = _layer(tmp_path, {"systems": {"s4": BASIC}})

    def one(kind, prefix):
        (found,) = [
            (k, r["Properties"])
            for k, r in resources.items()
            if r["Type"] == kind and k.startswith(prefix)
        ]
        return found

    dlq, _ = one("AWS::SQS::Queue", "WriteAlertDlq")
    _, fn = one("AWS::Lambda::Function", "WriteAlert")
    assert fn["DeadLetterConfig"]["TargetArn"] == {"Fn::GetAtt": [dlq, "Arn"]}
    _, alarm = one("AWS::CloudWatch::Alarm", "WriteAlertUndelivered")
    assert alarm["Dimensions"][0]["Value"] == {"Fn::GetAtt": [dlq, "QueueName"]}
    assert alarm["AlarmActions"]


# ── The sap-events bus: senders, the ingest ceiling, subscribe ──

HOST = "arn:aws:iam::222222222222:role/sap-host"
ANYWHERE = "arn:aws:iam::333333333333:role/roles-anywhere"


def test_event_sources_are_the_system_names_under_sap():
    assert names.event_source("s4") == "sap.s4"
    assert len(names.event_source("a" * names.SAP_SYSTEM_MAX)) == 20
    with pytest.raises(ValueError, match="at most 16"):
        names.event_source("a" * 17)
    assert names.event_bus_name(PREFIX) == f"{PREFIX}-sap-events"


@pytest.mark.parametrize(
    "systems, access, why",
    [
        ({"s4": {**BASIC, "senders": ["222222222222"]}}, {}, "IAM role ARN"),
        (
            {"s4": {**BASIC, "senders": ["arn:aws:iam::222222222222:root"]}},
            {},
            "IAM role ARN",
        ),
        (
            {"s4": {**BASIC, "senders": ["arn:aws:iam::222222222222:user/sap"]}},
            {},
            "not a user",
        ),
        (
            {
                "s4": {
                    **BASIC,
                    "senders": [
                        (
                            "arn:aws:iam::222222222222:role/aws-reserved/"
                            "sso.amazonaws.com/AWSReservedSSO_Admin_0123456789abcdef"
                        )
                    ],
                }
            },
            {},
            "a person, not a host",
        ),
        ({"s4": BASIC}, {"uc-a": {"ingest": ["ecc"]}}, "unknown system"),
        (
            {"s4": BASIC},
            {"uc-a": {"bypass_gateway": ["s4"], "ingest": ["s4"]}},
            "s4 has no senders",
        ),
        (
            {"s4": {**BASIC, "senders": [HOST]}},
            {"uc-a": {"read": ["s4"], "ingest": ["s4"]}},
            "not in layers.sap.access.uc-a.bypass_gateway",
        ),
    ],
)
def test_the_ingest_ceiling_names_systems_with_senders_it_reads_directly(
    systems, access, why
):
    with pytest.raises(ValueError, match=why):
        SapConfig(systems=systems, access=access)


def test_a_systems_topics_are_the_use_cases_that_ingest_it():
    cfg = SapConfig(
        systems={
            "s4": {**BASIC, "senders": [HOST]},
            "ecc": {**BASIC, "senders": [HOST]},
        },
        access={
            "uc-b": {"bypass_gateway": ["s4"], "ingest": ["s4"]},
            "uc-a": {"bypass_gateway": ["s4", "ecc"], "ingest": ["s4", "ecc"]},
            "uc-c": {"read": "*"},
        },
    )
    assert cfg.sender_systems() == ["ecc", "s4"]
    assert cfg.topics("s4") == ["uc-a", "uc-b"] and cfg.topics("ecc") == ["uc-a"]
    assert cfg.ingest("uc-a") == ["ecc", "s4"] and cfg.ingest("uc-c") == []


def _typed(resources: dict, kind: str) -> dict:
    return {k: r["Properties"] for k, r in resources.items() if r["Type"] == kind}


def test_no_senders_no_bus(tmp_path):
    resources = _layer(tmp_path, {"systems": {"s4": BASIC}})
    assert not _typed(resources, "AWS::Events::EventBus")
    assert not [k for k in resources if k.startswith(("SapSender", "Outbus"))]


INGEST_SAP = {
    "systems": {
        "s4": {**BASIC, "senders": [HOST, ANYWHERE]},
        "ecc": {**BASIC, "type": "ECC", "senders": [HOST]},
        "bw": BASIC,
    },
    "access": {
        "uc-a": {"bypass_gateway": ["s4"], "ingest": ["s4"]},
        "uc-b": {"bypass_gateway": ["s4"], "ingest": ["s4"]},
    },
}


def test_the_bus_admits_only_its_sender_roles_each_bound_to_its_source(tmp_path):
    resources = _layer(tmp_path, INGEST_SAP)
    ((bus_id, bus),) = _typed(resources, "AWS::Events::EventBus").items()
    assert bus["Name"] == f"{PREFIX}-sap-events"
    roles = _typed(resources, "AWS::IAM::Role")
    senders = {k: r for k, r in roles.items() if k.startswith("SapSender")}
    assert len(senders) == 2  # s4 and ecc; bw has no senders
    arns = sorted(({"Fn::GetAtt": [k, "Arn"]} for k in senders), key=json.dumps)
    statements = bus["Policy"]["Statement"]
    # Denies only: in its own account an allow would bypass the roles' conditions.
    assert {st["Effect"] for st in statements} == {"Deny"}
    deny = statements[0]
    by_sid = {st["Sid"]: st for st in statements}
    bound = 0
    for n in range(len(senders)):
        if f"DenySender{n}" in by_sid:  # no use case ingests it: it may put nothing
            assert set(by_sid[f"DenySender{n}"]["Condition"]) == {"ArnEquals"}
            continue
        source = by_sid[f"DenySender{n}OtherSource"]["Condition"]
        topic = by_sid[f"DenySender{n}OtherTopic"]["Condition"]
        assert source["ForAnyValue:StringNotEquals"]["events:source"][0].startswith(
            "sap."
        )
        assert topic["ForAnyValue:StringNotEquals"]["events:detail-type"]
        assert source["ArnEquals"] == topic["ArnEquals"]
        bound += 1
    assert bound >= 1
    assert deny == {
        "Sid": "DenyEveryoneElse",
        "Effect": "Deny",
        "Principal": "*",
        "Action": "events:PutEvents",
        "Resource": deny["Resource"],
        "Condition": {
            "ArnNotEquals": {
                "aws:PrincipalArn": deny["Condition"]["ArnNotEquals"][
                    "aws:PrincipalArn"
                ]
            }
        },
    }
    assert (
        sorted(deny["Condition"]["ArnNotEquals"]["aws:PrincipalArn"], key=json.dumps)
        == arns
    )
    assert "event-bus/agentcore-workshop-dev-sap-events" in json.dumps(deny["Resource"])

    (s4,) = [r for k, r in senders.items() if k.startswith("SapSenders4")]
    trust = s4["AssumeRolePolicyDocument"]["Statement"]
    assert {st["Action"] for st in trust} == {"sts:AssumeRole"}
    assert sorted(st["Principal"]["AWS"] for st in trust) == sorted([HOST, ANYWHERE])
    ((policy,),) = [[p for p in s4["Policies"]]]
    (statement,) = policy["PolicyDocument"]["Statement"]
    assert statement["Action"] == "events:PutEvents"
    assert statement["Resource"] == deny["Resource"]
    assert statement["Condition"] == {
        "ForAllValues:StringEquals": {
            "events:source": ["sap.s4"],
            "events:detail-type": ["uc-a", "uc-b"],
        },
        "Null": {"events:source": "false", "events:detail-type": "false"},
    }
    # No use case ingests ecc yet: its role can put nothing.
    (ecc,) = [r for k, r in senders.items() if k.startswith("SapSenderecc")]
    assert "Policies" not in ecc

    assert _param(resources, "bus-arn") == {"Fn::GetAtt": [bus_id, "Arn"]}
    (s4_id,) = [k for k in senders if k.startswith("SapSenders4")]
    assert _param(resources, "systems/s4/sender-role-arn") == {
        "Fn::GetAtt": [s4_id, "Arn"]
    }
    assert json.loads(_param(resources, "ingest")) == {
        "systems": {
            "ecc": {"senders": [HOST], "topics": []},
            "s4": {"senders": [HOST, ANYWHERE], "topics": ["uc-a", "uc-b"]},
        },
        "ceilings": {"uc-a": ["s4"], "uc-b": ["s4"]},
    }


def _subscriber(tmp_path: Path, uc_config: dict, ingest: list[str]):
    sap = {
        "systems": {"s4": {**BASIC, "senders": [HOST]}, "ecc": BASIC},
        "access": {
            "vendor-lookup": {
                "read": ["s4"],
                "bypass_gateway": ["s4", "ecc"],
                "ingest": ingest,
            }
        },
    }
    return synth(
        tmp_path,
        _config(sap, use_cases={"vendor-lookup": uc_config}),
        use_cases=LAYER / "tests" / "fixtures",
    )


def test_subscribe_routes_the_use_cases_own_topic_to_its_queue_alone(tmp_path):
    result = _subscriber(tmp_path, {"ingest": ["s4"], "cmk": True}, ["s4"])
    assert result.returncode == 0, result.stderr[-2000:]
    resources = template(tmp_path, f"{PREFIX}-uc-vendor-lookup")["Resources"]
    ((rule_id, rule),) = _typed(resources, "AWS::Events::Rule").items()
    assert rule["EventPattern"] == {
        "source": ["sap.s4"],
        "detail-type": ["vendor-lookup"],
    }
    assert "layerssapbusarn" in rule["EventBusName"]["Ref"]
    queues = _typed(resources, "AWS::SQS::Queue")
    (queue,) = [k for k in queues if k.startswith("IngestQueue")]
    (dlq,) = [k for k in queues if k.startswith("IngestDlq")]
    assert rule["Targets"] == [
        {
            "Id": "IngestQueue",
            "Arn": {"Fn::GetAtt": [queue, "Arn"]},
            "DeadLetterConfig": {"Arn": {"Fn::GetAtt": [dlq, "Arn"]}},
            "RetryPolicy": {
                "MaximumRetryAttempts": 185,
                "MaximumEventAgeInSeconds": 86400,
            },
        }
    ]
    # Each queue admits this rule and nothing else.
    for p in _typed(resources, "AWS::SQS::QueuePolicy").values():
        (st,) = p["PolicyDocument"]["Statement"]
        assert st["Principal"] == {"Service": "events.amazonaws.com"}
        assert st["Action"] == "sqs:SendMessage"
        assert st["Condition"] == {
            "ArnEquals": {"aws:SourceArn": {"Fn::GetAtt": [rule_id, "Arn"]}}
        }
    assert len(_typed(resources, "AWS::SQS::QueuePolicy")) == 2
    # The use case's own key lets EventBridge encrypt into both queues.
    (key,) = _typed(resources, "AWS::KMS::Key").values()
    events_st = [
        st
        for st in key["KeyPolicy"]["Statement"]
        if st["Principal"] == {"Service": "events.amazonaws.com"}
    ]
    assert [sorted(st["Action"]) for st in events_st] == [
        ["kms:Decrypt", "kms:GenerateDataKey"]
    ]
    # Only for this account's rules.
    assert events_st[0]["Condition"] == {
        "StringEquals": {"aws:SourceAccount": "111111111111"}
    }
    # The subscription verify holds the bus to: this rule, its one queue and DLQ.
    (sub,) = [
        p
        for p in _typed(resources, "AWS::SSM::Parameter").values()
        if p["Name"].endswith("/layers/sap/subscriptions/vendor-lookup")
    ]
    value = json.dumps(sub["Value"])
    assert rule_id in value and queue in value and dlq in value


@pytest.mark.parametrize(
    "uc_config, ingest, why",
    [
        ({"ingest": ["ecc"]}, ["s4"], "subscribes to ['ecc'], outside"),
        ({"ingest": ["s4"]}, [], "subscribes to ['s4'], outside"),
    ],
)
def test_subscribe_refuses_a_system_outside_the_ceiling(
    tmp_path, uc_config, ingest, why
):
    result = _subscriber(tmp_path, uc_config, ingest)
    assert result.returncode != 0
    assert why in result.stderr
    assert "layers.sap.access.vendor-lookup.ingest" in result.stderr


# ── verify: the bus, its roles, the probe and the rules ──


def _verify():
    spec = importlib.util.spec_from_file_location("sap_verify", LAYER / "verify.py")
    verify = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verify)
    return verify


BUS = "arn:aws:events:us-west-2:111111111111:event-bus/p-e-sap-events"
SENDER = "arn:aws:iam::111111111111:role/p-e-layer-sap-SapSenders4-ABC"
INGEST = {
    "systems": {"s4": {"senders": [HOST], "topics": ["uc-a"]}},
    "ceilings": {"uc-a": ["s4"], "uc-b": ["s4"]},
}


def _put_statement(**over):
    return {
        "Effect": "Allow",
        "Action": "events:PutEvents",
        "Resource": BUS,
        "Condition": {
            "ForAllValues:StringEquals": {
                "events:source": "sap.s4",
                "events:detail-type": ["uc-a"],
            },
            "Null": {"events:source": "false", "events:detail-type": "false"},
        },
        **over,
    }


class _Events:
    def __init__(
        self, *, deny=(SENDER,), probe="refused", rules=None, bound=True, extra=()
    ):
        self.deny, self.probe = list(deny), probe
        self.bound, self.extra = bound, list(extra)
        self.rules = (
            rules
            if rules is not None
            else {"r1": ({"source": ["sap.s4"], "detail-type": ["uc-a"]}, True)}
        )

    def describe_event_bus(self, Name):
        statements = list(self.extra)
        if self.bound:
            for key, values in (
                ("events:source", ["sap.s4"]),
                ("events:detail-type", ["uc-a"]),
            ):
                statements.append(
                    {
                        "Effect": "Deny",
                        "Principal": "*",
                        "Action": "events:PutEvents",
                        "Resource": Name,
                        "Condition": {
                            "ArnEquals": {"aws:PrincipalArn": SENDER},
                            "ForAnyValue:StringNotEquals": {key: values},
                        },
                    }
                )
        if self.deny:
            statements.append(
                {
                    "Effect": "Deny",
                    "Principal": "*",
                    "Action": "events:PutEvents",
                    "Resource": Name,
                    "Condition": {
                        "ArnNotEquals": {
                            "aws:PrincipalArn": self.deny[0]
                            if len(self.deny) == 1
                            else self.deny
                        }
                    },
                }
            )
        return {"Policy": json.dumps({"Statement": statements})}

    def put_events(self, Entries):
        from botocore.exceptions import ClientError

        if self.probe == "refused":
            raise ClientError({"Error": {"Code": "AccessDeniedException"}}, "PutEvents")
        if self.probe == "throttled":
            raise ClientError({"Error": {"Code": "ThrottlingException"}}, "PutEvents")
        return {"FailedEntryCount": 0, "Entries": [{"EventId": "1"}]}

    def get_paginator(self, _):
        rules = self.rules

        class P:
            def paginate(self, EventBusName):
                yield {
                    "Rules": [
                        {
                            "Name": n,
                            "Arn": f"arn:aws:events:r:1:rule/sap-events/{n}",
                            "EventPattern": json.dumps(spec[0]),
                        }
                        for n, spec in rules.items()
                    ]
                }

        return P()

    def list_targets_by_rule(self, Rule, EventBusName):
        spec = self.rules[Rule]
        dlq = spec[1]
        targets = spec[2] if len(spec) > 2 else None
        if targets is not None:
            return {"Targets": targets}
        return {
            "Targets": [
                {
                    "Id": "IngestQueue",
                    "Arn": QUEUE,
                    **({"DeadLetterConfig": {"Arn": DLQ}} if dlq else {}),
                }
            ]
        }


QUEUE = "arn:aws:sqs:r:1:uc-a-ingest"
DLQ = "arn:aws:sqs:r:1:uc-a-ingest-dlq"
SUBSCRIPTIONS = {
    "uc-a": {
        "rule_arn": "arn:aws:events:r:1:rule/sap-events/r1",
        "queue_arn": QUEUE,
        "dlq_arn": DLQ,
    }
}


class _Iam:
    def __init__(self, *, trust=(HOST,), statements=None, attached=()):
        self.trust, self.attached = list(trust), list(attached)
        self.statements = [_put_statement()] if statements is None else statements

    def get_role(self, RoleName):
        assert RoleName == "p-e-layer-sap-SapSenders4-ABC"
        principal = self.trust[0] if len(self.trust) == 1 else self.trust
        return {
            "Role": {
                "AssumeRolePolicyDocument": {
                    "Statement": [{"Effect": "Allow", "Principal": {"AWS": principal}}]
                }
            }
        }

    def list_attached_role_policies(self, RoleName):
        return {"AttachedPolicies": [{"PolicyName": n} for n in self.attached]}

    def list_role_policies(self, RoleName):
        return {"PolicyNames": ["PutSapEvents"]}

    def get_role_policy(self, RoleName, PolicyName):
        return {"PolicyDocument": {"Statement": self.statements}}


def _check(events=None, iam=None, subscriptions=SUBSCRIPTIONS):
    return _verify().check_ingest(
        INGEST, BUS, {"s4": SENDER}, events or _Events(), iam or _Iam(), subscriptions
    )


_TOPIC_A = {"source": ["sap.s4"], "detail-type": ["uc-a"]}


@pytest.mark.parametrize(
    "events, subscriptions, why",
    [
        # Another stack's rule on uc-a's topic, inside uc-a's ceiling.
        (
            _Events(rules={"r1": (_TOPIC_A, True), "r9": (_TOPIC_A, True)}),
            SUBSCRIPTIONS,
            "another stack added it",
        ),
        # A second target on uc-a's own rule.
        (
            _Events(
                rules={
                    "r1": (
                        _TOPIC_A,
                        True,
                        [
                            {
                                "Id": "IngestQueue",
                                "Arn": QUEUE,
                                "DeadLetterConfig": {"Arn": DLQ},
                            },
                            {
                                "Id": "x",
                                "Arn": "arn:aws:lambda:r:2:function:f",
                                "DeadLetterConfig": {"Arn": DLQ},
                            },
                        ],
                    )
                }
            ),
            SUBSCRIPTIONS,
            "2 targets",
        ),
        # uc-a's rule, repointed at another account's queue.
        (
            _Events(
                rules={
                    "r1": (
                        _TOPIC_A,
                        True,
                        [
                            {
                                "Id": "IngestQueue",
                                "Arn": "arn:aws:sqs:r:2:q",
                                "DeadLetterConfig": {"Arn": DLQ},
                            }
                        ],
                    )
                }
            ),
            SUBSCRIPTIONS,
            "not its subscribed queue",
        ),
        (
            _Events(
                rules={
                    "r1": (
                        _TOPIC_A,
                        True,
                        [
                            {
                                "Id": "IngestQueue",
                                "Arn": QUEUE,
                                "DeadLetterConfig": {"Arn": "arn:aws:sqs:r:2:d"},
                            }
                        ],
                    )
                }
            ),
            SUBSCRIPTIONS,
            "dead-letter queue is not",
        ),
        (
            _Events(
                rules={
                    "r1": (
                        _TOPIC_A,
                        True,
                        [
                            {
                                "Id": "IngestQueue",
                                "Arn": QUEUE,
                                "DeadLetterConfig": {"Arn": DLQ},
                                "InputTransformer": {},
                            }
                        ],
                    )
                }
            ),
            SUBSCRIPTIONS,
            "also sets InputTransformer",
        ),
        # A rule on the topic of a use case that never subscribed.
        (_Events(), {}, "publishes no subscription"),
    ],
)
def test_verify_holds_every_rule_to_its_use_cases_published_subscription(
    events, subscriptions, why
):
    failure, _ = _check(events, subscriptions=subscriptions)
    assert failure and why in failure


class _History:
    class exceptions:
        class ParameterNotFound(Exception):
            pass

    def __init__(self, writer=None):
        self.writer = writer

    def get_parameter_history(self, Name, MaxResults, **_):
        if self.writer is None:
            raise self.exceptions.ParameterNotFound(Name)
        return {
            "Parameters": [
                {
                    "Version": 1,
                    "Value": "{}",
                    "LastModifiedUser": "arn:aws:sts::1:assumed-role/x/y",
                },
                {
                    "Version": 2,
                    "Value": json.dumps(SUBSCRIPTIONS["uc-a"]),
                    "LastModifiedUser": self.writer,
                },
            ]
        }


def test_a_subscription_counts_only_when_a_stack_deploy_wrote_it():
    verify = _verify()
    cfn = "arn:aws:sts::111111111111:assumed-role/cdk-hnb659fds-cfn-exec-role-111111111111-us-west-2/AWSCloudFormation"
    assert (
        verify.subscription_of(_History(cfn), "n", "111111111111")
        == SUBSCRIPTIONS["uc-a"]
    )
    assert verify.subscription_of(_History(), "n", "111111111111") is None
    with pytest.raises(ValueError, match="not a stack deploy"):
        verify.subscription_of(
            _History("arn:aws:sts::111111111111:assumed-role/Admin/someone"),
            "n",
            "111111111111",
        )
    with pytest.raises(ValueError, match="not a stack deploy"):  # another account's
        verify.subscription_of(
            _History(cfn.replace("111111111111", "222222222222")), "n", "111111111111"
        )


def test_verify_passes_a_closed_door_and_reports_a_ceiling_with_no_rule():
    assert _check() == (None, ["uc-b"])


@pytest.mark.parametrize(
    "events, iam, why",
    [
        (_Events(deny=()), None, "does not deny PutEvents"),
        (
            _Events(
                extra=[
                    {
                        "Sid": "AllowSenderRoles",
                        "Effect": "Allow",
                        "Principal": {"AWS": SENDER},
                        "Action": "events:PutEvents",
                    }
                ]
            ),
            None,
            "past the sender roles",
        ),
        (_Events(bound=False), None, "does not hold"),
        (_Events(deny=(SENDER, ANYWHERE)), None, "does not deny PutEvents"),
        (None, _Iam(trust=(HOST, ANYWHERE)), "trusts"),
        (None, _Iam(attached=["AdministratorAccess"]), "managed policy"),
        (None, _Iam(statements=[_put_statement(Action="events:*")]), "may do more"),
        (
            None,
            _Iam(statements=[_put_statement(), {"Effect": "Allow", "Action": "s3:*"}]),
            "may do more",
        ),
        (
            None,
            _Iam(
                statements=[
                    _put_statement(
                        Condition={
                            "ForAllValues:StringEquals": {
                                "events:source": ["sap.s4"],
                                "events:detail-type": ["uc-a"],
                            }
                        }
                    )
                ]
            ),
            "may do more",
        ),
        (_Events(probe="accepted"), None, "accepted a sap.s4 event"),
        (
            _Events(
                rules={"r9": ({"source": ["sap.s4"], "detail-type": ["uc-z"]}, True)}
            ),
            None,
            "outside every",
        ),
        (
            _Events(
                rules={"r9": ({"source": ["sap.ecc"], "detail-type": ["uc-a"]}, True)}
            ),
            None,
            "outside every",
        ),
        (
            _Events(rules={"r9": ({"detail-type": ["uc-a"]}, True)}),
            None,
            "outside every",
        ),
        (
            _Events(
                rules={"r1": ({"source": ["sap.s4"], "detail-type": ["uc-a"]}, False)}
            ),
            None,
            "no dead-letter queue",
        ),
    ],
)
def test_verify_fails_a_door_open_wider_than_the_ceilings(events, iam, why):
    failure, _ = _check(events, iam)
    assert failure and why in failure


def test_verify_does_not_take_a_throttled_probe_for_a_refusal():
    from botocore.exceptions import ClientError

    with pytest.raises(ClientError):
        _check(_Events(probe="throttled"))
