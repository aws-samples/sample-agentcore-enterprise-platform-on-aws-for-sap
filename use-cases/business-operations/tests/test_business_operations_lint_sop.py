# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""owner-author-sop's lint: the same symbol rule as the write path, and the shipped SOPs pass."""

import importlib.util
import sys
from pathlib import Path

import pytest

UC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(UC / "api" / "layer" / "python"))
import pack_tiers

_spec = importlib.util.spec_from_file_location(
    "lint_sop", UC / "skills" / "owner-author-sop" / "lint_sop.py"
)
lint_sop = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lint_sop)

SOPS = sorted((UC / "packs").glob("*/sops/*.txt"))


def test_symbol_rule_matches_the_write_path():
    assert lint_sop.SYMBOL_RE.pattern == pack_tiers.SYMBOL_RE.pattern


@pytest.mark.parametrize("sop", SOPS, ids=lambda p: f"{p.parts[-3]}/{p.name}")
def test_shipped_sops_pass(sop):
    pack = sop.parents[1] / "pack.json"
    assert lint_sop.main([str(sop), "--pack-json", str(pack)]) == 0


def test_undeclared_symbol_and_missing_section_fail():
    text = SOPS[0].read_text(encoding="utf-8")
    text = text.replace("PRECONDITIONS", "PREREQUISITES") + "\n{{NOT_A_CONSTANT}}\n"
    found = lint_sop.problems(text, SOPS[0].name, {"pack_id": "p", "constants": {}})
    assert any("NOT_A_CONSTANT" in f for f in found)
    assert any(f.startswith("sections ") for f in found)
