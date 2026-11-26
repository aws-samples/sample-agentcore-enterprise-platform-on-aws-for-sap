# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""What the SOP says the outcome must be, computed from the seeded SAP numbers.

Every tolerance in `packs/finance_ops/` is a string substituted into SOP prose by
`agent/utils/config_overrides.py::substitute`, so the *only* thing that decides
whether the agent applied `PRICE_VARIANCE_PCT` correctly is the model reading the
sentence. This module is the second opinion: pure arithmetic over the same
`seed.inputs` the pack's seed posts, returning a key of `OUTCOME_TO_STATUS`.

Pure stdlib and plain dicts, deliberately: the tests import it, and nothing a test
imports may reach strands or mcp.

**It encodes each SOP rule a second time, so it can drift from the prose.** Neither
side is generated from the other; do not try. What is pinned instead is the shared
surface: `tests/test_business_operations_evals.py` fails if a constant named here is
absent from `pack.json`.

All nine `process_type`s are modelled. Three of them only became decidable once
their own SOP said what to do when its premise fails — `missing_purchase_order` 3.1,
`duplicate_invoice` 1.3 and `uom_mismatch` 1.4 each reprocess the case as a three-way
match, because the seed cannot fail a PO lookup, cannot post a second invoice
and sets no unit of measure. A `process_type` with no arithmetic here still raises,
because a plausible guess is worse than a gap: it would grade the agent against a
rule the SOP never states.
"""

from __future__ import annotations

from typing import Any

# Three keys of OUTCOME_TO_STATUS, below. Note that `approval_required`
# and `manual_review_required` map to the *same* status set there, so the choice
# between them changes no verdict today — it is kept honest anyway so that splitting
# them later is a corpus change and not a rewrite of this file.
AUTO_RELEASE = "auto_release"
APPROVAL_REQUIRED = "approval_required"
MANUAL_REVIEW = "manual_review_required"

# Maps an `expected.outcome` in a pack's evals.json to the CaseStatus values that
# satisfy it. Kept here, beside the outcomes, rather than in the schema, because
# "auto_release" is an eval-level assertion about agent behaviour, not a case state;
# `run_regression.py` grades with it and so does the console's test data page.
OUTCOME_TO_STATUS = {
    "auto_release": {"sap_updated", "complete"},
    "approval_required": {"awaiting_human_input", "manual_review_required"},
    "manual_review_required": {"manual_review_required", "awaiting_human_input"},
    # A case the run must decline to act on at all. Deliberately the widest set,
    # including the status the case was seeded with: an unentitled caller's run never
    # reaches the case data, so nothing writes a new status, and "the record is
    # untouched" is the pass. What carries the assertion is `forbidden_tool_calls`,
    # not this — a refusal that still called a write tool fails there.
    "refused": {"detected", "manual_review_required", "awaiting_human_input", "error"},
}

# The pack whose SOPs this file encodes, the only one the corpus covers.
PACK_ID = "finance_ops"

# Every constant this file reads. The tests assert each one exists
# in that pack's `constants`. A subset, not an equality: the three it omits are date-
# and history-based, and the seeded data reaches no clause that reads them —
# MISSING_GR_ESCALATION_DAYS and GOODS_IN_TRANSIT_REMINDER_DAYS only choose between
# two escalation messages that share one status set, and DUPLICATE_MATCH_WINDOW_USD
# is read by duplicate_invoice 3.1/3.2, which clause 1.3 makes unreachable on data
# carrying exactly one invoice.
CONSTANTS_USED = frozenset(
    {
        "PRICE_VARIANCE_PCT",
        "PRICE_VARIANCE_ABS_USD",
        "LINE_AMOUNT_TOLERANCE_USD",
        "QTY_VARIANCE_PCT",
        "QTY_VARIANCE_UNITS",
    }
)


def _line(sap_params: dict[str, Any]) -> tuple[float, float, float, float, float]:
    """`(po_amount, po_qty, invoice_amount, invoice_qty, gr_qty)` as the seed posts them.

    An absent key is "same as the PO", never zero. The seed's declared defaults are
    not that, so every case sets all five (tests/test_business_operations_evals.py),
    and this default is never what grades a case.
    """
    po_amount = float(sap_params["po_amount"])
    po_qty = float(sap_params.get("po_quantity") or 1)
    return (
        po_amount,
        po_qty,
        float(sap_params.get("invoice_amount") or po_amount),
        float(sap_params.get("invoice_quantity") or po_qty),
        float(sap_params.get("gr_quantity") or po_qty),
    )


def _price_above(sap_params: dict[str, Any], constants: dict[str, Any]) -> bool:
    """price_variance 2.2 + 2.3 — the per-unit test, or the money floor over it.

    Separate from `_price` because both clauses are also the *classification* test in
    `invoice_matching.txt` STEP 2.1/2.2, which every routed case passes through.

    2.3's amount is the OVERCHARGE's dollar impact — the per-unit overcharge times the
    invoiced quantity — and NOT `|invoice_amount - po_amount|`, which the clause rules
    out by name: that difference is just as large on a partial invoice priced exactly
    as ordered, so grading on it escalated clean three-way matches (80 ordered at $500,
    56 received and 56 invoiced at $500). 2.4 makes it over-billing only, so an
    undercharge measures zero.
    """
    po_amount, po_qty, invoice_amount, invoice_qty, _ = _line(sap_params)
    po_unit = po_amount / po_qty
    per_unit = invoice_amount / invoice_qty - po_unit
    pct = abs(per_unit) / po_unit * 100
    both_limits = (
        pct > constants["PRICE_VARIANCE_PCT"]
        and abs(per_unit) > constants["PRICE_VARIANCE_ABS_USD"]
    )
    overcharge = max(per_unit, 0.0) * invoice_qty  # 2.4: buyer's favour is zero
    return both_limits or overcharge > constants["LINE_AMOUNT_TOLERANCE_USD"]


def _qty_above(sap_params: dict[str, Any], constants: dict[str, Any]) -> bool:
    """quantity_variance 2.2 + 2.3, and also STEP 2.1/2.2's classification test.

    2.3's amount is the over-invoiced quantity at the PO unit price, which is what
    removes 2.2's dependence on price: `QTY_VARIANCE_UNITS` is an unpriced count, so
    the same 10 units is $50 on a cheap part and $5,000 on an expensive one. 2.4 makes
    it over-billing only — a short delivery bills nothing extra.
    """
    po_amount, po_qty, _, invoice_qty, gr_qty = _line(sap_params)
    units = abs(invoice_qty - gr_qty)
    pct = units / gr_qty * 100
    both_limits = (
        pct > constants["QTY_VARIANCE_PCT"] and units > constants["QTY_VARIANCE_UNITS"]
    )
    over_billed = max(invoice_qty - gr_qty, 0.0) * (po_amount / po_qty)  # 2.4
    return both_limits or over_billed > constants["LINE_AMOUNT_TOLERANCE_USD"]


def _price(sap_params: dict[str, Any], constants: dict[str, Any]) -> str:
    """price_variance resolution: 3.1 against 3.2/3.3, on STEP 2's tolerance verdict."""
    # 3.2 and 3.3 both end at awaiting_human_input; only the message differs, and
    # whether a contract is referenced is not in sap_params.
    return APPROVAL_REQUIRED if _price_above(sap_params, constants) else AUTO_RELEASE


def _quantity(sap_params: dict[str, Any], constants: dict[str, Any]) -> str:
    """quantity_variance resolution: 3.1/3.2/3.3, and 1.3 for a missing GR."""
    _, _, _, invoice_qty, gr_qty = _line(sap_params)
    if sap_params.get("skip_gr") or gr_qty == 0:
        return _missing_gr(sap_params, constants)  # 1.3
    if _qty_above(sap_params, constants) and invoice_qty > gr_qty:
        return APPROVAL_REQUIRED  # 3.3 over-billing
    return AUTO_RELEASE  # 3.1 within tolerance, or 3.2 under-billing


def _missing_gr(sap_params: dict[str, Any], constants: dict[str, Any]) -> str:
    """missing_goods_receipt — every branch escalates, so no DeliveryDate is needed.

    3.1 (in transit) and 3.2 (overdue) stop at awaiting_human_input and 3.3 hands off
    to AP, and 3.4 forbids posting without human approval either way. Distinguishing
    them needs the PO's DeliveryDate, which `sap_params` does not carry — but it would
    not change the graded answer, since both labels share one status set.
    """
    return MANUAL_REVIEW


def _routed(sap_params: dict[str, Any], constants: dict[str, Any]) -> str:
    """`invoice_matching` / `three_way_match` / `ap_exception` — classify, then delegate.

    Those three all map to `invoice_matching.txt`, whose STEP 2.1-2.3 classifies the
    case into exactly one type and STEP 2.4 then follows *that* SOP's resolution logic
    in place of its own STEP 3. So the outcome is the focused SOP's outcome, and the
    only work here is the classification.

    Three more land here by their own SOP's instruction rather than by analogy, each
    because the seed cannot produce the shape its focused SOP is about:

      * `missing_purchase_order` 3.1 — a PO found after normalization MUST be
        reprocessed through three-way matching. The seed always creates the PO, so
        3.1 is the only reachable branch and 3.2 (genuinely missing → email
        procurement, stop) never fires on seeded data.
      * `duplicate_invoice` 1.3 — with no earlier invoice for that invoicing party the
        duplicate premise does not hold, so STEP 2's comparison has nothing to compare
        against and the case is reprocessed. The seed posts exactly one invoice.
      * `uom_mismatch` 1.4 — identical units need no conversion, so this is not the
        missing factor 3.3 escalates on. The seed sets no UoM at all.

    Each of those three needed a clause added to say so; before that they raised here
    rather than being guessed at, because the terminal state a guess would assert was a
    rule no SOP stated. A case whose SAP data really did lack a PO, carry a second
    invoice or mix units would need an input this corpus cannot express.

    Which is where the one real ambiguity lives: STEP 2.1 demands exactly one type and
    states no precedence, so a case that breaches both the price and the quantity
    tolerance has two valid classifications. Where the two resolve the same way the
    precedence does not matter and the answer is graded; where they disagree this
    raises rather than picking one — the fix belongs in the SOP.
    """
    if sap_params.get("skip_gr"):
        return _missing_gr(sap_params, constants)
    above = {
        "price_variance": _price_above(sap_params, constants),
        "quantity_variance": _qty_above(sap_params, constants),
    }
    breached = [name for name, flag in above.items() if flag]
    if len(breached) > 1:
        # Both classifications are valid, so the missing precedence rule only matters
        # if they disagree. 45 ordered at $200 and 99 invoiced for the same money
        # breaches both, and both say approval_required.
        verdicts = {_BY_TYPE[name](sap_params, constants) for name in breached}
        if len(verdicts) > 1:
            raise NotImplementedError(
                f"undecidable: STEP 2.1 of invoice_matching.txt requires exactly one "
                f"classification and states no precedence, and this case breaches "
                f"{breached}, which resolve to {sorted(verdicts)}. Give the SOP a "
                f"precedence rule."
            )
        return verdicts.pop()
    if not breached:
        # Neither tolerance breached, so both STEP 3.1 and 3.2 read "within tolerance →
        # auto-accept, post invoice". A clean three-way match, whatever the case was
        # filed as. The remaining STEP 2.1 types are all ruled out by the seeded data:
        # there is always a PO, always exactly one invoice, and never a UoM.
        return AUTO_RELEASE
    return _BY_TYPE[breached[0]](sap_params, constants)


_BY_TYPE = {
    "price_variance": _price,
    "quantity_variance": _quantity,
    "missing_goods_receipt": _missing_gr,
    "invoice_matching": _routed,
    "three_way_match": _routed,
    "ap_exception": _routed,
    # These three send the case back through three-way matching by their own SOP's
    # premise-failure clause, which is the only branch seeded data can reach — 3.1,
    # 1.3 and 1.4 respectively. See _routed.
    "missing_purchase_order": _routed,
    "duplicate_invoice": _routed,
    "uom_mismatch": _routed,
}


def expected_outcome(
    process_type: str, sap_params: dict[str, Any], constants: dict[str, Any]
) -> str:
    """The outcome the SOP requires for this seeded case.

    Returns a key of `OUTCOME_TO_STATUS`. Raises `NotImplementedError`
    for a `process_type` no SOP arithmetic is encoded for, and for the one case a SOP
    leaves genuinely undecided — loudly, the way `check_expectations` already fails on
    an unrecognised outcome string.
    """
    try:
        rule = _BY_TYPE[process_type]
    except KeyError:
        raise NotImplementedError(
            f"no SOP arithmetic is encoded for process_type {process_type!r} — "
            f"modelled: {sorted(_BY_TYPE)}"
        ) from None
    return rule(sap_params, constants)


# ── The same arithmetic, rule by rule, for a person ──
#
# The test data page shows why the SOP decides what it does. These checks restate the
# tests `_price_above`, `_qty_above` and `_missing_gr` make, and `explain` takes its
# outcome from `expected_outcome`, so the page can never show a different answer from
# the one `evals-run` grades; a test holds the checks to that outcome on every case.

#: The classifying SOP for each test, as `invoice_matching.txt` STEP 2 names them.
_EXCEPTION_OF = {
    "price": "price_variance",
    "quantity": "quantity_variance",
    "receipt": "missing_goods_receipt",
}


def _money(x: float) -> str:
    return f"{x:,.2f} USD"


def _checks(sap_params: dict[str, Any], constants: dict[str, Any]) -> dict:
    """`{"receipt" | "price" | "quantity": [check, ...]}`, each check a dict the page
    renders as one line: `rule`, `label`, `detail`, `limit` and `over`."""
    po_amount, po_qty, invoice_amount, invoice_qty, gr_qty = _line(sap_params)
    missing = bool(sap_params.get("skip_gr")) or gr_qty == 0
    po_unit = po_amount / po_qty
    per_unit = invoice_amount / invoice_qty - po_unit
    price_pct = abs(per_unit) / po_unit * 100
    overcharge = max(per_unit, 0.0) * invoice_qty
    units = abs(invoice_qty - gr_qty)
    qty_pct = units / gr_qty * 100 if gr_qty else 0.0
    over_billed = max(invoice_qty - gr_qty, 0.0) * po_unit
    line = constants["LINE_AMOUNT_TOLERANCE_USD"]
    out = {
        "receipt": [
            {
                "rule": "missing_goods_receipt 1.3",
                "label": "Goods receipt posted",
                "detail": "None" if missing else f"{gr_qty:g} received",
                "limit": "A receipt for the invoiced line",
                "over": missing,
            }
        ],
        "price": [
            {
                "rule": "price_variance 2.2",
                "label": "Per unit over the PO price",
                "detail": f"{price_pct:.2f} % and {_money(abs(per_unit))} per unit",
                "limit": (
                    f"Both over {constants['PRICE_VARIANCE_PCT']:g} % and "
                    f"{_money(constants['PRICE_VARIANCE_ABS_USD'])}"
                ),
                "over": price_pct > constants["PRICE_VARIANCE_PCT"]
                and abs(per_unit) > constants["PRICE_VARIANCE_ABS_USD"],
            },
            {
                "rule": "price_variance 2.3",
                "label": "Overcharged on the line",
                "detail": _money(overcharge),
                "limit": f"Over {_money(line)}",
                "over": overcharge > line,
            },
        ],
    }
    if not missing:
        out["quantity"] = [
            {
                "rule": "quantity_variance 2.2",
                "label": "Invoiced against received",
                "detail": f"{units:g} units, {qty_pct:.2f} %",
                "limit": (
                    f"Both over {constants['QTY_VARIANCE_PCT']:g} % and "
                    f"{constants['QTY_VARIANCE_UNITS']:g} units"
                ),
                "over": qty_pct > constants["QTY_VARIANCE_PCT"]
                and units > constants["QTY_VARIANCE_UNITS"],
            },
            {
                "rule": "quantity_variance 2.3",
                "label": "Over-billed at the PO price",
                "detail": _money(over_billed),
                "limit": f"Over {_money(line)}",
                "over": over_billed > line,
            },
        ]
    return out


def explain(
    process_type: str, sap_params: dict[str, Any], constants: dict[str, Any]
) -> dict:
    """`{outcome, exception_type, checks}`: what the SOP decides and why, rule by rule.

    `outcome` is `expected_outcome`'s, or None with `undecided` saying why when that
    raises. `exception_type` is the type the classifying step lands on, None for a
    clean match or when two types are breached. `checks` are every test the
    classification makes, in the order the SOP makes them.
    """
    try:
        outcome: str | None = expected_outcome(process_type, sap_params, constants)
        undecided = None
    except NotImplementedError as error:
        outcome, undecided = None, str(error)
    groups = _checks(sap_params, constants)
    if groups["receipt"][0]["over"]:
        found = ["receipt"]
    else:
        found = [g for g in ("price", "quantity") if any(c["over"] for c in groups[g])]
    if process_type in _EXCEPTION_OF.values():
        exception_type: str | None = process_type
    else:
        exception_type = _EXCEPTION_OF[found[0]] if len(found) == 1 else None
    return {
        "outcome": outcome,
        "exception_type": exception_type,
        "checks": [c for group in groups.values() for c in group],
        **({"undecided": undecided} if undecided else {}),
    }
