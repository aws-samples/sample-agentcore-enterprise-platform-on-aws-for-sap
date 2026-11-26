# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""business-operations: every stack stays clear of CloudFormation's 500-resource limit.

Each feature flag adds resources to the main stack, and a stack that reaches 500
cannot deploy at all. Failing at 450, with every flag on, leaves room to plan a
split instead of being forced into one by the next feature.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from app_synth import synth_app

REPO = Path(__file__).resolve().parents[3]
LIMIT = 450

EVERY_FLAG_ON = {
    "api": {"erp_events_endpoint": True, "set_account_cloudwatch_role": True},
    "knowledge": {"api_docs_search": True, "agent_knowledge": {"enabled": True}},
    "notification": {"ses_sender_email": "ops@example.com"},
}


def test_no_stack_passes_450_resources_with_every_flag_on():
    config = yaml.safe_load(
        (
            REPO
            / "use-cases/business-operations/fixtures/contract/business-operations.yaml"
        ).read_text()
    )
    config["use_cases"]["business-operations"] = EVERY_FLAG_ON
    result, out = synth_app(yaml.safe_dump(config))
    assert result.returncode == 0, result.stderr[-2000:]
    sizes = {
        t.name.removesuffix(".template.json"): len(
            json.loads(t.read_text())["Resources"]
        )
        for t in out.glob("*.template.json")
    }
    assert any("uc-business-operations" in name for name in sizes)
    over = {name: n for name, n in sizes.items() if n > LIMIT}
    assert not over, f"over {LIMIT} resources (the limit is 500): {over}"
