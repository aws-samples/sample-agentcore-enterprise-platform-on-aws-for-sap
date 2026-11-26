# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""business-operations agent: evidence extraction, citation by quotation, trace caps, segments."""

import importlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
UC = REPO / "use-cases" / "business-operations"
AGENT = UC / "agent"


def _agent_utils(*names):
    """Import agent/utils modules without leaving `utils` bound in sys.modules.

    The agent imports its helpers as the package `utils`, and scripts/ has a
    `utils` module of its own that other tests import, so the binding is held
    only for the import.
    """

    def ours():
        return [k for k in sys.modules if k == "utils" or k.startswith("utils.")]

    held = {k: sys.modules.pop(k) for k in ours()}
    sys.path.insert(0, str(AGENT))
    try:
        return [importlib.import_module(f"utils.{n}") for n in names]
    finally:
        sys.path.remove(str(AGENT))
        for k in ours():
            del sys.modules[k]
        sys.modules.update(held)


evidence, segments = _agent_utils("evidence", "segments")
MAX_TRACES = evidence.MAX_TRACES
PROSE_MAX_CHARS = evidence.PROSE_MAX_CHARS
TOOL_INPUT_MAX_BYTES = evidence.TOOL_INPUT_MAX_BYTES
TOOL_RESULT_MAX_BYTES = evidence.TOOL_RESULT_MAX_BYTES
cap_traces = evidence.cap_traces
extract_evidence = evidence.extract_evidence
result_text = evidence.result_text
truncate_segment = evidence.truncate_segment
QUOTE_MIN_WORDS = evidence.QUOTE_MIN_WORDS
normalize_quote = evidence.normalize_quote
sop_text_in = evidence.sop_text_in
verify_citation = evidence.verify_citation
merge_evidence = evidence.merge_evidence
tool_failed = evidence.tool_failed
accumulate_segment = segments.accumulate_segment


# ── The evidence extractor's non-trivial logic: one extractor per tool family, the ──


AT = "2026-07-30T09:14:02Z"


def _ok(text: str = "sent") -> dict:
    return {"content": [{"text": text}], "status": "success", "toolUseId": "t1"}


def _err(text: str) -> dict:
    return {"content": [{"text": text}], "status": "error", "toolUseId": "t1"}


# ── result_text ──────────────────────────────────────────────────────────────


def test_result_text_joins_text_blocks():
    result = {"content": [{"text": "a"}, {"text": "b"}], "status": "success"}
    assert result_text(result) == "a\nb"


def test_result_text_serialises_a_json_block():
    result = {"content": [{"json": {"k": 1}}], "status": "success"}
    assert json.loads(result_text(result)) == {"k": 1}


def test_result_text_tolerates_a_missing_or_odd_result():
    assert result_text(None) == ""
    assert result_text({}) == ""
    assert result_text({"content": [None, "raw"]}) == ""


# ── sap_read ─────────────────────────────────────────────────────────────────


def test_odata_read_records_service_entity_and_fields():
    evidence = extract_evidence(
        "odata_read",
        {
            "service_name": "API_PURCHASEORDER_PROCESS_SRV",
            "entity_set": "A_PurchaseOrderItem",
            "filter": "PurchaseOrder eq '4500000123' and PurchaseOrderItem eq '10'",
        },
        _ok(json.dumps({"NetPriceAmount": "12.50", "OrderQuantity": "100"})),
        at=AT,
        mode="ENFORCE",
    )
    assert evidence["kind"] == "sap_read"
    assert evidence["at"] == AT
    assert evidence["source"] == {
        "service": "API_PURCHASEORDER_PROCESS_SRV",
        "entity": "A_PurchaseOrderItem",
        "key": "4500000123/10",
    }
    assert evidence["fields"] == [
        {"name": "NetPriceAmount", "value": "12.50"},
        {"name": "OrderQuantity", "value": "100"},
    ]
    assert evidence["authz"] == {"mode": "ENFORCE", "outcome": "permitted"}


def test_odata_read_unwraps_an_odata_results_envelope():
    payload = {"d": {"results": [{"NetPriceAmount": "12.50"}]}}
    evidence = extract_evidence(
        "odata_read", {"entity_set": "X"}, _ok(json.dumps(payload)), at=AT
    )
    assert evidence["fields"] == [{"name": "NetPriceAmount", "value": "12.50"}]


def test_odata_read_unwraps_the_mcp_server_envelope():
    # The shape the AWS for SAP MCP Server actually returns. Without the unwrap, `fields`
    # is absent entirely and every diff's before column renders empty.
    payload = {
        "result": {
            "success": True,
            "message": "Successfully fetched OData data for A_PurchaseOrder",
            "data": [
                {
                    "__metadata": {
                        "uri": "https://sap.example/A_PurchaseOrder('4500000001')"
                    },
                    "PurchaseOrder": "4500000001",
                    "CompanyCode": "1710",
                }
            ],
            "metadata": {"record_count": 1},
        }
    }
    evidence = extract_evidence(
        "odata_read", {"entity_set": "A_PurchaseOrder"}, _ok(json.dumps(payload)), at=AT
    )
    assert evidence["fields"] == [
        {"name": "PurchaseOrder", "value": "4500000001"},
        {"name": "CompanyCode", "value": "1710"},
    ]


def test_an_empty_result_set_yields_no_fields():
    # A read that found nothing must not look like a read that found an empty record.
    payload = {"result": {"success": True, "data": [], "metadata": {"record_count": 0}}}
    evidence = extract_evidence(
        "odata_read",
        {"service_name": "S", "entity_set": "E"},
        _ok(json.dumps(payload)),
        at=AT,
    )
    assert evidence["source"] == {"service": "S", "entity": "E"}
    assert "fields" not in evidence


def test_odata_read_with_unparseable_result_keeps_the_source_and_drops_fields():
    # Provenance must survive a result the extractor cannot read.
    evidence = extract_evidence(
        "odata_read",
        {"service_name": "S", "entity_set": "E"},
        _ok("<html>gateway timeout"),
        at=AT,
    )
    assert evidence["kind"] == "sap_read"
    assert evidence["source"] == {"service": "S", "entity": "E"}
    assert "fields" not in evidence


def test_field_count_is_bounded():
    wide = {f"F{i}": str(i) for i in range(40)}
    evidence = extract_evidence(
        "odata_read", {"entity_set": "E"}, _ok(json.dumps(wide)), at=AT
    )
    assert len(evidence["fields"]) == 12


def test_nested_values_are_skipped_not_stringified():
    payload = {"NetPriceAmount": "12.50", "to_Item": {"results": []}, "Nav": ["a"]}
    evidence = extract_evidence(
        "odata_read", {"entity_set": "E"}, _ok(json.dumps(payload)), at=AT
    )
    assert evidence["fields"] == [{"name": "NetPriceAmount", "value": "12.50"}]


# ── sap_write ────────────────────────────────────────────────────────────────


def test_odata_update_is_a_write_with_op_update():
    evidence = extract_evidence(
        "odata_update",
        {
            "service_name": "API_SUPPLIERINVOICE_PROCESS_SRV",
            "entity_set": "A_SupplierInvoice",
            "identifier_fields": {
                "SupplierInvoice": "5105600000",
                "FiscalYear": "2026",
            },
            "payload": {"PaymentBlockingReason": ""},
        },
        _ok("{}"),
        at=AT,
    )
    assert evidence["kind"] == "sap_write"
    assert evidence["op"] == "update"
    assert evidence["source"]["key"] == "5105600000/2026"
    assert evidence["fields"] == [{"name": "PaymentBlockingReason", "value": ""}]


def test_odata_create_is_a_write_with_op_create():
    evidence = extract_evidence(
        "odata_create",
        {"service_name": "S", "entity_set": "E", "payload": {"A": "1"}},
        _ok("{}"),
        at=AT,
    )
    assert evidence["op"] == "create"
    assert evidence["fields"] == [{"name": "A", "value": "1"}]


def test_function_import_records_the_function_as_the_entity():
    # Post and Release change no field; they move a document's lifecycle, so the
    # function name is the thing worth recording.
    evidence = extract_evidence(
        "odata_function_import",
        {
            "service_name": "API_SUPPLIERINVOICE_PROCESS_SRV",
            "function_name": "Post",
            "parameters": {"SupplierInvoice": "5105600000", "FiscalYear": "2026"},
        },
        _ok("{}"),
        at=AT,
    )
    assert evidence["kind"] == "sap_write"
    assert evidence["op"] == "function_import"
    assert evidence["source"]["entity"] == "Post"
    assert evidence["source"]["key"] == "5105600000/2026"
    assert {"name": "SupplierInvoice", "value": "5105600000"} in evidence["fields"]


# Both shapes as the AWS for SAP MCP Server really returns them, taken off a live Release
# against a deployed stack. Note the ToolResult status is "success" in both cases —
# the rejection is inside the wrapper, which is the whole reason `sap_result` exists.
_ACCEPTED = json.dumps(
    {
        "success": True,
        "message": "Successfully executed Function Import Release",
        "data": {"Release": {"__metadata": {"type": "Release"}, "Success": True}},
        "metadata": {},
    }
)
_REJECTED = json.dumps(
    {
        "success": False,
        "message": (
            "Error executing Function Import: OData request failed with status "
            "code 400. Reason: Bad Request. Error: Invoice is not blocked and "
            "cannot be released therefore."
        ),
        "data": None,
        "metadata": {
            "error": {
                "code": "MRM_BASE/000",
                "message": {"lang": "en", "value": "Invoice is not blocked"},
                "innererror": {
                    "application": {
                        "component_id": "MM-IV-GF",
                        "service_namespace": "/SAP/",
                        "service_id": "API_SUPPLIERINVOICE_PROCESS_SRV",
                        "service_version": "0001",
                    },
                    "transactionid": "1F44EC9D34600030E00697D0A4E4C1EC",
                },
            }
        },
    }
)


def _release(result: dict) -> dict:
    return extract_evidence(
        "odata_function_import",
        {
            "service_name": "API_SUPPLIERINVOICE_PROCESS_SRV",
            "function_name": "Release",
            "parameters": {"SupplierInvoice": "'5100001548'", "FiscalYear": "'2026'"},
        },
        result,
        at=AT,
    )


def test_an_accepted_write_records_sap_saying_so():
    evidence = _release(_ok(_ACCEPTED))
    assert evidence["sap_result"]["accepted"] is True
    assert "Successfully executed" in evidence["sap_result"]["message"]


def test_a_rejected_write_is_distinguishable_from_an_accepted_one():
    evidence = _release(_ok(_REJECTED))
    assert evidence["sap_result"]["accepted"] is False
    assert "not blocked and cannot be released" in evidence["sap_result"]["message"]
    # The field earns its place here: everything else about the two calls matches,
    # including authz — Cedar permitted the call that SAP then refused.
    assert evidence["authz"]["outcome"] == "permitted"
    assert evidence["source"] == _release(_ok(_ACCEPTED))["source"]


def test_a_response_with_no_wrapper_claims_nothing():
    # An OData body with no `success` sibling: unknown is not the same fact as
    # refused, so the key is absent rather than False.
    assert "sap_result" not in _release(_ok('{"d": {"results": []}}'))
    assert "sap_result" not in _release(None)


def test_the_refusal_survives_the_result_being_truncated():
    # Extraction runs before truncation, which is the only reason a 400 buried
    # past TOOL_RESULT_MAX_BYTES is still readable in the persisted trace.
    segment = {"tool_result": _REJECTED, "evidence": _release(_ok(_REJECTED))}
    truncate_segment(segment)
    assert len(segment["tool_result"].encode()) <= TOOL_RESULT_MAX_BYTES
    assert segment["evidence"]["truncated"] is True
    assert segment["evidence"]["sap_result"]["accepted"] is False


# ── sop_lookup ───────────────────────────────────────────────────────────────


def test_sop_lookup_extracts_numbered_clauses_in_order_without_duplicates():
    sop = (
        "STEP 1: GATHER DATA\n"
        "1.1  The agent MUST retrieve the invoice line items with\n"
        "     NetAmount and Quantity.\n"
        "1.2  The agent MUST retrieve the matching PO item.\n"
        "1.2  (restated)\n"
        "2.1  The agent MUST compute the price variance.\n"
    )
    evidence = extract_evidence(
        "search_sap_sops", {"query": "price variance"}, _ok(sop), at=AT
    )
    assert evidence["kind"] == "sop_lookup"
    assert evidence["clauses_retrieved"] == ["1.1", "1.2", "2.1"]


def test_sop_lookup_extracts_clauses_when_bedrock_collapses_the_newlines():
    """Retrieve returns each chunk on one line, which a `^`-anchored pattern misses.

    This is the production shape — the first real run recorded zero clauses on
    every SOP lookup while the SOPs themselves were numbered 1.1-5.3.
    """
    collapsed = (
        "STEP 4: EXECUTE RESOLUTION   "
        "4.1  The agent MUST park the invoice first.   "
        "4.2  The agent MUST update case state at each step."
    )
    evidence = extract_evidence(
        "search_sap_sops", {"query": "release"}, _ok(collapsed), at=AT
    )
    assert evidence["clauses_retrieved"] == ["4.1", "4.2"]


def test_sop_lookup_ignores_decimal_values_that_are_not_clauses():
    """The API-docs corpus is searched by the same tool and quotes amounts."""
    evidence = extract_evidence(
        "search_sap_api_docs",
        {"query": "purchase order net amount"},
        _ok("NetPriceAmount: 100.000   Currency: USD"),
        at=AT,
    )
    assert "clauses_retrieved" not in evidence


def test_sop_lookup_with_no_results_records_no_clauses():
    evidence = extract_evidence(
        "search_sap_sops", {"query": "x"}, _ok("No relevant results found."), at=AT
    )
    assert evidence["kind"] == "sop_lookup"
    assert "clauses_retrieved" not in evidence


# ── case_update, notification ────────────────────────────────────────────────


def test_update_case_state_is_a_case_update_carrying_the_transition():
    evidence = extract_evidence(
        "update_case_state",
        {
            "case_id": "4500000123-10",
            "updates": {"status": "complete"},
            "action": "resolve",
        },
        _ok("{}"),
        at=AT,
    )
    assert evidence["kind"] == "case_update"
    assert evidence["source"]["key"] == "4500000123-10"
    assert {"name": "status", "value": "complete"} in evidence["fields"]


def test_update_case_state_reads_the_json_string_its_tool_spec_declares():
    # The wire shape: the tool's InputSchema in stack.py types `updates` as a string, so
    # passing a dict here (as the test above does) never happens in production —
    # _scalar_fields returned [] for it and every real case_update recorded no fields.
    evidence = extract_evidence(
        "update_case_state",
        {
            "case_id": "4500000123-10",
            "updates": '{"status": "complete", "exception_type": "PRICE"}',
            "action": "resolve",
        },
        _ok("{}"),
        at=AT,
    )
    assert {"name": "status", "value": "complete"} in evidence["fields"]
    assert {"name": "exception_type", "value": "PRICE"} in evidence["fields"]


def test_an_unparseable_updates_string_records_no_fields():
    evidence = extract_evidence(
        "update_case_state",
        {"case_id": "4500000123-10", "updates": "status=complete", "action": "resolve"},
        _ok("{}"),
        at=AT,
    )
    assert "fields" not in evidence
    assert evidence["source"]["key"] == "4500000123-10"


def test_send_notification_records_the_recipient():
    evidence = extract_evidence(
        "send_notification",
        {
            "recipient": "ap-team@example.com",
            "subject": "Price variance",
            "body": "...",
        },
        _ok("{}"),
        at=AT,
    )
    assert evidence["kind"] == "notification"
    assert {"name": "recipient", "value": "ap-team@example.com"} in evidence["fields"]


def test_a_notification_keeps_the_clause_the_agent_cited():
    # The platform prompt tells the agent to cite the clause it acted on in the
    # notification body. Recording only the recipient dropped it on the floor.
    evidence = extract_evidence(
        "send_notification",
        {
            "recipient": "ap-team@example.com",
            "subject": "Price variance per §3.3",
            "body": "Escalating to the buyer per §3.3.",
        },
        _ok("{}"),
        at=AT,
    )
    values = [f["value"] for f in evidence["fields"]]
    assert "Price variance per §3.3" in values
    assert "Escalating to the buyer per §3.3." in values


def test_a_notification_body_is_capped_rather_than_stored_whole():
    evidence = extract_evidence(
        "send_notification",
        {"recipient": "a@b.c", "body": "x" * 900},
        _ok("{}"),
        at=AT,
    )
    body = next(f for f in evidence["fields"] if f["name"] == "body")
    assert len(body["value"]) == PROSE_MAX_CHARS


def test_a_notification_with_an_empty_body_records_no_field_for_it():
    # An absent field is a different claim than an empty one, and FieldList would
    # render a blank row for it.
    evidence = extract_evidence(
        "send_notification",
        {"recipient": "a@b.c", "subject": "   ", "body": ""},
        _ok("{}"),
        at=AT,
    )
    assert [f["name"] for f in evidence["fields"]] == ["recipient"]


def test_ticket_creation_is_the_escalation_channel():
    # proposed_write hangs off this segment, so the kind must be notification.
    evidence = extract_evidence(
        "demo_create_ticket",
        {
            "title": "Approve price variance",
            "description": "...",
            "assigned_to": "ap-lead",
        },
        _ok('{"ticket_id":"TKT-1"}'),
        at=AT,
    )
    assert evidence["kind"] == "notification"


def test_a_ticket_carrying_a_proposed_write_records_it():
    evidence = extract_evidence(
        "demo_create_ticket",
        {
            "title": "Approve price variance",
            "description": "...",
            "proposed_write": {
                "op": "update",
                "service": "API_PURCHASEORDER_PROCESS_SRV",
                "entity": "A_PurchaseOrderItem",
                "key": "4500000123/10",
                "fields": [
                    {"name": "NetPriceAmount", "current": "12.00", "proposed": "12.50"},
                    {"name": "PriceUnit", "proposed": "1"},
                ],
            },
        },
        _ok('{"ticket_id":"TKT-1"}'),
        at=AT,
    )
    assert evidence["proposed_write"] == {
        "op": "update",
        "service": "API_PURCHASEORDER_PROCESS_SRV",
        "entity": "A_PurchaseOrderItem",
        "key": "4500000123/10",
        "fields": [
            {"name": "NetPriceAmount", "proposed": "12.50", "current": "12.00"},
            {"name": "PriceUnit", "proposed": "1"},
        ],
    }


def test_a_proposed_write_field_list_is_bounded():
    proposal = {
        "op": "create",
        "fields": [{"name": f"F{i}", "proposed": str(i)} for i in range(40)],
    }
    evidence = extract_evidence(
        "demo_create_ticket",
        {"title": "t", "description": "d", "proposed_write": proposal},
        _ok("{}"),
        at=AT,
    )
    assert len(evidence["proposed_write"]["fields"]) == 12


def test_a_proposed_write_the_model_malformed_is_dropped():
    # An unusable proposal must be absent, not an empty object the console would
    # render as a diff of nothing.
    for proposal in (
        "update A_PurchaseOrderItem",  # prose where an object was asked for
        {"fields": [{"name": "A", "proposed": "1"}]},  # no op
        {
            "op": "delete",
            "fields": [{"name": "A", "proposed": "1"}],
        },  # not a write we do
        {"op": "update", "fields": []},
        {"op": "update", "fields": [{"proposed": "1"}]},  # no field name
    ):
        evidence = extract_evidence(
            "demo_create_ticket",
            {"title": "t", "description": "d", "proposed_write": proposal},
            _ok("{}"),
            at=AT,
        )
        assert "proposed_write" not in evidence, proposal


# ── computation and the fallthrough ──────────────────────────────────────────


def test_calculator_is_a_computation_with_its_input_and_result():
    evidence = extract_evidence(
        "calculator", {"expression": "(12.50-12.00)/12.00*100"}, _ok("4.1667"), at=AT
    )
    assert evidence["kind"] == "computation"
    assert {"name": "expression", "value": "(12.50-12.00)/12.00*100"} in evidence[
        "fields"
    ]
    assert {"name": "result", "value": "4.1667"} in evidence["fields"]


def test_a_local_tool_carries_no_authz_because_no_gateway_was_traversed():
    evidence = extract_evidence("calculator", {"expression": "1+1"}, _ok("2"), at=AT)
    assert "authz" not in evidence


def test_an_unknown_tool_falls_through_to_computation_with_no_source():
    evidence = extract_evidence("some_future_tool", {"a": 1}, _ok("done"), at=AT)
    assert evidence["kind"] == "computation"
    assert "source" not in evidence


def test_the_gateway_target_prefix_is_stripped_before_classifying():
    # _create_agent renames tools, but a direct-MCP call can still arrive prefixed.
    evidence = extract_evidence(
        "sap-target___odata_read",
        {"service_name": "S", "entity_set": "E"},
        _ok("{}"),
        at=AT,
    )
    assert evidence["kind"] == "sap_read"


def test_a_non_dict_input_does_not_crash_the_extractor():
    evidence = extract_evidence("odata_read", "not-a-dict", _ok("{}"), at=AT)
    assert evidence["kind"] == "sap_read"
    assert "source" not in evidence


# ── authz ────────────────────────────────────────────────────────────────────


def test_a_cedar_off_deployment_records_off_and_never_log_only():
    # The runtime sends OFF when Cedar is not enabled; a trace must not claim LOG_ONLY.
    for mode in ("OFF", "", "anything"):
        evidence = extract_evidence(
            "odata_read", {"entity_set": "E"}, _ok("{}"), at=AT, mode=mode
        )
        assert evidence["authz"] == {"mode": "OFF", "outcome": "permitted"}
    evidence = extract_evidence(
        "odata_read", {"entity_set": "E"}, _ok("{}"), at=AT, mode="LOG_ONLY"
    )
    assert evidence["authz"]["mode"] == "LOG_ONLY"


def test_a_run_checks_refusal_is_recorded_as_rejected():
    evidence = extract_evidence(
        "odata_function_import",
        {"function_name": "Release"},
        _err("Refused by the Gateway: a run writes only its own case's document"),
        at=AT,
    )
    assert evidence["authz"]["outcome"] == "rejected"


def test_success_false_and_an_error_body_are_failures():
    sap_401 = json.dumps(
        {"success": False, "error_type": "authentication_required", "message": "401"}
    )
    assert tool_failed(_ok(sap_401))
    assert tool_failed(_ok(json.dumps({"error": "Notification channel unavailable"})))
    assert tool_failed(_err("boom")) and tool_failed(None)
    assert not tool_failed(_ok(json.dumps({"success": True, "data": []})))
    assert not tool_failed(_ok(json.dumps({"error": ""})))
    assert not tool_failed(_ok("plain text"))
    # The Gateway permitted a call SAP refused: authz reads the transport, not SAP.
    evidence = extract_evidence("odata_read", {"entity_set": "E"}, _ok(sap_401), at=AT)
    assert evidence["authz"]["outcome"] == "permitted"


def test_an_authorization_failure_is_reported_as_rejected():
    evidence = extract_evidence(
        "odata_read",
        {"entity_set": "E"},
        _err(
            "PermissionError: Missing Gateway tool context — direct invocation is not permitted"
        ),
        at=AT,
    )
    assert evidence["authz"]["outcome"] == "rejected"


def test_a_non_authorization_failure_states_no_outcome():
    # Claiming "rejected" for a timeout would render a transport fault as a Cedar
    # denial. The segment's own status: "error" already says the call failed.
    evidence = extract_evidence(
        "odata_read", {"entity_set": "E"}, _err("504 Gateway Timeout after 30s"), at=AT
    )
    assert "outcome" not in evidence["authz"]


# ── truncation ───────────────────────────────────────────────────────────────


def test_truncation_leaves_a_small_segment_untouched():
    segment = {
        "type": "tool",
        "tool_input": "{}",
        "tool_result": "ok",
        "evidence": {"kind": "sap_read"},
    }
    assert truncate_segment(segment)["tool_result"] == "ok"
    assert "truncated" not in segment["evidence"]


def test_a_result_at_the_budget_is_not_marked_truncated():
    segment = {
        "type": "tool",
        "tool_result": "x" * TOOL_RESULT_MAX_BYTES,
        "evidence": {"kind": "sap_read"},
    }
    truncate_segment(segment)
    assert len(segment["tool_result"].encode()) == TOOL_RESULT_MAX_BYTES
    assert "truncated" not in segment["evidence"]


def test_one_byte_over_the_budget_truncates_and_marks_it():
    segment = {
        "type": "tool",
        "tool_result": "x" * (TOOL_RESULT_MAX_BYTES + 1),
        "evidence": {"kind": "sap_read"},
    }
    truncate_segment(segment)
    assert len(segment["tool_result"].encode()) <= TOOL_RESULT_MAX_BYTES
    assert segment["evidence"]["truncated"] is True


def test_the_input_budget_is_tighter_than_the_result_budget():
    segment = {
        "type": "tool",
        "tool_input": "y" * (TOOL_INPUT_MAX_BYTES + 1),
        "evidence": {"kind": "sap_read"},
    }
    truncate_segment(segment)
    assert len(segment["tool_input"].encode()) <= TOOL_INPUT_MAX_BYTES
    assert segment["evidence"]["truncated"] is True


def test_truncation_never_splits_a_multibyte_character():
    segment = {
        "type": "tool",
        "tool_result": "é" * TOOL_RESULT_MAX_BYTES,
        "evidence": {"kind": "sap_read"},
    }
    truncate_segment(segment)
    segment["tool_result"].encode("utf-8")  # would raise if a surrogate survived
    assert len(segment["tool_result"].encode()) <= TOOL_RESULT_MAX_BYTES


def test_truncation_works_on_a_segment_with_no_evidence():
    # Text segments and pre-migration tool segments both hit this path.
    segment = {"type": "text", "content": "hello"}
    assert truncate_segment(segment) == {"type": "text", "content": "hello"}


def test_an_oversized_result_on_a_segment_without_evidence_still_truncates():
    segment = {"type": "tool", "tool_result": "x" * 5000}
    truncate_segment(segment)
    assert len(segment["tool_result"].encode()) <= TOOL_RESULT_MAX_BYTES


# ── the trace cap ────────────────────────────────────────────────────────────


def test_under_the_cap_nothing_is_dropped():
    traces = [{"trace_id": str(i)} for i in range(MAX_TRACES)]
    kept, dropped = cap_traces(traces)
    assert dropped == 0
    assert kept == traces


def test_over_the_cap_the_oldest_go_first():
    traces = [{"trace_id": str(i)} for i in range(MAX_TRACES + 3)]
    kept, dropped = cap_traces(traces)
    assert dropped == 3
    assert len(kept) == MAX_TRACES
    assert kept[0]["trace_id"] == "3"
    assert kept[-1]["trace_id"] == str(MAX_TRACES + 2)


def test_the_cap_is_configurable_for_the_test_and_the_caller():
    kept, dropped = cap_traces([{"trace_id": str(i)} for i in range(5)], max_traces=2)
    assert dropped == 3
    assert [t["trace_id"] for t in kept] == ["3", "4"]


def test_the_cap_tolerates_a_missing_list():
    assert cap_traces(None) == ([], 0)
    assert cap_traces([]) == ([], 0)


# ── Citation by quotation: the substring check that replaced grading `per §N.N`. ──


_SOP = """# Price variance

3.1  The agent MUST compare the invoice unit price with the purchase order price.
3.2  The agent MUST escalate any variance above {{PRICE_VARIANCE_PCT}} percent to review.
3.3  The agent MUST NOT post an invoice while a variance is still open.
"""

CITED_AT = "2026-08-03T09:14:02Z"


def _cite(quote: str) -> str:
    return f'Escalated for review. per SOP: "{quote}"'


def _first_long_sentence(text: str) -> str:
    """A verbatim span from the document, long enough to clear the word floor.

    Lines carrying their own double quotes are skipped: wrapping one in a citation
    nests quote marks, and the extractor then sees only the fragment ahead of the
    inner quote. That is a real (benign) limitation of quote-mark delimiting, and
    it has its own case below rather than being smuggled into every corpus run.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if '"' in stripped:
            continue
        if len(stripped.split()) >= QUOTE_MIN_WORDS + 2 and "MUST" in stripped:
            return stripped
    raise AssertionError("no quotable normative line found")


# ── the corpus verifies, and inventions do not ───────────────────────────────


def test_a_verbatim_span_from_the_sop_verifies():
    quote = _first_long_sentence(_SOP)
    assert verify_citation(_cite(quote), _SOP) == {
        "quote": quote,
        "verified": True,
        "section": "3.1",
    }


def test_a_paraphrase_of_a_real_clause_does_not_verify():
    words = _first_long_sentence(_SOP).split()
    # Same words, reordered: semantically close, textually absent.
    verdict = verify_citation(_cite(" ".join(words[::-1])), _SOP)
    assert verdict is not None and verdict["verified"] is False


def test_a_fabricated_rule_under_a_real_clause_number_does_not_verify():
    # The exact case that grades PASS under `§N.N` set membership: §3.1 exists in
    # the document, and the rule attributed to it does not.
    body = _SOP
    assert "3.1" in body
    verdict = verify_citation(
        'Auto-approved per §3.1, "variances under $500 need no review"', body
    )
    assert verdict == {
        "quote": "variances under $500 need no review",
        "verified": False,
        # Not in the text, so it has no place in it: never the number it claims.
        "section": None,
    }


# ── the span bounds ──────────────────────────────────────────────────────────


def test_a_span_below_the_word_floor_is_not_gradeable():
    # "must not" matches nearly any SOP and proves nothing.
    body = "2.1  The agent MUST NOT fabricate a PO price under any circumstance."
    assert verify_citation('per SOP: "MUST NOT"', body) is None


def test_the_whole_sop_is_rejected_as_too_long():
    body = _SOP
    # Verbatim and present, but a citation has to be specific to be a citation.
    assert verify_citation(f'per SOP: "{body}"', body) is None


def test_a_long_span_carrying_few_full_stops_is_still_rejected():
    # The sentence ceiling alone does not bite here: a heading block or a run-on
    # clause can be pages long and terminate twice or not at all.
    body = "The agent MUST " + "review each line of the purchase order carefully " * 12
    assert verify_citation(f'per SOP: "{body}"', body) is None


def test_a_clause_containing_its_own_quote_marks_is_not_gradeable():
    # Quote-mark delimiting cannot survive nested quote marks — the span ends at the
    # inner mark. The verdict is "nothing to grade", which is the safe direction:
    # a compliant citation is never reported as invented.
    body = (
        'The agent MUST treat "ABOVE tolerance" as requiring approval before posting.'
    )
    assert verify_citation(f'per SOP: "{body}"', body) is None


def test_two_sentences_are_still_a_citation():
    body = "The agent MUST post the invoice without further review. Escalation is not required here."
    verdict = verify_citation(f'per SOP: "{body}"', body)
    assert verdict == {"quote": body, "verified": True, "section": None}


# ── the two renderings of one clause (the trap a single-path test misses) ────

_ASSEMBLED = """You are an AP specialist.

<sop_document>
2.2  Both limits MUST be breached for the variance to be ABOVE tolerance:
       ABOVE  tolerance = price_variance_pct > 2%
</sop_document>

Cite the rule you acted on.
"""

# What search_sap_sops returns for the same clause: contacts substituted, constants
# deliberately left visible, and newlines already collapsed to spaces by Retrieve.
_RETRIEVED = (
    "2.2  Both limits MUST be breached for the variance to be ABOVE tolerance: "
    "ABOVE  tolerance = price_variance_pct > {{PRICE_VARIANCE_PCT}}%"
)


def test_the_injected_sop_is_recovered_from_the_assembled_prompt():
    # resolve_skill substitutes AFTER wrapping, so this is the only place the text
    # the model actually saw exists — the raw file still reads {{PRICE_VARIANCE_PCT}}.
    extracted = sop_text_in(_ASSEMBLED)
    assert "price_variance_pct > 2%" in extracted
    assert "You are an AP specialist" not in extracted


def test_a_substituted_constant_verifies_against_the_assembled_prompt():
    quote = "ABOVE  tolerance = price_variance_pct > 2%"
    verdict = verify_citation(_cite(quote), sop_text_in(_ASSEMBLED))
    # A clause's wrapped line belongs to the clause above it.
    assert verdict == {"quote": quote, "verified": True, "section": "2.2"}


def test_the_same_clause_verifies_against_the_retrieval_path_rendering():
    # The union baseline is what makes both renderings gradeable. Quoting the
    # unsubstituted form is correct on the discovery path and must not read as
    # invented just because the injected copy renders it differently.
    baseline = f"{sop_text_in(_ASSEMBLED)}\n{_RETRIEVED}"
    for quote in (
        "ABOVE  tolerance = price_variance_pct > 2%",
        "ABOVE  tolerance = price_variance_pct > {{PRICE_VARIANCE_PCT}}%",
    ):
        assert verify_citation(_cite(quote), baseline)["verified"] is True


# ── an unnumbered SOP, which our own corpus cannot exercise ──────────────────

_UNNUMBERED = """Invoice Handling Procedure

When the invoiced amount matches the purchase order, the agent must post the
invoice without further review. Anything else goes to a human.
"""


def test_a_quote_from_an_unnumbered_sop_verifies_at_full_strength():
    # Most real SOPs are not numbered. The clause anchors in this repo's corpus are
    # hand-authored for the convention this check retires, so a verdict that leaned
    # on a number would overfit to the sample and fail on an adopter's documents.
    quote = "the agent must post the invoice without further review"
    verdict = verify_citation(f'per SOP: "{quote}"', _UNNUMBERED)
    assert verdict == {"quote": quote, "verified": True, "section": None}


def test_an_absent_number_never_weakens_the_verdict():
    quote = "the agent must post the invoice without further review"
    with_number = verify_citation(f'per §2.1, "{quote}"', _UNNUMBERED)
    without = verify_citation(f'per SOP: "{quote}"', _UNNUMBERED)
    assert with_number == without


# ── normalization ────────────────────────────────────────────────────────────


def test_normalization_folds_what_a_round_trip_through_a_model_changes():
    # Curly quotes, an em dash, a non-breaking space and a line break are all
    # things the model reintroduces; none of them is a citation error.
    original = "The agent MUST escalate — see the\ntolerance table."
    round_tripped = "the agent must escalate — see the tolerance table."
    assert normalize_quote(original) == normalize_quote(round_tripped)


def test_prose_with_no_quotation_marks_is_not_graded():
    # A paraphrase and an ungraded citation are both "unverified", never "violated" —
    # but only the absence of a *quote* means there is nothing to report at all.
    body = "2.1  The agent MUST compute the price variance using the calculator tool"
    assert verify_citation("Escalated per clause 2.1 as required.", body) is None


def test_an_empty_baseline_grades_nothing_rather_than_failing_everything():
    # A run with no SOP loaded must not report every citation as invented.
    verdict = verify_citation(_cite("the agent MUST post the invoice today"), "")
    assert verdict is not None and verdict["verified"] is False


# ── the extractor wiring ─────────────────────────────────────────────────────

_BASELINE = "3.2  The agent MUST notify the requester before closing the case."


def test_a_notification_records_the_verified_citation():
    evidence = extract_evidence(
        "send_notification",
        {
            "recipient": "ap-team@example.com",
            "body": _cite(
                "The agent MUST notify the requester before closing the case."
            ),
        },
        _ok(),
        at=CITED_AT,
        sop_baseline=_BASELINE,
    )
    assert evidence["citation"]["verified"] is True


def test_a_case_update_records_the_verified_citation():
    # tool_spec declares `updates` as a JSON string, so the citation arrives inside
    # a serialised note rather than as its own argument.
    evidence = extract_evidence(
        "update_case_state",
        {
            "case_id": "4500000123-10",
            "updates": '{"note": "Closed. per SOP: \\"The agent MUST notify the requester before closing the case.\\""}',
        },
        _ok(),
        at=CITED_AT,
        sop_baseline=_BASELINE,
    )
    assert evidence["citation"]["verified"] is True


def test_verification_runs_before_the_prose_preview_is_cut():
    # PROSE_MAX_CHARS caps the stored value at 240; a citation past that cut would
    # fail on a fragment of itself if the check ran on the preview.
    quote = "The agent MUST notify the requester before closing the case."
    body = "Context. " * 40 + _cite(quote)
    evidence = extract_evidence(
        "send_notification",
        {"recipient": "ap-team@example.com", "body": body},
        _ok(),
        at=CITED_AT,
        sop_baseline=_BASELINE,
    )
    assert len(body) > 240
    assert evidence["citation"] == {"quote": quote, "verified": True, "section": "3.2"}


def test_a_notification_with_no_quote_carries_no_citation_key():
    evidence = extract_evidence(
        "send_notification",
        {"recipient": "ap-team@example.com", "body": "Escalated for review."},
        _ok(),
        at=CITED_AT,
        sop_baseline=_BASELINE,
    )
    assert "citation" not in evidence


def test_extraction_without_a_baseline_still_works():
    # The default keeps every existing caller — and the tests in test_evidence.py —
    # on the same path they were on before verification existed.
    evidence = extract_evidence(
        "send_notification",
        {"recipient": "ap-team@example.com", "body": "Escalated."},
        _ok(),
        at=CITED_AT,
    )
    assert evidence["kind"] == "notification"
    assert "citation" not in evidence


# ── The merge seam between the hook and the persisted trace. ──


EVIDENCE_BY_ID = {
    "toolu_A": {
        "evidence": {"kind": "sap_read", "at": "2026-07-30T09:14:02Z"},
        "status": "success",
    },
    "toolu_B": {
        "evidence": {"kind": "sap_write", "op": "update"},
        "status": "error",
    },
}


def test_evidence_merges_onto_the_matching_segment():
    segments = [
        {"type": "text", "content": "Checking the PO."},
        {"type": "tool", "tool_call_id": "toolu_A", "tool_name": "odata_read"},
        {"type": "tool", "tool_call_id": "toolu_B", "tool_name": "odata_update"},
    ]
    merged = merge_evidence(segments, EVIDENCE_BY_ID)
    assert merged[0] == {"type": "text", "content": "Checking the PO."}
    assert merged[1]["evidence"]["kind"] == "sap_read"
    assert merged[1]["status"] == "success"
    assert merged[2]["evidence"]["op"] == "update"
    assert merged[2]["status"] == "error"


def test_a_segment_with_no_matching_evidence_is_left_alone():
    # The stream can fold a tool segment the hook never saw (e.g. the run was
    # cancelled mid-call). It must persist as today's shape, not crash.
    segments = [{"type": "tool", "tool_call_id": "toolu_UNKNOWN", "tool_name": "x"}]
    merged = merge_evidence(segments, EVIDENCE_BY_ID)
    assert "evidence" not in merged[0]
    assert "status" not in merged[0]


def test_a_segment_with_no_tool_call_id_is_left_alone():
    segments = [{"type": "tool", "tool_name": "legacy"}]
    assert merge_evidence(segments, EVIDENCE_BY_ID) == [
        {"type": "tool", "tool_name": "legacy"}
    ]


def test_no_evidence_at_all_is_a_no_op():
    segments = [{"type": "tool", "tool_call_id": "toolu_A"}]
    assert merge_evidence(segments, {}) == [{"type": "tool", "tool_call_id": "toolu_A"}]
    assert merge_evidence(segments, None) == [
        {"type": "tool", "tool_call_id": "toolu_A"}
    ]


def test_merge_applies_the_truncation_budgets():
    segments = [
        {
            "type": "tool",
            "tool_call_id": "toolu_A",
            "tool_result": "x" * 5000,
            "tool_input": "y" * 5000,
        }
    ]
    merged = merge_evidence(segments, EVIDENCE_BY_ID)
    assert len(merged[0]["tool_result"].encode()) <= 512
    assert len(merged[0]["tool_input"].encode()) <= 256
    assert merged[0]["evidence"]["truncated"] is True


def test_the_cap_drops_oldest_first_and_counts_what_went():
    traces = [{"trace_id": str(i)} for i in range(MAX_TRACES + 2)]
    kept, dropped = cap_traces(traces)
    assert dropped == 2
    assert kept[0]["trace_id"] == "2"
    assert len(kept) == MAX_TRACES


# ── Trace segment folding. ──


def _tool_call(tool_call_id="t1", name="odata_read", deltas=(), result=None, end=True):
    events = [
        {"type": "TOOL_CALL_START", "toolCallId": tool_call_id, "toolCallName": name},
    ]
    for delta in deltas:
        events.append(
            {"type": "TOOL_CALL_ARGS", "toolCallId": tool_call_id, "delta": delta}
        )
    if end:
        events.append({"type": "TOOL_CALL_END", "toolCallId": tool_call_id})
    if result is not None:
        events.append(
            {"type": "TOOL_CALL_RESULT", "toolCallId": tool_call_id, "content": result}
        )
    return events


def _fold(events):
    segments: list = []
    for event in events:
        accumulate_segment(segments, event)
    return segments


def test_tool_input_is_a_string_matching_the_schema():
    segments = _fold(_tool_call(deltas=['{"doc":', '"4500012345"}']))

    (segment,) = segments
    assert isinstance(segment["tool_input"], str), (
        "cases.schema.json types this as a string"
    )
    assert json.loads(segment["tool_input"]) == {"doc": "4500012345"}


def test_unparseable_arguments_keep_the_same_type():
    # The bug this replaces: parseable args produced a dict and unparseable ones a
    # string, so consumers saw two different types in the same field.
    segments = _fold(_tool_call(deltas=["not json at all"]))

    assert isinstance(segments[0]["tool_input"], str)
    assert segments[0]["tool_input"] == "not json at all"


def test_no_scratch_key_survives_a_completed_tool_call():
    segments = _fold(_tool_call(deltas=['{"a":1}'], result="ok"))

    assert "tool_input_raw" not in segments[0]


def test_no_scratch_key_survives_a_run_that_ends_mid_arguments():
    # A run cancelled or failed between TOOL_CALL_ARGS and TOOL_CALL_END used to
    # persist the accumulator alongside the real field.
    segments = _fold(_tool_call(deltas=['{"partial":'], end=False))

    assert "tool_input_raw" not in segments[0]
    assert segments[0]["tool_input"] == '{"partial":'


def test_arguments_accumulate_across_deltas_in_order():
    segments = _fold(_tool_call(deltas=["a", "b", "c"]))

    assert segments[0]["tool_input"] == "abc"


def test_result_is_recorded_against_the_matching_call():
    segments = _fold(
        _tool_call(tool_call_id="one", deltas=['{"x":1}'], result="first")
        + _tool_call(tool_call_id="two", deltas=['{"y":2}'], result="second")
    )

    assert [s["tool_result"] for s in segments] == ["first", "second"]
    assert [s["tool_call_id"] for s in segments] == ["one", "two"]


def test_text_deltas_fold_into_one_segment():
    segments = _fold(
        [
            {"type": "TEXT_MESSAGE_CONTENT", "delta": "Hello "},
            {"type": "TEXT_MESSAGE_CONTENT", "delta": "world"},
        ]
    )

    assert segments == [{"type": "text", "content": "Hello world"}]


def test_text_after_a_tool_call_starts_a_new_segment():
    segments = _fold(
        _tool_call(deltas=['{"a":1}'], result="ok")
        + [{"type": "TEXT_MESSAGE_CONTENT", "delta": "done"}]
    )

    assert [s["type"] for s in segments] == ["tool", "text"]


# ── the section a citation names ─────────────────────────────────────────────


def test_a_quote_names_the_clause_it_starts_in_as_the_sop_numbers_it():
    section_of = evidence.section_of

    sop = (UC / "packs" / "finance_ops" / "sops" / "price_variance.txt").read_text()
    # A lettered clause, and a wrapped line whose own first token looks like a number.
    assert (
        section_of("The agent MUST read the PO item's GoodsReceiptIsExpected", sop)
        == "1.2a"
    )
    assert section_of("Both limits MUST be breached", sop) == "2.2"
    wrapped = next(
        line.strip()
        for line in sop.splitlines()
        if line.startswith("     2.3 measures")
    )
    assert section_of(wrapped, sop) == "2.4"
    # Before the first clause, or not in the text at all.
    assert (
        section_of("This SOP defines how the agent resolves a PRICE VARIANCE", sop)
        is None
    )
    assert section_of("The agent MUST pay twice", sop) is None


def test_a_heading_or_the_next_sop_ends_a_clause():
    section_of = evidence.section_of

    text = (
        "4.1  The agent MUST escalate.\n"
        "================\n"
        "REVISION HISTORY\n"
        "The first release of this procedure.\n"
        "[sop finance_ops/short_pay.txt version v2]\n"
        "The agent MUST record the credit memo it asked for.\n"
    )
    assert section_of("The first release of this procedure", text) is None
    assert section_of("The agent MUST record the credit memo", text) is None


def test_a_collapsed_retrieval_chunk_gets_no_number():
    section_of = evidence.section_of

    chunk = (
        "1.1  The agent MUST read the invoice. "
        + "Filler text here. " * 20
        + ("2.4  The agent MUST NOT apply the dollar limit to an undercharge.")
    )
    assert len(chunk) > 200
    assert section_of("The agent MUST NOT apply the dollar limit", chunk) is None
