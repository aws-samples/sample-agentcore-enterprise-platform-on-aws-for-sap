# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""What a seeded case should end as: the SOP's answer, then this deployment's limits.

The test data page shows it for each preset and for the values a person types, and the
seeder records it on what it posts, so the case can later be graded against it. Two
layers, as the agent meets them:

1. **The SOP**, from the pack's checker (`oracle.py`, linked from `evals/`): a second
   copy of the SOP arithmetic, rule by rule. Only `finance_ops` has one, so any other
   pack gets no expectation, never a guess.
2. **The limits**, this stack's effective `AUTO_WRITE_LIMIT_USD` and
   `DUAL_APPROVAL_LIMIT_USD` (the pack's, lowered by `PUT /packs/{pack_id}/controls`). The SOP knows
   nothing of them; the run checks do. A release the SOP allows over the auto-write limit
   waits for an approver, and one over the dual-approval limit for two.

The amount and currency are the case's, as the seed declares them (`seed.case`), so
the limit is checked against the number the run checks will read off the case.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

import config_overrides
import oracle
import seed_steps

#: The pack checkers there are, by pack id.
CHECKERS = {oracle.PACK_ID: oracle}

AUTO = oracle.AUTO_RELEASE
APPROVAL = oracle.APPROVAL_REQUIRED
MANUAL = oracle.MANUAL_REVIEW
LABELS = {
    AUTO: "Released, no person",
    APPROVAL: "Needs approval",
    MANUAL: "Manual review",
}
AUTO_LIMIT = "AUTO_WRITE_LIMIT_USD"
DUAL_LIMIT = "DUAL_APPROVAL_LIMIT_USD"
LIMIT_CURRENCY = "USD"
_LIMIT_LABELS = {AUTO_LIMIT: "Auto-write limit", DUAL_LIMIT: "Two approvers above"}


def has_checker(pack_id: str) -> bool:
    return pack_id in CHECKERS


def _rendered(template, values: dict):
    """A `seed.case` attribute rendered from the inputs alone, or None when it names
    a document SAP has not created yet."""
    try:
        return seed_steps.render(template, values)
    except (seed_steps.TemplateError, TypeError, ValueError):
        return None


def _number(value) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


#: A case status the agent has settled on, in the expectation's terms.
_DECIDED = {
    "sap_updated": AUTO,
    "complete": AUTO,
    "awaiting_human_input": APPROVAL,
    "manual_review_required": MANUAL,
}


def outcome_of(status: str | None) -> str | None:
    """What a case's status says the agent decided, in the expectation's terms, or None
    while it has decided nothing yet."""
    return _DECIDED.get(status or "")


def result(expected: dict | None, status: str | None) -> str | None:
    """`as_expected`, `differs`, or None while there is nothing to compare: graded the
    way `evals-run` grades (`OUTCOME_TO_STATUS`)."""
    want = (expected or {}).get("outcome")
    if not want or outcome_of(status) is None:
        return None
    return "as_expected" if status in oracle.OUTCOME_TO_STATUS[want] else "differs"


def expect(pack_id: str, entry: dict, values: dict, overrides: dict) -> dict | None:
    """The `Expected` the contract describes, or None when the pack has no checker.

    `entry` is the pack as the registry publishes it (`{"pack", "source"}`); the case
    the seed makes starts as `source.process_type.default`, the triage type, as the
    poller files it. `values` are the seed's inputs, already checked
    (`seed_steps.inputs`); `overrides` are this stack's limit overrides
    (`config_overrides.constant_overrides`).
    """
    checker = CHECKERS.get(pack_id)
    if checker is None:
        return None
    document = entry.get("pack") or {}
    source = entry.get("source") or document.get("source") or {}
    declared = document.get("constants") or {}
    constants = config_overrides.resolved_constants(declared, overrides)
    process_type = (source.get("process_type") or {}).get("default") or ""
    sop = checker.explain(process_type, values, constants)
    attributes = ((document.get("seed") or {}).get("case") or {}).get(
        "attributes"
    ) or {}
    amount = _number(_rendered(attributes.get("amount"), values))
    currency = _rendered(attributes.get("currency"), values)
    # In doubt the run checks hold the write, so the expectation does too.
    comparable = amount is not None and currency == LIMIT_CURRENCY

    def over(symbol: str) -> bool:
        limit = _number(constants.get(symbol))
        return limit is not None and (not comparable or amount > limit)

    checks = [
        {
            "symbol": symbol,
            "label": _LIMIT_LABELS[symbol],
            "amount": float(amount) if amount is not None else None,
            "currency": currency,
            "limit": constants[symbol],
            "over": over(symbol),
        }
        for symbol in (AUTO_LIMIT, DUAL_LIMIT)
        if symbol in constants
    ]
    outcome = sop["outcome"]
    if outcome == AUTO and over(AUTO_LIMIT):
        outcome = APPROVAL
    approvers = (2 if over(DUAL_LIMIT) else 1) if outcome == APPROVAL else 0
    return {
        "outcome": outcome,
        "label": LABELS.get(outcome),
        "approvers": approvers,
        "exception_type": sop["exception_type"],
        "sop": {
            "outcome": sop["outcome"],
            "checks": sop["checks"],
            **({"undecided": sop["undecided"]} if "undecided" in sop else {}),
        },
        "limits": {"outcome": outcome, "checks": checks},
        "checker": "evals/oracle.py",
    }
