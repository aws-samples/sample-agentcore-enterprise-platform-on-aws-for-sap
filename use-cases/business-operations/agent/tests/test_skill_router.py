# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""resolve_skill: a mapped SOP that cannot be loaded fails the case, and the prompt
assembly that does succeed substitutes and renders what the pack declares.

A synthetic one-pack corpus: the shipped packs are their own port's to pin.
"""

import json

import pytest
from utils import skill_router as sr


class _Body:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


class _FakeS3:
    class exceptions:
        class NoSuchKey(Exception):
            pass

    def __init__(self, mode):
        self._mode = mode  # "notfound" | "error" | <content str>

    def get_object(self, Bucket, Key):
        if self._mode == "notfound":
            raise self.exceptions.NoSuchKey("missing")
        if self._mode == "error":
            raise RuntimeError("S3 access denied")
        return {"Body": _Body(self._mode.encode("utf-8"))}


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    pack = tmp_path / "demo_pack"
    pack.mkdir()
    (pack / "pack.json").write_text(
        json.dumps(
            {
                "pack_id": "demo_pack",
                "process_type_to_sop": {"price_variance": "demo_pack/pv.txt"},
                "constants": {"TOLERANCE": 5},
            }
        )
    )
    (pack / "base_prompt.txt").write_text("{PLATFORM_MECHANICS}\n{SOP_CONTENT}\n")
    (tmp_path / "_platform_prompt.txt").write_text("Shared mechanics.")
    sr._skills_index = None
    sr.registry.reset()
    monkeypatch.setattr(sr.config_overrides, "_contacts", {})
    monkeypatch.delenv("REGISTRY_BUCKET", raising=False)
    monkeypatch.setenv("PACKS_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_KNOWLEDGE_ENABLED", "true")
    yield
    sr._skills_index = None
    sr.registry.reset()


def _s3(monkeypatch, mode):
    monkeypatch.setattr(sr, "_s3", _FakeS3(mode))


def test_fetch_sop_returns_none_when_absent_and_raises_on_a_real_error(monkeypatch):
    _s3(monkeypatch, "notfound")
    assert sr._fetch_sop_from_s3("bucket", "p/missing.txt") is None
    _s3(monkeypatch, "error")
    with pytest.raises(sr.SopLoadError):
        sr._fetch_sop_from_s3("bucket", "p/x.txt")


@pytest.mark.parametrize("mode", ["notfound", "error"])
def test_a_mapped_sop_that_cannot_be_loaded_fails_the_case(corpus, monkeypatch, mode):
    # Absent is not softer than unreadable: either way the run has no procedure,
    # and a routed run without one may post on general knowledge.
    _s3(monkeypatch, mode)
    with pytest.raises(sr.SopLoadError, match="price_variance|access denied"):
        sr.resolve_skill("price_variance", sop_bucket="b")


def test_an_absent_sop_is_never_offered_as_a_discovery_skill(corpus, monkeypatch):
    _s3(monkeypatch, "notfound")
    assert sr.discovery_skills(sop_bucket="b") == []


def test_the_prompt_carries_the_fenced_sop_its_version_and_the_constants(
    corpus, monkeypatch
):
    _s3(monkeypatch, "Version 2.1\nAllow {{TOLERANCE}} percent.")
    skill = sr.resolve_skill("price_variance", sop_bucket="b")
    assert skill["sop_version"] == "2.1"
    assert skill["sop_key"] == "demo_pack/pv.txt"
    assert (
        "<sop_document>\nVersion 2.1\nAllow 5 percent.\n</sop_document>"
        in (skill["system_prompt"])
    )
    assert skill["system_prompt"].startswith("Shared mechanics.")
    assert "pass it as `pack_id`" in skill["system_prompt"]


def test_exemplars_are_appended_only_without_agent_knowledge(corpus, monkeypatch):
    _s3(monkeypatch, "Version 1.0\nProcedure.")
    monkeypatch.setattr(sr, "_fetch_exemplars", lambda *_: "## EXEMPLAR_MARKER")
    on = sr.resolve_skill("price_variance", sop_bucket="b")["system_prompt"]
    monkeypatch.setenv("AGENT_KNOWLEDGE_ENABLED", "false")
    off = sr.resolve_skill("price_variance", sop_bucket="b")["system_prompt"]
    assert "EXEMPLAR_MARKER" not in on and "EXEMPLAR_MARKER" in off


def test_substitute_replaces_declared_symbols_and_leaves_unknown_ones(monkeypatch):
    monkeypatch.setattr(sr.config_overrides, "_contacts", {})
    config = {"constants": {"TIER_1_DOLLAR": 50000}}
    text = "At {{TIER_1_DOLLAR}}, not {{UNKNOWN}}."
    assert sr._substitute(text, config) == "At 50000, not {{UNKNOWN}}."
    assert sr._substitute(text, {}) == text


def test_a_constants_block_cannot_redefine_a_contact(monkeypatch):
    # Otherwise a pack could reroute a notification to an address of its choosing.
    monkeypatch.setattr(
        sr.config_overrides, "_contacts", {"CONTACT_AR_ANALYST": "real@example.com"}
    )
    config = {"constants": {"CONTACT_AR_ANALYST": "should-not-win@example.com"}}
    assert sr._substitute("Notify {{CONTACT_AR_ANALYST}}.", config) == (
        "Notify real@example.com."
    )


def test_sap_service_info_renders_entities_and_function_imports():
    rendered = sr._sap_service_info(
        {
            "service": "SVC_A",
            "entities": {"invoice": "A_SupplierInvoice"},
            "function_imports": {
                "Release": {
                    "params": [
                        {"name": "Inv", "type": "Edm.String", "required": True},
                        {
                            "name": "Force",
                            "type": "Edm.Boolean",
                            "required": False,
                            "default": "false",
                        },
                    ]
                }
            },
        }
    )
    assert rendered.startswith(
        "Service: `SVC_A`\nEntities:\n  - invoice: `A_SupplierInvoice`"
    )
    assert "  - `Release`:" in rendered
    assert "    - Inv (Edm.String, required)" in rendered
    assert "    - Force (Edm.Boolean, optional, default 'false')" in rendered


def test_the_skill_index_follows_a_refreshed_registry(corpus, tmp_path):
    """A bundle import reaches a warm runtime: the invocation's refresh hands the
    registry a new corpus, and the index built from the old one is not reused."""
    assert set(sr._load_skills_index()) == {"price_variance"}
    pack = json.loads((tmp_path / "demo_pack" / "pack.json").read_text())
    pack["process_type_to_sop"]["short_pay"] = "demo_pack/sp.txt"
    (tmp_path / "demo_pack" / "pack.json").write_text(json.dumps(pack))
    assert set(sr._load_skills_index()) == {"price_variance"}, "never mid-run"
    sr.registry.refresh_for_request()  # what basic_agent does at each invocation
    assert set(sr._load_skills_index()) == {"price_variance", "short_pay"}


def test_the_prompt_names_the_dispositions_a_pack_declares(
    corpus, monkeypatch, tmp_path
):
    _s3(monkeypatch, "Version 1\nRelease it.")
    plain = sr.resolve_skill("price_variance", sop_bucket="b")["system_prompt"]
    assert "set `disposition` to one of" not in plain
    doc = json.loads((tmp_path / "demo_pack" / "pack.json").read_text())
    doc["dispositions"] = ["released", "escalated"]
    (tmp_path / "demo_pack" / "pack.json").write_text(json.dumps(doc))
    sr._skills_index = None
    sr.registry.reset()
    prompt = sr.resolve_skill("price_variance", sop_bucket="b")["system_prompt"]
    assert "set `disposition` to one of: `released`, `escalated`." in prompt
