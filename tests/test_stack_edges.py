# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Use-case and layer stacks depend on the stacks whose SSM parameters they read.

`deploy.sh` deploys several stacks at once, so these edges are the deploy order. Unit
tests over a small app first, then a synthesized use case on the SAP layer (each use
case's own footprint is checked in its tests).
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 — running our own app, no shell
import sys
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_ssm as ssm

from infra_utils import stack_edges
from tests.app_synth import synth_app

REPO = Path(__file__).resolve().parents[1]
PREFIX = "agentcore-workshop-dev"


def _deps(stack: cdk.Stack) -> set[str]:
    return {d.node.id for d in stack.dependencies}


def _app():
    app = cdk.App()
    core = {n: cdk.Stack(app, n) for n in ("auth", "gateway", "memory", "slow")}
    for name, stack in core.items():
        ssm.StringParameter(
            stack, "Out", parameter_name=f"/p/e/{name}/id", string_value=name
        )
    return app, core


def test_a_stack_depends_on_what_it_reads_and_nothing_else():
    app, core = _app()
    reader = cdk.Stack(app, "uc-a")
    ssm.StringParameter.value_for_string_parameter(reader, "/p/e/auth/id")
    other = cdk.Stack(app, "uc-a-runtime")
    cdk.CfnResource(
        other,
        "R",
        type="Custom::X",
        properties={"Env": {"M": "{{resolve:ssm:/p/e/memory/id}}"}},
    )
    stack_edges.add_read_edges(
        [reader, other],
        [core["auth"], core["memory"]],
        stack_edges.publishers(app),
        "uc-a",
    )
    assert _deps(reader) == {"auth"}
    assert _deps(other) == {"memory"}


def test_a_required_stack_nothing_reads_is_an_edge_on_every_stack():
    # A deploy-time read the walk cannot see keeps the manifest's coarse edge.
    app, core = _app()
    a, b = cdk.Stack(app, "uc-a"), cdk.Stack(app, "uc-a-two")
    ssm.StringParameter.value_for_string_parameter(a, "/p/e/auth/id")
    stack_edges.add_read_edges(
        [a, b], [core["auth"], core["slow"]], stack_edges.publishers(app), "uc-a"
    )
    assert _deps(a) == {"auth", "slow"}
    assert _deps(b) == {"slow"}


def test_reading_its_own_parameter_or_an_unpublished_one_adds_no_edge():
    app, _ = _app()
    stack = cdk.Stack(app, "uc-a")
    ssm.StringParameter(stack, "Out", parameter_name="/p/e/uc/x", string_value="1")
    ssm.StringParameter.value_for_string_parameter(stack, "/p/e/uc/x")
    ssm.StringParameter.value_for_string_parameter(stack, "/elsewhere/y")
    stack_edges.add_read_edges([stack], [], stack_edges.publishers(app), "uc-a")
    assert _deps(stack) == set()


def test_policy_stacks_are_chained_in_an_order_the_edges_allow():
    app = cdk.App()
    first, second, free, _plain = (
        cdk.Stack(app, n) for n in ("first", "second", "free", "plain")
    )
    second.add_stack_dependency(first)
    for stack in (second, first, free):
        cdk.CfnResource(stack, "P", type=stack_edges.POLICY)
    chain = stack_edges.chain_policy_stacks(app)
    assert chain.index("first") < chain.index("second")
    assert len(chain) == 3 and "plain" not in chain
    # Every pair is ordered one way or the other: never two at once.
    ordered = {s: stack_edges._ancestors(app.node.find_child(s)) for s in chain}
    for i, a in enumerate(chain):
        for b in chain[i + 1 :]:
            assert a in ordered[b]


# ── Synthesized ──

HELLO_SAP = REPO / "tests/fixtures/contract/hello-sap.yaml"


def graph(out: Path) -> dict[str, set[str]]:
    """The synthesized stacks' dependency edges, by name without the prefix."""
    artifacts = json.loads((out / "manifest.json").read_text())["artifacts"]
    return {
        name.removeprefix(f"{PREFIX}-"): {
            d.removeprefix(f"{PREFIX}-")
            for d in a.get("dependencies", [])
            if not d.endswith(".assets")
        }
        for name, a in artifacts.items()
        if a["type"] == "aws:cloudformation:stack"
    }


def closure(graph: dict[str, set[str]], node: str) -> set[str]:
    seen, todo = set(), list(graph[node])
    while todo:
        n = todo.pop()
        if n not in seen:
            seen.add(n)
            todo += graph[n]
    return seen


_NO_RESOLVE = """
import runpy, sys
from infra_utils import stack_edges
stack_edges.reads = lambda stack: set()
sys.argv = ["app.py"]
runpy.run_path("app.py", run_name="__main__")
"""


def assert_resolving_reads_changes_no_template(config: str, tmp_path: Path) -> None:
    """stack_edges.reads resolves every resource's properties before synth; a cached
    Lazy resolved that early would freeze. Same templates with and without it."""
    result, out = synth_app(config)
    assert result.returncode == 0, result.stderr[-3000:]
    path = tmp_path / "platform.yaml"
    path.write_text(config)
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"AWS_REGION", "AWS_DEFAULT_REGION", "PROJECT_NAME", "ENVIRONMENT"}
    } | {
        "PLATFORM_CONFIG": str(path),
        "CDK_DEFAULT_ACCOUNT": "111111111111",
        "CDK_DEFAULT_REGION": "us-east-1",
        "CDK_OUTDIR": str(tmp_path / "cdk.out"),
        "JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION": "1",
    }
    bare = subprocess.run(  # nosec B603 — fixed argv
        [sys.executable, "-B", "-c", _NO_RESOLVE],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert bare.returncode == 0, bare.stderr[-3000:]
    templates = sorted(out.glob("*.template.json"))
    assert templates
    for t in templates:
        assert t.read_text() == (tmp_path / "cdk.out" / t.name).read_text(), t.name


def test_resolving_templates_for_their_reads_changes_no_template(tmp_path):
    assert_resolving_reads_changes_no_template(HELLO_SAP.read_text(), tmp_path)


def test_a_use_case_on_a_layer_waits_for_the_layer_and_what_it_reads():
    result, out = synth_app(HELLO_SAP)
    assert result.returncode == 0, result.stderr[-3000:]
    g = graph(out)
    assert {"auth", "gateway", "layer-sap"} <= g["uc-hello-sap"]
    assert "observability" not in closure(g, "uc-hello-sap")


def test_a_requires_entry_the_footprint_lacks_is_refused_not_dropped(tmp_path):
    from tests.test_layers import FIXTURES, synth

    result = synth(
        tmp_path,
        {"layers": {}, "use_cases": {"needs-demo": {}}},
        use_cases=FIXTURES / "use-cases",
    )
    assert result.returncode != 0
    assert "layer 'demo'" in result.stderr
