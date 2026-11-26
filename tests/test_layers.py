# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The layer extension point: manifests, `requires: [layer-<name>]`, order, guards.

The synth tests run app.py against tests/fixtures/platform/, a layers/ and a
use-cases/ directory of their own, by pointing platform_config at them before
app.py imports it (`synth`). Other test files reuse `synth` for the real layers.
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 — running our own app, no shell
import sys
from pathlib import Path

import pytest
import yaml

import infra_utils.platform_config as pc
from infra_utils import synth_guards
from infra_utils.platform_config import LayerManifest, PlatformConfig

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "platform"
PREFIX = "agentcore-workshop-dev"

_RUNNER = """
import runpy, sys
from pathlib import Path
import infra_utils.platform_config as pc
from infra_utils import synth_guards
layers, use_cases = (Path(p) if p else None for p in sys.argv[1:3])
if layers:
    pc.LAYERS_DIR = layers
    pc.discover_layers = lambda root=layers: pc._discover(root, pc.LayerManifest)
if use_cases:
    pc.USE_CASES_DIR = use_cases
    pc.discover_use_cases = lambda root=use_cases: pc._discover(root, pc.UseCaseManifest)
sys.argv = ["app.py"]
runpy.run_path("app.py", run_name="__main__")
"""


def synth(
    tmp_path: Path,
    config: dict,
    *,
    layers: Path | None = None,
    use_cases: Path | None = None,
) -> subprocess.CompletedProcess:
    """app.py over `config`, optionally with its own layers/ and use-cases/ roots."""
    path = tmp_path / "platform.yaml"
    path.write_text(yaml.safe_dump(config))
    out = tmp_path / "cdk.out"
    env = {
        **{
            k: v
            for k, v in os.environ.items()
            if k
            not in {"AWS_REGION", "AWS_DEFAULT_REGION", "PROJECT_NAME", "ENVIRONMENT"}
        },
        "PLATFORM_CONFIG": str(path),
        "CDK_DEFAULT_ACCOUNT": "111111111111",
        "CDK_DEFAULT_REGION": config.get("region", "us-east-1"),
        "CDK_OUTDIR": str(out),
        "JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION": "1",
    }
    return subprocess.run(  # nosec B603 — fixed argv
        [
            sys.executable,
            "-B",
            "-c",
            _RUNNER,
            str(layers or ""),
            str(use_cases or ""),
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def template(tmp_path: Path, stack: str) -> dict:
    return json.loads((tmp_path / "cdk.out" / f"{stack}.template.json").read_text())


def dependencies(tmp_path: Path, stack: str) -> list[str]:
    artifacts = json.loads((tmp_path / "cdk.out" / "manifest.json").read_text())
    return artifacts["artifacts"][stack].get("dependencies", [])


@pytest.fixture
def fixture_roots(monkeypatch):
    """platform_config reads tests/fixtures/platform/ for layers and use cases."""
    layers, use_cases = FIXTURES / "layers", FIXTURES / "use-cases"
    monkeypatch.setattr(
        pc, "discover_layers", lambda root=layers: pc._discover(root, LayerManifest)
    )
    monkeypatch.setattr(
        pc,
        "discover_use_cases",
        lambda root=use_cases: pc._discover(root, pc.UseCaseManifest),
    )
    return layers, use_cases


def _manifest(**kw) -> dict:
    return {"name": "x1", "owner": "o", "summary": "s", "stacks": ["layer-x1"], **kw}


def test_layer_stacks_are_namespaced():
    with pytest.raises(ValueError, match="layer-x1"):
        LayerManifest.model_validate(_manifest(stacks=["uc-x1"]))
    with pytest.raises(ValueError, match="layer-x1"):
        LayerManifest.model_validate(_manifest(stacks=["gateway"]))


def test_a_layer_requires_core_stacks_only():
    with pytest.raises(ValueError, match="core stacks only"):
        LayerManifest.model_validate(_manifest(requires=["layer-other"]))


def test_a_layer_has_no_gateway_clients():
    # A layer is called; it never calls the Gateway.
    with pytest.raises(ValueError):
        LayerManifest.model_validate(
            _manifest(gateway_clients=[{"name": "a", "scopes": ["r"]}])
        )


def test_unknown_layer_is_a_validation_error(fixture_roots):
    with pytest.raises(ValueError, match="demo"):
        PlatformConfig.model_validate({"layers": {"no-such-layer": {}}})


def test_a_use_case_requiring_a_disabled_layer_fails_at_config_load(fixture_roots):
    with pytest.raises(ValueError) as e:
        PlatformConfig.model_validate({"use_cases": {"needs-demo": {}}})
    message = str(e.value)
    assert "requires layer 'demo'" in message
    assert "`layers:`" in message


def test_a_use_case_requiring_an_absent_core_stack_fails_at_config_load(monkeypatch):
    # app.py used to drop a requires entry it could not find and synth anyway.
    real = pc.discover_use_cases()
    tenant = pc.UseCaseManifest.model_validate(
        {
            "name": "tenant-a",
            "owner": "o",
            "summary": "s",
            "platform_interface": pc.PLATFORM_INTERFACE_VERSION,
            "requires": ["networking"],
            "stacks": ["uc-tenant-a"],
        }
    )
    monkeypatch.setattr(
        pc, "discover_use_cases", lambda root=None: {**real, "tenant-a": tenant}
    )
    with pytest.raises(ValueError, match="networking"):
        PlatformConfig.model_validate({"use_cases": {"tenant-a": {}}})
    PlatformConfig.model_validate(
        {"use_cases": {"tenant-a": {}}, "security": {"networking": True}}
    )


def test_layers_sit_between_core_and_use_cases(fixture_roots):
    config = PlatformConfig.model_validate(
        {"layers": {"demo": {}}, "use_cases": {"needs-demo": {}}}
    )
    stacks = config.expected_stacks()
    assert stacks[-2:] == [f"{PREFIX}-layer-demo", f"{PREFIX}-uc-needs-demo"]
    assert stacks.index(f"{PREFIX}-observability") < stacks.index(
        f"{PREFIX}-layer-demo"
    )


def test_no_layers_leaves_the_footprint_alone():
    assert not any(
        "layer-" in s for s in PlatformConfig.model_validate({}).expected_stacks()
    )


def test_a_layer_alone_synthesizes_after_its_core_requires(tmp_path):
    result = synth(tmp_path, {"layers": {"demo": {}}}, layers=FIXTURES / "layers")
    assert result.returncode == 0, result.stderr[-2000:]
    assert f"{PREFIX}-gateway" in dependencies(tmp_path, f"{PREFIX}-layer-demo")


def test_a_use_case_depends_on_its_layer_and_imports_its_lib(tmp_path):
    result = synth(
        tmp_path,
        {"layers": {"demo": {}}, "use_cases": {"needs-demo": {}}},
        layers=FIXTURES / "layers",
        use_cases=FIXTURES / "use-cases",
    )
    assert result.returncode == 0, result.stderr[-2000:]
    deps = dependencies(tmp_path, f"{PREFIX}-uc-needs-demo")
    assert {f"{PREFIX}-layer-demo", f"{PREFIX}-gateway"} <= set(deps)
    params = template(tmp_path, f"{PREFIX}-uc-needs-demo")["Resources"]
    (value,) = [
        r["Properties"]["Value"]
        for r in params.values()
        if r["Type"] == "AWS::SSM::Parameter"
    ]
    assert value == "from the demo layer's lib"


def test_app_runs_every_guard_over_every_owner(tmp_path):
    # The fixture layer violates every guard, and the use case one of the per-owner
    # ones, so one synth proves app.py runs them all, over layers and use cases.
    result = synth(
        tmp_path,
        {
            "layers": {"demo": {"violate": True}},
            "use_cases": {"needs-demo": {"wide_grant": True}},
            "gateway": {
                "interceptor_extensions": [
                    {"path": "layers/sap/sap_check", "target_prefixes": ["sap-"]}
                ]
            },
        },
        layers=FIXTURES / "layers",
        use_cases=FIXTURES / "use-cases",
    )
    assert result.returncode != 0
    for guard in synth_guards.GUARDS:
        assert guard.title in result.stderr, guard.title
    assert f"{PREFIX}-layer-demo/GatewayRole" in result.stderr
    assert f"{PREFIX}-uc-needs-demo/GatewayRole" in result.stderr
    assert "target 'sap-squatter' starts with 'sap-'" in result.stderr


def test_every_layer_ships_a_verify():
    for root in (pc.LAYERS_DIR, FIXTURES / "layers"):
        for name in pc._discover(root, LayerManifest):
            assert (root / name / "verify.py").is_file(), name


def test_layer_verify_runs_after_core_and_before_use_cases():
    sys.path.insert(0, str(REPO / "scripts"))
    from verify import checks_for

    names = [
        n
        for n, _ in checks_for(
            {"gateway"}, "orchestrator", use_cases=["u"], layers=["l"]
        )
    ]
    assert names[-2:] == ["layer l", "use case u"]


def test_a_target_may_not_carry_another_owners_check_prefix():
    from infra_utils.platform_config import InterceptorExtension, UseCaseManifest

    def target(name):
        return {
            "s": {
                "Resources": {
                    "T": {
                        "Type": "AWS::BedrockAgentCore::GatewayTarget",
                        "Properties": {"Name": name},
                    }
                }
            }
        }

    extensions = (
        InterceptorExtension(path="layers/sap/sap_check", target_prefixes=["sap-"]),
        InterceptorExtension(
            path="use-cases/hello-sap/hello_sap_check",
            target_prefixes=["hello-sap-", "sap-"],
        ),
    )

    def findings(manifest, name):
        scope = synth_guards.Scope(PREFIX, "/p", manifest, extensions)
        return synth_guards._foreign_prefix_targets(target(name), scope)

    sap = LayerManifest(name="sap", owner="t", summary="t", stacks=["layer-sap"])
    v = pc.PLATFORM_INTERFACE_VERSION
    uc = UseCaseManifest(
        name="hello-sap", owner="t", summary="t", platform_interface=v, stacks=["uc-a"]
    )
    squatter = UseCaseManifest(
        name="hello-sap-x",
        owner="t",
        summary="t",
        platform_interface=v,
        stacks=["uc-x"],
    )
    # Each owner names targets with its own prefixes, a prefix two owners share included.
    assert findings(sap, "sap-s4") == []
    assert findings(uc, "hello-sap-tickets") == []
    # Another tenant may not take either.
    assert findings(squatter, "hello-sap-x-orders")
    assert findings(squatter, "sap-s4")
    assert findings(sap, "hello-sap-sap-s4")
