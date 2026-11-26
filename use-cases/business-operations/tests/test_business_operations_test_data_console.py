# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""business-operations test data from the console: the expectation, the route, the record.

The checker's rule-by-rule account must give the answer `evals-run` grades with; the
limits must hold what the run checks hold; the route must post nothing itself and hand each
copy to the seeder once; and the case's `test_data` mark must come from the seeder's
own record, never from whoever built the case item.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from decimal import Decimal
from unittest import mock

import pytest
from test_business_operations_api import (
    ADMINS,
    CONSOLE,
    LAYER,
    SAP_SYSTEM,
    UC,
    USERS,
    _event,
    _of,
    _stack,
)

for path in (LAYER, UC / "evals"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import oracle
import seed_steps
import test_data_expectation as te
import test_data_records

PACK = json.loads((UC / "packs/finance_ops/pack.json").read_text())
ENTRY = {
    "pack": {k: v for k, v in PACK.items() if k not in ("source", "fields")},
    "source": PACK["source"],
}
EVALS = json.loads((UC / "packs/finance_ops/evals.json").read_text())
TODAY = "2026-08-31"


def _values(given: dict) -> dict:
    return seed_steps.inputs(PACK["seed"], given, lambda: TODAY)


# ── the checker, rule by rule ──


@pytest.mark.parametrize(
    "case",
    [c for c in EVALS if (c.get("seed") or {}).get("inputs")],
    ids=lambda c: c["test_id"],
)
def test_the_rule_by_rule_account_gives_the_graded_answer(case):
    constants = PACK["constants"]
    inputs = case["seed"]["inputs"]
    explained = oracle.explain(case["process_type"], inputs, constants)
    try:
        graded = oracle.expected_outcome(case["process_type"], inputs, constants)
    except NotImplementedError:
        graded = None
    assert explained["outcome"] == graded
    # The checks are the same tests the classification makes.
    by_rule = {c["rule"]: c["over"] for c in explained["checks"]}
    price = by_rule["price_variance 2.2"] or by_rule["price_variance 2.3"]
    assert price == oracle._price_above(inputs, constants)
    if "quantity_variance 2.2" in by_rule:
        qty = by_rule["quantity_variance 2.2"] or by_rule["quantity_variance 2.3"]
        assert qty == oracle._qty_above(inputs, constants)


def test_a_clean_match_names_no_exception_and_a_price_breach_names_it():
    constants = PACK["constants"]
    clean = _values({"po_amount": 25000, "invoice_amount": 25000})
    assert (
        oracle.explain("invoice_matching", clean, constants)["exception_type"] is None
    )
    over = _values({"po_amount": 50000, "invoice_amount": 52500})
    explained = oracle.explain("invoice_matching", over, constants)
    assert explained["exception_type"] == "price_variance"
    assert explained["outcome"] == oracle.APPROVAL_REQUIRED


# ── the expectation: the SOP, then the limits ──


def test_every_shipped_preset_has_an_expectation_the_page_can_label():
    for preset in PACK["seed"]["presets"]:
        expected = te.expect("finance_ops", ENTRY, _values(preset["values"]), {})
        assert expected["label"] in te.LABELS.values(), preset["label"]
        assert expected["checker"] == "evals/oracle.py"


def test_a_release_the_sop_allows_over_the_lowered_limit_waits_for_an_approver():
    # Preview frame 2: 50,040 USD is within the price tolerances, but over a limit this
    # stack lowered to 50,000.
    values = _values({"po_amount": 50000, "invoice_amount": 50040})
    as_shipped = te.expect("finance_ops", ENTRY, values, {})
    assert as_shipped["outcome"] == te.AUTO and as_shipped["approvers"] == 0
    lowered = te.expect(
        "finance_ops", ENTRY, values, {"AUTO_WRITE_LIMIT_USD": Decimal(50000)}
    )
    assert lowered["sop"]["outcome"] == te.AUTO
    assert (lowered["outcome"], lowered["label"], lowered["approvers"]) == (
        te.APPROVAL,
        "Needs approval",
        1,
    )
    auto = next(c for c in lowered["limits"]["checks"] if c["symbol"] == te.AUTO_LIMIT)
    assert (auto["amount"], auto["limit"], auto["over"]) == (50040.0, 50000, True)


def test_an_override_never_raises_a_limit():
    values = _values({"po_amount": 150000, "invoice_amount": 150000})
    raised = te.expect(
        "finance_ops", ENTRY, values, {"AUTO_WRITE_LIMIT_USD": Decimal(500000)}
    )
    assert raised["outcome"] == te.APPROVAL


def test_over_the_dual_approval_limit_it_needs_two():
    values = _values({"po_amount": 300000, "invoice_amount": 315000})
    assert te.expect("finance_ops", ENTRY, values, {})["approvers"] == 2


def test_an_amount_in_another_currency_is_held_as_the_run_checks_hold_it():
    entry = copy.deepcopy(ENTRY)
    entry["pack"]["seed"]["case"]["attributes"]["currency"] = "EUR"
    values = _values({"po_amount": 25000, "invoice_amount": 25000})
    expected = te.expect("finance_ops", entry, values, {})
    assert expected["sop"]["outcome"] == te.AUTO
    assert (expected["outcome"], expected["approvers"]) == (te.APPROVAL, 2)


def test_a_pack_with_no_checker_gets_no_expectation():
    assert te.expect("hr_ops", ENTRY, _values({}), {}) is None
    assert not te.has_checker("hr_ops") and te.has_checker("finance_ops")


@pytest.mark.parametrize(
    "outcome,status,result",
    [
        (te.APPROVAL, "awaiting_human_input", "as_expected"),
        (te.APPROVAL, "sap_updated", "differs"),
        (te.AUTO, "complete", "as_expected"),
        (te.AUTO, "manual_review_required", "differs"),
        (te.APPROVAL, "processing", None),
        (te.APPROVAL, None, None),
        (None, "complete", None),
    ],
)
def test_a_case_is_graded_the_way_evals_run_grades_it(outcome, status, result):
    assert te.result({"outcome": outcome}, status) == result


# ── the route ──

POST_SYSTEMS = {"s4": "sandbox"}


@pytest.fixture
def route(monkeypatch):
    for k, v in {
        "API_ALLOWED_CLIENTS": CONSOLE,
        "USERS_GROUP": USERS,
        "ADMINS_GROUP": ADMINS,
        "ENTITLEMENT_MAP": json.dumps({"finance_ops": {}}),
        "SEEDER_FUNCTION_NAME": "seeder",
        "TEST_DATA_TABLE": "test-data",
        "CASES_TABLE": "cases",
        "SEED_SYSTEMS": json.dumps(POST_SYSTEMS),
    }.items():
        monkeypatch.setenv(k, v)
    with mock.patch("boto3.client"), mock.patch("boto3.resource"):
        spec = importlib.util.spec_from_file_location(
            "bo_api_test_data", UC / "api" / "test_data" / "index.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    monkeypatch.setattr(mod.registry, "packs", lambda: {"finance_ops": ENTRY})
    monkeypatch.setattr(mod.config_overrides, "constant_overrides", lambda pack: {})
    mod.lambda_client = mock.MagicMock()
    mod.dynamodb = mock.MagicMock()
    mod.dynamodb.Table.return_value.query.return_value = {"Items": []}
    mod.dynamodb.batch_get_item.return_value = {"Responses": {"cases": []}}
    return mod


def _call(mod, method, path, body=None, groups=(USERS, ADMINS), pack="finance_ops"):
    resource = "/packs/{pack_id}/" + path
    event = _event(
        method,
        resource,
        groups=groups,
        pathParameters={"pack_id": pack},
        **({"body": json.dumps(body)} if body is not None else {}),
    )
    response = mod.handler(event, None)
    return response["statusCode"], json.loads(response["body"])


def _invokes(mod) -> list[dict]:
    return [
        {**c.kwargs, "Payload": json.loads(c.kwargs["Payload"])}
        for c in mod.lambda_client.invoke.call_args_list
    ]


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("GET", "test-data", None),
        ("POST", "test-data/expectation", {"inputs": {}}),
        ("POST", "test-data", {"inputs": {}}),
    ],
)
def test_only_an_operator_who_sees_the_pack_may_use_test_data(
    route, monkeypatch, method, path, body
):
    assert _call(route, method, path, body, groups=(USERS,))[0] == 403
    monkeypatch.setenv(
        "ENTITLEMENT_MAP",
        json.dumps({"finance_ops": {"groups": ["business-operations-ap"]}}),
    )
    status, reply = _call(route, method, path, body)
    assert (status, reply["error"]) == (403, "Not entitled to this pack")
    route.lambda_client.invoke.assert_not_called()


def test_a_pack_with_no_recipe_is_a_404(route, monkeypatch):
    bare = copy.deepcopy(ENTRY)
    bare["pack"].pop("seed")
    monkeypatch.setattr(route.registry, "packs", lambda: {"finance_ops": bare})
    assert _call(route, "GET", "test-data")[0] == 404


def test_the_page_is_the_recipe_with_each_presets_expectation(route):
    status, page = _call(route, "GET", "test-data")
    assert status == 200
    assert page["sap_system"] == {"name": "s4", "system_role": "sandbox"}
    assert (page["may_post"], page["refusal"]) == (True, None)
    assert [s["id"] for s in page["steps"]] == ["po", "gr", "invoice"]
    assert all("payload" not in s for s in page["steps"])
    assert page["steps"][1]["when"] == "not skip_gr"
    assert len(page["presets"]) == len(PACK["seed"]["presets"])
    assert all(p["expected"]["label"] for p in page["presets"])
    assert page["inputs"] == PACK["seed"]["inputs"]


def test_a_system_that_is_not_a_sandbox_reads_but_never_posts(route, monkeypatch):
    monkeypatch.setattr(route, "SEED_SYSTEMS", {"s4": "quality"})
    _, page = _call(route, "GET", "test-data")
    assert page["may_post"] is False and "quality" in page["refusal"]
    status, reply = _call(route, "POST", "test-data", {"inputs": {}})
    assert status == 409 and reply["error"].startswith(
        "Test data posts only to a sandbox"
    )
    route.lambda_client.invoke.assert_not_called()


def test_the_expectation_follows_typed_values_and_refuses_undeclared_ones(route):
    status, reply = _call(
        route, "POST", "test-data/expectation", {"inputs": {"invoice_amount": 52500}}
    )
    assert status == 200 and reply["expected"]["outcome"] == te.APPROVAL
    status, reply = _call(route, "POST", "test-data/expectation", {"inputs": {"x": 1}})
    assert status == 400 and "not an input" in reply["details"][0]
    status, _ = _call(
        route, "POST", "test-data/expectation", {"inputs": {"po_amount": 0}}
    )
    assert status == 400


def test_a_post_hands_each_copy_to_the_seeder_once_and_asynchronously(route):
    body = {
        "inputs": {"invoice_amount": 52500},
        "preset": "Price variance 5% — over-invoiced",
        "copies": 3,
        # Neither is the caller's to set.
        "requested_by": "someone else",
        "post_id": "1999-01-01T00:00:00Z#00000000",
    }
    status, reply = _call(route, "POST", "test-data", body)
    assert status == 202 and len(reply["posts"]) == 3
    invokes = _invokes(route)
    assert len(invokes) == 3
    assert {i["InvocationType"] for i in invokes} == {"Event"}
    assert {i["FunctionName"] for i in invokes} == {"seeder"}
    ids = [i["Payload"]["post_id"] for i in invokes]
    assert ids == [p["post_id"] for p in reply["posts"]] and len(set(ids)) == 3
    assert all(test_data_records.POST_ID_RE.fullmatch(i) for i in ids)
    payload = invokes[0]["Payload"]
    # The caller's name from the token (`caller_identity`), whatever the body said.
    assert payload["requested_by"] == "op@example.com"
    assert payload["inputs"] == {"invoice_amount": 52500}
    assert payload["preset"] == body["preset"]


@pytest.mark.parametrize(
    "body",
    [
        {"inputs": {"non_po": True}},
        {"inputs": []},
        {"inputs": {}, "copies": 0},
        {"inputs": {}, "copies": 6},
        {"inputs": {}, "copies": True},
        {"inputs": {}, "preset": 7},
    ],
)
def test_a_post_that_cannot_be_checked_posts_nothing(route, body):
    assert _call(route, "POST", "test-data", body)[0] == 400
    route.lambda_client.invoke.assert_not_called()


def test_the_posts_say_what_each_case_did_against_what_it_should(route):
    expected = te.expect("finance_ops", ENTRY, _values({"invoice_amount": 52500}), {})
    record = {
        "pack_id": "finance_ops",
        "post_id": "2026-11-20T09:14:03Z#7c1d02aa",
        "posted_at": "2026-11-20T09:14:03Z",
        "posted_by": "Sam Lee",
        "inputs": {"invoice_amount": Decimal(52500)},
        "expected": json.loads(json.dumps(expected), parse_float=Decimal),
        "status": "posted",
        "documents": {"invoice_number": "5100002044"},
        "case_id": "ap-a",
    }
    route.dynamodb.Table.return_value.query.return_value = {
        "Items": [
            record,
            {
                **record,
                "post_id": "2026-11-20T09:13:00Z#00000000",
                "status": "posting",
                "case_id": None,
            },
        ]
    }
    route.dynamodb.batch_get_item.return_value = {
        "Responses": {
            "cases": [{"case_id": "ap-a", "status": "sap_updated", "key": {}}]
        }
    }
    with mock.patch.object(route, "to_case_key", lambda c: {"case_id": c}):
        _, page = _call(route, "GET", "test-data")
    first, second = page["posts"]
    assert first["inputs"] == {"invoice_amount": 52500}
    assert first["case"] == {"status": "sap_updated", "key": {}}
    assert first["result"] == "differs"
    assert (second["case"], second["result"]) == (None, None)


# ── the seeder's record ──


@pytest.fixture
def seeder(monkeypatch):
    from test_business_operations_test_data import FakeSap

    monkeypatch.setenv("SAP_SYSTEMS", json.dumps({"s4": {"secret": "s", **SAP_SYSTEM}}))
    monkeypatch.setenv("TEST_DATA_TABLE", "test-data")
    monkeypatch.setenv("CASES_TABLE", "cases")
    with mock.patch("boto3.client"), mock.patch("boto3.resource"):
        for name in ("sap_http", "sap_odata", "registry"):
            sys.modules.pop(name, None)
        spec = importlib.util.spec_from_file_location(
            "bo_test_data_record", UC / "test_data" / "index.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    monkeypatch.setattr(mod.registry, "refresh_for_request", lambda: None)
    monkeypatch.setattr(
        mod.registry, "packs", lambda: {"finance_ops": copy.deepcopy(ENTRY)}
    )
    monkeypatch.setattr(mod.sap_http, "system", lambda name, spec: mock.Mock(name=name))
    monkeypatch.setattr(mod.seed_steps, "PAUSE_SECONDS", 0)
    monkeypatch.setattr(mod.config_overrides, "constant_overrides", lambda pack: {})
    sap = FakeSap()
    order: list[str] = []
    monkeypatch.setattr(
        mod.sap_odata, "post", lambda _sap, *a: order.append("sap") or sap(*a)
    )
    tables: dict[str, mock.MagicMock] = {}

    def table(name=None):
        return tables.setdefault(name or mod.TEST_DATA_TABLE, mock.MagicMock())

    monkeypatch.setattr(mod, "_table", table)
    table().put_item.side_effect = lambda **kw: order.append(kw["Item"]["status"])
    mod.order, mod.tables, mod.fake_sap = order, tables, sap
    return mod


def _puts(mod) -> list[dict]:
    return [c.kwargs for c in mod.tables["test-data"].put_item.call_args_list]


def test_a_seed_is_recorded_before_its_first_sap_call_and_settled_after(seeder):
    event = {
        "pack_id": "finance_ops",
        "inputs": {"posting_date": TODAY, "invoice_amount": 52500},
        "post_id": "2026-11-20T09:14:03Z#7c1d02aa",
        "preset": "Price variance 5% — over-invoiced",
        "requested_by": "Sam Lee",
    }
    out = seeder.handler(event, None)
    assert out["ok"] and out["post_id"] == event["post_id"]
    assert seeder.order[0] == "posting" and seeder.order[-1] == "posted"
    assert "sap" in seeder.order[1:-1]
    first, last = _puts(seeder)
    # Only a new post: a replayed id cannot overwrite another post's record.
    assert first["ConditionExpression"] == "attribute_not_exists(post_id)"
    record = last["Item"]
    assert record["posted_by"] == "Sam Lee" and record["preset"] == event["preset"]
    assert record["expected"]["outcome"] == te.APPROVAL
    assert record["case_id"] == out["case"]["case_id"]
    assert record["documents"]["invoice_number"] == "5105600101"
    assert record["documents"]["po_number"] == "4500000001"
    assert isinstance(record["inputs"]["invoice_amount"], (int, Decimal))


def test_a_seed_whose_record_cannot_be_written_posts_nothing(seeder):
    seeder.tables["test-data"].put_item.side_effect = RuntimeError("throttled")
    out = seeder.handler(
        {"pack_id": "finance_ops", "inputs": {"posting_date": TODAY}}, None
    )
    assert not out["ok"] and out["error"] == "Could not record the seed"
    assert not seeder.fake_sap.posts


def test_ops_seed_gets_a_post_id_and_a_forged_one_is_refused(seeder):
    out = seeder.handler(
        {"pack_id": "finance_ops", "inputs": {"posting_date": TODAY}}, None
    )
    assert test_data_records.POST_ID_RE.fullmatch(out["post_id"])
    bad = seeder.handler(
        {"pack_id": "finance_ops", "inputs": {"posting_date": TODAY}, "post_id": "x"},
        None,
    )
    assert not bad["ok"] and len(seeder.fake_sap.posts) == 3


def test_a_failed_chain_is_settled_as_failed(seeder):
    seeder.fake_sap.refuse = "A_SupplierInvoice"
    out = seeder.handler(
        {"pack_id": "finance_ops", "inputs": {"posting_date": TODAY}}, None
    )
    assert not out["ok"]
    record = _puts(seeder)[-1]["Item"]
    assert record["status"] == "failed" and "period not open" in record["error"]
    assert "case_id" not in record


def test_settling_marks_a_case_already_open_and_only_one_with_no_mark(seeder):
    out = seeder.handler(
        {"pack_id": "finance_ops", "inputs": {"posting_date": TODAY}}, None
    )
    (call,) = seeder.tables["cases"].update_item.call_args_list
    assert call.kwargs["UpdateExpression"] == "SET test_data = :m"
    assert "attribute_not_exists(test_data)" in call.kwargs["ConditionExpression"]
    assert call.kwargs["Key"] == {"case_id": out["case"]["case_id"]}
    mark = call.kwargs["ExpressionAttributeValues"][":m"]
    assert set(mark) <= set(test_data_records.MARK_FIELDS)
    assert "documents" not in mark and "status" not in mark


# ── the mark on a new case ──


@pytest.fixture
def case_store(monkeypatch):
    monkeypatch.setenv("TEST_DATA_TABLE", "test-data")
    with mock.patch("boto3.client"), mock.patch("boto3.resource"):
        spec = importlib.util.spec_from_file_location(
            "bo_case_store_mark", UC / "pipeline" / "case_store.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


def test_a_new_case_carries_the_mark_of_the_newest_record_for_it(case_store):
    records = case_store._dynamodb.Table.return_value
    records.query.return_value = {
        "Items": [
            {
                "post_id": "2026-11-20T09:00:00Z#00000001",
                "posted_by": "old",
                "status": "posted",
            },
            {
                "post_id": "2026-11-20T09:14:03Z#7c1d02aa",
                "posted_by": "Sam Lee",
                "status": "posted",
            },
        ]
    }
    cases = mock.MagicMock()
    assert case_store.put_case(
        cases, {"case_id": "ap-a", "test_data": {"posted_by": "forged"}}
    )
    item = cases.put_item.call_args.kwargs["Item"]
    assert item["test_data"] == {
        "post_id": "2026-11-20T09:14:03Z#7c1d02aa",
        "posted_by": "Sam Lee",
    }
    assert records.query.call_args.kwargs["IndexName"] == test_data_records.CASE_INDEX


def test_a_producer_cannot_mark_a_case_the_seeder_did_not_post(case_store):
    case_store._dynamodb.Table.return_value.query.return_value = {"Items": []}
    cases = mock.MagicMock()
    case_store.put_case(
        cases, {"case_id": "ap-a", "test_data": {"posted_by": "forged"}}
    )
    assert "test_data" not in cases.put_item.call_args.kwargs["Item"]


def test_an_unreadable_record_still_opens_the_case(case_store):
    case_store._dynamodb.Table.return_value.query.side_effect = RuntimeError(
        "throttled"
    )
    cases = mock.MagicMock()
    assert case_store.put_case(cases, {"case_id": "ap-a"})
    assert "test_data" not in cases.put_item.call_args.kwargs["Item"]


def test_every_case_is_written_with_the_amount_its_list_sorts_by(
    case_store, monkeypatch
):
    from decimal import Decimal

    fields = {
        "amount": {"label": "Amount", "type": "amount", "currency_field": "currency"},
        "currency": {"label": "Currency", "type": "string"},
    }
    monkeypatch.setattr(
        case_store.registry, "packs", lambda: {"ap": {"fields": fields}}
    )
    case_store._dynamodb.Table.return_value.query.return_value = {"Items": []}
    cases = mock.MagicMock()
    item = {"case_id": "ap-a", "pack_id": "ap", "attributes": {"amount": "25.50"}}
    case_store.put_case(cases, item)
    assert cases.put_item.call_args.kwargs["Item"]["amount_sort"] == Decimal("25.50")
    case_store.put_case(cases, {**item, "attributes": {"amount": "n/a"}})
    stored = cases.put_item.call_args.kwargs["Item"]["amount_sort"]
    assert stored == case_store.case_query.NO_AMOUNT


def test_without_test_data_no_record_is_read(case_store, monkeypatch):
    monkeypatch.setattr(case_store, "TEST_DATA_TABLE", "")
    case_store.put_case(mock.MagicMock(), {"case_id": "ap-a"})
    case_store._dynamodb.Table.return_value.query.assert_not_called()


# ── the synthesized stack ──


@pytest.fixture(scope="module")
def stack():
    return _stack(
        "business-operations.yaml",
        {"test_data": {"enabled": True}, "sap": {"systems": {"s4": SAP_SYSTEM}}},
    )


def _role_statements(template: dict, function_id: str) -> list[dict]:
    return [
        s
        for p in _of(template, "AWS::IAM::Policy")
        if function_id in json.dumps(p["Properties"]["Roles"])
        for s in p["Properties"]["PolicyDocument"]["Statement"]
    ]


def _actions(statement: dict) -> list[str]:
    a = statement["Action"]
    return a if isinstance(a, list) else [a]


def _records_table_id(template: dict) -> str:
    (table_id,) = [
        k
        for k, r in template["Resources"].items()
        if r["Type"] == "AWS::DynamoDB::Table"
        and any(a["AttributeName"] == "post_id" for a in r["Properties"]["KeySchema"])
    ]
    return table_id


def test_the_record_table_is_keyed_by_pack_and_post_with_a_case_index(stack):
    table = stack["Resources"][_records_table_id(stack)]["Properties"]
    assert [k["AttributeName"] for k in table["KeySchema"]] == ["pack_id", "post_id"]
    (index,) = table["GlobalSecondaryIndexes"]
    assert index["IndexName"] == test_data_records.CASE_INDEX
    assert index["KeySchema"][0]["AttributeName"] == "case_id"


def test_only_the_seeder_writes_the_record_table(stack):
    table_id = _records_table_id(stack)
    writes = {
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
        "dynamodb:BatchWriteItem",
        "dynamodb:*",
    }
    writers = set()
    for policy in _of(stack, "AWS::IAM::Policy"):
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            if table_id in json.dumps(statement.get("Resource")) and writes & set(
                _actions(statement)
            ):
                writers.add(json.dumps(policy["Properties"]["Roles"]))
    assert len(writers) == 1 and "TestDataFunction" in writers.pop()


def test_the_seeder_is_never_retried_and_only_the_route_may_invoke_it(stack):
    (config,) = [
        c
        for c in _of(stack, "AWS::Lambda::EventInvokeConfig")
        if "TestDataFunction" in json.dumps(c["Properties"]["FunctionName"])
    ]
    assert config["Properties"]["MaximumRetryAttempts"] == 0
    invokers = [
        json.dumps(p["Properties"]["Roles"])
        for p in _of(stack, "AWS::IAM::Policy")
        for s in p["Properties"]["PolicyDocument"]["Statement"]
        if "lambda:InvokeFunction" in _actions(s)
        and "TestDataFunction" in json.dumps(s["Resource"])
    ]
    assert invokers and all("TestDataApiFunction" in r for r in invokers)


def test_the_poller_reads_the_records_to_mark_a_case(stack):
    pollers = [
        f["Properties"]["Environment"]["Variables"]
        for f in _of(stack, "AWS::Lambda::Function")
        if "TEST_DATA_TABLE"
        in f["Properties"].get("Environment", {}).get("Variables", {})
    ]
    # The seeder, the route, and the poller.
    assert len(pollers) == 3
