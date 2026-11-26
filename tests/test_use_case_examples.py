# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Every use case ships platform.example.yaml: its own entries for platform.yaml.

Merged into the greenfield preset, each example must load, so it cannot drift from the
schema. It names only its own use case, under use_cases and under any layer's access.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from infra_utils.platform_config import (
    USE_CASES_DIR,
    PlatformConfig,
    discover_use_cases,
)

REPO = Path(__file__).resolve().parents[1]


def _merge(base: dict, extra: dict) -> dict:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


@pytest.mark.parametrize("name", sorted(discover_use_cases()))
def test_the_example_config_names_only_its_use_case_and_loads(name):
    example = yaml.safe_load(
        (USE_CASES_DIR / name / "platform.example.yaml").read_text()
    )
    assert set(example["use_cases"]) == {name}
    for layer, block in (example.get("layers") or {}).items():
        assert set(block.get("access") or {}) <= {name}, layer
    base = yaml.safe_load((REPO / "presets" / "greenfield.yaml").read_text())
    config = PlatformConfig.model_validate(_merge(base, example))
    assert name in config.use_cases
