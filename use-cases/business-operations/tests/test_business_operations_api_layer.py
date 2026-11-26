# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""business-operations API layer: the case id codec, pagination, the live_run claim.

The codec accepts the derived form only; the earlier system's legacy
`{document}-{item}` form has no stored case to keep resolving here.
"""

from __future__ import annotations

import re
import sys
import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest
from botocore.exceptions import ClientError

REPO = Path(__file__).resolve().parents[3]
UC = REPO / "use-cases" / "business-operations"
sys.path.insert(0, str(UC / "api" / "layer" / "python"))

from case_key import (
    DERIVED_CASE_ID_PATTERN,
    RUNTIME_SESSION_MIN_LENGTH,
    CaseKeyError,
    canonical_key,
    derive_case_id,
    normalize_case_id,
    to_case_key,
    to_runtime_session_id,
    try_normalize_case_id,
)
from ddb_paginate import paginate
from live_run_claim import (
    STALE_SECONDS,
    AlreadyRunning,
    CaseSettled,
    MissingCase,
    claim_live_run,
    release_live_run,
)

AP_KEY = {"SupplierInvoice": "5100001976", "FiscalYear": "2026"}
CASE = derive_case_id("finance_ops", "ap", AP_KEY)


# ── case_key ──


def test_minting_is_deterministic_and_ignores_field_order():
    reordered = {"FiscalYear": "2026", "SupplierInvoice": "5100001976"}
    assert derive_case_id("finance_ops", "ap", reordered) == CASE
    assert derive_case_id("finance_ops", "ap", AP_KEY) == CASE


@pytest.mark.parametrize(
    "key",
    [
        {"Material": "ABC-123", "Plant": "1000"},
        {"WBSElement": "P/1000/2.4", "Plant": "1000"},
        {"CostCenter": "0000001000 "},
        {"A": "1", "B": "2", "C": "3", "D": "4", "E": "5"},
        {"Note": "réf 100 %"},
    ],
)
def test_any_key_shape_yields_an_id_that_needs_no_escaping(key):
    case_id = derive_case_id("materials", "mat", key)
    assert re.match(DERIVED_CASE_ID_PATTERN, case_id)
    assert not re.search(r"[^a-z0-9-]", case_id)
    assert urllib.parse.quote(case_id, safe="") == case_id


def test_different_keys_and_different_packs_get_different_ids():
    assert derive_case_id("finance_ops", "ap", {**AP_KEY, "FiscalYear": "2027"}) != CASE
    # Two packs sharing a short id must not collide.
    assert derive_case_id("finance_ar", "ap", AP_KEY) != CASE


def test_an_id_fits_every_consumer_that_bounds_its_length():
    assert 29 <= len(CASE) <= 35
    assert re.match(r"^[A-Za-z0-9-]{1,50}$", CASE)  # an AppSync channel segment
    assert re.fullmatch(r"[A-Za-z0-9\-_]{1,128}", CASE)  # an SQS MessageGroupId


@pytest.mark.parametrize(
    "pack,short,key",
    [
        ("finance_ops", "ap", {}),
        ("finance_ops", "ap", {"SupplierInvoice": ""}),
        ("finance_ops", "ap", {"SupplierInvoice": "  "}),
        ("", "ap", AP_KEY),
        ("finance_ops", "", AP_KEY),
        ("finance_ops", "AP", AP_KEY),
        ("finance_ops", "a", AP_KEY),
        ("finance_ops", "accountspayable", AP_KEY),
        ("finance_ops", "ap", {"Bad Field": "1"}),
        ("finance_ops", "ap", {"X": "a\x1fb"}),
    ],
)
def test_minting_refuses_a_key_it_cannot_identify(pack, short, key):
    with pytest.raises(CaseKeyError):
        derive_case_id(pack, short, key)


def test_canonical_key_is_the_digest_input_and_is_sorted():
    assert canonical_key("finance_ops", AP_KEY) == (
        "finance_ops\x1fFiscalYear=2026\x1fSupplierInvoice=5100001976"
    )


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        None,
        "5100001976-2026",  # the earlier system's legacy form
        "5100001976#2026",
        CASE + "x",
        CASE.upper(),
        f"{CASE} OR 1=1",
        f"../../{CASE}",
    ],
)
def test_normalize_accepts_only_the_derived_form(bad):
    with pytest.raises(CaseKeyError):
        normalize_case_id(bad)
    assert try_normalize_case_id(bad) is None


def test_a_minted_id_normalizes_and_keys_the_table():
    assert normalize_case_id(f"  {CASE} ") == CASE
    assert to_case_key(CASE) == {"case_id": CASE}
    with pytest.raises(CaseKeyError):
        to_case_key("not a case")


def test_session_id_is_stable_distinct_and_clears_the_runtime_floor():
    session = to_runtime_session_id(CASE)
    assert session == to_runtime_session_id(CASE)
    assert len(session) >= RUNTIME_SESSION_MIN_LENGTH and CASE in session
    assert not re.search(r"[^a-zA-Z0-9\-_/:]", session)
    other = derive_case_id("finance_ops", "ap", {**AP_KEY, "FiscalYear": "2027"})
    assert to_runtime_session_id(other) != session
    with pytest.raises(CaseKeyError):
        to_runtime_session_id("not a case")


# ── ddb_paginate ──


class _Pages:
    def __init__(self, pages):
        self.pages, self.calls = list(pages), []

    def scan(self, **kwargs):
        self.calls.append(dict(kwargs))
        return self.pages.pop(0)


def test_every_page_is_read_and_the_filter_travels_with_the_key():
    table = _Pages(
        [
            {"Items": [{"id": "a"}], "LastEvaluatedKey": {"id": "a"}},
            {"Items": [{"id": "b"}]},
        ]
    )
    assert paginate(table.scan, FilterExpression="f") == [{"id": "a"}, {"id": "b"}]
    assert table.calls == [
        {"FilterExpression": "f"},
        {"FilterExpression": "f", "ExclusiveStartKey": {"id": "a"}},
    ]


def test_one_page_is_one_call():
    table = _Pages([{"Items": [{"id": "a"}]}])
    assert paginate(table.scan) == [{"id": "a"}]
    assert len(table.calls) == 1


# ── live_run_claim ──

KEY = {"case_id": CASE}


def _conditional_failure(item: dict | None) -> ClientError:
    response = {"Error": {"Code": "ConditionalCheckFailedException", "Message": "no"}}
    if item is not None:
        response["Item"] = item
    return ClientError(response, "UpdateItem")


def test_claim_writes_the_shape_the_agent_later_overwrites():
    table = mock.Mock()
    claim_live_run(table, KEY, run_id="run-1", trigger="manual", prompt="why held?")
    kwargs = table.update_item.call_args.kwargs
    live = kwargs["ExpressionAttributeValues"][":live"]
    assert kwargs["Key"] == KEY
    assert (live["run_id"], live["trigger"], live["prompt"]) == (
        "run-1",
        "manual",
        "why held?",
    )
    assert live["segments"] == [] and live["started_at"] == live["updated_at"]


def test_claim_condition_lets_a_stale_run_be_taken_over():
    table = mock.Mock()
    claim_live_run(table, KEY, run_id="run-1", trigger="manual", prompt="p")
    kwargs = table.update_item.call_args.kwargs
    assert "attribute_exists(case_id)" in kwargs["ConditionExpression"]
    assert "attribute_not_exists(live_run)" in kwargs["ConditionExpression"]
    assert "live_run.updated_at < :stale" in kwargs["ConditionExpression"]
    stale = datetime.fromisoformat(kwargs["ExpressionAttributeValues"][":stale"])
    expected = datetime.now(UTC) - timedelta(seconds=STALE_SECONDS)
    assert abs((stale - expected).total_seconds()) < 5


def test_a_claim_refuses_a_case_in_a_status_it_is_told_to_refuse():
    table = mock.Mock()
    claim_live_run(table, KEY, run_id="run-1", trigger="manual", prompt="p")
    assert "#st" not in table.update_item.call_args.kwargs["ConditionExpression"]
    refuse = frozenset({"complete", "sap_updated"})
    claim_live_run(
        table, KEY, run_id="run-1", trigger="manual", prompt="p", refuse=refuse
    )
    kwargs = table.update_item.call_args.kwargs
    assert "NOT (#st IN (:refuse0, :refuse1))" in kwargs["ConditionExpression"]
    assert kwargs["ExpressionAttributeNames"] == {"#st": "status"}
    values = kwargs["ExpressionAttributeValues"]
    assert {values[":refuse0"], values[":refuse1"]} == refuse
    # A refused status wins over a run in flight: the case is not worth following.
    table.update_item.side_effect = _conditional_failure(
        {
            "status": {"S": "complete"},
            "live_run": {"M": {"run_id": {"S": "run-0"}}},
        }
    )
    with pytest.raises(CaseSettled) as raised:
        claim_live_run(
            table, KEY, run_id="run-1", trigger="manual", prompt="p", refuse=refuse
        )
    assert raised.value.status == "complete"


def test_a_live_run_names_its_holder_and_a_missing_case_is_told_apart():
    table = mock.Mock()
    table.update_item.side_effect = _conditional_failure(
        {"case_id": {"S": CASE}, "live_run": {"M": {"run_id": {"S": "run-0"}}}}
    )
    with pytest.raises(AlreadyRunning) as raised:
        claim_live_run(table, KEY, run_id="run-1", trigger="manual", prompt="p")
    assert raised.value.run_id == "run-0"

    table.update_item.side_effect = _conditional_failure(None)
    with pytest.raises(MissingCase):
        claim_live_run(table, KEY, run_id="run-1", trigger="manual", prompt="p")


def test_release_only_drops_the_run_that_still_holds_the_case():
    table = mock.Mock()
    assert release_live_run(table, KEY, run_id="run-1") is True
    kwargs = table.update_item.call_args.kwargs
    assert kwargs["UpdateExpression"] == "REMOVE live_run"
    assert kwargs["ConditionExpression"] == "live_run.run_id = :run_id"
    table.update_item.side_effect = _conditional_failure(None)
    assert release_live_run(table, KEY, run_id="run-1") is False


# ── POST /cases/enqueue's request model agrees with the codec ──


def test_the_enqueue_model_is_the_codecs_pattern_and_refuses_breakouts():
    from test_business_operations_api import _of, _stack

    (model,) = _of(_stack("business-operations.yaml"), "AWS::ApiGateway::Model")
    pattern = model["Properties"]["Schema"]["properties"]["case_id"]["pattern"]
    assert pattern == DERIVED_CASE_ID_PATTERN
    assert pattern.startswith("^") and pattern.endswith("$")
    assert re.match(pattern, CASE)
    for bad in [
        "5100001922-2026",
        CASE + '","trigger":"poller',
        "x" * 129,
        "",
    ]:
        assert not re.match(pattern, bad), bad
