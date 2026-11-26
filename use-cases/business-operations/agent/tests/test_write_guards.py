# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The write guards: SOP rules that protect money, enforced against SAP in code.

Every case here is a stubbed SAP that answers `odata_read` by entity set, in the
envelope the AWS for SAP MCP Server really returns. The two P5C300 misses — a non-PO invoice
released after an approved ticket the agent raised itself — are reproduced from their
traces and must be refused whatever the ticket says.
"""

import asyncio
import json
from decimal import Decimal
from unittest import mock

import pytest
from strands.types._events import ToolResultEvent
from utils import write_gate as wg
from utils import write_guards as g

CASE = "ap-bchzfkch6kxm6uwqvqe5hhlkvx"
OTHER_CASE = "ap-2zjshtajnetsyhzykixwskpw54"
INVOICE, YEAR = "5100002330", "2026"
ALL = list(g.GUARDS)
CONSTANTS = {
    "QTY_VARIANCE_PCT": 5,
    "QTY_VARIANCE_UNITS": 10,
    "PRICE_VARIANCE_PCT": 2,
    "PRICE_VARIANCE_ABS_USD": 0.50,
    "LINE_AMOUNT_TOLERANCE_USD": 100,
    "DUPLICATE_MATCH_WINDOW_USD": 100,
}
APPROVERS = ["ap-manager"]


def _header(**over):
    return {
        "SupplierInvoice": INVOICE,
        "FiscalYear": YEAR,
        "InvoicingParty": "USSU-VSF06",
        "SupplierInvoiceIDByInvcgParty": "BMK-P5C300-0001",
        "InvoiceGrossAmount": "8720.07",
        "DocumentCurrency": "USD",
        **over,
    }


def _item(amount="8720.07", qty="10"):
    return {
        "SupplierInvoiceItem": "1",
        "PurchaseOrder": "4500002717",
        "PurchaseOrderItem": "10",
        "SupplierInvoiceItemAmount": amount,
        "QuantityInPurchaseOrderUnit": qty,
    }


def _po(price="864.22", qty="10", gr_expected=True):
    return {
        "PurchaseOrder": "4500002717",
        "PurchaseOrderItem": "10",
        "OrderQuantity": qty,
        "NetPriceAmount": price,
        "NetPriceQuantity": "1",
        "GoodsReceiptIsExpected": gr_expected,
    }


def _gr(qty="10", code="S", movement="101"):
    return {
        "MaterialDocument": "5000003455",
        "GoodsMovementType": movement,
        "DebitCreditCode": code,
        "QuantityInEntryUnit": qty,
    }


class SAP:
    """A stubbed SAP keyed by entity set. `dups` is what the 1.3 read returns besides self."""

    def __init__(self, header=None, items=None, dups=(), po=None, gr=None):
        self.items = [_item()] if items is None else items
        # Gross defaults to what the lines bill, so a test that moves a line amount is
        # not also testing unmatched money unless it says so.
        gross = sum(Decimal(i["SupplierInvoiceItemAmount"]) for i in self.items)
        self.header = header or _header(InvoiceGrossAmount=str(gross))
        self.dups = list(dups)
        self.po = [_po()] if po is None else po
        self.gr = [_gr()] if gr is None else gr
        self.calls = []

    async def read(self, service, entity_set, filter_, select):
        return self.rows(entity_set, filter_)

    def rows(self, entity_set, filter_):
        self.calls.append((entity_set, filter_))
        if entity_set == "A_SupplierInvoice":
            if filter_.startswith("SupplierInvoice eq"):
                return [self.header]
            return [self.header, *self.dups]
        return {
            "A_SuplrInvcItemPurOrdRef": self.items,
            "A_PurchaseOrderItem": self.po,
            "A_MaterialDocumentItem": self.gr,
        }[entity_set]


def _ticket(**over):
    return {
        "ticket_id": "TKT-C84810DF",
        "case_id": CASE,
        "category": "approval_request",
        "response_type": "approval",
        "status": "approved",
        "created_by": "agent",
        "decided_by": "b1e3-reviewer-sub",
        "decided_by_groups": ["ap-clerk", "ap-manager"],
        "title": "Approve release",
        "description": "",
        **over,
    }


RESUMED = {
    "trigger": "ticket-action",
    "payload": {"ticket_id": "TKT-C84810DF", "ticket_decision": "approved"},
}


def _check(sap, payload=None, ticket=None, guards=ALL, constants=CONSTANTS):
    async def get_ticket(_tid):
        return ticket

    return asyncio.run(
        g.check(
            guards,
            invoice=INVOICE,
            fiscal_year=YEAR,
            case_id=CASE,
            payload=payload or {},
            constants=constants,
            approve_groups=APPROVERS,
            read=sap.read,
            get_ticket=get_ticket,
        )
    )


def _refused(sap, **kw) -> g.GuardRefusal:
    with pytest.raises(g.GuardRefusal) as exc:
        _check(sap, **kw)
    return exc.value


# --------------------------------------------------------------------------- #
# A clean three-way match passes, and the gate reads what the SOPs read
# --------------------------------------------------------------------------- #


def test_a_clean_match_passes_every_guard():
    sap = SAP()
    _check(sap)
    assert [c[0] for c in sap.calls] == [
        "A_SupplierInvoice",
        "A_SuplrInvcItemPurOrdRef",
        "A_SupplierInvoice",
        "A_PurchaseOrderItem",
        "A_MaterialDocumentItem",
    ]
    # invoice_matching 1.3, word for word: never filtered on SupplierInvoice.
    assert sap.calls[2][1] == (
        "InvoicingParty eq 'USSU-VSF06' and SupplierInvoiceIDByInvcgParty eq "
        "'BMK-P5C300-0001' and ReverseDocument eq ''"
    )


def test_an_undeclared_guard_set_reads_nothing_it_does_not_need():
    sap = SAP()
    _check(sap, guards=["no_duplicate"])
    assert [c[0] for c in sap.calls] == ["A_SupplierInvoice", "A_SupplierInvoice"]


# --------------------------------------------------------------------------- #
# po_reference — missing_purchase_order 3.3
# --------------------------------------------------------------------------- #


def test_a_non_po_invoice_is_refused():
    refusal = _refused(SAP(items=[]))
    assert refusal.predicate == "po_reference"
    assert "manual_review_required" in refusal.next_step


@pytest.mark.parametrize(
    "case_ref, invoice",
    [("BMK-P5C300-0103", "5100002423"), ("BMK-P5C300-0292", "5100002629")],
)
def test_the_two_p5_missing_po_releases_are_refused(case_ref, invoice):
    """The only money-losing errors in the 300-case run, from their traces.

    A_SuplrInvcItemPurOrdRef returned `data: []`; the agent raised its own approval
    ticket ("Approve release of non-PO invoice …", category approval_request), the
    reviewer approved it, and the resumed run released. The approval is genuine and
    entitled here, so the only thing that can stop it is po_reference.
    """
    sap = SAP(
        header=_header(SupplierInvoice=invoice, SupplierInvoiceIDByInvcgParty=case_ref),
        items=[],
    )
    ticket = _ticket(title=f"Approve release of non-PO invoice {invoice} / FY 2026")
    refusal = _refused(sap, payload=RESUMED, ticket=ticket)
    assert refusal.predicate == "po_reference"
    assert refusal.reason.startswith("write_guard_refused: po_reference: ")


# --------------------------------------------------------------------------- #
# no_duplicate — invoice_matching 1.3 / 4.0 / 4.0a
# --------------------------------------------------------------------------- #

_ORIGINAL = {
    "SupplierInvoice": "5100002000",
    "FiscalYear": "2026",
    "InvoiceGrossAmount": "8720.07",
}


def test_a_posted_duplicate_is_refused():
    refusal = _refused(SAP(dups=[_ORIGINAL]))
    assert refusal.predicate == "no_duplicate"
    assert "5100002000" in refusal.detail


def test_the_invoice_itself_is_not_its_own_duplicate():
    # The 1.3 read always returns this invoice; leading zeros must not make it a match.
    _check(SAP(dups=[{"SupplierInvoice": "0005100002330", "FiscalYear": "2026"}]))


def test_no_reference_falls_back_to_the_amount_window():
    sap = SAP(header=_header(SupplierInvoiceIDByInvcgParty=""))
    _check(sap)
    assert (
        "InvoiceGrossAmount ge 8620.07M and InvoiceGrossAmount le 8820.07M"
        in sap.calls[2][1]
    )


def test_the_amount_window_is_refused_on_a_non_usd_invoice():
    sap = SAP(header=_header(SupplierInvoiceIDByInvcgParty="", DocumentCurrency="EUR"))
    assert _refused(sap, guards=["no_duplicate"]).predicate == "no_duplicate"


def _dup_ticket(**over):
    over = {
        "category": "duplicate_review",
        "description": "Possible duplicate of original invoice 5100002000 (FY 2026).",
        **over,
    }
    return _ticket(**over)


def test_4_0a_exempts_an_approved_duplicate_review_naming_every_original():
    _check(SAP(dups=[_ORIGINAL]), payload=RESUMED, ticket=_dup_ticket())


@pytest.mark.parametrize(
    "ticket, why",
    [
        (_ticket(description="original 5100002000"), "not a duplicate_review"),
        (_dup_ticket(description="Possible duplicate of 5100002001."), "does not name"),
        (_dup_ticket(decided_by_groups=["ap-clerk"]), "approve_groups"),
        (_dup_ticket(status="denied"), "not approved"),
    ],
)
def test_4_0a_does_not_exempt_an_unbound_ticket(ticket, why):
    refusal = _refused(SAP(dups=[_ORIGINAL]), payload=RESUMED, ticket=ticket)
    assert refusal.predicate == "no_duplicate"
    assert why in refusal.detail


def test_4_0a_needs_every_match_named():
    second = {**_ORIGINAL, "SupplierInvoice": "5100002999"}
    refusal = _refused(
        SAP(dups=[_ORIGINAL, second]), payload=RESUMED, ticket=_dup_ticket()
    )
    assert "5100002999" in refusal.detail


def test_4_0a_needs_the_run_to_have_resumed_on_the_ticket():
    refusal = _refused(
        SAP(dups=[_ORIGINAL]), payload={"trigger": "sap-poller"}, ticket=_dup_ticket()
    )
    assert "did not resume" in refusal.detail


# --------------------------------------------------------------------------- #
# gr_coverage — invoice_matching 2.1a, missing_goods_receipt 3.4
# --------------------------------------------------------------------------- #


def test_no_goods_receipt_is_refused_even_when_approved():
    refusal = _refused(SAP(gr=[]), payload=RESUMED, ticket=_ticket())
    assert refusal.predicate == "gr_coverage"
    assert "missing_goods_receipt" in refusal.next_step


def test_a_reversed_receipt_counts_as_none():
    assert (
        _refused(SAP(gr=[_gr(), _gr(code="H", movement="102")])).predicate
        == "gr_coverage"
    )


def test_a_po_that_expects_no_receipt_is_matched_against_the_po():
    sap = SAP(po=[_po(gr_expected=False)], gr=[])
    _check(sap)
    assert "A_MaterialDocumentItem" not in [c[0] for c in sap.calls]


# --------------------------------------------------------------------------- #
# within_tolerance_or_approved — price_variance / quantity_variance 2.2-2.4
# --------------------------------------------------------------------------- #


def test_price_within_both_limits_and_under_the_line_floor_passes():
    # The P5 price_within shape: 0.9% and $77.84 over on the line.
    _check(SAP(items=[_item(amount="8720.07")]))


def test_one_limit_breached_is_within_tolerance():
    # 10% over on a $1 part: the percentage breaches, $0.10/unit does not.
    _check(SAP(items=[_item(amount="11.00")], po=[_po(price="1.00")]))


def test_the_line_floor_overrides_the_two_limit_test():
    # 1.0% over at $1,000/unit is under 2%, but it bills $100.10 extra on the line.
    sap = SAP(items=[_item(amount="10100.10")], po=[_po(price="1000.00")])
    assert _refused(sap).predicate == "within_tolerance_or_approved"


def test_price_above_tolerance_needs_an_approval_ticket():
    sap = SAP(items=[_item(amount="9000.00")])  # 4.1%, $35.78/unit
    refusal = _refused(sap)
    assert refusal.predicate == "within_tolerance_or_approved"
    assert (
        '"approval"' in refusal.next_step
        and "awaiting_human_input" in refusal.next_step
    )


def test_price_above_tolerance_passes_on_an_entitled_approval():
    _check(SAP(items=[_item(amount="9000.00")]), payload=RESUMED, ticket=_ticket())


def test_quantity_one_unit_over_is_within_tolerance():
    # The P5 qty_within shape: 11 invoiced against 10 received at under $90/unit.
    _check(SAP(items=[_item(amount="889.90", qty="11")], po=[_po(price="80.90")]))


def test_quantity_over_billing_needs_approval_and_then_passes():
    sap = SAP(items=[_item(amount="18148.62", qty="21")], gr=[_gr(qty="10")])
    assert _refused(sap).predicate == "within_tolerance_or_approved"
    _check(sap, payload=RESUMED, ticket=_ticket())


def test_under_billing_auto_accepts():
    _check(SAP(items=[_item(amount="6913.76", qty="8")], gr=[_gr(qty="10")]))


def test_a_non_usd_invoice_always_needs_a_human():
    sap = SAP(header=_header(DocumentCurrency="EUR"))
    refusal = _refused(sap)
    assert "EUR" in refusal.detail


def test_a_missing_constant_refuses_rather_than_skipping_the_check():
    constants = {k: v for k, v in CONSTANTS.items() if k != "PRICE_VARIANCE_PCT"}
    assert _refused(SAP(), constants=constants).predicate == "config"


# --------------------------------------------------------------------------- #
# Approval provenance
# --------------------------------------------------------------------------- #

_ABOVE = {"items": [_item(amount="9000.00")]}


@pytest.mark.parametrize(
    "ticket, why",
    [
        (_ticket(decided_by_groups=["ap-clerk"]), "approve_groups"),
        (_ticket(decided_by="agent"), "raised it"),
        (_ticket(created_by="b1e3-reviewer-sub"), "raised it"),
        (_ticket(decided_by=None, decided_by_groups=None), "no principal"),
        (_ticket(case_id=OTHER_CASE), "not on this case"),
        (_ticket(response_type="free_text"), "not an approval"),
        (_ticket(status="replied"), "not approved"),
        (None, "could not be read"),
    ],
)
def test_an_approval_without_provenance_is_refused(ticket, why):
    refusal = _refused(SAP(**_ABOVE), payload=RESUMED, ticket=ticket)
    assert refusal.predicate == "within_tolerance_or_approved"
    assert why in refusal.detail
    assert "manual_review_required" in refusal.next_step


def test_a_payload_saying_approved_is_not_an_approval_without_the_ticket():
    # A console approval has no ticket to read back, and the trigger alone proves nothing.
    payload = {"trigger": "console-action", "payload": {"decision": "approved"}}
    assert (
        _refused(SAP(**_ABOVE), payload=payload).predicate
        == "within_tolerance_or_approved"
    )


# --------------------------------------------------------------------------- #
# Fail-closed
# --------------------------------------------------------------------------- #


def test_a_read_failure_refuses():
    sap = SAP()

    async def broken(*_a):
        raise g.GuardRefusal("sap_reread", "odata_read failed: 503")

    sap.read = broken
    assert _refused(sap).predicate == "sap_reread"


def test_a_missing_po_item_refuses():
    assert _refused(SAP(po=[])).predicate == "sap_reread"


@pytest.mark.parametrize(
    "text",
    [
        '{"success": false, "message": "OData request failed with status code 500", "data": null}',
        "not json",
        '{"success": true, "data": null}',
    ],
)
def test_an_unreadable_response_refuses(text):
    with pytest.raises(g.GuardRefusal) as exc:
        g.records(text)
    assert exc.value.predicate == "sap_reread"


def test_records_reads_both_envelopes():
    assert g.records('{"success": true, "data": [{"A": 1}], "metadata": {}}') == [
        {"A": 1}
    ]
    assert g.records('{"d": {"results": [{"A": 1}]}}') == [{"A": 1}]
    assert g.records('{"success": true, "data": []}') == []


# --------------------------------------------------------------------------- #
# Which calls are payment releases
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "tool, args, released",
    [
        (
            "odata_function_import",
            {
                "service_name": g.INVOICE_SERVICE,
                "function_name": "Release",
                "parameters": {
                    "SupplierInvoice": "'5100002330'",
                    "FiscalYear": "'2026'",
                },
            },
            ("5100002330", "2026"),
        ),
        (
            "odata_function_import",
            {
                "function_name": "Post",
                "parameters": '{"SupplierInvoice": "5100002330", "FiscalYear": "2026"}',
            },
            ("5100002330", "2026"),
        ),
        ("odata_function_import", {"function_name": "Cancel", "parameters": {}}, None),
        (
            "odata_function_import",
            {"service_name": "API_SALES_ORDER_SRV", "function_name": "Release"},
            None,
        ),
        (
            "odata_update",
            {
                "service_name": "API_PURCHASEORDER_PROCESS_SRV",
                "entity_set": "A_PurchaseOrder",
            },
            None,
        ),
    ],
)
def test_only_release_and_post_on_supplier_invoices_are_gated(tool, args, released):
    assert g.releasing_invoice(tool, args) == released


def test_a_release_that_names_no_invoice_is_refused():
    with pytest.raises(g.GuardRefusal):
        g.releasing_invoice(
            "odata_function_import", {"function_name": "Release", "parameters": {}}
        )


# --------------------------------------------------------------------------- #
# Through the wrapper: the read tool, the refusal text, the inner write
# --------------------------------------------------------------------------- #


class _Tool:
    """A registry tool: records calls, answers with `answer(input)` as MCP text."""

    def __init__(self, name, answer=None, status="success"):
        self._agent_tool_name = name
        self.answer = answer or (lambda _i: "{}")
        self.status = status
        self.calls = []

    tool_name = property(lambda self: self._agent_tool_name)
    tool_spec = property(lambda self: {"name": self._agent_tool_name})
    tool_type = property(lambda self: "fake")

    async def stream(self, tool_use, invocation_state, **kwargs):
        self.calls.append(tool_use["input"])
        yield ToolResultEvent(
            {
                "toolUseId": tool_use["toolUseId"],
                "status": self.status,
                "content": [{"text": self.answer(tool_use["input"])}],
            }
        )


def _odata(sap):
    def answer(args):
        rows = sap.rows(args["entity_set"], args["filter"])
        return json.dumps({"success": True, "data": rows, "metadata": {}})

    return answer


_RELEASE = {
    "service_name": g.INVOICE_SERVICE,
    "function_name": "Release",
    "parameters": {"SupplierInvoice": "'5100002330'", "FiscalYear": "'2026'"},
}


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(wg, "constants", lambda: {"finance_ops": CONSTANTS})
    monkeypatch.setattr(wg, "constant_overrides", lambda _p: {})
    monkeypatch.setattr(wg, "guards_by_pack", lambda: {"finance_ops": ALL})
    monkeypatch.setattr(
        wg, "entitlement_map", lambda: {"finance_ops": {"approve_groups": APPROVERS}}
    )
    monkeypatch.setattr(
        wg,
        "_case_identity",
        lambda _c: ("finance_ops", {"SupplierInvoice": INVOICE, "FiscalYear": YEAR}),
    )

    def build(sap, payload=None, read_status="success"):
        inner = _Tool("odata_function_import")
        read = _Tool("odata_read", _odata(sap), status=read_status)
        tickets = _Tool("demo_get_ticket", lambda _i: json.dumps(_ticket()))
        gate = wg.WriteGateTool(inner, CASE, payload or {}, read, tickets)
        return inner, read, gate

    return build


def _run(gate, args):
    use = {"toolUseId": "tu-1", "name": gate.tool_name, "input": args}

    async def drain():
        return [e async for e in gate.stream(use, {})]

    return asyncio.run(drain())[-1].tool_result


def test_the_wrapper_lets_a_clean_release_through(wired):
    inner, read, gate = wired(SAP())
    assert _run(gate, _RELEASE)["status"] == "success"
    assert len(inner.calls) == 1 and len(read.calls) == 5


def test_the_wrapper_refuses_before_the_write_runs(wired):
    inner, _, gate = wired(SAP(items=[]))
    result = _run(gate, _RELEASE)
    assert result["status"] == "error" and inner.calls == []
    text = result["content"][0]["text"]
    assert "write_guard_refused: po_reference: " in text and "do not retry" in text


def test_the_wrapper_fails_closed_when_the_read_tool_errors(wired):
    inner, _, gate = wired(SAP(), read_status="error")
    result = _run(gate, _RELEASE)
    assert "write_guard_refused: sap_reread: " in result["content"][0]["text"]
    assert inner.calls == []


def test_the_wrapper_fails_closed_with_no_read_tool(wired):
    inner, _, gate = wired(SAP())
    gate._read_tool = None
    assert "sap_reread" in _run(gate, _RELEASE)["content"][0]["text"]
    assert inner.calls == []


def test_the_wrapper_refuses_releasing_another_cases_invoice(wired):
    inner, read, gate = wired(SAP())
    args = {
        **_RELEASE,
        "parameters": {"SupplierInvoice": "'5100009999'", "FiscalYear": "'2026'"},
    }
    assert "case_binding" in _run(gate, args)["content"][0]["text"]
    assert inner.calls == [] and read.calls == []


def test_the_wrapper_does_not_read_sap_for_a_cancel(wired):
    inner, read, gate = wired(SAP(items=[]))
    args = {**_RELEASE, "function_name": "Cancel"}
    assert _run(gate, args)["status"] == "success"
    assert read.calls == [] and len(inner.calls) == 1


def test_the_wrapper_reads_the_ticket_for_an_approved_variance(wired):
    _, _, gate = wired(SAP(items=[_item(amount="9000.00")]), payload=RESUMED)
    assert _run(gate, _RELEASE)["status"] == "success"
    assert gate._ticket_tool.calls == [{"ticket_id": "TKT-C84810DF"}]


def test_a_pack_without_guards_is_not_checked(wired, monkeypatch):
    monkeypatch.setattr(wg, "guards_by_pack", lambda: {"material_ops": ALL})
    _inner, read, gate = wired(SAP(items=[]))
    assert _run(gate, _RELEASE)["status"] == "success"
    assert read.calls == []


def test_guards_alone_arm_the_wrapper(monkeypatch):
    monkeypatch.setattr(wg, "constants", lambda: {"finance_ops": {}})
    monkeypatch.setattr(wg, "guards_by_pack", lambda: {"finance_ops": ALL})
    registry = {
        n: _Tool(n) for n in ("odata_function_import", "odata_read", "demo_get_ticket")
    }
    agent = mock.Mock(tool_registry=mock.Mock(registry=registry))
    assert wg.wrap_write_gate_tools(agent, {"case_id": CASE}) == 1
    assert registry["odata_function_import"]._read_tool is registry["odata_read"]


# --------------------------------------------------------------------------- #
# Review round 1
# --------------------------------------------------------------------------- #

_P = {"SupplierInvoice": "'5100002330'", "FiscalYear": "'2026'"}


@pytest.mark.parametrize(
    "service, function",
    [
        ("api_supplierinvoice_process_srv", "Release"),
        (" API_SUPPLIERINVOICE_PROCESS_SRV", "Release"),
        ("API_SUPPLIERINVOICE_PROCESS_SRV;v=0001", "Release"),
        ("/sap/opu/odata/sap/API_SUPPLIERINVOICE_PROCESS_SRV/", "Release"),
        ("API_SUPPLIERINVOICE_PROCESS_SRV", "release"),
        ("API_SUPPLIERINVOICE_PROCESS_SRV", " POST "),
        # A function import nobody listed is still a write on the invoice: gated.
        ("API_SUPPLIERINVOICE_PROCESS_SRV", "ReleaseSupplierInvoice"),
    ],
)
def test_i1_any_spelling_of_a_release_is_gated(service, function):
    args = {"service_name": service, "function_name": function, "parameters": _P}
    assert g.releasing_invoice("odata_function_import", args) == (INVOICE, YEAR)


def test_i1_cancel_in_any_case_is_not_a_release():
    args = {
        "service_name": "api_supplierinvoice_process_srv",
        "function_name": "cancel",
    }
    assert g.releasing_invoice("odata_function_import", args) is None


def test_i1_a_patch_on_the_invoice_is_gated_as_a_release():
    # PaymentBlockingReason = '' unblocks payment as surely as Release does.
    args = {
        "service_name": g.INVOICE_SERVICE,
        "entity_set": "A_SupplierInvoice",
        "identifier_fields": _P,
        "payload": {"PaymentBlockingReason": ""},
    }
    assert g.releasing_invoice("odata_update", args) == (INVOICE, YEAR)


@pytest.mark.parametrize("tool", ["odata_create", "odata_delete"])
def test_i1_a_create_or_delete_on_the_invoice_service_is_refused(tool):
    with pytest.raises(g.GuardRefusal) as exc:
        g.releasing_invoice(
            tool, {"service_name": g.INVOICE_SERVICE, "entity_set": "A_SupplierInvoice"}
        )
    assert exc.value.predicate == "unguardable_write"


def test_i2_money_on_no_po_line_needs_approval():
    # $40,000 gross: a clean $100 PO line and $39,900 on a G/L line.
    sap = SAP(
        header=_header(InvoiceGrossAmount="40000.00"),
        items=[_item(amount="100.00", qty="1")],
        po=[_po(price="100.00", qty="1")],
        gr=[_gr(qty="1")],
    )
    refusal = _refused(sap)
    assert refusal.predicate == "within_tolerance_or_approved"
    assert (
        "39900.00 USD of the invoice gross is on no purchase-order line"
        in refusal.detail
    )
    _check(sap, payload=RESUMED, ticket=_ticket())


def test_i2_unmatched_money_within_the_line_floor_passes():
    sap = SAP(header=_header(InvoiceGrossAmount="8800.07"))  # $80 over the PO line
    _check(sap)


def test_i3_two_lines_on_one_po_item_are_graded_together():
    # 10 received, two invoice lines of 10 each: 20 billed against 10.
    sap = SAP(
        items=[
            _item(amount="8642.20"),
            {**_item(amount="8642.20"), "SupplierInvoiceItem": "2"},
        ]
    )
    refusal = _refused(sap)
    assert "invoiced quantity 20 exceeds the supported 10" in refusal.detail
    assert [c[0] for c in sap.calls].count("A_PurchaseOrderItem") == 1


def test_i3_lines_on_different_po_items_stay_separate():
    other = {**_item(), "SupplierInvoiceItem": "2", "PurchaseOrderItem": "20"}
    sap = SAP(items=[_item(), other])
    _check(sap)
    assert [c[0] for c in sap.calls].count("A_PurchaseOrderItem") == 2


def test_i4_the_ticket_is_read_through_either_envelope():
    bare = json.dumps(_ticket())
    lambda_shape = json.dumps({"content": [{"type": "text", "text": bare}]})
    assert g.ticket_from(bare)["case_id"] == CASE
    assert g.ticket_from(lambda_shape)["case_id"] == CASE
    assert g.ticket_from("No ticket found: TKT-X") is None


def test_i4_the_wrapper_accepts_the_lambda_envelope(wired):
    _, _, gate = wired(SAP(items=[_item(amount="9000.00")]), payload=RESUMED)
    envelope = {"content": [{"type": "text", "text": json.dumps(_ticket())}]}
    gate._ticket_tool.answer = lambda _i: json.dumps(envelope)
    assert _run(gate, _RELEASE)["status"] == "success"


def test_m3_a_ticket_with_no_response_type_is_not_an_approval():
    ticket = _ticket()
    del ticket["response_type"]
    refusal = _refused(SAP(**_ABOVE), payload=RESUMED, ticket=ticket)
    assert "not an approval" in refusal.detail


def test_m4_an_error_body_is_a_read_failure_not_a_record():
    with pytest.raises(g.GuardRefusal) as exc:
        g.records('{"error": {"code": "SY/530", "message": "boom"}}')
    assert exc.value.predicate == "sap_reread"


def test_m4_a_full_page_may_be_truncated_and_refuses():
    rows = [{"A": i} for i in range(g.READ_TOP)]
    with pytest.raises(g.GuardRefusal):
        g.records(json.dumps({"success": True, "data": rows}))


def test_m7_a_receipt_in_another_unit_needs_a_human():
    sap = SAP(
        po=[{**_po(), "PurchaseOrderQuantityUnit": "PC"}],
        gr=[{**_gr(), "EntryUnit": "BOX"}],
    )
    assert "differ from the PO unit PC" in _refused(sap).detail


def test_the_read_page_is_within_what_odata_read_accepts():
    # `top` above 100 is a ValidationException from the AWS for SAP MCP Server, measured live:
    # every guard read failed closed and an approved release was refused.
    assert g.READ_TOP <= 100


def test_the_guard_map_names_every_constant_a_guard_reads_and_no_other():
    """`guard_rules.GUARD_SYMBOLS` is what `GET /config` and `ops pack status` show as
    `guarded_by`, so a `_const` call it misses would show a control as a SOP value."""
    import ast
    from pathlib import Path

    from utils import guard_rules

    tree = ast.parse(Path(g.__file__).read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_const"
    ]
    # A symbol computed at run time could not be checked against the map.
    assert calls and all(
        len(c.args) == 2 and isinstance(c.args[1], ast.Constant) for c in calls
    )
    read = {c.args[1].value for c in calls}
    mapped = set().union(*guard_rules.GUARD_SYMBOLS.values())
    assert read - mapped == set(), "a guard reads a constant the map does not name"
    assert mapped - read == set(), "the map names a constant no guard reads"
    assert set(guard_rules.GUARD_SYMBOLS) == set(g.GUARDS)


def test_guarded_by_names_only_the_enabled_guards_that_read_the_symbol():
    from utils import guard_rules

    enabled = ["no_duplicate", "within_tolerance_or_approved"]
    assert guard_rules.guarded_by("PRICE_VARIANCE_PCT", enabled) == [
        "within_tolerance_or_approved"
    ]
    assert guard_rules.guarded_by("PRICE_VARIANCE_PCT", ["no_duplicate"]) == []
    assert guard_rules.guarded_by("MISSING_GR_ESCALATION_DAYS", enabled) == []
    rows = guard_rules.annotate(
        {
            "DUPLICATE_MATCH_WINDOW_USD": {"value": 100, "kind": "rule"},
            "AUTO_WRITE_LIMIT_USD": {"value": 1, "kind": "limit"},
            "CONTACT_AP": {"value": "a@b.c", "kind": "contact"},
        },
        enabled,
    )
    assert rows["DUPLICATE_MATCH_WINDOW_USD"]["guarded_by"] == ["no_duplicate"]
    assert rows["AUTO_WRITE_LIMIT_USD"]["guarded_by"] == []
    assert "guarded_by" not in rows["CONTACT_AP"]
