# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""business-operations agent: the /config overrides read-through and the prompt's substitution.

The `load_sop` Gateway tool (sop_tools/) reads this same file through a symlink, so
the prompt and a mid-run reload cannot state different tolerances.
"""

import importlib
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

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


def _skill_router():
    (sr,) = _agent_utils("skill_router")
    return sr


@pytest.fixture
def overrides(monkeypatch):
    monkeypatch.setenv("CONFIG_TABLE", "cfg-table")
    with patch("boto3.resource"):
        (mod,) = _agent_utils("config_overrides")
    mod._table = MagicMock()
    return mod


def _serve(overrides, items):
    overrides._table.query.return_value = {"Items": items}


def test_contact_overrides_are_keyed_to_match_the_placeholder(overrides):
    # The table stores the `contacts` key ("ap_team"); the SOP writes
    # {{CONTACT_AP_TEAM}}. A mismatch here leaves the placeholder verbatim.
    _serve(overrides, [{"config_key": "ap_team", "value": "new@example.com"}])
    assert overrides.contact_overrides() == {"CONTACT_AP_TEAM": "new@example.com"}


def test_constant_overrides_query_the_skills_own_namespace(overrides):
    _serve(overrides, [{"config_key": "AUTO_WRITE_LIMIT_USD", "value": Decimal(7)}])
    assert overrides.constant_overrides("finance_ops") == {
        "AUTO_WRITE_LIMIT_USD": Decimal(7)
    }
    condition = overrides._table.query.call_args.kwargs["KeyConditionExpression"]
    assert "constant#finance_ops" in str(condition.get_expression()["values"])


def test_a_stale_rule_override_row_is_ignored_and_logged_once(overrides, caplog):
    # A row an older release wrote for a rule constant: the pack's value is the only
    # one, so the row reads as absent, and says so once rather than on every case.
    _serve(
        overrides,
        [
            {"config_key": "QTY_VARIANCE_PCT", "value": Decimal(9)},
            {"config_key": "DUAL_APPROVAL_LIMIT_USD", "value": Decimal(10)},
        ],
    )
    overrides._ignored.clear()
    with caplog.at_level("WARNING"):
        for _ in range(3):
            assert overrides.constant_overrides("finance_ops") == {
                "DUAL_APPROVAL_LIMIT_USD": Decimal(10)
            }
    assert [r.message.count("QTY_VARIANCE_PCT") for r in caplog.records] == [1]
    declared = {"QTY_VARIANCE_PCT": 5, "DUAL_APPROVAL_LIMIT_USD": 100}
    text = overrides.substitute(
        "{{QTY_VARIANCE_PCT}} {{DUAL_APPROVAL_LIMIT_USD}}", {}, declared, "finance_ops"
    )
    assert text == "5 10"


def test_the_limit_symbols_are_the_ones_the_api_and_the_run_checks_hold():
    import pack_tiers
    from business_operations_check import run_checks

    (mod,) = _agent_utils("config_overrides")
    assert mod.LIMIT_SYMBOLS == pack_tiers.LIMIT_SYMBOLS
    assert mod.LIMIT_SYMBOLS == {run_checks.LIMIT_SYMBOL, run_checks.DUAL_LIMIT_SYMBOL}


@pytest.mark.parametrize(
    "override, value, source",
    [
        (None, 100, "pack v2"),
        (Decimal(40), 40, "stack override (lowered from 100)"),
        # Above the ceiling: a row older than a version that lowered it never widens it.
        (Decimal(250), 100, "pack v2"),
        (Decimal(100), 100, "pack v2"),
    ],
)
def test_a_limit_override_applies_only_below_the_packs_ceiling(
    overrides, override, value, source
):
    rows = {"AUTO_WRITE_LIMIT_USD": override} if override is not None else {}
    (row,) = overrides.effective_constants(
        {"AUTO_WRITE_LIMIT_USD": 100}, rows, "pack v2"
    ).values()
    assert row == {"value": value, "kind": "limit", "source": source}
    assert overrides.resolved_constants({"AUTO_WRITE_LIMIT_USD": 100}, rows) == {
        "AUTO_WRITE_LIMIT_USD": value
    }


def test_no_config_table_means_no_overrides_not_a_crash(overrides, monkeypatch):
    # Local dev and any deployment that predates the config table. The deployed
    # defaults must still resolve.
    monkeypatch.delenv("CONFIG_TABLE")
    overrides._table = None
    assert overrides.contact_overrides() == {}
    assert overrides.constant_overrides("finance_ops") == {}


def test_a_failed_read_falls_back_to_the_deployed_default(overrides):
    # Throttle, permission change, table deleted. Returning {} means the SOP still
    # names a real threshold; raising would fail the case, and returning a blank
    # would have the agent compare against nothing.
    overrides._table.query.side_effect = RuntimeError("throttled")
    assert overrides.constant_overrides("finance_ops") == {}


def test_rows_without_a_value_are_skipped(overrides):
    _serve(overrides, [{"config_key": "QTY_VARIANCE_PCT"}])
    assert overrides.constant_overrides("finance_ops") == {}


def test_an_empty_pack_id_does_not_query_the_contact_namespace(overrides):
    # "constant#" + "" would be a namespace of its own, but a bug that passed the
    # empty string should read nothing rather than something.
    assert overrides.constant_overrides("") == {}
    overrides._table.query.assert_not_called()


# The reader is only useful if both substitution paths consult it. These two pin
# the seam — a plausible refactor that drops the merge in either place would leave
# every unit above passing while operator edits silently did nothing.


def test_a_lowered_limit_wins_over_the_packs_value_in_the_prompt(monkeypatch):
    sr = _skill_router()
    monkeypatch.setattr(sr.config_overrides, "_contacts", {})
    monkeypatch.setattr(
        sr.config_overrides, "constant_overrides", lambda _: {"AUTO_WRITE_LIMIT_USD": 9}
    )
    config = {"pack_id": "finance_ops", "constants": {"AUTO_WRITE_LIMIT_USD": 50}}
    assert sr._substitute("up to {{AUTO_WRITE_LIMIT_USD}}", config) == "up to 9"


def test_an_override_for_an_undeclared_symbol_cannot_introduce_one(monkeypatch):
    sr = _skill_router()
    monkeypatch.setattr(sr.config_overrides, "_contacts", {})
    monkeypatch.setattr(
        sr.config_overrides, "constant_overrides", lambda _: {"SMUGGLED_LIMIT": 1}
    )
    config = {"pack_id": "finance_ops", "constants": {"QTY_VARIANCE_PCT": 5}}
    text = "cap {{SMUGGLED_LIMIT}}"
    # Verbatim, exactly as for any undeclared symbol: a value nothing declared is
    # a typo, and blanking it would hide the typo behind a working-looking prompt.
    assert sr._substitute(text, config) == text


def test_a_contact_declared_with_no_value_stays_a_visible_placeholder(monkeypatch):
    sr = _skill_router()
    # A contact key declared with no address arrives with a null. Rendering it would
    # put the literal string "None" in front of the model as an address to email.
    monkeypatch.setattr(sr.config_overrides, "_contacts", {"CONTACT_PROCUREMENT": None})
    monkeypatch.setattr(sr.config_overrides, "contact_overrides", dict)
    text = "escalate to {{CONTACT_PROCUREMENT}}"
    assert sr._substitute(text, {}) == text


def test_a_threshold_of_zero_still_substitutes(monkeypatch):
    sr = _skill_router()
    # The empty-value rule is not a falsiness check: zero tolerance is a real
    # tolerance, and leaving {{...}} here would read as a config defect.
    monkeypatch.setattr(sr.config_overrides, "_contacts", {})
    monkeypatch.setattr(sr.config_overrides, "constant_overrides", lambda _: {})
    config = {"pack_id": "finance_ops", "constants": {"PRICE_VARIANCE_PCT": 0}}
    assert sr._substitute("above {{PRICE_VARIANCE_PCT}}%", config) == "above 0%"


_PARITY_TEXT = (
    "Above {{QTY_VARIANCE_PCT}}% notify {{CONTACT_AP_TEAM}} and "
    "{{CONTACT_TIER2_OWNER}}; {{UNDECLARED}} stays."
)


def test_the_prompt_resolves_constants_and_digit_bearing_contacts(monkeypatch):
    # The earlier system's two copies of the placeholder rules once disagreed about a
    # contact key with a digit in it; the shared rules must resolve it.
    sr = _skill_router()
    monkeypatch.setattr(
        sr.config_overrides,
        "_contacts",
        {"CONTACT_AP_TEAM": "ap@example.com", "CONTACT_TIER2_OWNER": "t2@example.com"},
    )
    monkeypatch.setattr(sr.config_overrides, "contact_overrides", dict)
    monkeypatch.setattr(sr.config_overrides, "constant_overrides", lambda _: {})
    text = sr._substitute(
        _PARITY_TEXT, {"pack_id": "finance_ops", "constants": {"QTY_VARIANCE_PCT": 5}}
    )
    assert "5%" in text
    assert "t2@example.com" in text
    assert "{{UNDECLARED}}" in text


def test_the_router_keeps_no_placeholder_pattern_of_its_own():
    # A re-added local regex is how the earlier system's two copies drifted the first time.
    source = (AGENT / "utils" / "skill_router.py").read_text(encoding="utf-8")
    assert "config_overrides.substitute" in source
    assert r"\{\{" not in source
