# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Kinds of SOP: triage, exception, shared and alias, and where each is checked.

Only `shared_sops` is declared; the other kinds follow from `process_type_to_sop` and
`source.process_type`. A pack that declares no `shared_sops` stays valid.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
UC = REPO / "use-cases" / "business-operations"
LAYER = UC / "api" / "layer" / "python"
for p in (str(UC), str(LAYER)):
    if p not in sys.path:
        sys.path.insert(0, p)

import registry
from business_operations import validate_packs
from test_business_operations_packs import _edit, _known, tree  # noqa: F401
from test_business_operations_packs_composer import (  # noqa: F401
    _PACK,
    _PROMPT,
    composer,
)

FINANCE = json.loads((UC / "packs" / "finance_ops" / "pack.json").read_text())


def _doc(mapping: dict, default: str | None = None, shared=None, rules=None) -> dict:
    doc: dict = {"process_type_to_sop": mapping}
    if default or rules:
        doc["source"] = {
            "process_type": {
                **({"default": default} if default else {}),
                **({"rules": rules} if rules else {}),
            }
        }
    if shared is not None:
        doc["shared_sops"] = shared
    return doc


def _kinds(doc: dict) -> dict[str, str]:
    return {r["process_type"]: r["kind"] for r in registry.sop_kinds(doc)}


def test_finance_ops_has_one_triage_six_exceptions_one_shared_and_two_aliases():
    rows = registry.sop_kinds(FINANCE)
    assert [r["process_type"] for r in rows] == list(FINANCE["process_type_to_sop"])
    kinds = _kinds(FINANCE)
    assert kinds.pop("invoice_matching") == "triage"
    assert kinds.pop("short_pay") == "shared"
    assert {kinds.pop(a) for a in ("three_way_match", "ap_exception")} == {"alias"}
    assert set(kinds.values()) == {"exception"} and len(kinds) == 6
    aliases = {r["process_type"]: r.get("alias_of") for r in rows}
    assert aliases["three_way_match"] == aliases["ap_exception"] == "invoice_matching"
    assert all("alias_of" not in r for r in rows if r["kind"] != "alias")


def test_a_pack_without_shared_sops_reads_every_non_triage_entry_as_an_exception():
    doc = _doc({"a": "p/a.txt", "b": "p/b.txt"}, default="a")
    assert _kinds(doc) == {"a": "triage", "b": "exception"}
    assert registry.sop_kind_errors(doc) == []
    # No source at all: no triage either.
    assert _kinds(_doc({"a": "p/a.txt"})) == {"a": "exception"}


def test_an_alias_points_at_triage_then_shared_then_the_file_name_then_the_first():
    same = {"x": "p/f.txt", "f": "p/f.txt", "y": "p/f.txt"}
    rows = {r["process_type"]: r for r in registry.sop_kinds(_doc(same))}
    assert rows["f"]["kind"] == "exception"
    assert rows["x"]["alias_of"] == rows["y"]["alias_of"] == "f"
    rows = {r["process_type"]: r for r in registry.sop_kinds(_doc(same, shared=["y"]))}
    assert rows["y"]["kind"] == "shared" and rows["f"]["alias_of"] == "y"
    rows = {r["process_type"]: r for r in registry.sop_kinds(_doc(same, default="x"))}
    assert rows["x"]["kind"] == "triage" and rows["f"]["alias_of"] == "x"
    rows = {
        r["process_type"]: r
        for r in registry.sop_kinds(_doc({"b": "p/z.txt", "a": "p/z.txt"}))
    }
    assert rows["b"]["kind"] == "exception" and rows["a"]["alias_of"] == "b"


def test_only_an_exception_type_is_recorded_and_an_alias_as_what_it_aliases():
    doc = _doc(
        {
            "t": "p/t.txt",
            "t2": "p/t.txt",
            "e": "p/e.txt",
            "e2": "p/e.txt",
            "s": "p/s.txt",
        },
        default="t",
        shared=["s"],
    )
    assert registry.exception_type(doc, "e") == "e"
    assert registry.exception_type(doc, "e2") == "e"
    for not_one in ("t", "t2", "s", "unmapped"):
        assert registry.exception_type(doc, not_one) is None
    assert registry.exception_type(FINANCE, "price_variance") == "price_variance"
    assert registry.exception_type(FINANCE, "three_way_match") is None


@pytest.mark.parametrize(
    "doc,needle",
    [
        (_doc({"a": "p/a.txt"}, shared=["b"]), "does not map"),
        (_doc({"a": "p/a.txt", "b": "p/b.txt"}, shared=["b", "b"]), "more than once"),
        (_doc({"a": "p/a.txt"}, default="a", shared=["a"]), "starts a case with"),
        (
            _doc(
                {"a": "p/a.txt", "b": "p/b.txt"},
                default="a",
                shared=["b"],
                rules=[{"when": {"op": "blank", "field": "X"}, "then": "b"}],
            ),
            "starts a case with",
        ),
        (
            _doc({"t": "p/t.txt", "u": "p/t.txt"}, default="t", shared=["u"]),
            "same SOP as 't'",
        ),
    ],
)
def test_a_shared_sops_entry_that_cannot_stand_is_refused(doc, needle):
    errors = registry.sop_kind_errors(doc)
    assert any(needle in e for e in errors), errors


def test_synth_refuses_a_bad_shared_sops(tree):  # noqa: F811
    _edit(tree, "finance_ops", shared_sops=["short_pay", "no_such_type"])
    errors = validate_packs.validate(_known(), tree)
    assert any("no_such_type" in e and "does not map" in e for e in errors), errors


def test_synth_accepts_finance_ops_with_and_without_shared_sops(tree):  # noqa: F811
    assert validate_packs.validate(_known(), tree) == []
    doc = json.loads((tree / "finance_ops" / "pack.json").read_text())
    doc.pop("shared_sops")
    (tree / "finance_ops" / "pack.json").write_text(json.dumps(doc))
    assert validate_packs.validate(_known(), tree) == []


def test_shared_sops_is_structure():
    import pack_tiers

    assert "shared_sops" in pack_tiers.STRUCTURE_KEYS
    assert pack_tiers.classify({"shared_sops"}) == pack_tiers.STRUCTURE


def test_get_registry_lists_each_packs_sops_with_their_kinds(monkeypatch):
    monkeypatch.setattr(
        registry,
        "packs",
        # As `registry.load` publishes it: `source` beside the pack, not inside it.
        lambda: {
            "finance_ops": {
                "pack": {k: v for k, v in FINANCE.items() if k != "source"},
                "source": FINANCE["source"],
            }
        },
    )
    (row,) = registry.public_view()
    assert row["sops"] == registry.sop_kinds(FINANCE)
    assert [s["process_type"] for s in row["sops"] if s["kind"] == "triage"] == [
        "invoice_matching"
    ]
    assert row["process_types"] == sorted(FINANCE["process_type_to_sop"])


def test_an_import_or_edit_refuses_a_bad_shared_sops(composer):  # noqa: F811
    """Every edit is imported as the next version, so this one check covers a patch,
    a SOP write and a bundle import alike."""
    module, _ = composer
    bundle = {
        "pack_id": "finance_x",
        "pack": {**_PACK, "shared_sops": ["a_check"]},
        "sops": {"a.txt": "Version 1.0\n1. The agent MUST read the case.\n"},
        "base_prompt": _PROMPT,
    }
    assert not [e for e in module._content_errors(bundle) if "shared_sops" in e]
    bundle["pack"]["shared_sops"] = ["not_mapped"]
    assert any(
        "shared_sops" in e and "not_mapped" in e for e in module._content_errors(bundle)
    )
