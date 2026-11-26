# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The write guards a pack may enable, and the rule constants each one reads.

A rule a guard reads is a control as well as a SOP value: changing it changes what the
agent may post without an approval. `GET /packs/{pack_id}/rules` and `ops pack status`
say so per symbol
(`guarded_by`). `write_guards.py` enforces the guards; this is the map, kept apart so the
API layer can read it without the agent package (it links this file). Stdlib only, no
package-relative import, for the same reason.

`agent/tests/test_write_guards.py` fails when a `_const` call in `write_guards.py` names a
symbol this map lacks, or the map names one no call reads.
"""

from __future__ import annotations

GUARDS = ("po_reference", "no_duplicate", "gr_coverage", "within_tolerance_or_approved")

#: Every constant each guard reads (`write_guards._const`). `po_reference` and
#: `gr_coverage` read none: SAP alone answers them.
GUARD_SYMBOLS: dict[str, frozenset[str]] = {
    "po_reference": frozenset(),
    "no_duplicate": frozenset({"DUPLICATE_MATCH_WINDOW_USD"}),
    "gr_coverage": frozenset(),
    "within_tolerance_or_approved": frozenset(
        {
            "PRICE_VARIANCE_PCT",
            "PRICE_VARIANCE_ABS_USD",
            "QTY_VARIANCE_PCT",
            "QTY_VARIANCE_UNITS",
            "LINE_AMOUNT_TOLERANCE_USD",
        }
    ),
}


def guarded_by(symbol: str, enabled) -> list[str]:
    """The guards in `enabled` (a pack's `write_guards`) that read `symbol`, sorted."""
    return sorted(
        g for g in set(enabled or ()) if symbol in GUARD_SYMBOLS.get(g, frozenset())
    )


def annotate(rows: dict, enabled) -> dict:
    """`rows` (`{SYMBOL: {value, kind, source}}`, `config_overrides.effective_*`) with
    `guarded_by` on every constant row. A contact row is left as it is."""
    for symbol, row in rows.items():
        if isinstance(row, dict) and row.get("kind") != "contact":
            row["guarded_by"] = guarded_by(symbol, enabled)
    return rows
