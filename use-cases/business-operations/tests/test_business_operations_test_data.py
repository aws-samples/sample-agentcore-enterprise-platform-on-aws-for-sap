# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""business-operations test data and product alarms.

The seeder: invoked over IAM by `ops.py seed`, with no route; the pack's declarative
`seed.steps`, rendered and posted in order; and the verb's input parsing. The alarms:
every one publishes to the platform topic on ALARM and OK, and reads this stack's own
namespace.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from unittest import mock

import pytest
from test_business_operations_api import LAYER, SAP_SYSTEM, UC, _of, _stack

SRC = UC / "test_data"
for path in (SRC, LAYER, UC.parents[1] / "layers" / "sap" / "lib" / "sap_client"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import seed_steps

PACK = json.loads((UC / "packs/finance_ops/pack.json").read_text())
SEED = PACK["seed"]


# ── the templates ──


def test_a_template_is_arithmetic_over_inputs_and_earlier_steps():
    scope = {"q": 10, "amount": 50050, "po": {"PurchaseOrder": "4500000001"}}
    render = seed_steps.render
    assert render("{{round(amount / q, 2)}}", scope) == "5005"
    assert render("INV-{{po.PurchaseOrder}}", scope) == "INV-4500000001"
    assert render({"a": "{{q > 5}}", "b": "{{None}}", "c": [True]}, scope) == {
        "a": True,
        "c": [True],
    }
    assert render("{{'x' if not q else 'y'}}", scope) == "y"
    assert render("{{sap_date('2026-08-31', -7)}}", {}) == "/Date(1787529600000)/"
    assert render("{{year('2026-08-31')}}", {}) == "2026"


@pytest.mark.parametrize(
    "template",
    [
        "{{__import__('os')}}",
        "{{q.__class__}}",
        "{{[1, 2]}}",
        "{{2 ** 99}}",
        "{{'x' * 10}}",
        "{{open('f')}}",
        "{{round(x=1)}}",
        "{{missing}}",
        "{{po.Nothing}}",
        "{{q / 0}}",
        "{{sap_date('31/08/2026')}}",
    ],
)
def test_a_template_refuses_anything_but_its_grammar(template):
    with pytest.raises(seed_steps.TemplateError):
        seed_steps.render(template, {"q": 1, "po": {}})


def test_template_names_are_what_the_validator_checks():
    assert seed_steps.template_names(
        {"a": ["{{round(x / y, 2)}}", "{{po.PurchaseOrder}}"], "b": 3}
    ) == {"x", "y", "po"}


# ── the inputs ──


def _open():
    return "2026-08-31"


def test_inputs_start_from_the_declaration_and_are_checked_against_it():
    values = seed_steps.inputs(SEED, {"po_amount": 7}, _open)
    assert values["po_amount"] == 7 and values["payment_block"] == "R"
    # Blank posting_date is whatever SAP says is open.
    assert values["posting_date"] == "2026-08-31"
    given = seed_steps.inputs(SEED, {"posting_date": "2026-07-01"}, None)
    assert given["posting_date"] == "2026-07-01"
    for bad in (
        {"po_amount": "7"},
        {"po_amount": float("nan")},
        {"po_amount": True},
        {"po_amount": 0},
        {"skip_gr": "yes"},
        {"payment_block": "Z"},
        {"posting_date": "31.08.2026"},
        {"non_po": True},
    ):
        with pytest.raises(ValueError):
            seed_steps.inputs(SEED, bad)


def test_a_blank_posting_date_that_sap_cannot_answer_names_posting_date():
    def closed():
        raise RuntimeError("1710 has no period open")

    for open_date in (None, closed):
        with pytest.raises(ValueError, match="posting_date"):
            seed_steps.inputs(SEED, {}, open_date)
    # A given date never asks SAP.
    seed_steps.inputs(SEED, {"posting_date": "2026-08-31"}, closed)


# ── the chain ──


class FakeSap:
    """Records every POST and answers with the document numbers SAP would."""

    def __init__(self, refuse: str | None = None):
        self.posts: list[tuple[str, str, dict]] = []
        self.refuse = refuse

    def __call__(self, service, entity, body):
        self.posts.append((service, entity, body))
        if entity == self.refuse:
            raise RuntimeError(f"{entity} creation failed (400): period not open")
        return {
            "A_PurchaseOrder": {"PurchaseOrder": "4500000001"},
            "A_MaterialDocumentHeader": {"MaterialDocument": "5000000001"},
            "A_SupplierInvoice": {
                "SupplierInvoice": "5105600101",
                "FiscalYear": "2026",
            },
        }[entity]


def _chain(given: dict, sap: FakeSap) -> dict:
    return seed_steps.run(SEED, seed_steps.inputs(SEED, given, _open), sap, pause=0)


def test_finance_ops_posts_a_po_a_receipt_and_a_blocked_invoice():
    sap = FakeSap()
    out = _chain(
        {"po_amount": 50000, "invoice_amount": 50050, "posting_date": "2026-08-31"}, sap
    )
    assert [e for _, e, _ in sap.posts] == [
        "A_PurchaseOrder",
        "A_MaterialDocumentHeader",
        "A_SupplierInvoice",
    ]
    po, gr, invoice = (b for _, _, b in sap.posts)
    item = po["to_PurchaseOrderItem"][0]
    assert item["NetPriceAmount"] == "5000" and item["OrderQuantity"] == "10"
    assert item["GoodsReceiptIsExpected"] is True
    receipt = gr["to_MaterialDocumentItem"]["results"][0]
    assert receipt["PurchaseOrder"] == "4500000001"
    assert receipt["QuantityInEntryUnit"] == "10"
    assert gr["PostingDate"] == "/Date(1788134400000)/"  # 2026-08-31
    assert gr["DocumentDate"] == "/Date(1787529600000)/"  # seven days earlier
    assert invoice["FiscalYear"] == "2026" and invoice["PaymentBlockingReason"] == "R"
    assert invoice["InvoiceGrossAmount"] == "50050"
    assert invoice["SupplierInvoiceIDByInvcgParty"] == "INV-4500000001"
    ref = invoice["to_SuplrInvcItemPurOrdRef"][0]
    assert ref["PurchaseOrder"] == "4500000001"
    assert out["po_number"] == "4500000001" and out["gr_document"] == "5000000001"
    assert out["invoice_number"] == "5105600101" and out["fiscal_year"] == "2026"
    assert out["case"]["key"] == {"SupplierInvoice": "5105600101", "FiscalYear": "2026"}
    assert out["case"]["attributes"]["purchase_order"] == "4500000001"
    assert not [k for k in out if k.endswith("_error")]


def test_skipping_the_receipt_posts_two_documents():
    sap = FakeSap()
    out = _chain({"skip_gr": True}, sap)
    assert [e for _, e, _ in sap.posts] == ["A_PurchaseOrder", "A_SupplierInvoice"]
    assert out.get("gr_document") is None and out["invoice_number"]


def test_the_step_sap_refuses_ends_the_chain_and_keeps_what_landed():
    sap = FakeSap(refuse="A_MaterialDocumentHeader")
    out = _chain({}, sap)
    assert len(sap.posts) == 2  # no invoice against a receipt that did not post
    assert out["po_number"] == "4500000001"
    assert "period not open" in out["gr_error"] and "case" not in out


def test_a_po_that_came_back_without_a_number_stops_before_the_receipt():
    def post(service, entity, body):
        return {}  # 201 with a body that was not JSON

    out = seed_steps.run(SEED, seed_steps.inputs(SEED, {}, _open), post, pause=0)
    assert out["po_number"] is None and "PurchaseOrder" in out["gr_error"]


def test_a_step_posts_only_under_the_odata_root():
    seed = copy.deepcopy(SEED)
    seed["steps"][0]["service"] = "API_X/../../bc/admin"
    sap = FakeSap()
    out = seed_steps.run(seed, seed_steps.inputs(seed, {}, _open), sap, pause=0)
    assert not sap.posts and "OData names" in out["po_error"]


# ── the handler ──


@pytest.fixture
def seeder(monkeypatch):
    monkeypatch.setenv("SAP_SYSTEMS", json.dumps({"s4": {"secret": "s", **SAP_SYSTEM}}))
    with mock.patch("boto3.client"), mock.patch("boto3.resource"):
        for name in ("sap_http", "sap_odata", "registry"):
            sys.modules.pop(name, None)
        spec = importlib.util.spec_from_file_location("bo_test_data", SRC / "index.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    published = {
        "finance_ops": {
            "pack": {k: v for k, v in PACK.items() if k not in ("source", "fields")},
            "source": {**PACK["source"], "pack_id": "finance_ops"},
        }
    }
    monkeypatch.setattr(mod.registry, "refresh_for_request", lambda: None)
    monkeypatch.setattr(mod.registry, "packs", lambda: published)
    monkeypatch.setattr(mod.sap_http, "system", lambda name, spec: mock.Mock(name=name))
    sap = FakeSap()
    monkeypatch.setattr(mod.sap_odata, "post", lambda _sap, *a: sap(*a))
    monkeypatch.setattr(mod.seed_steps, "PAUSE_SECONDS", 0)
    asked = []
    mod.real_open_posting_date = mod.sap_odata.open_posting_date
    monkeypatch.setattr(
        mod.sap_odata,
        "open_posting_date",
        lambda _sap, cc: asked.append(cc) or "2026-08-31",
    )
    mod.asked = asked
    mod.fake_sap, mod.published = sap, published
    return mod


def test_only_a_published_pack_with_steps_seeds(seeder):
    assert not seeder.handler({"pack_id": "os"}, None)["ok"]
    assert not seeder.handler("not an object", None)["ok"]
    seeder.published["finance_ops"]["pack"]["seed"] = {"label": "x", "inputs": []}
    assert "seed.steps" in seeder.handler({"pack_id": "finance_ops"}, None)["error"]
    assert not seeder.fake_sap.posts


def test_a_seed_reports_the_case_the_poller_will_open(seeder):
    out = seeder.handler(
        {"pack_id": "finance_ops", "inputs": {"posting_date": "2026-08-31"}}, None
    )
    assert out["ok"] and out["system"] == "s4"
    from case_key import derive_case_id

    assert out["case"]["case_id"] == derive_case_id(
        "finance_ops", "ap", {"SupplierInvoice": "5105600101", "FiscalYear": "2026"}
    )
    assert not seeder.handler({"pack_id": "finance_ops", "inputs": []}, None)["ok"]
    bad = seeder.handler({"pack_id": "finance_ops", "inputs": {"non_po": True}}, None)
    assert not bad["ok"] and "not an input" in bad["error"]


def test_a_seed_with_no_posting_date_posts_on_the_date_sap_has_open(seeder):
    out = seeder.handler({"pack_id": "finance_ops"}, None)
    assert out["ok"] and out["inputs"]["posting_date"] == "2026-08-31"
    assert seeder.asked == ["1710"]
    assert seeder.fake_sap.posts[1][2]["PostingDate"] == "/Date(1788134400000)/"


def test_a_blank_posting_date_on_a_token_flow_fails_naming_posting_date(
    seeder, monkeypatch
):
    # The real period read, over a system that signs in with m2m-sap: no ADT call,
    # no document, and an error that says what to set.
    monkeypatch.setattr(
        seeder.sap_odata, "open_posting_date", seeder.real_open_posting_date
    )
    sap = mock.Mock(oauth={"flow": "m2m-sap"})
    monkeypatch.setattr(seeder.sap_http, "system", lambda name, spec: sap)
    out = seeder.handler({"pack_id": "finance_ops"}, None)
    assert not out["ok"] and "m2m-sap" in out["error"]
    assert "Set posting_date (YYYY-MM-DD)" in out["error"]
    assert not seeder.fake_sap.posts and not sap.mock_calls
    given = {"pack_id": "finance_ops", "inputs": {"posting_date": "2026-08-31"}}
    assert seeder.handler(given, None)["ok"]


def test_seed_list_says_posting_date_is_required_on_a_token_flow(monkeypatch, capsys):
    ops = _ops()
    described = {"ok": True, "system": "s4", "flow": "basic", "seed": SEED}
    monkeypatch.setattr(ops, "_own_function", lambda key: ("fn", "us-west-2"))
    monkeypatch.setattr(ops, "_invoke", lambda *a: described)
    assert ops.seed(["finance_ops", "--list"]) == 0
    assert "posting_date is required" not in capsys.readouterr().out
    described["flow"] = "m2m-sap"
    assert ops.seed(["finance_ops", "--list"]) == 0
    assert (
        "posting_date is required: s4 signs in with m2m-sap" in capsys.readouterr().out
    )


def test_a_refused_step_is_a_failed_seed_that_names_what_landed(seeder):
    seeder.fake_sap.refuse = "A_SupplierInvoice"
    out = seeder.handler({"pack_id": "finance_ops"}, None)
    assert not out["ok"] and out["po_number"] and "period not open" in out["error"]


def test_a_read_back_gets_the_supplier_line_a_release_would_change(seeder, monkeypatch):
    sap = mock.Mock()
    sap.base_url = "https://sap.example.com"
    sap.get_json.return_value = {"d": {"results": [{"PaymentBlockingReason": "R"}]}}
    monkeypatch.setattr(seeder.sap_http, "system", lambda name, spec: sap)
    read = {
        "key": {"SupplierInvoice": "5100004457", "FiscalYear": "2026"},
        "fields": ["PaymentBlockingReason"],
    }
    out = seeder.handler({"pack_id": "finance_ops", "read": read}, None)
    assert out == {"ok": True, "record": {"PaymentBlockingReason": "R"}}
    url, params = sap.get_json.call_args.args
    assert url.endswith("/FAP_VENDOR_LINE_ITEMS_SRV/Items")
    assert params["$filter"] == (
        "CompanyCode eq '1710' and OriginalReferenceDocument eq '51000044572026'"
    )
    # A quote in a key value is doubled inside the filter's literal.
    read["key"]["SupplierInvoice"] = "x' or '1' eq '1"
    seeder.handler({"pack_id": "finance_ops", "read": read}, None)
    assert "x'' or ''1'' eq ''1" in sap.get_json.call_args.args[1]["$filter"]
    sap.get_json.return_value = {"d": {"results": []}}
    assert (
        "SAP has no Items"
        in (seeder.handler({"pack_id": "finance_ops", "read": read}, None)["error"])
    )


def test_a_read_back_gets_the_source_document_by_its_key(seeder, monkeypatch):
    seeder.published["finance_ops"]["pack"]["seed"].pop("sap_state")
    sap = mock.Mock()
    sap.base_url = "https://sap.example.com"
    sap.get_json.return_value = {"d": {"PaymentBlockingReason": "R"}}
    monkeypatch.setattr(seeder.sap_http, "system", lambda name, spec: sap)
    # A quote in a key value is doubled, OData's escape, never a way out of it.
    key = {"SupplierInvoice": "A'1", "FiscalYear": "2026"}
    out = seeder.handler(
        {
            "pack_id": "finance_ops",
            "read": {"key": key, "fields": ["PaymentBlockingReason"]},
        },
        None,
    )
    assert out == {"ok": True, "record": {"PaymentBlockingReason": "R"}}
    url = sap.get_json.call_args.args[0]
    assert url.endswith(
        "/API_SUPPLIERINVOICE_PROCESS_SRV/A_SupplierInvoice("
        "SupplierInvoice=%27A%27%271%27,FiscalYear=%272026%27)"
    )
    for read in (
        {"key": {"SupplierInvoice": "1"}, "fields": ["X"]},
        {"key": key, "fields": ["X)/$batch"]},
    ):
        assert not seeder.handler({"pack_id": "finance_ops", "read": read}, None)["ok"]
    assert not seeder.fake_sap.posts


# ── the declaration ──


def test_finance_ops_seed_is_valid_against_the_schema_and_the_validator():
    from business_operations import validate_packs

    schema = json.loads((UC / "packs/pack.schema.json").read_text())
    assert validate_packs.schema_errors(PACK, schema) == []
    assert validate_packs._seed("finance_ops", SEED, PACK["source"]) == []


@pytest.mark.parametrize(
    "mutate, message",
    [
        (
            lambda s: s["steps"][1]["payload"].update(X="{{invoice.SupplierInvoice}}"),
            "before",
        ),
        (lambda s: s["steps"][0]["payload"].update(X="{{q ** 2}}"), "not allowed"),
        (lambda s: s["steps"][1].update(when="not nothing"), "before"),
        (lambda s: s["steps"][2].update(id="po"), "already"),
        (lambda s: s["case"]["key"].pop("FiscalYear"), "source keys"),
    ],
)
def test_the_validator_refuses_a_step_that_cannot_run(mutate, message):
    from business_operations import validate_packs

    seed = copy.deepcopy(SEED)
    mutate(seed)
    errors = validate_packs._seed("finance_ops", seed, PACK["source"])
    assert any(message in e for e in errors), errors


# ── the stack ──

ON = {"test_data": {"enabled": True}, "sap": {"systems": {"s4": SAP_SYSTEM}}}


def _seed_function(t: dict) -> dict | None:
    return next(
        (
            f["Properties"]
            for f in _of(t, "AWS::Lambda::Function")
            if "SAP_SYSTEMS"
            in f["Properties"].get("Environment", {}).get("Variables", {})
            and "TestData" in json.dumps(f["Properties"].get("LoggingConfig", {}))
        ),
        None,
    )


def test_test_data_is_off_by_default():
    t = _stack("business-operations.yaml", {"sap": ON["sap"]})
    assert _seed_function(t) is None


def test_the_seeder_posts_to_the_pack_system_and_has_no_route():
    t = _stack("business-operations.yaml", ON)
    fn = _seed_function(t)
    env = fn["Environment"]["Variables"]
    assert list(json.loads(env["SAP_SYSTEMS"])) == ["s4"] and env["REGISTRY_BUCKET"]
    # API Gateway never invokes the seeder itself: the console's route function does,
    # and IAM is the gate for everyone else (ops.py and verify.py look it up).
    gateway_targets = [
        json.dumps(p["Properties"]["FunctionName"])
        for p in _of(t, "AWS::Lambda::Permission")
        if p["Properties"].get("Principal") == "apigateway.amazonaws.com"
    ]
    assert not [f for f in gateway_targets if "TestDataFunction" in f]
    assert [f for f in gateway_targets if "TestDataApiFunction" in f]
    assert any(
        p["Properties"]["Name"].endswith("/test-data-function")
        for p in _of(t, "AWS::SSM::Parameter")
        if isinstance(p["Properties"]["Name"], str)
    )


def test_without_test_data_there_is_no_route_and_no_table():
    t = _stack("business-operations.yaml", {"sap": {"systems": {"s4": SAP_SYSTEM}}})
    assert not [
        r
        for r in _of(t, "AWS::ApiGateway::Resource")
        if r["Properties"]["PathPart"] in ("test-data", "seed")
    ]
    assert not [
        r
        for r in _of(t, "AWS::DynamoDB::Table")
        if any(k["AttributeName"] == "post_id" for k in r["Properties"]["KeySchema"])
    ]


def test_test_data_without_a_sap_system_fails_the_synth():
    with pytest.raises(ValueError, match="finance_ops seeds SAP"):
        _stack("business-operations.yaml", {"test_data": {"enabled": True}})


def _seeder_policy(t: dict) -> str:
    """Every statement on the seeder's role, as one string."""
    return json.dumps(
        [
            p["Properties"]["PolicyDocument"]
            for p in _of(t, "AWS::IAM::Policy")
            if "TestDataFunction" in json.dumps(p["Properties"]["Roles"])
        ]
    )


def _system_role(system_role: str) -> dict:
    return {**SAP_SYSTEM, "system_role": system_role}


def test_the_seeder_holds_iam_only_on_a_sandbox_system(monkeypatch):
    from business_operations import test_data

    # finance_ops names its system, so two direct systems still resolve.
    monkeypatch.setattr(
        test_data,
        "declared_seeds",
        lambda: {"finance_ops": {**PACK, "sap_system": "s4"}},
    )
    prd = {**_system_role("production"), "secret_name": "business-operations/sap/prd"}
    t = _stack(
        "business-operations.yaml",
        {
            "test_data": {"enabled": True},
            "sap": {"systems": {"s4": SAP_SYSTEM, "prd": prd}},
        },
    )
    env = _seed_function(t)["Environment"]["Variables"]
    # Both stay in its map, so a pack resolves by the poller's rule (E1)...
    assert json.loads(env["SAP_SYSTEMS"])["prd"]["system_role"] == "production"
    # ...but only the sandbox's secret and connection are granted.
    policy = _seeder_policy(t)
    assert "business-operations/sap/s4-??????" in policy
    assert "systems/s4/connection" in policy
    assert "sap/prd" not in policy and "systems/prd" not in policy


@pytest.mark.parametrize("system_role", ["development", "quality", "production"])
def test_a_seeding_pack_on_a_system_that_is_not_a_sandbox_fails_the_synth(system_role):
    with pytest.raises(ValueError, match=f"whose system role is {system_role}"):
        _stack(
            "business-operations.yaml",
            {
                "test_data": {"enabled": True},
                "sap": {"systems": {"s4": _system_role(system_role)}},
            },
        )


def test_the_seeder_refuses_an_obo_system(monkeypatch):
    # On an obo system SAP authorizes each person; the seeder would post as the
    # unattended block's service account instead.
    from business_operations import test_data

    monkeypatch.setattr(test_data, "declared_seeds", lambda: {"finance_ops": PACK})
    obo = {
        **_system_role("sandbox"),
        "flow": "obo",
        "scopes": ["api://sap-app/SAP.Access"],
        "gateway_scopes": ["api://mcp-app/.default"],
        "unattended": {"secret_name": "business-operations/sap/s4-svc"},
    }
    with pytest.raises(ValueError, match="flow: obo"):
        _stack(
            "business-operations.yaml",
            {"test_data": {"enabled": True}, "sap": {"systems": {"s4": obo}}},
        )


def test_test_data_with_no_sandbox_system_fails_the_synth(monkeypatch):
    from business_operations import test_data

    monkeypatch.setattr(test_data, "declared_seeds", dict)
    with pytest.raises(ValueError, match="no system .* has system_role: sandbox"):
        _stack(
            "business-operations.yaml",
            {
                "test_data": {"enabled": True},
                "sap": {"systems": {"s4": _system_role("quality")}},
            },
        )


# ── the alarms ──


def test_every_alarm_publishes_to_the_platform_topic_on_alarm_and_ok():
    t = _stack(
        "business-operations.yaml",
        ON,
    )
    alarms = {
        k: v["Properties"]
        for k, v in t["Resources"].items()
        if v["Type"] == "AWS::CloudWatch::Alarm"
    }
    for id in (
        "DlqAlarm",
        "AgentFailureAlarm",
        "AgentCostAlarm",
        "RunChannelAlarm",
        "LambdaErrorsAlarm",
        "LambdaThrottlesAlarm",
        "NotificationChannelAlarm",
        "RegistryUnavailableAlarm",
        "RunCheckRefusalAlarm",
    ):
        assert any(k.startswith(id) for k in alarms), id
    for id, a in alarms.items():
        assert a["AlarmActions"] and a["AlarmActions"] == a["OKActions"], id
        assert "AlarmName" not in a, id
        if "Namespace" in a:
            assert a["Namespace"] in ("p-dev/business-operations", "AWS/SQS"), id
    errors = next(a for k, a in alarms.items() if k.startswith("LambdaErrorsAlarm"))
    # Every watched function, the seeder, the operator re-run and containment included.
    assert len(errors["Metrics"]) - 1 == 7


# ── the verbs ──


def _ops():
    if str(UC) not in sys.path:
        sys.path.insert(0, str(UC))
    import ops

    return ops


def test_seed_inputs_start_from_the_pack_then_a_preset_then_the_words():
    ops = _ops()
    spec = json.loads((UC / "packs/finance_ops/pack.json").read_text())["seed"]
    defaults = ops.seed_inputs(spec, [])
    assert defaults["po_amount"] == 50000 and defaults["payment_block"] == "R"
    preset = ops.seed_inputs(spec, ["--preset", "1"])
    assert preset["invoice_amount"] == 25000
    words = ops.seed_inputs(
        spec, ["--preset", "1", "po_amount=12.5", "skip_gr=yes", "non_po=true"]
    )
    assert words["po_amount"] == 12.5 and words["skip_gr"] is True
    assert words["non_po"] is True  # undeclared: JSON, for the recipe to check
    assert ops.seed_inputs(spec, ["po_amount=7"])["po_amount"] == 7
    for bad in (
        ["--preset", "0"],
        ["--preset", "99"],
        ["--preset"],
        ["po_amount=lots"],
        ["skip_gr=maybe"],
        ["payment_block=Z"],
        ["po_amount"],
    ):
        with pytest.raises(ValueError):
            ops.seed_inputs(spec, bad)


def test_seed_refuses_what_is_not_a_pack_id_before_any_aws_call(capsys):
    ops = _ops()
    with mock.patch("boto3.client") as client:
        assert ops.seed(["../packs", "--yes"]) == 1
        assert ops.seed(["finance_ops\n", "--yes"]) == 1
        assert ops.seed([]) == 2
    client.assert_not_called()


def test_seed_confirms_the_published_seed_not_the_checkouts(monkeypatch, capsys):
    ops = _ops()
    published = {**SEED, "label": "Published label", "steps": SEED["steps"][:1]}
    calls = []

    def invoke(fn, region, payload, seconds):
        calls.append(payload)
        if payload.get("describe"):
            return {"ok": True, "seed": published}
        return {"ok": True}

    monkeypatch.setattr(ops, "_own_function", lambda key: ("fn", "us-west-2"))
    monkeypatch.setattr(ops, "_invoke", invoke)
    monkeypatch.setattr(ops, "_caller", lambda region: "me")
    assert ops.seed(["finance_ops", "--yes"]) == 0
    shown = capsys.readouterr().out
    step = SEED["steps"][0]
    assert "Published label" in shown and f"{step['service']}/{step['entity']}" in shown
    assert SEED["label"] not in shown or SEED["label"] == "Published label"
    assert [bool(c.get("describe")) for c in calls] == [True, False]


def test_the_seeder_describes_the_published_seed_without_posting(seeder):
    out = seeder.handler({"pack_id": "finance_ops", "describe": True}, None)
    assert out["ok"] and out["seed"]["steps"] == SEED["steps"]
    assert not seeder.fake_sap.posts


def test_a_step_target_with_a_trailing_newline_is_refused():
    seed = {"steps": [{"id": "s", "service": "API_X\n", "entity": "E", "payload": {}}]}
    out = seed_steps.run(seed, {}, lambda *a: {"ok": 1}, pause=0)
    assert "OData names" in out["s_error"]


def test_the_poller_polls_only_the_named_pack(monkeypatch):
    sys.path.insert(0, str(UC / "pipeline"))
    with mock.patch("boto3.client"), mock.patch("boto3.resource"):
        sys.modules.pop("sap_poller", None)
        import sap_poller
    monkeypatch.setattr(sap_poller.registry, "refresh_for_request", lambda: None)
    monkeypatch.setattr(
        sap_poller,
        "load_pack_sources",
        lambda: [{"pack_id": "a"}, {"pack_id": "b"}],
    )
    monkeypatch.setattr(sap_poller, "system_of", lambda cfg: None)
    monkeypatch.setattr(sap_poller, "get_cases_table", lambda: None)
    out = sap_poller.handler({"pack_id": "b"}, None)
    assert list(out["packs"]) == ["b"]
    assert sap_poller.handler({"pack_id": "zz"}, None)["packs"] == {
        "zz": {"error": "not-polled"}
    }
    # The schedule's event names no pack: every pack is polled.
    assert set(
        sap_poller.handler({"detail-type": "Scheduled Event"}, None)["packs"]
    ) == {
        "a",
        "b",
    }


def test_seed_fails_when_a_later_document_did_not_land(monkeypatch, capsys):
    ops = _ops()
    monkeypatch.setattr(ops, "_own_function", lambda key: ("fn", "r"))
    monkeypatch.setattr(ops, "_caller", lambda region: "arn")
    out = {"ok": True, "po_number": "45", "invoice_error": "posting period"}
    monkeypatch.setattr(
        ops,
        "_invoke",
        lambda fn, r, payload, s: (
            {"ok": True, "seed": SEED} if payload.get("describe") else out
        ),
    )
    assert ops.seed(["finance_ops", "--yes"]) == 1
    assert "invoice_error" in capsys.readouterr().err
    out.pop("invoice_error")
    assert ops.seed(["finance_ops", "--yes"]) == 0


def test_a_read_back_refuses_a_service_or_entity_that_is_not_an_odata_name(
    seeder, monkeypatch
):
    sap = mock.Mock()
    monkeypatch.setattr(seeder.sap_http, "system", lambda name, spec: sap)
    seed = seeder.published["finance_ops"]["pack"]["seed"]
    bad = {"service": "API_X_SRV", "entity": "A_X/../$batch", "filter": "X eq 1"}
    monkeypatch.setitem(seed, "sap_state", bad)
    key = {"SupplierInvoice": "1", "FiscalYear": "2026"}
    read = {"key": key, "fields": ["PaymentBlockingReason"]}
    assert not seeder.handler({"pack_id": "finance_ops", "read": read}, None)["ok"]
    sap.get_json.assert_not_called()


# ── only a sandbox, at run time ──


@pytest.mark.parametrize("system_role", ["production", "quality", "development", None])
@pytest.mark.parametrize(
    "event",
    [
        {"describe": True},
        {"inputs": {"posting_date": "2026-08-31"}},
        {"read": {"key": {}, "fields": []}},
    ],
)
def test_the_seeder_refuses_a_system_that_is_not_a_sandbox_before_any_sap_call(
    seeder, monkeypatch, system_role, event
):
    # A pack imported after the synth, or a seeder deployed before the key existed.
    if system_role is None:
        monkeypatch.delitem(seeder.SAP_SYSTEMS["s4"], "system_role")
    else:
        monkeypatch.setitem(seeder.SAP_SYSTEMS["s4"], "system_role", system_role)
    reached = mock.Mock(side_effect=AssertionError("reached SAP"))
    monkeypatch.setattr(seeder.sap_http, "system", reached)
    out = seeder.handler({"pack_id": "finance_ops", **event}, None)
    assert not out["ok"] and out["system"] == "s4" and out["system_role"] == system_role
    assert f"whose system role is {system_role}" in out["error"]
    assert not seeder.fake_sap.posts and not seeder.asked


def test_describe_reports_the_sandbox_after_one_get_of_the_first_steps_service(
    seeder, monkeypatch
):
    sap = mock.Mock(base_url="https://sap.example.com")
    monkeypatch.setattr(seeder.sap_http, "system", lambda name, spec: sap)
    out = seeder.handler({"pack_id": "finance_ops", "describe": True}, None)
    assert out["ok"] and (out["system"], out["system_role"]) == ("s4", "sandbox")
    (call,) = sap.get_json.call_args_list
    assert call.args == (
        f"https://sap.example.com/sap/opu/odata/sap/{SEED['steps'][0]['service']}/",
    )
    sap.get_json.side_effect = seeder.sap_http.SapError("s4: URLError")
    down = seeder.handler({"pack_id": "finance_ops", "describe": True}, None)
    assert not down["ok"] and down["error"] == "SAP did not answer: s4: URLError"
    assert not seeder.fake_sap.posts
