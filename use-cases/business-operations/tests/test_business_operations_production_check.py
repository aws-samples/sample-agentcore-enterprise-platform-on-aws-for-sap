# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""`ops.py production-check`: each row passes on a production config, fails on the
setting it watches, and the verb exits 1 on any FAIL."""

from __future__ import annotations

import copy
import json
import sys
from unittest import mock

import yaml
from test_business_operations_api import REPO, UC

sys.path.insert(0, str(UC))

from business_operations import audit_alerts, packs
from business_operations import production_check as pc

FIXTURES = REPO / "use-cases" / "business-operations" / "fixtures" / "contract"
UC_KEY = "business-operations"


def _raw(name: str) -> dict:
    return yaml.safe_load((FIXTURES / name).read_text())


def _ready() -> dict:
    """The production fixture, with every use-case row this check reads made right."""
    raw = _raw("business-operations-production.yaml")
    raw["observability"]["log_retention_days"] = 365
    contacts = {k: f"{k}@corp.test" for k in packs.unconfigured_contacts(_uc({}))}
    raw["use_cases"][UC_KEY] = {
        "api": {"sample_ticketing": False},
        "contacts": contacts,
    }
    return raw


def _uc(block: dict):
    from business_operations import config

    return config.load(block)


def _by_check(raw: dict) -> dict[str, pc.Row]:
    return {r.check: r for r in pc.config_rows(raw)}


def test_a_production_config_has_no_failing_row():
    rows = _by_check(_ready())
    assert {k: r.status for k, r in rows.items() if r.status != pc.PASS} == {
        # Phase 1 signs nothing, so a production role always fails this row.
        "bundle signing trust": pc.FAIL,
        "AWS for SAP MCP Server TLS": pc.WARN,
    }


def test_a_workshop_config_lists_the_platform_controls_it_lacks():
    row = _by_check(_raw("business-operations.yaml"))["deployment.mode"]
    assert row.status == pc.FAIL and "security.egress_filter" in row.detail
    assert pc.platform_gaps(_ready()) == []


def test_a_remote_client_is_one_gap_and_hides_no_other():
    raw = _raw("business-operations.yaml")
    raw.setdefault("gateway", {})["people"] = {
        "enabled": True,
        "remote_clients": [{"name": "quick", "regions": ["us-east-1"]}],
    }
    gaps = pc.platform_gaps(raw)
    assert gaps[-1].startswith("gateway.people quick: set allow_in_production: true")
    assert any(g.startswith("security.egress_filter") for g in gaps)
    assert not any("pydantic" in g or "validation error" in g for g in gaps)
    ready = _ready()
    ready.setdefault("gateway", {})["people"] = {
        "enabled": True,
        "remote_clients": [
            {"name": "quick", "regions": ["us-east-1"], "allow_in_production": True}
        ],
    }
    assert pc.platform_gaps(ready) == []


def test_each_setting_fails_its_own_row():
    cases = {
        "identity.self_sign_up": lambda r: r["identity"].update(self_sign_up=True),
        "api.sample_ticketing": lambda r: r["use_cases"][UC_KEY]["api"].update(
            sample_ticketing=True
        ),
        "test_data.enabled": lambda r: r["use_cases"][UC_KEY].update(
            test_data={"enabled": True}
        ),
        "observability.log_retention_days": lambda r: r["observability"].update(
            log_retention_days=30
        ),
        "contacts": lambda r: r["use_cases"][UC_KEY].update(contacts={}),
        "promotion.role": lambda r: r["use_cases"][UC_KEY].update(
            promotion={"role": "development"}
        ),
    }
    for check, mutate in cases.items():
        raw = copy.deepcopy(_ready())
        mutate(raw)
        assert _by_check(raw)[check].status == pc.FAIL, check
    # Production refuses Cedar short of ENFORCE at load, so the row matters elsewhere.
    raw = _raw("business-operations.yaml")
    raw["security"] = {"cedar": {"enabled": True, "mode": "LOG_ONLY"}}
    assert _by_check(raw)["security.cedar"].status == pc.FAIL


def test_the_egress_guardrail_must_be_on_and_block_in_production():
    rows = _by_check(_ready())
    assert rows["security.egress_filter"].status == pc.PASS
    assert rows["security.egress_request_action"].status == pc.PASS
    # The workshop config has the filter off and masks by default.
    workshop = _by_check(_raw("business-operations.yaml"))
    assert workshop["security.egress_filter"].status == pc.FAIL
    assert workshop["security.egress_request_action"].status == pc.FAIL
    raw = _raw("business-operations.yaml")
    raw["security"] = {"egress_filter": True, "egress_request_action": "mask"}
    rows = _by_check(raw)
    assert rows["security.egress_filter"].status == pc.PASS
    assert rows["security.egress_request_action"].status == pc.FAIL
    raw["security"]["egress_request_action"] = "block"
    assert _by_check(raw)["security.egress_request_action"].status == pc.PASS


def test_warnings_that_do_not_block():
    raw = copy.deepcopy(_ready())
    raw["observability"]["log_retention_days"] = 180
    first = next(iter(raw["use_cases"][UC_KEY]["contacts"]))
    raw["use_cases"][UC_KEY]["contacts"][first] = "someone@example.com"
    rows = _by_check(raw)
    assert rows["observability.log_retention_days"].status == pc.WARN
    assert rows["contacts"].status == pc.WARN
    # Outside a production role the missing signature is a warning, not a block.
    raw = _raw("business-operations.yaml")
    assert _by_check(raw)["bundle signing trust"].status == pc.WARN
    raw["use_cases"][UC_KEY] = {"promotion": {"role": "quality"}}
    assert _by_check(raw)["bundle signing trust"].status == pc.WARN


def test_audit_alarm_row_reads_eventbridge_and_never_raises():
    def client(patterns):
        events = mock.Mock()
        events.get_paginator.return_value.paginate.return_value = [
            {"Rules": [{"EventPattern": p} for p in patterns]}
        ]
        return events

    param = pc.trigger_mode_parameter("p", "e")
    other = pc.trigger_mode_parameter("p", "other")
    with mock.patch("boto3.client", return_value=client(['{"source":["aws.s3"]}'])):
        assert pc.audit_alarm_row("us-east-1", param).status == pc.WARN
    # Another deployment's rule in the same Region is not this one's.
    watched = json.dumps(audit_alerts.trigger_mode_pattern(other))
    with mock.patch("boto3.client", return_value=client([watched])):
        assert pc.audit_alarm_row("us-east-1", param).status == pc.WARN
    watched = json.dumps(audit_alerts.trigger_mode_pattern(param))
    with mock.patch("boto3.client", return_value=client([watched])):
        assert pc.audit_alarm_row("us-east-1", param).status == pc.PASS
    with mock.patch("boto3.client", side_effect=RuntimeError("no credentials")):
        row = pc.audit_alarm_row("us-east-1", param)
    assert row.status == pc.WARN and "no credentials" in row.detail


def test_the_verb_exits_on_its_rows(tmp_path, monkeypatch, capsys):
    from test_business_operations_test_data import _ops

    ops = _ops()
    assert ops.main(["production-check", "--bogus"]) == 2
    path = tmp_path / "platform.yaml"
    path.write_text(yaml.safe_dump(_raw("business-operations.yaml")))
    monkeypatch.setenv("PLATFORM_CONFIG", str(path))
    assert ops.main(["production-check", "--offline"]) == 1
    assert capsys.readouterr().out.splitlines()[-1].startswith("FAIL: ")
    assert pc.report([pc.Row(pc.PASS, "a", "b"), pc.Row(pc.WARN, "c", "d")]) == 0
    monkeypatch.setenv("PLATFORM_CONFIG", str(tmp_path / "absent.yaml"))
    assert ops.main(["production-check", "--offline"]) == 1
