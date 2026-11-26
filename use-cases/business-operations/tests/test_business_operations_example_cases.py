# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""business-operations evaluations from the console: example cases and published runs.

An example case changes through `pack_composer` like any edit, as the pack's next
version, and only for the pack's authors in development. A run's results arrive by the
composer's `record_evaluation` verb, over IAM, and the Evaluations route reads both.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_business_operations_packs as tp
from test_business_operations_bundles import _composer, _packs_route, _registry, tree
from test_business_operations_packs import FakeS3

# isort: split
import example_cases

PACK = json.loads((tp.UC / "packs/finance_ops/pack.json").read_text())
EVALS = json.loads((tp.UC / "packs/finance_ops/evals.json").read_text())
__all__ = ["tree"]  # the fixture, imported for this module's tests

CASE = {
    "description": "Invoice 50,040 USD within price tolerance but over the lowered limit",
    "process_type": "price_variance",
    "seed": {"inputs": {"po_amount": 50000, "invoice_amount": 50040}},
    "expected": {
        "outcome": "approval_required",
        "reason": "Over this deployment's auto-write limit",
    },
}


# ── the rule ──


def test_every_shipped_example_case_passes_the_rule():
    assert {
        c["test_id"]: example_cases.check(c["test_id"], c, PACK) for c in EVALS
    } == {c["test_id"]: [] for c in EVALS}


def test_the_groups_are_the_tabs():
    groups = [example_cases.group(c) for c in EVALS]
    assert (
        groups.count("behaviour"),
        groups.count("boundaries"),
        groups.count("security"),
    ) == (
        6,
        15,
        8,
    )


@pytest.mark.parametrize(
    "change,error",
    [
        ({"process_type": "payroll"}, "process_type must be one this pack maps"),
        ({"expected": {"outcome": "approved", "reason": "x"}}, "expected.outcome"),
        ({"expected": {"outcome": "auto_release"}}, "expected.reason is required"),
        (
            {"expected": {"outcome": "auto_release", "reason": "x", "pass": True}},
            "not checked",
        ),
        ({"seed": {"inputs": {"vendor": "x"}}}, "seed.inputs: vendor is not an input"),
        (
            {"seed": {"inputs": {"po_amount": 0}}},
            "seed.inputs: po_amount must be at least",
        ),
        ({"seed": {"steps": []}}, "seed must be"),
        ({"description": ""}, "description is required"),
        ({"test_id": "another_one"}, "test_id in the body must be the path's"),
        ({"script": "x"}, "script is not a key"),
        ({"tags": ["x"] * 11}, "tags must be"),
    ],
)
def test_an_example_case_that_breaks_the_rule_is_refused(change, error):
    errors = example_cases.check("new_case", {**CASE, **change}, PACK)
    assert any(error in e for e in errors), errors


def test_a_put_replaces_in_place_or_adds_and_a_delete_needs_the_case():
    cases = [{"test_id": "a"}, {"test_id": "b"}]
    assert example_cases.apply(cases, "put", "a", {"x": 1}) == [
        {"test_id": "a", "x": 1},
        {"test_id": "b"},
    ]
    assert example_cases.apply(cases, "put", "c", {})[-1] == {"test_id": "c"}
    assert example_cases.apply(cases, "delete", "b", None) == [{"test_id": "a"}]
    assert example_cases.apply(cases, "delete", "z", None) is None


def test_the_price_of_a_run_comes_from_the_last_one():
    assert example_cases.estimate(29, None) == 5.8
    assert example_cases.estimate(29, {"total": 29, "cost_usd": 4.85}) == 4.85
    assert example_cases.estimate(10, {"total": 0, "cost_usd": 3}) == 2.0


# ── the composer ──


def _change(mod, op="put", test_id="lowered_limit_hold", case=CASE, **event):
    return mod.handler(
        {
            "actor": "analyst@example.com",
            "roles": ["author"],
            "pack_id": "finance_ops",
            "example_case": {"op": op, "test_id": test_id, "case": case},
            **event,
        },
        None,
    )


def _active_evals(mod) -> list[dict]:
    pointer, _ = mod._pointer("finance_ops")
    return mod._stored("finance_ops", pointer["version"])["evals"]


def test_saving_an_example_case_is_the_packs_next_version(monkeypatch, tree):
    s3 = FakeS3()
    mod = _composer(monkeypatch, s3)
    tp._seed(mod, s3, tree)
    shipped = len(_active_evals(mod))

    added = _change(mod, expected_version=1)
    assert added["version"] == 2, added
    evals = _active_evals(mod)
    assert len(evals) == shipped + 1 and evals[-1]["test_id"] == "lowered_limit_hold"
    edit = mod.handler({"verb": "versions", "pack_id": "finance_ops"}, None)
    assert edit["versions"][-1]["edited_by"] == "analyst@example.com"

    replaced = _change(mod, case={**CASE, "description": "Held over the limit"})
    assert replaced["version"] == 3
    assert _active_evals(mod)[-1]["description"] == "Held over the limit"
    assert len(_active_evals(mod)) == shipped + 1

    gone = _change(mod, op="delete", case=None)
    assert gone["version"] == 4 and len(_active_evals(mod)) == shipped
    # The rest of the pack is what it was.
    assert (
        _registry(s3)["packs"]["finance_ops"]["pack"]["display_name"]
        == PACK["display_name"]
    )


@pytest.mark.parametrize(
    "event,status",
    [
        ({"roles": []}, 403),  # not the pack's author
        ({"case": {**CASE, "process_type": "payroll"}}, 400),
        ({"test_id": "../x"}, 400),
        ({"op": "delete", "test_id": "no_such_case", "case": None}, 404),
        ({"expected_version": 7}, 409),
    ],
)
def test_the_composer_refuses_what_the_route_would(monkeypatch, tree, event, status):
    s3 = FakeS3()
    mod = _composer(monkeypatch, s3)
    tp._seed(mod, s3, tree)
    op = event.pop("op", "put")
    test_id = event.pop("test_id", "lowered_limit_hold")
    case = event.pop("case", CASE)
    refused = _change(mod, op=op, test_id=test_id, case=case, **event)
    assert refused["status"] == status, refused
    assert mod._pointer("finance_ops")[0]["version"] == 1


@pytest.mark.parametrize("role", ["quality", "production"])
def test_no_example_case_changes_outside_development(monkeypatch, tree, role):
    s3 = FakeS3()
    tp._seed(_composer(monkeypatch, s3), s3, tree)
    mod = _composer(monkeypatch, s3, role=role)
    assert _change(mod)["status"] == 403


RESULTS = {
    "run_id": "2026-11-13T10:02:11Z",
    "pack_id": "finance_ops",
    "pack_version": 1,
    "total": 2,
    "passed": 1,
    "failed": 1,
    "cost_usd": 0.35,
    "judges": {"Builtin.Helpfulness": 0.86},
    "results": {"a": {"passed": True}, "b": {"passed": False, "failures": ["x"]}},
}


@pytest.mark.parametrize("role", ["development", "quality"])
def test_a_run_is_recorded_as_itself_and_as_the_latest(monkeypatch, tree, role):
    s3 = FakeS3()
    tp._seed(_composer(monkeypatch, s3), s3, tree)
    mod = _composer(monkeypatch, s3, role=role)
    before = mod._pointer("finance_ops")[0]
    got = mod.handler(
        {
            "verb": "record_evaluation",
            "pack_id": "finance_ops",
            "results": RESULTS,
            "actor": "arn:op",
        },
        None,
    )
    assert got == {
        "pack_id": "finance_ops",
        "run_id": RESULTS["run_id"],
        "recorded": True,
    }
    latest = json.loads(s3.objects[("reg", "_evaluations/finance_ops/latest.json")])
    assert latest["passed"] == 1 and latest["recorded_by"] == "arn:op"
    assert (
        "reg",
        "_evaluations/finance_ops/runs/2026-11-13T100211Z.json",
    ) in s3.objects
    # Results are no pack's content: no version, and no corpus rebuild reads them.
    assert mod._pointer("finance_ops")[0] == before
    assert not [k for k in mod._corpus_keys() if k.startswith("_evaluations")]


@pytest.mark.parametrize(
    "event",
    [
        {"pack_id": "no_pack", "results": {**RESULTS, "pack_id": "no_pack"}},
        {"pack_id": "finance_ops", "results": {**RESULTS, "pack_id": "other"}},
        {"pack_id": "finance_ops", "results": {**RESULTS, "total": "2"}},
        {"pack_id": "finance_ops", "results": {**RESULTS, "run_id": "../x"}},
        {"pack_id": "finance_ops", "results": []},
    ],
)
def test_a_record_that_is_not_a_runs_results_is_refused(monkeypatch, tree, event):
    s3 = FakeS3()
    mod = _composer(monkeypatch, s3)
    tp._seed(mod, s3, tree)
    got = mod.handler({"verb": "record_evaluation", **event}, None)
    assert got["status"] in (400, 404)
    assert not [k for k in s3.objects if k[1].startswith("_evaluations/")]


# ── the route ──


PUBLISHED = {
    "schema": "pack-registry/1",
    # Open to every user of the use case, so the groups below are the whole question.
    "packs": {
        "finance_ops": {
            "pack": {
                **{k: v for k, v in PACK.items() if k != "entitlement"},
                "pack_id": "finance_ops",
            }
        }
    },
}


def _route(monkeypatch, role="development"):
    mod, sent = _packs_route(monkeypatch, role, PUBLISHED)
    aws = mod.s3
    bundle = {"pack_id": "finance_ops", "version": 3, "evals": EVALS}
    aws.objects[("reg", "_bundles/finance_ops/active.json")] = json.dumps(
        {"version": 3, "sha256": "s3"}
    ).encode()
    aws.objects[("reg", "_bundles/finance_ops/3.json")] = json.dumps(bundle).encode()
    return mod, sent, aws


def _call(mod, method, test_id=None, body=None, groups=(tp.USERS, tp.AUTHORS)):
    resource = "/packs/{pack_id}/evaluations" + ("/{test_id}" if test_id else "")
    event = tp._event(method, resource, groups=groups, body=body)
    event["pathParameters"] = {
        "pack_id": "finance_ops",
        **({"test_id": test_id} if test_id else {}),
    }
    response = mod.handler(event, None)
    return response["statusCode"], json.loads(response["body"])


def test_the_evaluations_part_lists_the_active_cases_grouped_and_priced(monkeypatch):
    mod, _, aws = _route(monkeypatch)
    status, body = _call(mod, "GET", groups=(tp.USERS,))
    assert status == 200 and body["version"] == 3
    assert len(body["example_cases"]) == len(EVALS)
    assert {c["group"] for c in body["example_cases"]} == {
        "behaviour",
        "boundaries",
        "security",
    }
    assert body["last_run"] is None and body["estimate_usd"] == 5.8
    # A viewer reads the part; only an author may change it.
    assert (body["may_edit"], body["may_run"]) == (False, False)
    assert "ops.py evals-run --pack finance_ops" in body["run_refusal"]

    aws.objects[("reg", "_evaluations/finance_ops/latest.json")] = json.dumps(
        {**RESULTS, "total": 29, "cost_usd": 4.85}
    ).encode()
    _, body = _call(mod, "GET")
    assert body["last_run"]["passed"] == 1 and body["estimate_usd"] == 4.85
    assert body["may_edit"] is True


def test_a_caller_who_cannot_see_the_pack_reads_none_of_it(monkeypatch):
    mod, _, _ = _route(monkeypatch)
    monkeypatch.setenv(
        "ENTITLEMENT_MAP", json.dumps({"finance_ops": {"groups": ["x"]}})
    )
    assert _call(mod, "GET")[0] == 403


def test_saving_an_example_case_goes_to_the_composer_as_the_authors_edit(monkeypatch):
    mod, sent, _ = _route(monkeypatch)
    status, _ = _call(
        mod, "PUT", "lowered_limit_hold", {"case": CASE, "expected_version": 3}
    )
    assert status == 200
    (payload,) = sent
    assert payload["example_case"] == {
        "op": "put",
        "test_id": "lowered_limit_hold",
        "case": CASE,
    }
    assert payload["expected_version"] == 3 and "author" in payload["roles"]
    assert payload["actor"] == "a@example.com"
    status, _ = _call(mod, "DELETE", "lowered_limit_hold", {})
    assert status == 200 and sent[-1]["example_case"]["op"] == "delete"


@pytest.mark.parametrize(
    "groups,body,status",
    [
        ((tp.USERS,), {"case": CASE}, 403),  # not an author
        ((tp.USERS, tp.AUTHORS), {"case": {**CASE, "process_type": "payroll"}}, 400),
        ((tp.USERS, tp.AUTHORS), {}, 400),  # no case
    ],
)
def test_the_route_refuses_before_the_composer(monkeypatch, groups, body, status):
    mod, sent, _ = _route(monkeypatch)
    got, reply = _call(mod, "PUT", "lowered_limit_hold", body, groups=groups)
    assert got == status and not sent
    if status == 400 and "case" in body:
        assert any("process_type" in d for d in reply["details"])


@pytest.mark.parametrize("role", ["quality", "production"])
def test_the_route_changes_no_example_case_outside_development(monkeypatch, role):
    mod, sent, _ = _route(monkeypatch, role)
    assert _call(mod, "PUT", "lowered_limit_hold", {"case": CASE})[0] == 403
    assert not sent


def test_an_example_case_id_is_checked_before_anything_else(monkeypatch):
    mod, sent, _ = _route(monkeypatch)
    assert _call(mod, "PUT", "X-1", {"case": CASE})[0] == 400 and not sent


# ── evals-run --publish ──


def test_a_run_is_published_per_pack_with_each_cases_verdict():
    spec = importlib.util.spec_from_file_location(
        "bo_run_regression_publish", tp.UC / "evals/run_regression.py"
    )
    regression = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(regression)
    results = [
        {
            "test_id": "a",
            "pack_id": "finance_ops",
            "assertions_passed": True,
            "estimated_cost_usd": 0.2,
            "wall_s": 40,
            "status": "sap_updated",
            "evals": [{"evaluator": "Builtin.Helpfulness", "score": 0.9}],
            "judged": True,
        },
        {
            "test_id": "b",
            "pack_id": "finance_ops",
            "assertions_passed": False,
            "failures": ["outcome: expected auto_release"],
            "estimated_cost_usd": 0.15,
            "wall_s": 50,
        },
        {"test_id": "c", "pack_id": "finance_ops", "skipped": True},
    ]
    (record,) = regression.publishable(
        results, {"finance_ops": {"version": 14}}, "2026-11-13T10:02:11Z"
    ).values()
    assert not example_cases.check_results("finance_ops", record)
    assert (record["total"], record["passed"], record["failed"]) == (2, 1, 1)
    assert record["pack_version"] == 14 and record["cost_usd"] == 0.35
    assert record["results"]["a"]["judges"] == {"Builtin.Helpfulness": 0.9}
    assert record["results"]["b"]["failures"] == ["outcome: expected auto_release"]
    assert "c" not in record["results"]


# ── GET /packs/{pack_id} ──


def _document(mod, pack_id="finance_ops", groups=(tp.USERS,)):
    event = tp._event("GET", "/packs/{pack_id}", groups=groups)
    event["pathParameters"] = {"pack_id": pack_id}
    response = mod.handler(event, None)
    return response["statusCode"], json.loads(response["body"])


def test_the_pack_is_served_with_the_part_of_each_key(monkeypatch):
    import pack_tiers

    mod, _, _ = _route(monkeypatch)
    status, body = _document(mod)
    assert status == 200 and body["version"] == 3
    document = PUBLISHED["packs"]["finance_ops"]["pack"]
    assert body["pack"] == document
    # One part per key the document carries, from the one map.
    assert set(body["parts"]) == set(document) - {"$schema"}
    assert body["parts"]["source"] == "intake"
    assert body["parts"]["process_type_to_sop"] == "procedures"
    assert body["parts"]["constants"] == "procedures"
    assert body["parts"]["write_guards"] == "controls"
    assert body["parts"]["gateway_tools"] == "agent_configuration"
    assert body["parts"]["seed"] == "evaluations"
    assert set(body["parts"].values()) <= set(pack_tiers.PART_NAMES)
    assert body["constant_parts"] == {
        "AUTO_WRITE_LIMIT_USD": "controls",
        "DUAL_APPROVAL_LIMIT_USD": "controls",
    }
    assert body["access"]["see"] is True


def test_the_pack_is_served_only_to_who_may_see_it(monkeypatch):
    mod, _, _ = _route(monkeypatch)
    assert _document(mod, "no_such_pack")[0] == 404
    monkeypatch.setenv(
        "ENTITLEMENT_MAP", json.dumps({"finance_ops": {"groups": ["x"]}})
    )
    assert _document(mod)[0] == 403
