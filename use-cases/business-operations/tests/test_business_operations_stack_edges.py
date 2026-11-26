# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""business-operations' stacks depend on the stacks whose SSM parameters they read.

The synthesized footprint with one SAP system; infra_utils/stack_edges.py and its own
tests are in tests/test_stack_edges.py.
"""

from __future__ import annotations

import json
from pathlib import Path

from infra_utils import stack_edges
from tests.app_synth import synth_app  # the cache the helpers below share
from tests.test_stack_edges import (
    PREFIX,
    assert_resolving_reads_changes_no_template,
    closure,
    graph,
)

REPO = Path(__file__).resolve().parents[3]

_BO = (
    (REPO / "use-cases/business-operations/fixtures/contract/business-operations.yaml")
    .read_text()
    .replace(
        "  sap:\n",
        "  sap:\n    systems:\n      s4:\n        system_role: sandbox\n        base_url: https://sap.example.com/sap/opu/odata/sap/\n"
        '        client: "100"\n        secret_name: sap/s4\n',
        1,
    )
)


def test_use_case_stacks_wait_only_for_what_they_read():
    result, out = synth_app(_BO)
    assert result.returncode == 0, result.stderr[-3000:]
    g = graph(out)
    uc = "uc-business-operations"
    assert g[f"{uc}-console"] == {"auth"}
    # The ops topic has its own stack, so nothing waits for the runtime chain.
    assert "ops-topic" in g[uc] and "ops-topic" in g["layer-sap"]
    for name in (
        uc,
        f"{uc}-console",
        f"{uc}-knowledge",
        f"{uc}-sap",
        f"{uc}-runtime",
        "layer-sap",
    ):
        assert "observability" not in closure(g, name), name
    assert "memory" in g[f"{uc}-runtime"]
    # Its SAP permits name the layer's targets, so the SAP stack reads the layer.
    assert "layer-sap" in g[f"{uc}-sap"]
    assert "layer-sap" not in closure(g, f"{uc}-console")


def test_cedar_policy_stacks_never_deploy_at_once():
    result, out = synth_app(_BO)
    assert result.returncode == 0, result.stderr[-3000:]
    g = graph(out)
    artifacts = json.loads((out / "manifest.json").read_text())["artifacts"]
    with_policies = []
    for name, a in artifacts.items():
        if a["type"] != "aws:cloudformation:stack":
            continue
        t = json.loads((out / a["properties"]["templateFile"]).read_text())
        if any(r["Type"] == stack_edges.POLICY for r in t["Resources"].values()):
            with_policies.append(name.removeprefix(f"{PREFIX}-"))
    assert len(with_policies) >= 3
    for i, a in enumerate(with_policies):
        for b in with_policies[i + 1 :]:
            assert a in closure(g, b) or b in closure(g, a), (a, b)


def test_resolving_templates_for_their_reads_changes_no_template(tmp_path):
    assert_resolving_reads_changes_no_template(_BO, tmp_path)
