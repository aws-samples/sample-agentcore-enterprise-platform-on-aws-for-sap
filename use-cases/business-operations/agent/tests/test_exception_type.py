# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The case's exception type: recorded by the runtime from the SOPs the run was given,
for an exception SOP of the case's own pack only, with the trace that evidences it."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from utils import case_persistence, hooks, registry

CASE = "ap-aaaaaaaaaaaaaaaaaaaaaaaaaa"
FINANCE = json.loads(
    (Path(__file__).resolve().parents[2] / "packs/finance_ops/pack.json").read_text()
)


def _load(process_type, text, status="success", tool="load_sop", call="t1"):
    return SimpleNamespace(
        tool_use={
            "toolUseId": call,
            "name": tool,
            "input": {"process_type": process_type},
        },
        result={"toolUseId": call, "status": status, "content": [{"text": text}]},
    )


SERVED = "[sop finance_ops/price_variance.txt version v1]\n1.1  The agent MUST..."


def test_the_hook_notes_only_a_load_sop_the_gateway_served(monkeypatch):
    monkeypatch.setenv("CEDAR_ENFORCEMENT_MODE", "OFF")
    hook = hooks.EvidenceHook()
    hook.on_tool_call(_load("price_variance", SERVED))
    # Prefixed by its target on a direct-MCP call: still load_sop.
    hook.on_tool_call(
        _load(
            "short_pay", SERVED, tool="business-operations-sops___load_sop", call="t2"
        )
    )
    # Refused by the run checks, an error reply, no header, another tool: none counts.
    hook.on_tool_call(_load("duplicate_invoice", "Refused", status="error", call="t3"))
    hook.on_tool_call(_load("uom_mismatch", json.dumps({"error": "x"}), call="t4"))
    hook.on_tool_call(_load("uom_mismatch", "SOP text with no header", call="t5"))
    hook.on_tool_call(_load("quantity_variance", SERVED, tool="odata_read", call="t6"))
    assert hook.loaded_sops() == ["price_variance", "short_pay"]
    hook.on_start(None)
    assert hook.loaded_sops() == []


def test_the_last_exception_sop_of_the_cases_pack_wins(monkeypatch):
    # As `registry.load` publishes it: `source` beside the pack, not inside it.
    published = {k: v for k, v in FINANCE.items() if k != "source"}
    monkeypatch.setattr(
        registry,
        "packs",
        lambda: {"finance_ops": {"pack": published, "source": FINANCE["source"]}},
    )
    decide = case_persistence.identified_exception_type
    # Triage, then an exception, then the shared short pay: the exception stands.
    assert (
        decide("finance_ops", ["invoice_matching", "price_variance", "short_pay"])
        == "price_variance"
    )
    assert (
        decide(
            "finance_ops", ["invoice_matching", "price_variance", "duplicate_invoice"]
        )
        == "duplicate_invoice"
    )
    # Routed straight to an exception SOP.
    assert decide("finance_ops", ["uom_mismatch"]) == "uom_mismatch"
    # Triage and its aliases alone, or a type the pack does not map: nothing.
    assert decide("finance_ops", ["three_way_match", "ap_exception"]) is None
    assert decide("finance_ops", ["invoice_matching", "not_a_type"]) is None
    # Another pack's type is not this case's.
    assert decide("other_pack", ["price_variance"]) is None


def test_an_unreadable_registry_records_nothing(monkeypatch):
    def boom():
        raise registry.RegistryUnavailable("s3")

    monkeypatch.setattr(registry, "packs", boom)
    assert case_persistence.identified_exception_type("finance_ops", ["x"]) is None


def _saved(monkeypatch, **kw):
    table = mock.Mock()
    table.update_item.return_value = {"Attributes": {"agent_traces": []}}
    monkeypatch.setattr(
        case_persistence, "_cases_table", lambda _c: (table, {"case_id": CASE})
    )
    case_persistence._save_trace_to_ddb(
        CASE, "sqs", "p", [{"type": "text", "content": "x"}], "complete", **kw
    )
    (append,) = [
        c.kwargs
        for c in table.update_item.call_args_list
        if ":trace" in c.kwargs.get("ExpressionAttributeValues", {})
    ]
    return append


def test_the_type_lands_on_the_case_and_its_trace_in_the_append(monkeypatch):
    append = _saved(monkeypatch, exception_type="price_variance")
    assert "exception_type = :exception_type" in append["UpdateExpression"]
    values = append["ExpressionAttributeValues"]
    assert values[":exception_type"] == "price_variance"
    assert values[":trace"][0]["exception_type"] == "price_variance"


def test_a_run_that_identified_none_leaves_the_case_as_it_was(monkeypatch):
    append = _saved(monkeypatch)
    assert "exception_type" not in append["UpdateExpression"]
    assert ":exception_type" not in append["ExpressionAttributeValues"]
    assert "exception_type" not in append["ExpressionAttributeValues"][":trace"][0]
