# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The create tier: what a browser may mint, and what it may not reach while doing it.

Three things have to hold for a created pack to be safe, and each one is a test below.
It has to be **well-formed** — `packs/pack.schema.json` is the authority, and the
composer refuses a document off it, so a create that composed one would always fail. It has to be **incomplete**, because that is the whole reason minting a pack from a
form is a small act: `registry` omits an incomplete pack from `sources()` and empties its
`sops`, so the poller is never given it and no `process_type` routes to it. And it has to
hold **read tools only**, because Cedar is action-scoped with no per-pack condition — a
`gateway_tools` entry is a real grant of reach.
"""

import json
import sys
from pathlib import Path

import pytest

_UC = Path(__file__).resolve().parents[3] / "use-cases" / "business-operations"
sys.path.insert(0, str(_UC / "api" / "layer" / "python"))
sys.path.insert(0, str(_UC))

import pack_compose
import pack_tiers
import registry
from business_operations import names
from business_operations.validate_packs import schema_errors

from sap_kit import names as core_names

_PACK_SCHEMA = json.loads((_UC / "packs/pack.schema.json").read_text())


@pytest.fixture(autouse=True)
def _created_tools(monkeypatch):
    monkeypatch.setenv(
        pack_compose.CREATED_TOOLS_ENV, json.dumps(names.created_pack_tools("s4"))
    )


def spec(**overrides) -> dict:
    """A spec that passes, with one field swapped per test."""
    return {
        "display_name": "Field Service Parts",
        "description": "Parts shortages that block a scheduled service call.",
        "short_id": "fsp",
        "kind": "filter",
        "definition": "A confirmed service order whose parts are not in stock.",
        "process_types": ["parts_shortage"],
        "attributes": [
            {"name": "order_number", "label": "Service Order", "type": "string"},
            {
                "name": "shortfall",
                "label": "Shortfall",
                "type": "amount",
                "currency_field": "currency",
            },
            {"name": "currency", "label": "Currency", "type": "string", "panel": False},
        ],
        "constants": {"MIN_STOCK_PCT": 20},
        **overrides,
    }


# --- the three properties that make a created pack safe ---------------------


def test_the_composed_document_validates_against_the_pack_schema():
    document, _ = pack_compose.compose("field_service", spec())
    # `$schema` is a pointer for an editor, not an instance keyword this validator knows.
    instance = {k: v for k, v in document.items() if k != "$schema"}
    assert schema_errors(instance, _PACK_SCHEMA) == []


def test_the_composed_pack_is_born_incomplete():
    document, _ = pack_compose.compose("field_service", spec())
    assert registry.is_incomplete(document["source"]), (
        "a created pack that is not incomplete is polled and routed to on the next cold "
        "start, with no SAP paths and no SOPs behind it"
    )


def test_the_composed_pack_holds_read_tools_only():
    document, _ = pack_compose.compose("field_service", spec())
    assert document["gateway_tools"] == list(names.created_pack_tools("s4"))
    bare = {t.split(core_names.TOOL_SEPARATOR)[-1] for t in document["gateway_tools"]}
    for write_tool in ("odata_create", "odata_update", "odata_function_import"):
        assert write_tool not in bare, (
            f"{write_tool} is a write to SAP, and Cedar carries no per-pack condition — "
            "naming it here would make a browser form a grant of that reach"
        )


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    """Point `registry` at a one-pack corpus on disk, the way a deployed reader sees it."""
    monkeypatch.setenv("PACKS_DIR", str(tmp_path))
    monkeypatch.delenv("REGISTRY_BUCKET", raising=False)

    def write(pack_id: str, **overrides) -> dict:
        document, base_prompt = pack_compose.compose(pack_id, spec(**overrides))
        pack_dir = tmp_path / pack_id
        pack_dir.mkdir()
        (pack_dir / "pack.json").write_text(json.dumps(document))
        (pack_dir / "base_prompt.txt").write_text(base_prompt)
        registry.reset()
        return document

    yield write
    registry.reset()


def test_the_composed_pack_survives_a_registry_build_and_is_inert(corpus):
    """The composer's post-build guard refuses a pack that does not survive, so a document
    `registry.build` silently drops would be a 500 rather than a publish. Run against the
    real readers, because "incomplete means inert" is the claim the whole create path rests on.
    """
    corpus("field_service")
    artifact = registry.build()
    assert "field_service" in artifact["packs"], "the pack must survive the build"

    # The poller is never given it, and no process_type routes to it — so the SOP mapping
    # for an SOP nobody has uploaded yet cannot take anything offline.
    assert [s["pack_id"] for s in registry.sources()] == []
    assert registry.sop_index()["field_service"]["sops"] == {}
    assert registry.process_type_pack_map() == {}

    # Constants stay visible: an incomplete pack's thresholds are still the pack's.
    assert registry.constants()["field_service"]["MIN_STOCK_PCT"] == 20
    # And it gets an entitlement entry, which is what keeps it authorable by its creator.
    assert registry.entitlement_map()["field_service"] == {}


def test_the_composed_prompt_passes_the_content_tier():
    """A create writes `base_prompt.txt`, and the content tier is what replaces it later.
    A skeleton the content tier would refuse is one an author cannot revise incrementally.
    """
    _, base_prompt = pack_compose.compose("field_service", spec())
    assert pack_tiers.check_values(None, base_prompt) == []


def test_the_composed_pack_declares_no_entitlement():
    """So it stays authorable by whoever cleared the deployment floor to create it.
    `registry.entitlement_map()` gives every pack an entry — `{}` when it declares
    nothing — and `CallerEntitlement._permits` reads an unrestricted entry as permitted.
    """
    document, _ = pack_compose.compose("field_service", spec())
    assert "entitlement" not in document


# --- what the spec may not carry --------------------------------------------


def test_a_privileged_key_in_the_spec_is_refused_and_says_where_to_go():
    errors = pack_compose.check_spec(spec(gateway_tools=["odata_update"]))
    assert any("gateway_tools" in e for e in errors)
    assert any("pack-admin" in e for e in errors), (
        "the refusal must name the other path"
    )


@pytest.mark.parametrize(
    "key", ["entitlement", "model_tier", "source", "fields", "seed"]
)
def test_every_privileged_key_is_refused_by_name(key):
    assert pack_compose.check_spec(spec(**{key: "anything"}))


@pytest.mark.parametrize("kind", ["event", "comparison", "derived", "nonsense"])
def test_a_kind_this_deployment_cannot_execute_is_refused(kind):
    errors = pack_compose.check_spec(spec(kind=kind))
    assert any("kind" in e for e in errors)


def test_an_attribute_may_not_shadow_a_platform_property():
    errors = pack_compose.check_spec(
        spec(attributes=[{"name": "status", "label": "Status", "type": "string"}])
    )
    assert any("status" in e for e in errors)


def test_an_amount_needs_a_currency_field_this_pack_declares():
    errors = pack_compose.check_spec(
        spec(
            attributes=[
                {
                    "name": "net",
                    "label": "Net",
                    "type": "amount",
                    "currency_field": "waldo",
                }
            ]
        )
    )
    assert any("currency_field" in e for e in errors)


def test_a_constant_out_of_range_is_refused_by_the_constants_tiers_own_check():
    errors = pack_compose.check_spec(spec(constants={"TOL_PCT": 150}))
    assert any("TOL_PCT" in e for e in errors)


def test_a_bad_short_id_is_refused():
    for short_id in ["", "F", "field_service", "FS", "1fs", "toolongprefix"]:
        assert pack_compose.check_spec(spec(short_id=short_id)), short_id


def test_process_types_is_required_and_must_be_routable_names():
    assert pack_compose.check_spec(spec(process_types=[]))
    assert pack_compose.check_spec(spec(process_types=["Parts Shortage"]))
    assert pack_compose.check_spec(spec(process_types=["a", "a"]))


def test_both_lists_are_capped_because_every_runtime_loads_the_corpus():
    names = [f"p{i}" for i in range(51)]
    assert pack_compose.check_spec(spec(process_types=names))
    assert not pack_compose.check_spec(spec(process_types=names[:50]))
    attrs = [{"name": f"a{i}", "label": "A", "type": "string"} for i in range(101)]
    assert pack_compose.check_spec(spec(attributes=attrs))
    assert not pack_compose.check_spec(spec(attributes=attrs[:100]))


def test_aging_needs_a_window_and_nothing_else_may_carry_one():
    assert pack_compose.check_spec(spec(kind="aging"))
    assert pack_compose.check_spec(spec(kind="aging", older_than_days=0))
    assert pack_compose.check_spec(spec(kind="aging", older_than_days=True))
    assert pack_compose.check_spec(spec(kind="filter", older_than_days=30))
    assert pack_compose.check_spec(spec(kind="aging", older_than_days=30)) == []


def test_an_aging_pack_composes_its_window_and_stays_marked():
    document, _ = pack_compose.compose(
        "slow_movers", spec(kind="aging", older_than_days=45)
    )
    assert document["source"]["aging"]["older_than_days"] == 45
    assert registry.is_incomplete(document["source"])
    assert (
        schema_errors(
            {k: v for k, v in document.items() if k != "$schema"}, _PACK_SCHEMA
        )
        == []
    )


def test_an_absence_pack_validates_too():
    document, _ = pack_compose.compose("missing_gr", spec(kind="absence"))
    assert (
        schema_errors(
            {k: v for k, v in document.items() if k != "$schema"}, _PACK_SCHEMA
        )
        == []
    )
    assert registry.is_incomplete(document["source"])


def test_a_pack_with_no_attributes_is_allowed_and_still_valid():
    """Legal, and worth being legal: `fields` is privileged, so requiring attributes here
    would mean a pack created without them could never be given any over HTTP either."""
    document, _ = pack_compose.compose("bare", spec(attributes=[]))
    assert document["fields"] == {}
    assert document["source"]["field_map"] == {}
    assert (
        schema_errors(
            {k: v for k, v in document.items() if k != "$schema"}, _PACK_SCHEMA
        )
        == []
    )


def test_free_text_is_bounded():
    assert pack_compose.check_spec(spec(description="x" * 5000))


def test_a_spec_that_is_not_an_object_is_refused():
    for bad in [None, "create", 3, [], True]:
        assert pack_compose.check_spec(bad)


def test_a_create_is_not_a_patch_kind():
    """A create names no key, so the key classifier never mistakes one for a patch."""
    assert pack_tiers.classify(set()) is None
    assert pack_tiers.CREATE not in (pack_tiers.CONTENT, pack_tiers.STRUCTURE)
