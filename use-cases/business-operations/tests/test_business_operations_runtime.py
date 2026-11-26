# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""business-operations: the agent runtime, its tool filter, and the invoker's run grant."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from infra_utils.use_case_context import UseCaseContext

REPO = Path(__file__).resolve().parents[3]
UC = REPO / "use-cases" / "business-operations"
sys.path.insert(0, str(UC))
sys.path.insert(0, str(REPO))

from business_operations import config as uc_config
from business_operations import names
from business_operations import runtime as uc_runtime
from sap_layer import names as sap_names


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


topology = _load("bo_mcp_topology", UC / "agent/utils/mcp_topology.py")
CASE = names.tool_name(names.CASE_TARGET, "get_case_state")
WRITE = names.tool_name(names.CASE_TARGET, "update_case_state")


# ── The tool filter: full, target-prefixed names, own targets only ──


def test_the_prefixes_are_the_use_cases_and_the_sap_layers():
    assert names.CASE_TARGET.startswith(topology.TARGET_PREFIXES)
    assert topology.TARGET_PREFIXES == (f"{names.NAME}-", sap_names.TARGET_PREFIX)


def test_another_tenants_same_named_tool_is_never_held():
    assert topology.permitted(CASE, None)
    assert not topology.permitted("other-use-case___get_case_state", None)
    assert not topology.permitted("other-use-case___get_case_state", ["get_case_state"])
    assert not topology.permitted("get_case_state", None)


def test_a_pack_allowlist_matches_full_names_only():
    assert topology.permitted(CASE, [CASE])
    assert not topology.permitted(WRITE, [CASE])
    # A bare name in a pack's list grants nothing on the shared Gateway.
    assert not topology.permitted(CASE, ["get_case_state"])


def test_an_empty_allowlist_grants_nothing():
    assert not topology.permitted(CASE, [])


# ── Two SAP systems: the same tool names on two targets ──

S4, ECC = sap_names.sap_target("s4"), sap_names.sap_target("ecc")


def test_the_layer_names_a_systems_target_the_way_the_agent_reads_it():
    assert S4 == f"{topology.SAP_TARGET_PREFIX}s4"


def test_two_systems_route_by_the_packs_sap_system():
    tools = [CASE] + [
        names.tool_name(t, n) for t in (S4, ECC) for n in ("odata_read", "odata_update")
    ]
    # Each pack lists its own system's tools; a pack naming both still gets one.
    loaded = [t for t in tools if topology.permitted(t, tools, sap_system="s4")]
    assert loaded == [CASE, f"{S4}___odata_read", f"{S4}___odata_update"]
    assert topology.bare_names(loaded) == {
        CASE: "get_case_state",
        f"{S4}___odata_read": "odata_read",
        f"{S4}___odata_update": "odata_update",
    }
    # With no sap_system both systems load, and a name both serve keeps its target.
    loaded = [t for t in tools if topology.permitted(t, tools)]
    assert topology.bare_names(loaded) == {CASE: "get_case_state"}
    # A discovery run holds no write, and the reads stay apart.
    loaded = [t for t in tools if topology.permitted(t, None)]
    assert loaded == [CASE, f"{S4}___odata_read", f"{ECC}___odata_read"]
    assert topology.bare_names(loaded) == {CASE: "get_case_state"}


def test_a_bare_name_never_replaces_a_tool_registered_under_it():
    with pytest.raises(ValueError, match="would replace"):
        topology.bare_names(["calculator", "sap-s4___calculator"])


def test_denied_and_read_only_tools():
    sop = f"{names.NAME}-sop-management___put_sop"
    assert not topology.permitted(sop, None)
    assert not topology.permitted(sop, [sop])
    assert topology.permitted(WRITE, [WRITE])
    assert not topology.permitted(WRITE, [WRITE], read_only=True)
    assert topology.permitted(CASE, [CASE], read_only=True)


def test_audit_headers_carry_the_run_id_the_run_checks_look_up():
    headers = topology.audit_headers(
        {"correlation_id": "run-1", "initiator": "sub-1", "trigger": ""}
    )
    assert headers == {"x-audit-correlation-id": "run-1", "x-audit-initiator": "sub-1"}
    assert topology.audit_headers(None) == {}


# ── The platform prompt's tool blocks ──


def _filter_tool_blocks():
    # skill_router imports boto3 and yaml at module level; the function needs neither.
    source = (UC / "agent/utils/skill_router.py").read_text()
    start = source.index("_TOOL_BLOCK_RE = re.compile(")
    end = source.index("def platform_mechanics(")
    scope: dict = {}
    exec("import os, re\n" + source[start:end], scope)  # nosec B102 - our own source  # noqa: S102
    return scope["filter_tool_blocks"]


def test_tool_blocks_gate_on_full_names_and_unserved_tools(monkeypatch):
    filter_tool_blocks = _filter_tool_blocks()
    text = "a\n[[tools: search_sap_api_docs]]\ndocs\n[[/tools]]\nb\n"
    api_docs = names.tool_name(names.KNOWLEDGE_TARGET, "search_sap_api_docs")
    monkeypatch.delenv("UNSERVED_TOOLS", raising=False)
    assert filter_tool_blocks(text, [api_docs]) == "a\ndocs\nb\n"
    assert filter_tool_blocks(text, [CASE]) == "a\nb\n"
    assert filter_tool_blocks(text, None) == "a\ndocs\nb\n"
    # Turned off: not even a discovery run (no allowlist) is told to call it.
    monkeypatch.setenv("UNSERVED_TOOLS", "search_sap_api_docs")
    assert filter_tool_blocks(text, None) == "a\nb\n"
    assert filter_tool_blocks(text, [api_docs]) == "a\nb\n"


def test_the_platform_prompt_gates_every_api_docs_mention():
    filter_tool_blocks = _filter_tool_blocks()
    prompt = (UC / "packs/_platform_prompt.txt").read_text()
    assert "search_sap_api_docs" in prompt
    assert "search_sap_api_docs" not in filter_tool_blocks(prompt, [CASE])


# ── Models, guardrail and config ──


def _ctx(allowed=(), model_id=""):
    return UseCaseContext(
        "p", "dev", None, model_id=model_id, allowed_models=tuple(allowed)
    )


def test_tiers_default_and_follow_agents_model_id():
    cfg = uc_config.load({})
    assert uc_runtime.model_ids(_ctx(), cfg) == {
        "sonnet": uc_runtime.DEFAULT_SONNET,
        "haiku": uc_runtime.DEFAULT_HAIKU,
        "opus": uc_runtime.DEFAULT_OPUS,
    }
    assert uc_runtime.model_ids(_ctx(model_id="m-1"), cfg)["sonnet"] == "m-1"
    # An allowlist without Haiku: the haiku tier runs on the allowed sonnet model.
    # Without Opus: the opus tier is not served at all, so a pack naming it fails synth.
    assert uc_runtime.model_ids(_ctx(["m-1"], "m-1"), cfg) == {
        "sonnet": "m-1",
        "haiku": "m-1",
    }
    allowed = ["m-1", uc_runtime.DEFAULT_OPUS]
    assert uc_runtime.model_ids(_ctx(allowed, "m-1"), cfg)["opus"] == (
        uc_runtime.DEFAULT_OPUS
    )


def test_a_tier_outside_the_allowlist_fails_the_synth():
    cfg = uc_config.load({"runtime": {"haiku_model_id": "h-1"}})
    assert uc_runtime.model_ids(_ctx(["m-1", "h-1"], "m-1"), cfg)
    with pytest.raises(ValueError, match="haiku=h-2"):
        uc_runtime.model_ids(
            _ctx(["m-1"], "m-1"), uc_config.load({"runtime": {"haiku_model_id": "h-2"}})
        )
    with pytest.raises(ValueError, match="opus=o-1"):
        uc_runtime.model_ids(
            _ctx(["m-1"], "m-1"), uc_config.load({"runtime": {"opus_model_id": "o-1"}})
        )


def test_the_tier_set_is_one_set():
    """The schema's two tier enums, the runtime's env map and the agent's MODEL_TIERS."""
    schema = json.loads((UC / "packs" / "pack.schema.json").read_text())
    props = schema["properties"]
    enums = [
        props["model_tier"]["enum"],
        props["verifier"]["properties"]["model_tier"]["enum"],
    ]
    assert all(set(e) == set(uc_runtime.TIER_ENV) for e in enums), enums
    agent = (UC / "agent" / "basic_agent.py").read_text()
    for tier, var in uc_runtime.TIER_ENV.items():
        assert f'"{tier}": os.environ["{var}"]' in agent, tier


def test_the_agent_guardrail_keeps_the_production_floor():
    from infra_utils.guardrail_policy import floor_violations

    policy = uc_runtime.guardrail_policy()
    assert floor_violations(policy) == []
    entities = {
        e["Type"]
        for e in policy["SensitiveInformationPolicyConfig"]["PiiEntitiesConfig"]
    }
    # Supplier names and remit-to addresses are the work.
    assert not entities & {"NAME", "ADDRESS"}


def test_max_tokens_has_a_floor():
    assert uc_config.load({}).runtime.max_tokens == 8192
    with pytest.raises(ValueError):
        uc_config.load({"runtime": {"max_tokens": 10}})


def test_knowledge_flags_name_the_unserved_tools():
    assert uc_runtime.unserved_tools(uc_config.load({})) == [
        "search_sap_api_docs",
        "get_precedent",
        "check_vendor_risk",
    ]
    on = uc_config.load(
        {"knowledge": {"api_docs_search": True, "agent_knowledge": {"enabled": True}}}
    )
    assert uc_runtime.unserved_tools(on) == []
    off = uc_config.load({"knowledge": {"sop_search": False}})
    assert uc_runtime.unserved_tools(off)[0] == "search_sap_sops"


def test_new_names_match_the_botocore_models():
    import botocore.session

    session = botocore.session.get_session()
    provider = (
        session.get_service_model("bedrock-agentcore-control")
        .operation_model("CreateOauth2CredentialProvider")
        .input_shape.members["name"]
        .metadata
    )
    api = (
        session.get_service_model("appsync")
        .operation_model("CreateApi")
        .input_shape.members["name"]
        .metadata
    )
    assert (provider["max"], provider["pattern"]) == (
        names.CREDENTIAL_PROVIDER_NAME_MAX,
        names.CREDENTIAL_PROVIDER_NAME.pattern,
    )
    assert (api["max"], api["pattern"]) == (
        names.EVENT_API_NAME_MAX,
        names.EVENT_API_NAME.pattern,
    )
    assert (
        names.run_events_api_name("acme-dev")
        == "acme-dev-business-operations-run-events"
    )
    assert len(names.run_events_api_name("p" * 40)) == names.EVENT_API_NAME_MAX


# ── Two copies of the case-id codec: the agent image and the API layer ──


# ── The invoker's run grant ──


@pytest.fixture
def invoker(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("RUNTIME_ARN_PARAMETER", "/p/e/runtimes/business-operations/arn")
    monkeypatch.setenv("AGENT_CLIENT_ID", "agent-client")
    monkeypatch.syspath_prepend(str(UC / "api/layer/python"))
    return _load("bo_invoker", UC / "invoker/index.py")


def test_the_grant_names_the_agents_gateway_client(invoker):
    item = invoker._grant_item(
        {"case_id": "", "visible_packs": ["b", "a", "a"]},
        "run-1",
        attended=True,
        now=1000,
        secret_sha256="h",
    )
    assert item == {
        "run_id": "run-1",
        "client_id": "agent-client",
        "secret_sha256": "h",
        "approved": False,
        "expires_at": 1000 + invoker._GRANT_TTL_SECONDS,
        "packs": ["a", "b"],
    }
    # Outlives the function, so a run never loses its grant mid-flight.
    assert invoker._GRANT_TTL_SECONDS > invoker._FUNCTION_TIMEOUT_SECONDS


def test_the_invokers_timeout_is_the_functions(invoker):
    from business_operations.invoker import TIMEOUT_SECONDS

    assert invoker._FUNCTION_TIMEOUT_SECONDS == TIMEOUT_SECONDS


def test_a_grant_without_the_agent_client_is_refused(invoker, monkeypatch):
    monkeypatch.setattr(invoker, "AGENT_CLIENT_ID", "")
    monkeypatch.setattr(invoker, "RUN_GRANTS_TABLE", "grants")
    with pytest.raises(RuntimeError, match="AGENT_CLIENT_ID"):
        invoker._write_grant({}, "run-1", attended=True)


# ── Synthesized: who may call the runtime, and what it may touch ──


def test_the_runtime_admits_only_the_delegating_client():
    from app_synth import synth_app

    result, out = synth_app(
        REPO
        / "use-cases/business-operations/fixtures/contract/business-operations.yaml"
    )
    assert result.returncode == 0, result.stderr[-2000:]
    template = json.loads(
        (
            out / "agentcore-workshop-dev-uc-business-operations-runtime.template.json"
        ).read_text()
    )
    resources = template["Resources"].values()
    runtime = next(
        r["Properties"]
        for r in resources
        if r["Type"] == "AWS::BedrockAgentCore::Runtime"
    )
    clients = runtime["AuthorizerConfiguration"]["CustomJWTAuthorizer"][
        "AllowedClients"
    ]
    # The delegated-identity header is trusted only because this list is one client.
    assert clients == [
        "{{resolve:ssm:/agentcore-workshop/dev/use-cases/business-operations/delegating-client-id}}"
    ]
    assert runtime["RequestHeaderConfiguration"]["RequestHeaderAllowlist"] == [
        "Authorization",
        uc_runtime.DELEGATED_IDENTITY_HEADER,
        uc_runtime.RUN_GRANT_HEADER,
    ]
    env = runtime["EnvironmentVariables"]
    assert env["RUNTIME_DELEGATING_CLIENTS"] == clients[0]
    assert env["GATEWAY_SCOPES"] == uc_runtime.GATEWAY_SCOPES
    assert (
        env["UNSERVED_TOOLS"] == "search_sap_api_docs,get_precedent,check_vendor_risk"
    )
    assert env["UNIFIED_TRACES_DESTINATION_ENABLED"] == "false"
    assert "OTEL_SEMCONV_STABILITY_OPT_IN" not in env  # content on outside production
    events = [r for r in resources if r["Type"] == "AWS::AppSync::Api"]
    assert len(events) == 1
    auth = events[0]["Properties"]["EventConfig"]
    assert [p["AuthType"] for p in auth["DefaultPublishAuthModes"]] == ["AWS_IAM"]
    assert [p["AuthType"] for p in auth["DefaultSubscribeAuthModes"]] == [
        "AMAZON_COGNITO_USER_POOLS"
    ]
    # Cognito proves a console sign-in, nothing more: every subscribe is held to the
    # users group and the channel's pack by the direct Lambda handler.
    (namespace,) = [
        r for r in resources if r["Type"] == "AWS::AppSync::ChannelNamespace"
    ]
    on_subscribe = namespace["Properties"]["HandlerConfigs"]["OnSubscribe"]
    assert on_subscribe["Behavior"] == "DIRECT"
    assert on_subscribe["Integration"]["LambdaConfig"] == {
        "InvokeType": "REQUEST_RESPONSE"
    }
    assert "OnPublish" not in namespace["Properties"]["HandlerConfigs"]
    (fn,) = [
        r["Properties"]
        for r in resources
        if r["Type"] == "AWS::Lambda::Function"
        and "USERS_GROUP" in json.dumps(r["Properties"].get("Environment"))
    ]
    assert fn["Environment"]["Variables"]["USERS_GROUP"] == "business-operations-users"
