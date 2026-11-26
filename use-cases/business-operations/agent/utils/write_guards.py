# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The SOP rules that protect money, checked in code before a payment is released.

`write_gate.py` holds one limit as code — the amount ceiling. Every other rule a
finance SOP states is prose the model is asked to follow, and the e2e benchmark shows
what that costs: a non-PO invoice released after the agent raised its own approval
ticket (missing_purchase_order 3.3), a duplicate paid because the duplicate read was
skipped (invoice_matching 1.3/4.0), a release with no goods receipt (2.1a). None of
those is a judgement call. Each is a fact SAP can answer, so the runtime asks SAP
itself, through the same `odata_read` tool the agent holds, and refuses the write when
the answer is wrong — whichever model is driving and whatever it wrote in its notes.

**What is gated.** Every write to `API_SUPPLIERINVOICE_PROCESS_SRV` except a `Cancel`,
which reverses a document and pays nothing — see `releasing_invoice`. A pack opts in by naming guards in `write_guards`; one that names
none is not checked (declare-to-restrict, as `AUTO_WRITE_LIMIT_USD` is).

**Fail-closed.** A read that errors, returns nothing, or returns a shape this module
cannot read refuses the write as `sap_reread`. A constant a guard needs and the pack
does not declare refuses it as `config`.

**What counts as approval** (`within_tolerance_or_approved`, and the duplicate
exemption of invoice_matching 4.0a): the run resumed on a ticket, and that ticket —
read back through `demo_get_ticket`, never from the model's text — is on this case, is
an approval ticket, is approved, and was decided by a principal holding one of the
pack's `approve_groups` who is not the ticket's creator. The tickets route records
`decided_by` / `decided_by_groups` from the authorizer when the decision is made; an
approval recorded before that field existed carries neither and does not count.

Nothing here can approve anything: `po_reference` and `gr_coverage` have no override.
An approval cannot conjure a purchase order or a delivery.
"""

import json
import re
from collections.abc import Awaitable, Callable
from decimal import Decimal, InvalidOperation

from .case_key import try_normalize_case_id
from .guard_rules import GUARDS
from .mcp_topology import SAP_WRITE_TOOLS

INVOICE_SERVICE = "API_SUPPLIERINVOICE_PROCESS_SRV"
PO_SERVICE = "API_PURCHASEORDER_PROCESS_SRV"
GR_SERVICE = "API_MATERIAL_DOCUMENT_SRV"
#: The one function import on the invoice service that is not gated: it reverses a
#: document and pays nothing. Anything else there is treated as a release.
_NON_RELEASING = frozenset({"CANCEL"})

#: The currency every `_USD` constant is denominated in (invoice_matching 2.4).
USD = "USD"

#: Read(service, entity_set, filter, select) -> records. Raises GuardRefusal on failure.
Read = Callable[[str, str, str, str], Awaitable[list[dict]]]
#: GetTicket(ticket_id) -> the stored ticket, or None when there is none.
GetTicket = Callable[[str], Awaitable[dict | None]]


class GuardRefusal(Exception):
    """A guard said no. `predicate` is what the benchmark counts; `detail` is why."""

    #: The refusal's name on the span and at the head of `reason`.
    kind = "write_guard_refused"
    #: What the agent is told made the decision, so it does not argue with a model.
    source = "a check of SAP data made in code"

    def __init__(self, predicate: str, detail: str, next_step: str = ""):
        super().__init__(f"{predicate}: {detail}")
        self.predicate = predicate
        self.detail = detail
        self.next_step = next_step

    @property
    def reason(self) -> str:
        return f"{self.kind}: {self.predicate}: {self.detail}"


_MANUAL_REVIEW = (
    "Set the case to manual_review_required with this reason in the note, and stop."
)


def _unquote(value) -> str:
    """`'5100001548'` (an OData literal, as the agent passes it) to `5100001548`."""
    return str(value or "").strip().strip("'").strip()


def _service(value) -> str:
    """A service name however it is spelled: case, whitespace, a path, a `;v=` suffix."""
    name = str(value or "").strip().split(";", 1)[0].rstrip("/")
    return name.rsplit("/", 1)[-1].strip().upper()


def _params(value) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = None
    return value if isinstance(value, dict) else {}


def releasing_invoice(
    tool_name: str, tool_input, cancel_too: bool = False
) -> tuple[str, str] | None:
    """`(SupplierInvoice, FiscalYear)` when this call could release a payment, else None.

    `cancel_too` counts a `Cancel` as well, for the write verifier: a reversal pays
    nothing, but cancelling an invoice that should have been paid is a judgement error
    too, and no guard below can grade it.

    Fail-closed on the supplier-invoice service: every write to it is treated as a
    release except a `Cancel`, which reverses a document and pays nothing. That covers
    Release and Post however they are spelled, any function import this module has
    never heard of, and an `odata_update` that could clear PaymentBlockingReason. A
    create or delete there cannot be checked against an existing invoice at all, so it
    is refused. Writes to any other service are not payment releases.

    Raises GuardRefusal when it is such a write whose invoice cannot be read off the
    arguments — a release the guards cannot check is not one they let through.
    """
    if not isinstance(tool_input, dict) or tool_name not in SAP_WRITE_TOOLS:
        return None
    service = _service(tool_input.get("service_name"))
    if service and service != INVOICE_SERVICE:
        return None
    if tool_name == "odata_function_import":
        function = str(tool_input.get("function_name") or "").strip().upper()
        if function in _NON_RELEASING and not cancel_too:
            return None
        params = _params(tool_input.get("parameters"))
    elif tool_name == "odata_update":
        params = _params(tool_input.get("identifier_fields"))
    else:
        raise GuardRefusal(
            "unguardable_write",
            f"{tool_name} on {INVOICE_SERVICE} cannot be checked against an existing invoice",
            _MANUAL_REVIEW,
        )
    invoice, year = (
        _unquote(params.get("SupplierInvoice")),
        _unquote(params.get("FiscalYear")),
    )
    if not invoice or not year:
        raise GuardRefusal(
            "case_binding",
            "the write names no SupplierInvoice and FiscalYear to check",
            _MANUAL_REVIEW,
        )
    return invoice, year


def same_number(a, b) -> bool:
    """SAP document numbers compared the way SAP's ALPHA conversion does."""
    return _unquote(a).lstrip("0") == _unquote(b).lstrip("0")


def records(text: str) -> list[dict]:
    """The records in an `odata_read` result, through either envelope it arrives in.

    The AWS for SAP MCP Server wraps a read as `{"success", "message", "data": [...]}`; a raw
    OData v2 body is `{"d": {"results": [...]}}`. `success: false` is a refusal even
    though the tool call itself reported success.
    """
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        raise GuardRefusal("sap_reread", f"unreadable SAP response: {str(text)[:160]}")
    if isinstance(payload, dict) and payload.get("success") is False:
        raise GuardRefusal(
            "sap_reread", f"SAP refused the read: {payload.get('message')}"
        )
    for _ in range(3):
        if not isinstance(payload, dict):
            break
        key = next((k for k in ("data", "d", "results", "value") if k in payload), None)
        if key is None:
            break
        payload = payload[key]
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        raise GuardRefusal(
            "sap_reread", f"no records in SAP response: {str(text)[:160]}"
        )
    rows = [r for r in payload if isinstance(r, dict)]
    if len(rows) >= READ_TOP:
        raise GuardRefusal(
            "sap_reread", f"the read hit the {READ_TOP}-row page and may be truncated"
        )
    if any("error" in r for r in rows):
        raise GuardRefusal("sap_reread", f"SAP returned an error: {str(text)[:160]}")
    return rows


#: `$top` on every guard read. A read that returns this many rows may be truncated. 100
#: is the most the AWS for SAP MCP Server's `odata_read` accepts: `top` above it is a
#: ValidationException (measured live 2026-10-26).
READ_TOP = 100


def ticket_from(text: str) -> dict | None:
    """The stored ticket out of a `demo_get_ticket` result, or None.

    Through the Gateway the text is the bare ticket (measured live 2026-10-26). The
    ticket Lambda itself returns an MCP `content` envelope, so that shape is unwrapped
    too rather than read as a ticket with no `case_id`.
    """
    for _ in range(3):
        try:
            value = json.loads(text)
        except (ValueError, TypeError):
            return None
        content = value.get("content") if isinstance(value, dict) else None
        if not isinstance(content, list):
            return value if isinstance(value, dict) and "ticket_id" in value else None
        text = "".join(c.get("text", "") for c in content if isinstance(c, dict))
    return None


def _q(value) -> str:
    """An OData string literal, single quotes doubled (invoice_matching 1.3)."""
    return "'" + str(value).replace("'", "''") + "'"


def _dec(value, what: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise GuardRefusal("sap_reread", f"{what} is not a number: {value!r}")
    if not number.is_finite():
        raise GuardRefusal("sap_reread", f"{what} is not a number: {value!r}")
    return number


def _const(constants: dict, symbol: str) -> Decimal:
    if symbol not in constants:
        raise GuardRefusal("config", f"the pack declares no {symbol}", _MANUAL_REVIEW)
    try:
        return Decimal(str(constants[symbol]))
    except (InvalidOperation, TypeError, ValueError):
        raise GuardRefusal(
            "config", f"{symbol}={constants[symbol]!r} is not a number", _MANUAL_REVIEW
        )


def _true(value) -> bool:
    return value is True or str(value).lower() in ("true", "x")


def received_quantity(rows: list[dict]) -> Decimal:
    """Net goods-receipt quantity: receipts minus reversals and returns.

    `DebitCreditCode` S adds and H subtracts, which nets a 101 against its 102 and a
    122 return without enumerating movement types. A row with no code falls back to
    101/102, and anything else is skipped rather than guessed at.
    """
    total = Decimal(0)
    for row in rows:
        quantity = _dec(
            row.get("QuantityInEntryUnit", row.get("QuantityInBaseUnit")),
            "goods receipt quantity",
        )
        code = str(row.get("DebitCreditCode") or "").upper()
        movement = str(row.get("GoodsMovementType") or "")
        if code == "S" or (not code and movement == "101"):
            total += quantity
        elif code == "H" or (not code and movement == "102"):
            total -= quantity
    return total


def variances_above_tolerance(
    *,
    invoice_amount: Decimal,
    invoice_qty: Decimal,
    po_price: Decimal,
    reference_qty: Decimal,
    currency: str,
    constants: dict,
) -> list[str]:
    """Why this item is ABOVE tolerance, as SOP clauses define it; empty when within.

    Price (price_variance 2.2-2.4): above when the per-unit variance breaches BOTH
    `PRICE_VARIANCE_PCT` and `PRICE_VARIANCE_ABS_USD`, or when the line bills more than
    `LINE_AMOUNT_TOLERANCE_USD` over the PO price. Quantity (quantity_variance 2.2-2.4,
    3.2): only over-billing counts — an under-billed invoice auto-accepts — and it is
    above when it breaches BOTH `QTY_VARIANCE_PCT` and `QTY_VARIANCE_UNITS`, or bills
    more than `LINE_AMOUNT_TOLERANCE_USD` beyond the supported quantity.

    A non-USD invoice is above by definition (invoice_matching 2.4): the limits are in
    dollars and no exchange rate is declared, so only a human can grade it.
    """
    if currency and currency != USD:
        return [f"the invoice is in {currency} and every tolerance is in {USD}"]
    if invoice_qty <= 0 or po_price <= 0:
        raise GuardRefusal(
            "sap_reread",
            f"cannot grade a variance with invoiced quantity {invoice_qty} and PO price {po_price}",
        )
    line_limit = _const(constants, "LINE_AMOUNT_TOLERANCE_USD")
    reasons = []

    unit_diff = invoice_amount / invoice_qty - po_price
    price_pct = abs(unit_diff) / po_price * 100
    overcharge = invoice_amount - po_price * invoice_qty
    if (
        price_pct > _const(constants, "PRICE_VARIANCE_PCT")
        and abs(unit_diff) > _const(constants, "PRICE_VARIANCE_ABS_USD")
    ) or overcharge > line_limit:
        reasons.append(
            f"price variance {price_pct:.2f}% ({unit_diff:+.2f}/unit, line "
            f"{overcharge:+.2f} USD) exceeds tolerance"
        )

    if invoice_qty > reference_qty:
        units = invoice_qty - reference_qty
        qty_pct = units / reference_qty * 100 if reference_qty > 0 else Decimal(100)
        if (
            qty_pct > _const(constants, "QTY_VARIANCE_PCT")
            and units > _const(constants, "QTY_VARIANCE_UNITS")
        ) or units * po_price > line_limit:
            reasons.append(
                f"invoiced quantity {invoice_qty} exceeds the supported {reference_qty} "
                f"by {units} ({qty_pct:.2f}%, {units * po_price:.2f} USD)"
            )
    return reasons


def approval_problem(
    ticket: dict | None, case_id: str, approve_groups: list[str]
) -> str | None:
    """Why this ticket is not an approval a payment release may rely on, or None."""
    if not ticket:
        return "the ticket could not be read back"
    tid = ticket.get("ticket_id", "?")
    if try_normalize_case_id(ticket.get("case_id")) != case_id:
        return f"ticket {tid} is not on this case"
    if ticket.get("response_type") != "approval":
        return (
            f"ticket {tid} is a {ticket.get('response_type')} ticket, not an approval"
        )
    if ticket.get("status") != "approved":
        return f"ticket {tid} is {ticket.get('status')!r}, not approved"
    decider = str(ticket.get("decided_by") or "")
    if not decider:
        return f"ticket {tid} records no principal who decided it"
    if decider in ("agent", str(ticket.get("created_by") or "")):
        return f"ticket {tid} was decided by the principal that raised it"
    if not approve_groups:
        return "this pack declares no approve_groups, so no approval can be verified"
    if not set(ticket.get("decided_by_groups") or []) & set(approve_groups):
        return (
            f"ticket {tid} was decided by a principal in none of the pack's "
            f"approve_groups {sorted(approve_groups)}"
        )
    return None


def resumed_ticket(payload: dict) -> str | None:
    """The ticket an approved `ticket-action` run resumed on, or None."""
    ctx = payload.get("payload") or {}
    if (
        payload.get("trigger") != "ticket-action"
        or ctx.get("ticket_decision") != "approved"
    ):
        return None
    return str(ctx.get("ticket_id") or "") or None


async def check(
    guards: list[str],
    *,
    invoice: str,
    fiscal_year: str,
    case_id: str,
    payload: dict,
    constants: dict,
    approve_groups: list[str],
    read: Read,
    get_ticket: GetTicket,
) -> None:
    """Raise GuardRefusal unless every enabled guard holds for this invoice."""
    unknown = sorted(set(guards) - set(GUARDS))
    if unknown:
        raise GuardRefusal("config", f"unknown write_guards {unknown}", _MANUAL_REVIEW)
    enabled = set(guards)
    key = f"SupplierInvoice eq {_q(invoice)} and FiscalYear eq {_q(fiscal_year)}"

    approval_cache: dict = {}

    async def approval() -> tuple[dict | None, str | None]:
        """`(ticket, problem)` for the ticket this run resumed on; read at most once."""
        if "v" not in approval_cache:
            ticket_id = resumed_ticket(payload)
            if not ticket_id:
                approval_cache["v"] = (
                    None,
                    "this run did not resume on an approved ticket",
                )
            else:
                ticket = await get_ticket(ticket_id)
                approval_cache["v"] = (
                    ticket,
                    approval_problem(ticket, case_id, approve_groups),
                )
        return approval_cache["v"]

    headers = await read(
        INVOICE_SERVICE,
        "A_SupplierInvoice",
        key,
        "SupplierInvoice,FiscalYear,InvoicingParty,SupplierInvoiceIDByInvcgParty,"
        # SupplierInvoiceStatus rides along for the verifier: invoice_matching 4.1 keys
        # Release on it, and a checker that cannot see it refuses.
        "InvoiceGrossAmount,DocumentCurrency,SupplierInvoiceStatus",
    )
    if len(headers) != 1:
        raise GuardRefusal(
            "sap_reread",
            f"invoice {invoice}/{fiscal_year} read back {len(headers)} headers",
        )
    header = headers[0]
    currency = str(header.get("DocumentCurrency") or "")

    po_items: list[dict] = []
    if enabled & {"po_reference", "gr_coverage", "within_tolerance_or_approved"}:
        items = await read(
            INVOICE_SERVICE,
            "A_SuplrInvcItemPurOrdRef",
            key,
            "SupplierInvoiceItem,PurchaseOrder,PurchaseOrderItem,SupplierInvoiceItemAmount,"
            "QuantityInPurchaseOrderUnit",
        )
        po_items = [i for i in items if _unquote(i.get("PurchaseOrder"))]
    # First, because it is the one no approval can lift.
    if "po_reference" in enabled and not po_items:
        raise GuardRefusal(
            "po_reference",
            f"invoice {invoice}/{fiscal_year} has no purchase-order item",
            "Per missing_purchase_order 3.3 and 4.2 the agent never releases an invoice "
            "without a valid PO reference, and no ticket or approval changes that: set "
            "the case to manual_review_required for the AP team and stop.",
        )

    if "no_duplicate" in enabled:
        await _check_duplicate(
            header, invoice, fiscal_year, currency, constants, read, approval
        )

    # One entry per PO item, however many invoice lines bill it: two lines of 10 against
    # a receipt of 10 are 20 billed, not two clean 10s.
    lines: dict[tuple[str, str], dict] = {}
    for item in po_items:
        po, po_item = item.get("PurchaseOrder"), item.get("PurchaseOrderItem")
        line = lines.setdefault(
            (_unquote(po).lstrip("0"), _unquote(po_item).lstrip("0")),
            {
                "PurchaseOrder": po,
                "PurchaseOrderItem": po_item,
                "amount": Decimal(0),
                "qty": Decimal(0),
            },
        )
        line["amount"] += _dec(
            item.get("SupplierInvoiceItemAmount"), "SupplierInvoiceItemAmount"
        )
        line["qty"] += _dec(
            item.get("QuantityInPurchaseOrderUnit"), "QuantityInPurchaseOrderUnit"
        )

    above: list[str] = []
    for line in lines.values():
        above += await _check_item(line, enabled, currency, constants, read)

    # Money on the invoice that no PO line accounts for — a G/L line, unplanned delivery
    # cost — is graded too, or one clean $100 PO line carries a $39,900 G/L line through.
    # The line floor is invoice_matching 2.2's: over-billing beyond what the PO supports.
    if (
        "within_tolerance_or_approved" in enabled
        and po_items
        and (not currency or currency == USD)
    ):
        unmatched = _dec(header.get("InvoiceGrossAmount"), "InvoiceGrossAmount") - sum(
            (line["amount"] for line in lines.values()), Decimal(0)
        )
        if unmatched > _const(constants, "LINE_AMOUNT_TOLERANCE_USD"):
            above.append(
                f"{unmatched:.2f} USD of the invoice gross is on no purchase-order line"
            )

    if above and "within_tolerance_or_approved" in enabled:
        ticket, problem = await approval()  # noqa: RUF059
        if problem:
            resumed = resumed_ticket(payload) is not None
            raise GuardRefusal(
                "within_tolerance_or_approved",
                f"{'; '.join(above)}, and {problem}",
                (
                    "The approval this run resumed on does not qualify. "
                    + _MANUAL_REVIEW
                    if resumed
                    else "Raise an approval ticket for this write — response_type "
                    '"approval", with the variance and the limit it breaches — then set '
                    "the case to awaiting_human_input and stop."
                ),
            )


async def _check_duplicate(
    header, invoice, fiscal_year, currency, constants, read, approval
):
    """invoice_matching 1.3's read, made by the gate itself; 4.0a is the one exemption."""
    party = header.get("InvoicingParty")
    reference = header.get("SupplierInvoiceIDByInvcgParty")
    if not party:
        raise GuardRefusal("sap_reread", f"invoice {invoice} has no InvoicingParty")
    if reference:
        term = f"SupplierInvoiceIDByInvcgParty eq {_q(reference)}"
    else:
        if currency and currency != USD:
            raise GuardRefusal(
                "no_duplicate",
                f"invoice {invoice} has no reference and is in {currency}, so the USD "
                "amount window cannot be applied",
                _MANUAL_REVIEW,
            )
        gross = _dec(header.get("InvoiceGrossAmount"), "InvoiceGrossAmount")
        window = _const(constants, "DUPLICATE_MATCH_WINDOW_USD")
        term = f"InvoiceGrossAmount ge {gross - window}M and InvoiceGrossAmount le {gross + window}M"
    found = await read(
        INVOICE_SERVICE,
        "A_SupplierInvoice",
        f"InvoicingParty eq {_q(party)} and {term} and ReverseDocument eq ''",
        "SupplierInvoice,FiscalYear,InvoiceGrossAmount,ReverseDocument",
    )
    matches = sorted(
        str(r.get("SupplierInvoice"))
        for r in found
        if not (
            same_number(r.get("SupplierInvoice"), invoice)
            and same_number(r.get("FiscalYear"), fiscal_year)
        )
    )
    if not matches:
        return
    ticket, problem = await approval()
    if not problem:
        if (ticket or {}).get("category") != "duplicate_review":
            problem = "the approved ticket is not a duplicate_review ticket"
        else:
            named = f"{ticket.get('title', '')}\n{ticket.get('description', '')}"
            missing = [
                m
                for m in matches
                if not re.search(rf"(?<!\d)0*{re.escape(m.lstrip('0'))}(?!\d)", named)
            ]
            if missing:
                problem = f"the duplicate_review ticket does not name {missing}"
    if problem:
        raise GuardRefusal(
            "no_duplicate",
            f"invoice {invoice} matches posted invoice(s) {matches} from invoicing party "
            f"{party}, and {problem}",
            "Per invoice_matching 1.3 this is a duplicate: load_sop duplicate_invoice and "
            "follow it. Only an approved duplicate_review ticket naming every one of "
            f"{matches} lets this write through (invoice_matching 4.0a).",
        )


async def _check_item(item, enabled, currency, constants, read) -> list[str]:
    """gr_coverage for one PO item, and the tolerance reasons it contributes.

    `item` is every invoice line on that PO item summed: `amount` and `qty`.
    """
    po, po_item = item.get("PurchaseOrder"), item.get("PurchaseOrderItem")
    line = f"PurchaseOrder eq {_q(po)} and PurchaseOrderItem eq {_q(po_item)}"
    po_rows = await read(
        PO_SERVICE,
        "A_PurchaseOrderItem",
        line,
        "PurchaseOrder,PurchaseOrderItem,OrderQuantity,PurchaseOrderQuantityUnit,"
        "NetPriceAmount,NetPriceQuantity,GoodsReceiptIsExpected",
    )
    if len(po_rows) != 1:
        raise GuardRefusal(
            "sap_reread", f"PO item {po}/{po_item} read back {len(po_rows)} rows"
        )
    po_row = po_rows[0]
    gr_expected = _true(po_row.get("GoodsReceiptIsExpected"))

    received = None
    units: list[str] = []
    if gr_expected:
        gr_rows = await read(
            GR_SERVICE,
            "A_MaterialDocumentItem",
            line,
            "MaterialDocument,GoodsMovementType,DebitCreditCode,QuantityInEntryUnit,"
            "EntryUnit,QuantityInBaseUnit",
        )
        received = received_quantity(gr_rows)
        po_unit = str(po_row.get("PurchaseOrderQuantityUnit") or "")
        entry_units = {str(r.get("EntryUnit") or "") for r in gr_rows} - {""}
        if po_unit and entry_units - {po_unit}:
            # The invoice is in PO units; a receipt in another unit cannot be compared
            # without a conversion factor this module does not read (uom_mismatch).
            units = [
                f"goods receipt units {sorted(entry_units)} differ from the PO unit {po_unit}"
            ]
        if "gr_coverage" in enabled and received <= 0:
            raise GuardRefusal(
                "gr_coverage",
                f"PO item {po}/{po_item} expects a goods receipt and none is posted",
                "Per invoice_matching 2.1a this is a missing goods receipt: load_sop "
                "missing_goods_receipt and follow it. The release is possible only once "
                "a receipt is posted, and no approval changes that.",
            )

    if "within_tolerance_or_approved" not in enabled:
        return []
    price_unit = _dec(
        po_row.get("NetPriceQuantity") or 1, "NetPriceQuantity"
    ) or Decimal(1)
    # ponytail: compares to everything received, not to what earlier invoices left
    # uninvoiced; a second invoice for an already billed receipt needs cumulative
    # invoiced quantity (PO history) to catch.
    return units + variances_above_tolerance(
        invoice_amount=item["amount"],
        invoice_qty=item["qty"],
        po_price=_dec(po_row.get("NetPriceAmount"), "NetPriceAmount") / price_unit,
        reference_qty=received
        if received is not None
        else _dec(po_row.get("OrderQuantity"), "OrderQuantity"),
        currency=currency,
        constants=constants,
    )
