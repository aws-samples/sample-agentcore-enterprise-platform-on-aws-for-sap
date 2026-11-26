# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Trigger mode per pack: an unattended producer queues a case only when the deployment's
trigger mode and the case's pack both say `auto`, and an absent pack mode is `manual`."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

UC = Path(__file__).resolve().parents[3] / "use-cases" / "business-operations"
for p in (UC / "pipeline", UC / "api" / "layer" / "python"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import batch_runner
import case_store


class _Ssm:
    """`get_parameter` over a dict; a missing name raises, as SSM's NotFound does."""

    def __init__(self, values):
        self.values, self.reads = dict(values), []

    def get_parameter(self, Name):
        self.reads.append(Name)
        if Name not in self.values:
            raise RuntimeError("ParameterNotFound")
        return {"Parameter": {"Value": self.values[Name]}}


SWITCH = "/p/dev/use-cases/business-operations/autonomy/trigger-mode"
PACKS = "/p/dev/use-cases/business-operations/autonomy/packs"


@pytest.fixture
def ssm(monkeypatch):
    def make(values):
        fake = _Ssm(values)
        monkeypatch.setattr(case_store, "_ssm", fake)
        monkeypatch.setattr(case_store, "TRIGGER_MODE_PARAMETER", SWITCH)
        monkeypatch.setattr(case_store, "PACK_MODE_PREFIX", PACKS)
        return fake

    return make


@pytest.mark.parametrize(
    "switch,pack,allowed",
    [
        ("auto", "auto", True),
        ("auto", "manual", False),
        ("auto", None, False),  # a pack nobody set, a new one included
        ("manual", "auto", False),  # containment and break glass stop every pack
        (None, "auto", False),
    ],
)
def test_a_case_is_queued_only_when_both_say_auto(ssm, switch, pack, allowed):
    values = {}
    if switch:
        values[SWITCH] = switch
    if pack:
        values[f"{PACKS}/ap"] = pack
    ssm(values)
    assert case_store.Gate().allows("ap") is allowed


def test_the_gate_reads_each_pack_once_and_no_pack_under_a_manual_switch(ssm):
    fake = ssm({SWITCH: "auto", f"{PACKS}/ap": "auto"})
    gate = case_store.Gate()
    assert gate.allows("ap") and gate.allows("ap") and not gate.allows("gl")
    assert fake.reads == [SWITCH, f"{PACKS}/ap", f"{PACKS}/gl"]
    fake = ssm({SWITCH: "manual", f"{PACKS}/ap": "auto"})
    assert not case_store.Gate().allows("ap")
    assert fake.reads == [SWITCH]


@pytest.mark.parametrize("pack_id", ["", "AP", "a/b", "../trigger-mode", None])
def test_a_name_that_is_not_a_pack_id_reads_nothing_and_is_manual(ssm, pack_id):
    fake = ssm({SWITCH: "auto"})
    assert case_store.get_pack_mode(pack_id) == "manual"
    assert fake.reads == []


def test_enqueue_sends_only_through_an_open_gate(ssm, monkeypatch):
    ssm({SWITCH: "auto", f"{PACKS}/ap": "auto"})
    sent = []
    monkeypatch.setattr(case_store, "QUEUE_URL", "q")
    monkeypatch.setattr(case_store, "send", lambda c, t: sent.append((c, t)))
    gate = case_store.Gate()
    assert case_store.enqueue("ap-1", "ap", "poller", gate)
    assert not case_store.enqueue("gl-1", "gl", "poller", gate)
    assert sent == [("ap-1", "poller")]


def test_the_sweep_skips_the_cases_of_a_manual_pack(ssm, monkeypatch):
    ssm({SWITCH: "auto", f"{PACKS}/ap": "auto", f"{PACKS}/gl": "manual"})
    cases = [
        {"case_id": "ap-aaaaaaaaaaaaaaaaaaaaaaaaaa", "pack_id": "ap"},
        {"case_id": "gl-bbbbbbbbbbbbbbbbbbbbbbbbbb", "pack_id": "gl"},
    ]
    sent = []
    monkeypatch.setattr(batch_runner, "QUEUE_URL", "q")
    monkeypatch.setattr(batch_runner, "get_cases_table", lambda: None)
    monkeypatch.setattr(batch_runner, "stale_detected_cases", lambda _t, _c: cases)
    monkeypatch.setattr(batch_runner, "send", lambda c, t: sent.append(c))
    monkeypatch.setattr(batch_runner, "WARM_DELAY_SECONDS", 0)
    assert batch_runner.handler({}, None) == {"swept": 2, "enqueued": 1}
    assert sent == ["ap-aaaaaaaaaaaaaaaaaaaaaaaaaa"]


def test_the_sweep_is_a_no_op_under_a_manual_switch(ssm, monkeypatch):
    fake = ssm({SWITCH: "manual", f"{PACKS}/ap": "auto"})
    monkeypatch.setattr(batch_runner, "QUEUE_URL", "q")
    assert batch_runner.handler({}, None)["skipped"] == "manual"
    assert fake.reads == [SWITCH]


# ── ops.py trigger-mode ──


class _OpsSsm:
    class exceptions:
        class ParameterNotFound(Exception):
            pass

    def __init__(self, values):
        self.values, self.puts = dict(values), []

    def get_parameter(self, Name):
        if Name not in self.values:
            raise self.exceptions.ParameterNotFound(Name)
        return {"Parameter": {"Value": self.values[Name]}}

    def get_paginator(self, _name):
        values = self.values

        class _P:
            def paginate(self, Path):
                yield {
                    "Parameters": [
                        {"Name": k, "Value": v}
                        for k, v in values.items()
                        if k.startswith(Path + "/")
                    ]
                }

        return _P()

    def put_parameter(self, **kw):
        self.puts.append(kw)
        self.values[kw["Name"]] = kw["Value"]


def _ops():
    import importlib.util

    if str(UC) not in sys.path:
        sys.path.insert(0, str(UC))
    spec = importlib.util.spec_from_file_location("bo_ops_trigger_mode", UC / "ops.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PARAMS = {
    "prefix": "/p/dev/use-cases/business-operations/autonomy",
    "switch": SWITCH,
    "packs": PACKS,
    "pack_actors": "/p/dev/use-cases/business-operations/autonomy/pack-actors",
}


def _run(ssm, *args, checkout=("finance_ops",)):
    return _ops().trigger_mode_with(
        ssm,
        PARAMS,
        list(args),
        lambda: "arn:aws:sts::1:assumed-role/Admin/ana",
        list(checkout),
    )


def test_get_lists_each_pack_and_warns_when_none_runs(capsys):
    assert _run(_OpsSsm({SWITCH: "auto"}), "get") == 0
    out = capsys.readouterr().out
    assert "finance_ops = manual (unset); effective manual" in out
    assert "WARN: the deployment's trigger mode is auto but no pack is" in out
    ssm = _OpsSsm(
        {SWITCH: "auto", f"{PACKS}/finance_ops": "auto", f"{PACKS}/gl": "manual"}
    )
    assert _run(ssm) == 0
    out = capsys.readouterr().out
    assert "finance_ops = auto; effective auto" in out
    assert "gl = manual; effective manual" in out and "WARN" not in out


def test_set_pack_writes_its_mode_then_who_set_it():
    ssm = _OpsSsm({})
    assert _run(ssm, "set", "auto", "--pack", "finance_ops", "--yes") == 0
    mode, actor = ssm.puts
    assert mode["Name"] == f"{PACKS}/finance_ops" and mode["Value"] == "auto"
    assert mode["Description"] == "arn:aws:sts::1:assumed-role/Admin/ana"
    assert actor["Name"] == f"{PARAMS['pack_actors']}/finance_ops"
    assert actor["Value"].startswith(
        "auto set by arn:aws:sts::1:assumed-role/Admin/ana at "
    )


@pytest.mark.parametrize(
    "args",
    [
        ("set", "auto", "--pack", "../trigger-mode", "--yes"),
        ("set", "auto", "--pack", "unknown_pack", "--yes"),
        ("set", "sometimes", "--pack", "finance_ops"),
        ("set", "auto", "--pack"),
    ],
)
def test_set_pack_refuses_what_is_not_a_known_pack_and_mode(args):
    ssm = _OpsSsm({})
    assert _run(ssm, *args) == 2
    assert not ssm.puts


def test_set_without_pack_still_sets_the_switch():
    ssm = _OpsSsm({})
    assert _run(ssm, "set", "auto", "--yes") == 0
    assert ssm.puts[0]["Name"] == SWITCH


def test_the_pack_mode_grants_follow_the_parameters_the_code_reads():
    """The producers read `packs/<pack_id>` and the route also writes `pack-actors/`,
    so the deployment's grants must reach both (a 403 from SSM reads as `manual`)."""
    if str(UC) not in sys.path:
        sys.path.insert(0, str(UC))
    from business_operations import names, operator_policy

    params = names.trigger_mode_parameters("/p/dev")
    assert params == {
        **PARAMS,
        "stop": "/p/dev/use-cases/business-operations/autonomy/emergency-stop",
    }
    policy = operator_policy.render(
        "trigger-mode", region="r", account="1", project="p", env="dev", values={}
    )
    listed = [
        s for s in policy["Statement"] if "ssm:GetParametersByPath" in s["Action"]
    ]
    assert listed[0]["Resource"] == [f"arn:aws:ssm:r:1:parameter{PACKS}"]


def test_the_producers_and_the_route_are_granted_the_pack_modes():
    import json

    from app_synth import synth_app

    repo = UC.parents[1]
    result, out = synth_app(
        repo
        / "use-cases/business-operations/fixtures/contract/business-operations-production.yaml"
    )
    assert result.returncode == 0, result.stderr[-2000:]
    resources = json.loads(
        (
            out / "agentcore-production-prod-uc-business-operations.template.json"
        ).read_text()
    )["Resources"]
    packs = "agentcore-production/prod/use-cases/business-operations/autonomy/packs/*"

    def granted(statement) -> list[str]:
        found = statement.get("Resource")
        found = found if isinstance(found, list) else [found]
        return [json.dumps(r) for r in found]

    switch = packs.replace("packs/*", "trigger-mode")
    # Every producer that reads the switch reads the pack modes in the same statement.
    readers = {
        k: any(packs in g for g in granted(st))
        for k, r in resources.items()
        if r["Type"] == "AWS::IAM::Policy"
        for st in r["Properties"]["PolicyDocument"]["Statement"]
        if st.get("Action") == "ssm:GetParameter"
        and any(switch in g for g in granted(st))
    }
    assert len(readers) >= 2 and all(readers.values()), readers  # poller, sweeper
    envs = [
        r["Properties"]["Environment"]["Variables"]
        for r in resources.values()
        if r["Type"] == "AWS::Lambda::Function"
        and "PACK_MODE_PREFIX"
        in (r["Properties"].get("Environment") or {}).get("Variables", {})
    ]
    assert len(envs) >= 4  # three producers and the trigger-mode route
    assert all(v["PACK_MODE_PREFIX"].endswith("/autonomy/packs") for v in envs)


def test_ops_refuses_auto_while_a_stop_stands_and_allows_manual(capsys):
    import json

    params = {**PARAMS, "stop": f"{PARAMS['prefix']}/emergency-stop"}

    def run(ssm, *args):
        return _ops().trigger_mode_with(
            ssm, params, list(args), lambda: "arn:x", ["finance_ops"]
        )

    stop = json.dumps({"stands": True, "reason": "SAP looks wrong", "stopped_by": "op"})
    ssm = _OpsSsm({params["stop"]: stop})
    assert run(ssm, "set", "auto", "--pack", "finance_ops", "--yes") == 1
    assert run(ssm, "set", "auto", "--yes") == 1
    assert "an emergency stop stands: SAP looks wrong" in capsys.readouterr().err
    assert not ssm.puts
    ssm.values[SWITCH] = "auto"
    assert run(ssm, "set", "manual") == 0 and ssm.puts[0]["Value"] == "manual"
    ssm = _OpsSsm({params["stop"]: json.dumps({"stands": False})})
    assert run(ssm, "set", "auto", "--yes") == 0
