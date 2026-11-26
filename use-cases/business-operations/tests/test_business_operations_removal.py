# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Turning the use case off as docs/USE_CASE_REMOVAL.md says leaves the platform and
another SAP use case synthesizing, with nothing of this use case in their stacks."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import yaml
from app_synth import synth_app  # tests/, on the path through conftest.py

REPO = Path(__file__).resolve().parents[3]
NAME = "business-operations"
PREFIX = "agentcore-workshop-dev"
# The SAP layer took over resources this use case built first, with their descriptions
# and cost tag unchanged (layers/sap/lib/sap_layer/names.py, LEGACY_LABEL).
LEGACY_DESCRIPTION = f"{NAME}: AWS for SAP MCP server"
LEGACY_COMPONENT = f"uc-{NAME}-sap"


def _merge(a: dict, b: dict) -> dict:
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(a.get(k), dict):
            _merge(a[k], v)
        else:
            a[k] = copy.deepcopy(v)
    return a


def _load(path: str) -> dict:
    return yaml.safe_load((REPO / path).read_text()) or {}


def _before() -> dict:
    config = _load("presets/greenfield.yaml")
    _merge(config, _load(f"use-cases/{NAME}/platform.example.yaml"))
    _merge(config, _load("use-cases/hello-sap/platform.example.yaml"))
    config["layers"]["sap"]["systems"]["s4"]["secret_name"] = "sap/s4"
    return config


def _removed(config: dict) -> dict:
    """Step 5 of the procedure: delete every entry that names the use case."""
    c = copy.deepcopy(config)
    del c["use_cases"][NAME]
    del c["layers"]["sap"]["access"][NAME]
    c["gateway"]["interceptor_extensions"] = [
        e
        for e in c["gateway"]["interceptor_extensions"]
        if not e["path"].startswith(f"use-cases/{NAME}/")
    ]
    del c["security"]["egress_unscanned_responses"]
    return c


def _strings(node, key=""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _strings(v, k)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v, key)
    elif isinstance(node, str):
        yield key, node


def test_turning_the_use_case_off_leaves_nothing_of_it_on_the_platform():
    stacks = yaml.safe_load((REPO / f"use-cases/{NAME}/manifest.yaml").read_text())[
        "stacks"
    ]
    before, before_out = synth_app(yaml.safe_dump(_before()))
    assert before.returncode == 0, before.stderr[-2000:]
    after, out = synth_app(yaml.safe_dump(_removed(_before())))
    assert after.returncode == 0, after.stderr[-2000:]

    for suffix in stacks:
        assert (before_out / f"{PREFIX}-{suffix}.template.json").is_file()
        assert not (out / f"{PREFIX}-{suffix}.template.json").exists()
    assert (out / f"{PREFIX}-uc-hello-sap.template.json").is_file()

    left = []
    for template in sorted(out.glob("*.template.json")):
        resources = json.loads(template.read_text()).get("Resources", {})
        for logical_id, resource in resources.items():
            if "businessoperations" in logical_id.lower():
                left.append(f"{template.name}: {logical_id}")
            for key, value in _strings(resource):
                legacy = template.name.endswith("-layer-sap.template.json") and (
                    value == LEGACY_COMPONENT or value.startswith(LEGACY_DESCRIPTION)
                )
                if (NAME in value or "business_operations" in value) and not legacy:
                    left.append(f"{template.name}: {logical_id}.{key} = {value[:80]}")
    assert not left, left
