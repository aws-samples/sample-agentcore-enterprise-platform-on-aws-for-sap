# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""business-operations runtime data contracts: `types/*.schema.json` and what copies them.

The console's TypeScript twins are generated, so they are checked by regenerating.
The Python readers each keep a hand copy of the few values they enforce (no Python
twin is generated, and no Lambda bundles pydantic), so each copy is pinned here to
the schema it came from. A schema edit that leaves a copy behind fails this file.
"""

from __future__ import annotations

import ast
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

from business_operations import data_contracts


def _schema(name: str) -> dict:
    return json.loads((UC / "types" / f"{name}.schema.json").read_text())


CASES, TICKETS, SAP_EVENTS = _schema("cases"), _schema("tickets"), _schema("sap-events")


def _const(relpath: str, name: str):
    """A module-level literal, read without importing the module (most build boto3
    clients at import). `frozenset({...})` and `set(...)` read as their argument."""
    tree = ast.parse((UC / relpath).read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            value = node.value
            if isinstance(value, ast.Call) and getattr(value.func, "id", "") in (
                "frozenset",
                "set",
            ):
                value = value.args[0]
            return ast.literal_eval(value)
    raise AssertionError(f"{relpath} has no module-level {name}")


def test_typescript_twins_are_current():
    stale = [str(p.relative_to(UC)) for p in data_contracts.stale()]
    assert not stale, (
        f"stale twins {stale}: run `deploy.sh usecase run business-operations types`"
    )


def test_generator_refuses_a_keyword_it_does_not_understand(tmp_path, monkeypatch):
    schema = {
        "title": "T",
        "type": "object",
        "properties": {"x": {"anyOf": [{"type": "string"}]}},
    }
    (tmp_path / "t.schema.json").write_text(json.dumps(schema))
    monkeypatch.setattr(data_contracts, "SCHEMAS", tmp_path)
    with pytest.raises(ValueError, match="anyOf"):
        data_contracts.render("t.schema.json")


def test_ticket_routes_check_the_schema_enums():
    defs = TICKETS["definitions"]
    tickets, record = "api/tickets/index.py", "api/layer/python/ticket_record.py"
    assert _const(tickets, "VALID_STATUSES") == set(defs["TicketStatus"]["enum"])
    assert _const(record, "VALID_PRIORITIES") == set(defs["TicketPriority"]["enum"])
    assert _const(record, "VALID_RESPONSE_TYPES") == set(
        TICKETS["properties"]["response_type"]["enum"]
    )


def test_every_ticket_writer_builds_the_schema_shape():
    import ticket_record

    ticket = ticket_record.new_ticket(" t ", created_by="agent", case_id="nope")
    assert set(TICKETS["required"]) <= set(ticket) <= set(TICKETS["properties"])
    assert ticket["title"] == "t" and ticket["case_id"] == ""
    assert ticket["priority"] == "medium" and ticket["response_type"] == "approval"
    assert ticket_record.new_ticket("t", created_by="a", priority=["x"])[
        "priority"
    ] == ("medium")
    for bad in ({"title": ""}, {"title": "t", "response_type": "vote"}):
        with pytest.raises(ValueError):
            ticket_record.new_ticket(bad.pop("title"), created_by="a", **bad)
    # No writer builds its own: each one calls new_ticket.
    for path in (
        "api/tickets/index.py",
        "tickets_tool/handler.py",
        "notification/handler.py",
    ):
        source = (UC / path).read_text()
        assert "new_ticket(" in source and '"TKT-' not in source, path


def test_push_envelope_check_matches_the_contract():
    keys = _const("pipeline/sap_ingest.py", "_ENVELOPE_KEYS")
    extra = _const("pipeline/sap_ingest.py", "TYPED_EXTRA_KEYS")
    assert keys == set(SAP_EVENTS["required"])
    # The optional keys: a `version`, and a `system` the door must match.
    assert keys | extra == set(SAP_EVENTS["properties"])
    assert SAP_EVENTS["properties"]["version"]["enum"] == [
        _const("pipeline/sap_ingest.py", "VERSION")
    ]
    # envelope_error refuses any other key.
    assert SAP_EVENTS["additionalProperties"] is False
    records = SAP_EVENTS["properties"]["records"]
    assert _const("pipeline/sap_ingest.py", "MAX_RECORDS") == records["maxItems"]
    assert records["minItems"] == 1


def test_nudge_check_matches_the_contract():
    nudge = _schema("sap-nudge")
    assert _const("pipeline/sap_ingest.py", "NUDGE_KEYS") == set(nudge["properties"])
    assert nudge["additionalProperties"] is False and nudge["required"] == ["pack_id"]
    keys = nudge["properties"]["keys"]
    assert _const("pipeline/sap_ingest.py", "MAX_KEYS") == keys["maxItems"]
    pattern = keys["items"]["additionalProperties"]["pattern"]
    tree_pattern = (UC / "pipeline/sap_ingest.py").read_text()
    assert f're.compile(r"{pattern[1:-1]}")' in tree_pattern
    assert nudge["properties"]["version"]["enum"] == [
        _const("pipeline/sap_ingest.py", "VERSION")
    ]


def test_case_statuses_the_code_names_are_schema_statuses():
    import case_status

    statuses = set(CASES["definitions"]["CaseStatus"]["enum"])
    assert _const("pipeline/polling_engine.py", "_INITIAL_STATUS") in statuses
    assert case_status.RUN_TERMINAL_STATUSES <= statuses


def test_model_writable_fields_are_case_fields():
    assert _const("case_management/handler.py", "WRITABLE") <= set(CASES["properties"])


def test_reserved_attribute_names_are_exactly_the_case_fields():
    from pack_compose import RESERVED_ATTRIBUTE_NAMES

    assert RESERVED_ATTRIBUTE_NAMES == set(CASES["properties"])


def test_the_case_id_codec_matches_the_shared_vectors():
    """frontend/src/lib/caseKey.test.ts reads the same file."""
    import case_key

    vectors = json.loads((UC / "types" / "case-id-vectors.json").read_text())
    assert case_key.DERIVED_CASE_ID_PATTERN == CASES["properties"]["case_id"]["pattern"]
    for row in vectors["derived"]:
        case_id = case_key.derive_case_id(row["pack_id"], row["short_id"], row["key"])
        assert case_id == row["case_id"]
        assert case_key.to_runtime_session_id(case_id) == row["session_id"]
    for raw in vectors["invalid"]:
        assert case_key.try_normalize_case_id(raw) is None, raw
    for row in vectors["trimmed"]:
        assert case_key.normalize_case_id(row["raw"]) == row["case_id"]
