# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The business-operations use case: its name checks, config, and synth both ways.

The platform must synthesize with the use case off (the tenancy rule) and on.
check-contract.sh also synthesizes it under deployment.mode: production
(use-cases/business-operations/fixtures/contract/business-operations-production.yaml).
"""

from __future__ import annotations

import json
import re
import subprocess  # nosec B404 — running our own app and scripts, no shell
import sys
from pathlib import Path

import botocore.session
import pytest

REPO = Path(__file__).resolve().parents[3]
UC = REPO / "use-cases" / "business-operations"
sys.path.insert(0, str(UC))

from app_synth import synth_app
from business_operations import config as uc_config
from business_operations import names
from sap_layer import names as sap_names

from sap_kit import names as core_names

STACK = "agentcore-workshop-dev-uc-business-operations"


# ── The limits are the service models' ──


def _member(service: str, operation_or_shape: str, member: str):
    model = botocore.session.get_session().get_service_model(service)
    if operation_or_shape in model.operation_names:
        shape = model.operation_model(operation_or_shape).input_shape
    else:
        shape = model.shape_for(operation_or_shape)
    return shape.members[member].metadata


def test_limits_match_the_botocore_models():
    runtime = _member(
        "bedrock-agentcore-control", "CreateAgentRuntime", "agentRuntimeName"
    )
    assert runtime["pattern"] == core_names.RUNTIME_NAME.pattern
    assert (
        names.RUNTIME_NAME_MAX
        == 48
        == int(re.search(r"\{0,(\d+)\}", runtime["pattern"])[1]) + 1
    )
    policy = _member("bedrock-agentcore-control", "CreatePolicy", "name")
    assert (policy["max"], policy["pattern"]) == (
        core_names.POLICY_NAME_MAX,
        core_names.POLICY_NAME.pattern,
    )
    target = _member("bedrock-agentcore-control", "CreateGatewayTarget", "name")
    assert target["pattern"] == core_names.TARGET_NAME.pattern
    tool = _member("bedrock-runtime", "ToolSpecification", "name")
    assert (tool["max"], tool["pattern"]) == (
        core_names.TOOL_NAME_MAX,
        core_names.TOOL_NAME.pattern,
    )


# ── Runtime name: 48 ──


def test_runtime_name_mirrors_runtime_stack():
    assert names.runtime_name("agentcore-workshop", "dev") == (
        "agentcore_workshop_dev_business_operations"
    )
    assert (
        names.runtime_name("abo", "hotel", generation=2)
        == "abo_hotel_business_operations_g2"
    )


def test_runtime_name_allows_a_28_character_prefix_and_no_more():
    assert len(names.runtime_name("p" * 24, "dev")) == 48  # 'ppp…_dev' is 28
    with pytest.raises(ValueError, match="may be at most 28"):
        names.runtime_name("p" * 25, "dev")


def test_regenerated_runtime_name_allows_25():
    names.runtime_name("p" * 21, "dev", generation=2)
    with pytest.raises(ValueError, match="at most 25 with the _g2 suffix"):
        names.runtime_name("p" * 22, "dev", generation=2)


# ── Tool names: 64 ──


def test_tool_name_is_target_prefixed():
    assert names.tool_name("business-operations-case-management", "get_case_state") == (
        "business-operations-case-management___get_case_state"
    )


def test_tool_name_over_64_fails():
    with pytest.raises(ValueError, match="Converse allows 64"):
        names.tool_name("t" * 40, "x" * 22)


def test_tool_name_refuses_a_bad_target():
    with pytest.raises(ValueError, match="Gateway target name"):
        names.tool_name("bad_target", "x")


def test_sap_system_names_cap_at_16():
    assert sap_names.SAP_SYSTEM_MAX == 16
    target = sap_names.sap_target("s" * 16)
    assert len(names.tool_name(target, "odata_function_import")) <= 64
    with pytest.raises(ValueError, match="at most 16 fit"):
        sap_names.sap_target("s" * 17)


# ── Cedar policy names: 48, unique per account and Region ──


def test_policy_name_is_the_policy_plus_a_stable_hash():
    name = names.policy_name("agentcore-workshop", "dev", "read_cases")
    assert name == "read_cases_fd9e8e24"  # changing it replaces every deployed policy
    assert "workshop" not in name


def test_policy_names_differ_per_deployment():
    assert names.policy_name("abo", "alpha", "read_cases") != names.policy_name(
        "abo", "hotel", "read_cases"
    )


def test_policy_name_too_long_or_malformed_fails():
    names.policy_name("p", "dev", "x" * 39)
    with pytest.raises(ValueError, match="at most 39"):
        names.policy_name("p", "dev", "x" * 40)
    with pytest.raises(ValueError, match="start with a letter"):
        names.policy_name("p", "dev", "read-cases")


# ── Config ──


def test_config_block_refuses_unknown_keys():
    uc_config.load({})
    uc_config.load(None)
    with pytest.raises(ValueError, match="use_cases.business-operations"):
        uc_config.load({"cedar_mode": "ENFORCE"})


def test_a_schedule_must_be_an_eventbridge_expression():
    uc_config.load({"pipeline": {"poller_schedule": "cron(0 6 * * ? *)"}})
    for bad in ("5 minutes", "rate(5 minutes", "every(1 hour)", "rate()"):
        with pytest.raises(ValueError, match="pattern"):
            uc_config.load({"pipeline": {"batch_schedule": bad}})


def test_the_graph_mailbox_is_not_shipped():
    with pytest.raises(ValueError, match="mailbox"):
        uc_config.load({"pipeline": {"mailbox": {"enabled": True}}})


# ── Synth, both ways ──


def _synth(tmp_path: Path, config: Path) -> subprocess.CompletedProcess:
    return synth_app(config)[0]


def test_platform_synthesizes_with_the_use_case_off():
    result, out = synth_app(REPO / "presets/greenfield.yaml")
    assert result.returncode == 0, result.stderr[-2000:]
    assert not (out / f"{STACK}.template.json").exists()
    assert (
        "business-operations"
        not in (out / "agentcore-workshop-dev-gateway.template.json").read_text()
    )


def test_platform_synthesizes_with_the_use_case_on():
    result, out = synth_app(
        REPO
        / "use-cases/business-operations/fixtures/contract/business-operations.yaml"
    )
    assert result.returncode == 0, result.stderr[-2000:]
    template = json.loads((out / f"{STACK}.template.json").read_text())
    resources = template["Resources"].values()
    policies = [
        r["Properties"]
        for r in resources
        if r["Type"] == "AWS::BedrockAgentCore::Policy"
    ]
    assert sorted(p["Name"] for p in policies) == [
        f"{n}_fd9e8e24"
        for n in (
            "load_sop",
            "notifications",
            "read_cases",
            "tickets_create",
            "tickets_get",
            "write_cases",
        )
    ]
    for p in policies:
        statement = json.dumps(p["Definition"])
        assert "forbid(" not in statement, "Cedar copies no check a Lambda makes"
        # Reads included: an unconditioned permit lets every tenant's client in.
        assert (
            'principal.getTag(\\"scope\\") == \\"uc-business-operations/' in statement
        )
    groups = [
        r["Properties"]["GroupName"]
        for r in resources
        if r["Type"] == "AWS::Cognito::UserPoolGroup"
    ]
    # The fixed four, pack-admins in development, and every group a shipped pack's
    # entitlement names.
    assert sorted(groups) == [
        "business-operations-admins",
        "business-operations-ap-clerk",
        "business-operations-ap-manager",
        "business-operations-authors",
        "business-operations-case-workers",
        "business-operations-limit-owners",
        "business-operations-pack-admins",
        "business-operations-users",
    ]
    assert not [r for r in resources if r["Type"] == "AWS::Cognito::UserPoolUser"]


def test_the_run_checks_parameter_names_the_tables_and_the_cedar_mode():
    result, out = synth_app(
        REPO
        / "use-cases/business-operations/fixtures/contract/business-operations.yaml"
    )
    assert result.returncode == 0, result.stderr[-2000:]
    template = (out / f"{STACK}.template.json").read_text()
    resources = json.loads(template)["Resources"]
    (run_checks,) = [
        r["Properties"]
        for r in resources.values()
        if r["Type"] == "AWS::SSM::Parameter"
        and r["Properties"]["Name"].endswith(
            "/use-cases/business-operations/run-checks"
        )
    ]
    value = json.dumps(run_checks["Value"])
    for key in (
        "run_grants_table",
        "cases_table",
        "tickets_table",
        "config_table",
        "registry_bucket",
    ):
        assert key in value
    assert '\\"cedar_enforced\\":false' in value  # the fixture is LOG_ONLY
    # The check trusts it only when CloudFormation wrote it: it may read the history,
    # and the layer's `rewrite` can have CloudFormation write it again.
    assert "SapRewriteNonce" in json.dumps(run_checks["Description"])
    (logical,) = [
        k
        for k, r in resources.items()
        if r["Type"] == "AWS::SSM::Parameter" and r["Properties"] is run_checks
    ]
    reads = [
        st
        for r in resources.values()
        if r["Type"] == "AWS::IAM::Policy"
        for st in r["Properties"]["PolicyDocument"]["Statement"]
        if "ssm:GetParameterHistory" in st["Action"]
    ]
    assert len(reads) == 1 and {"Ref": logical} in reads[0]["Resource"]["Fn::Join"][1]


def test_the_use_case_refuses_to_synth_without_its_run_checks(tmp_path):
    config = tmp_path / "platform.yaml"
    config.write_text(
        "project: agentcore-workshop\nenvironment: dev\n"
        "layers:\n  sap:\n    access:\n      business-operations: {}\n"
        "use_cases:\n  business-operations: {}\n"
    )
    result = _synth(tmp_path, config)
    assert result.returncode != 0
    assert "needs its interceptor check" in result.stderr


def test_the_use_case_requires_the_sap_layer_at_config_load(tmp_path):
    config = tmp_path / "platform.yaml"
    config.write_text(
        "project: agentcore-workshop\nenvironment: dev\n"
        "use_cases:\n  business-operations: {}\n"
    )
    result = _synth(tmp_path, config)
    assert result.returncode != 0
    assert "requires layer 'sap', which platform.yaml does not enable" in result.stderr


def test_its_old_sap_block_says_where_it_moved():
    with pytest.raises(ValueError, match="moved to the SAP layer"):
        uc_config.load({"sap": {"systems": {}}})


def test_its_sap_stack_claims_its_apps_and_keeps_its_permit_names():
    result, out = synth_app(
        REPO
        / "use-cases/business-operations/fixtures/contract/business-operations-production.yaml"
    )
    assert result.returncode == 0, result.stderr[-2000:]
    sap = "agentcore-production-prod-uc-business-operations-sap"
    resources = json.loads((out / f"{sap}.template.json").read_text())["Resources"]
    # The permit names (the use case's hash) and logical ids it has always had, so
    # the move to the layer replaced no policy.
    policies = {
        k: r["Properties"]
        for k, r in resources.items()
        if r["Type"] == "AWS::BedrockAgentCore::Policy"
    }
    hashed = core_names.deployment_hash("agentcore-production", "prod", names.NAME)
    assert {k: p["Name"] for k, p in policies.items()} == {
        "Policysaps4read": f"sap_s4_read_{hashed}",
        "Policysaps4write": f"sap_s4_write_{hashed}",
    }
    assert resources["Policysaps4write"]["DependsOn"] == ["Policysaps4read"]
    (claim,) = [
        r["Properties"]["Value"]
        for r in resources.values()
        if r["Type"] == "AWS::SSM::Parameter"
        and r["Properties"]["Name"].endswith("/layers/sap/claims/business-operations")
    ]
    text = json.dumps(claim)
    assert "businessoperationswriterclientid" in text.replace("-", "").replace("/", "")
    assert "businessoperationsreaderclientid" in text.replace("-", "").replace("/", "")
    assert '\\"governed_by\\":\\"business_operations_check\\"' in text
    # The example's write set: function imports only, in the claim and the permit.
    assert '\\"write\\":{\\"s4\\":[\\"function_import\\"]}' in text
    permit = json.dumps(policies["Policysaps4write"])
    assert "odata_function_import" in permit
    assert "odata_update" not in permit and "odata_create" not in permit
    deps = json.loads((out / "manifest.json").read_text())["artifacts"][sap][
        "dependencies"
    ]
    assert "agentcore-production-prod-layer-sap" in deps
    # The run checks decide for the same apps on a SAP target, and pass every other.
    main = json.loads(
        (
            out / "agentcore-production-prod-uc-business-operations.template.json"
        ).read_text()
    )["Resources"]
    (run_checks,) = [
        r["Properties"]["Value"]
        for r in main.values()
        if r["Type"] == "AWS::SSM::Parameter"
        and r["Properties"]["Name"].endswith(
            "/use-cases/business-operations/run-checks"
        )
    ]
    assert "sap_callers" in json.dumps(run_checks) and "writerclientid" in json.dumps(
        run_checks
    ).replace("-", "").replace("/", "")


def test_a_too_long_prefix_fails_the_synth_with_the_reason(tmp_path):
    config = tmp_path / "platform.yaml"
    config.write_text(
        "project: a-very-long-project-name-x\nenvironment: dev\n"
        "layers:\n  sap:\n    access:\n      business-operations: {}\n"
        "use_cases:\n  business-operations: {}\n"
    )
    result = _synth(tmp_path, config)
    assert result.returncode != 0
    assert "may be at most 28" in result.stderr


def test_ops_refuses_an_unknown_verb():
    result = subprocess.run(  # nosec B603 — fixed argv
        [
            sys.executable,
            str(REPO / "scripts/usecase.py"),
            "run",
            "business-operations",
            "nope",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "status" in result.stderr


# ── platform.example.yaml merges into a preset and synthesizes clean ──


def _merge(base: dict, extra: dict) -> dict:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def test_the_example_config_merges_into_a_preset_and_loads():
    import yaml
    from business_operations import packs

    from infra_utils.platform_config import PlatformConfig

    raw = _merge(
        yaml.safe_load((REPO / "presets" / "greenfield.yaml").read_text()),
        yaml.safe_load((UC / "platform.example.yaml").read_text()),
    )
    platform = PlatformConfig.model_validate(raw)
    cfg = uc_config.load(platform.use_cases[names.NAME])
    # Every contact the shipped SOPs cite is in it, so a merge leaves no synth warning.
    assert packs.unconfigured_contacts(cfg) == []
    assert set(raw["layers"]["sap"]["systems"]) >= set(packs._named_systems())
    # Only the SOP tools' responses skip the egress scan, named as the Gateway names them.
    assert platform.security.egress_unscanned_responses == [
        names.tool_name(names.SOPS_TARGET, "load_sop"),
        names.tool_name(names.KNOWLEDGE_TARGET, "search_sap_sops"),
    ]


def test_the_quickstart_config_is_the_preset_plus_the_example_and_loads():
    """docs/quickstart.md copies this file whole, so it must not drift from the
    preset and the example it is built from."""
    import yaml

    from infra_utils.platform_config import PlatformConfig

    quick = yaml.safe_load((UC / "quickstart.platform.yaml").read_text())
    expected = _merge(
        _merge(
            yaml.safe_load((REPO / "presets" / "greenfield.yaml").read_text()),
            yaml.safe_load((UC / "platform.example.yaml").read_text()),
        ),
        {
            "deployment": {"platform_account": "000000000000"},
            "identity": {"self_sign_up": False},
            "agents": {"memory": {"strategies": []}},
            "use_cases": {names.NAME: {"test_data": {"enabled": True}}},
        },
    )
    assert quick == expected
    # The account is a placeholder on purpose: design refuses it and says what to put.
    quick["deployment"]["platform_account"] = "111122223333"
    platform = PlatformConfig.model_validate(quick)
    assert uc_config.load(platform.use_cases[names.NAME]).test_data.enabled


def test_the_quickstart_scripted_snippet_writes_a_valid_platform_yaml(tmp_path):
    """The quickstart's "Scripted" section writes platform.yaml with no hand edit: run its
    snippet as published, with every variable set, and the result validates."""
    import textwrap

    import yaml

    from infra_utils.platform_config import PlatformConfig

    doc = (UC / "docs" / "quickstart.md").read_text()
    section = doc.split("## Scripted run", 1)[1].split("\n## ", 1)[0]
    code = re.search(r"python3 - <<'EOF'\n(.*?)\n\s*EOF", section, re.DOTALL).group(1)
    (tmp_path / "use-cases").symlink_to(REPO / "use-cases")
    env = {
        "PATH": "/usr/bin:/bin",
        "PROJECT": "lab",
        "ENV": "t01",
        "REGION": "us-west-2",
        "ACCOUNT": "111122223333",
        "SAP_BASE_URL": "https://sap.example.org/sap/opu/odata/sap/",
        "SAP_CLIENT": "100",
        "SAP_SECRET_NAME": "lab/sap/s4",
        "CONTACT_EMAIL": "lab@example.org",
    }
    subprocess.run(  # nosec B603 — fixed argv
        [sys.executable, "-c", textwrap.dedent(code)], cwd=tmp_path, env=env, check=True
    )
    written = yaml.safe_load((tmp_path / "platform.yaml").read_text())
    platform = PlatformConfig.model_validate(written)
    assert (platform.project, platform.environment) == ("lab", "t01")
    assert written["layers"]["sap"]["systems"]["s4"]["secret_name"] == "lab/sap/s4"
    assert uc_config.load(platform.use_cases[names.NAME]).test_data.enabled


def test_the_example_names_exactly_the_writes_the_shipped_packs_use():
    """No `write: "*"` in shipped config: the example and the fixtures that copy it name
    each system's write operations, and the example's are the ones the packs call."""
    import yaml
    from sap_layer import names as sap_names

    by_tool = {t: op for op, t in sap_names.WRITE_TOOLS.items()}
    used: dict[str, set[str]] = {}
    for pack_file in (UC / "packs").glob("[!_]*/pack.json"):
        for tool in json.loads(pack_file.read_text())["gateway_tools"]:
            target, _, bare = tool.partition(core_names.TOOL_SEPARATOR)
            if target.startswith(sap_names.TARGET_PREFIX) and bare in by_tool:
                system = target.removeprefix(sap_names.TARGET_PREFIX)
                used.setdefault(system, set()).add(by_tool[bare])
    assert used, "no shipped pack writes to SAP: this test checks nothing"
    configs = [
        UC / "platform.example.yaml",
        *(UC / "fixtures" / "contract").glob("*.yaml"),
    ]
    for path in configs:
        row = (
            ((yaml.safe_load(path.read_text()).get("layers") or {}).get("sap") or {})
            .get("access", {})
            .get(names.NAME)
        )
        if row is not None:
            assert row.get("write", {}) != "*", path.name
    row = yaml.safe_load((UC / "platform.example.yaml").read_text())["layers"]["sap"]
    write = row["access"][names.NAME]["write"]
    assert {s: set(ops) for s, ops in write.items()} == used


def test_every_guardrail_consumer_names_a_published_version_not_draft():
    """The production fixture: platform runtimes, the Gateway and this use case."""
    result, out = synth_app(
        UC / "fixtures/contract/business-operations-production.yaml"
    )
    assert result.returncode == 0, result.stderr[-2000:]
    seen = set()
    for path in out.glob("*.template.json"):
        template = json.loads(path.read_text())
        resources = template["Resources"]
        parameters = template.get("Parameters", {})
        for logical_id, r in resources.items():
            props = r.get("Properties", {})
            env = props.get("Environment", {}).get("Variables") or props.get(
                "EnvironmentVariables", {}
            )
            for key, value in env.items():
                if not key.endswith("GUARDRAIL_VERSION"):
                    continue
                seen.add(key)
                where = f"{path.name} {logical_id}.{key}"
                if "Ref" in value:
                    # Another stack's version, read from SSM at every update.
                    assert parameters[value["Ref"]]["Default"].endswith(
                        "/persist-guardrail-version"
                    ), where
                else:
                    version = resources[value["Fn::GetAtt"][0]]
                    assert version["Type"] == "AWS::Bedrock::GuardrailVersion", where
        assert "DRAFT" not in path.read_text(), path.name
    assert seen == {"GUARDRAIL_VERSION", "PERSIST_GUARDRAIL_VERSION"}
