# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""business-operations: synthesized-template guards carried over from the earlier system's CDK tests.

The guardrail's content, the run-event channel, the run route's secrets, the SAP
token grant and the agent-knowledge cluster, asserted on what app.py synthesizes.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
import yaml
from app_synth import synth_app
from test_app_wiring import _client_names

from infra_utils.platform_config import PlatformConfig

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "use-cases" / "business-operations"))

from business_operations import runtime as uc_runtime

PREFIX = "agentcore-workshop-dev"
MAIN = f"{PREFIX}-uc-business-operations"
RUNTIME = f"{MAIN}-runtime"


@pytest.fixture(scope="module")
def out() -> Path:
    result, out = synth_app(
        REPO
        / "use-cases/business-operations/fixtures/contract/business-operations.yaml"
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return out


def _resources(out: Path, stack: str) -> dict:
    return json.loads((out / f"{stack}.template.json").read_text())["Resources"]


def _statements(resources: dict, policy_prefix: str) -> list[dict]:
    return [
        s
        for k, r in resources.items()
        if r["Type"] == "AWS::IAM::Policy" and k.startswith(policy_prefix)
        for s in r["Properties"]["PolicyDocument"]["Statement"]
    ]


def _actions(statement: dict) -> list[str]:
    a = statement["Action"]
    return a if isinstance(a, list) else [a]


# ── The agent guardrail's content ──


def test_the_guardrail_filters_prompt_attacks_on_input_and_says_it_blocked():
    policy = uc_runtime.guardrail_policy()
    (attack,) = [
        f
        for f in policy["ContentPolicyConfig"]["FiltersConfig"]
        if f["Type"] == "PROMPT_ATTACK"
    ]
    # Output strength must be NONE: Amazon Bedrock rejects any other for PROMPT_ATTACK.
    assert (attack["InputStrength"], attack["OutputStrength"]) == ("HIGH", "NONE")
    assert "guardrail" in policy["BlockedInputMessaging"]
    assert "guardrail" in policy["BlockedOutputsMessaging"]


def test_the_guardrail_has_no_denied_topic():
    # A topic short enough for the classic tier blocked SOP-directed releases; the
    # run checks, the amount gate, approvals and the write guards bound the agent instead.
    assert "TopicPolicyConfig" not in uc_runtime.guardrail_policy()


def test_aws_credentials_are_blocked_and_vendor_identifiers_masked():
    actions = {
        e["Type"]: e["Action"]
        for e in uc_runtime.guardrail_policy()["SensitiveInformationPolicyConfig"][
            "PiiEntitiesConfig"
        ]
    }
    assert actions["AWS_ACCESS_KEY"] == actions["AWS_SECRET_KEY"] == "BLOCK"
    assert actions["US_BANK_ACCOUNT_NUMBER"] == "ANONYMIZE"


# ── The run-event channel ──


def test_browsers_subscribe_with_the_console_client_and_only_iam_publishes(out):
    resources = _resources(out, RUNTIME)
    (api,) = [r for r in resources.values() if r["Type"] == "AWS::AppSync::Api"]
    config = api["Properties"]["EventConfig"]
    assert config["DefaultPublishAuthModes"] == [{"AuthType": "AWS_IAM"}]
    assert config["DefaultSubscribeAuthModes"] == [
        {"AuthType": "AMAZON_COGNITO_USER_POOLS"}
    ]
    assert config["ConnectionAuthModes"] == [{"AuthType": "AMAZON_COGNITO_USER_POOLS"}]
    (pool,) = [p for p in config["AuthProviders"] if "CognitoConfig" in p]
    # The pool is shared: any other client on it could otherwise watch every run.
    assert "consoleclientid" in json.dumps(pool["CognitoConfig"]["AppIdClientRegex"])


def test_every_run_publishes_under_one_namespace_and_nowhere_wider(out):
    resources = _resources(out, RUNTIME)
    namespaces = [
        r["Properties"]["Name"]
        for r in resources.values()
        if r["Type"] == "AWS::AppSync::ChannelNamespace"
    ]
    assert namespaces == [uc_runtime.RUN_EVENTS_NAMESPACE]
    publish = [
        s
        for s in _statements(resources, "RuntimeRole")
        if any(a.startswith("appsync:") for a in _actions(s))
    ]
    assert len(publish) == 1
    assert _actions(publish[0]) == ["appsync:EventPublish"]
    assert json.dumps(publish[0]["Resource"]).endswith(
        f'"/channelNamespace/{uc_runtime.RUN_EVENTS_NAMESPACE}"]]}}'
    )


def test_the_runtime_is_told_where_to_publish(out):
    (rt,) = [
        r
        for r in _resources(out, RUNTIME).values()
        if r["Type"] == "AWS::BedrockAgentCore::Runtime"
    ]
    env = rt["Properties"]["EnvironmentVariables"]
    assert env["RUN_EVENTS_NAMESPACE"] == uc_runtime.RUN_EVENTS_NAMESPACE
    assert "RunEvents" in json.dumps(env["RUN_EVENTS_HTTP_DOMAIN"])


def test_the_runtime_touches_only_its_own_memory_actors(out):
    resources = _resources(out, RUNTIME)
    memory = [
        s
        for s in _statements(resources, "RuntimeRole")
        if "bedrock-agentcore:ListEvents" in _actions(s)
    ]
    # One statement, conditioned: an unconditioned one beside it would undo the partition.
    assert len(memory) == 1
    assert memory[0]["Condition"] == {
        "StringLike": {"bedrock-agentcore:actorId": "uc-business-operations:*"}
    }
    # Retrieval carries the namespace, not the actor: held to the same actors there.
    (retrieve,) = [
        s
        for s in _statements(resources, "RuntimeRole")
        if "bedrock-agentcore:RetrieveMemoryRecords" in _actions(s)
    ]
    assert retrieve["Condition"] == {
        "StringLikeIfExists": dict.fromkeys(
            ("bedrock-agentcore:namespace", "bedrock-agentcore:namespacePath"),
            "/strategies/*/actors/uc-business-operations:*",
        )
    }
    (rt,) = [
        r for r in resources.values() if r["Type"] == "AWS::BedrockAgentCore::Runtime"
    ]
    assert (
        rt["Properties"]["EnvironmentVariables"]["MEMORY_ACTOR_PREFIX"]
        == "uc-business-operations:"
    )


def test_the_agent_prefixes_its_memory_actor_id(monkeypatch):
    # auth.py imports PyJWT, which this job lacks: run the one function on its own.
    import ast
    import re

    source = (REPO / "use-cases/business-operations/agent/utils/auth.py").read_text()
    (fn,) = [
        n
        for n in ast.parse(source).body
        if isinstance(n, ast.FunctionDef) and n.name == "to_memory_actor_id"
    ]
    scope = {"os": os, "_ACTOR_ID_DISALLOWED": re.compile(r"[^a-zA-Z0-9\-_/:]")}
    exec(ast.get_source_segment(source, fn), scope)  # noqa: S102  # nosec B102 - repo source
    monkeypatch.setenv("MEMORY_ACTOR_PREFIX", "uc-business-operations:")
    assert scope["to_memory_actor_id"]("a@b.c") == "uc-business-operations:a_b_c"


# ── The run route ──


def test_the_run_route_reads_only_its_delegating_secret(out):
    secrets = [
        s["Resource"]
        for s in _statements(_resources(out, MAIN), "RunsFunction")
        if any(a.startswith("secretsmanager:") for a in _actions(s))
    ]
    # Never a SAP credential: a run reaches SAP through the Gateway, as the agent.
    assert secrets == [{"Ref": "DelegatingClientSecretF0D92575"}]


# ── SAP and agent knowledge ──


def test_agent_knowledge_pauses_when_idle_and_cannot_batch(tmp_path):
    from test_business_operations_knowledge import _synth

    resources = _synth(tmp_path, {"agent_knowledge": {"enabled": True}})["Resources"]
    (cluster,) = [
        r["Properties"]
        for r in resources.values()
        if r["Type"] == "AWS::RDS::DBCluster"
    ]
    assert cluster["EnableHttpEndpoint"] is True
    assert cluster["ServerlessV2ScalingConfiguration"]["MinCapacity"] == 0
    actions = {
        a
        for r in resources.values()
        if r["Type"] == "AWS::IAM::Policy"
        for s in r["Properties"]["PolicyDocument"]["Statement"]
        for a in _actions(s)
    }
    assert "rds-data:ExecuteStatement" in actions
    assert "rds-data:BatchExecuteStatement" not in actions


# ── Log retention and stable asset hashes ──


def _retention_names(resources: dict) -> list[str]:
    return [
        json.dumps(r["Properties"]["LogGroupName"])
        for r in resources.values()
        if r["Type"] == "Custom::LogRetention"
    ]


def test_log_groups_aws_creates_for_us_get_the_platforms_retention(out):
    # AgentCore, CodeBuild and API Gateway create these themselves, with no retention.
    runtime = _retention_names(_resources(out, RUNTIME))
    assert len(runtime) == 2
    assert any("/aws/bedrock-agentcore/runtimes/" in n for n in runtime)
    assert any("/aws/codebuild/" in n for n in runtime)
    stages = _retention_names(_resources(out, MAIN))
    assert stages and all("API-Gateway-Execution-Logs_" in n for n in stages)
    # The SAP runtimes' are the SAP layer's (layers/sap/tests/test_sap_layer.py).


def test_every_asset_leaves_untracked_bytecode_out_of_its_hash():
    # A __pycache__ one lane has and another lacks would otherwise republish the
    # asset, and replace a layer, on every deploy from the other lane.
    import ast

    uc = REPO / "use-cases" / "business-operations"
    missing = []
    for path in [
        uc / "stack.py",
        REPO / "sap_kit" / "functions.py",
        *sorted((uc / "business_operations").glob("*.py")),
    ]:
        for node in ast.walk(ast.parse(path.read_text())):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"from_asset", "asset", "Asset"}
            ):
                continue
            kws = {k.arg: k.value for k in node.keywords}
            if "asset_hash" in kws:  # hashed over named files only
                continue
            if "ASSET_EXCLUDE" not in ast.unparse(kws.get("exclude", ast.List([]))):
                missing.append(f"{path.name}:{node.lineno}")
    assert not missing, missing


def test_every_ssm_parameter_a_stack_reads_comes_from_a_stack_it_depends_on(out):
    # One `deploy.sh deploy` orders stacks by the assembly's dependencies alone, so on
    # a fresh environment a parameter written by a stack that is not upstream of its
    # reader would not exist yet when the reader deploys.
    manifest = json.loads((out / "manifest.json").read_text())
    stacks = {
        n: a
        for n, a in manifest["artifacts"].items()
        if a["type"] == "aws:cloudformation:stack"
    }
    templates = {
        n: json.loads((out / a["properties"]["templateFile"]).read_text())
        for n, a in stacks.items()
    }

    def upstream(name: str, seen: set[str]) -> set[str]:
        for dep in set(stacks[name].get("dependencies", [])) & stacks.keys() - seen:
            seen.add(dep)
            upstream(dep, seen)
        return seen

    writers = {
        r["Properties"]["Name"]: n
        for n, t in templates.items()
        for r in t["Resources"].values()
        if r["Type"] == "AWS::SSM::Parameter"
        and isinstance(r["Properties"].get("Name"), str)
    }
    assert len(writers) > 10 and any(MAIN in w for w in writers.values())
    reads = [
        (n, p["Default"])
        for n, t in templates.items()
        for p in t.get("Parameters", {}).values()
        if str(p.get("Type", "")).startswith("AWS::SSM::Parameter::Value")
        and writers.get(p.get("Default"), n) != n
    ]
    assert len(reads) > 10
    unordered = [
        f"{n} reads {name} from {writers[name]}"
        for n, name in reads
        if writers[name] not in upstream(n, set())
    ]
    assert not unordered, unordered


# ── What app.py hands the core stacks for this use case (tests/test_app_wiring.py) ──

CONTRACT = REPO / "use-cases/business-operations/fixtures/contract"


def test_the_gateway_gets_its_clients_only_while_it_is_enabled():
    on = _client_names(CONTRACT / "business-operations.yaml")
    off = _client_names(CONTRACT / "business-operations-off.yaml")
    assert any("business-operations" in n for n in on)
    assert not any("business-operations" in n for n in off)


def test_pattern_none_deploys_it_without_the_sample_orchestrator():
    config = yaml.safe_load((CONTRACT / "business-operations.yaml").read_text())
    config["agents"] = {"pattern": "none"}
    result, out = synth_app(yaml.safe_dump(config))
    assert result.returncode == 0, result.stderr[-2000:]
    built = {t.name.removesuffix(".template.json") for t in out.glob("*.template.json")}
    assert built == set(PlatformConfig.model_validate(config).expected_stacks())
    assert RUNTIME in built
    assert not any(name.endswith("-runtime-orchestrator") for name in built)
