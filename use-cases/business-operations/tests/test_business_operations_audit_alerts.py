# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""business-operations: the audit-change alerts (a trigger-mode change, a registry.json
write, a SOP write by anyone but its writers), as synthesized and as the handler
words them."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest import mock

import pytest
from app_synth import synth_app

REPO = Path(__file__).resolve().parents[3]
UC = REPO / "use-cases" / "business-operations"
sys.path.insert(0, str(UC))

MAIN = "agentcore-workshop-dev-uc-business-operations"
PARAM = "/agentcore-workshop/dev/use-cases/business-operations/autonomy/trigger-mode"
COMPOSER, SOP_API = "AROACOMPOSER", "AROASOPAPI"


@pytest.fixture
def alert(monkeypatch):
    monkeypatch.setenv("SOPS_BUCKET", "sops-b")
    monkeypatch.setenv("SOP_WRITER_ROLE_IDS", f"{COMPOSER},{SOP_API}")
    monkeypatch.setenv("TOPIC_ARN", "arn:aws:sns:us-east-1:1:ops")
    with mock.patch("boto3.client"):
        spec = importlib.util.spec_from_file_location(
            "bo_audit_alert", UC / "audit_alert" / "index.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


def _s3(bucket: str, key: str, who: str, name="ObjectCreated:Put") -> dict:
    return {
        "eventName": name,
        "eventTime": "2026-11-04T00:00:00Z",
        "userIdentity": {"principalId": who},
        "s3": {"bucket": {"name": bucket}, "object": {"key": key}},
    }


def test_a_flip_names_the_parameter_and_the_principal_never_the_value(alert):
    detail = {
        "eventName": "PutParameter",
        "eventTime": "t",
        "userIdentity": {"arn": "arn:aws:sts::1:assumed-role/Admin/someone"},
        "requestParameters": {"name": PARAM, "value": "manual", "overwrite": True},
    }
    alert.handler({"detail": detail}, None)
    ((_, kw),) = alert._sns.publish.call_args_list
    assert PARAM in kw["Message"] and "assumed-role/Admin/someone" in kw["Message"]
    assert "manual" not in kw["Message"]


def test_a_pack_mode_flip_is_named_as_one(alert):
    name = PARAM.replace("trigger-mode", "packs/finance_ops")
    detail = {
        "eventName": "PutParameter",
        "eventTime": "t",
        "userIdentity": {"arn": "arn:aws:sts::1:assumed-role/Admin/someone"},
        "requestParameters": {"name": name, "value": "auto"},
    }
    ((subject, message),) = alert.alerts({"detail": detail})
    assert subject == "pack trigger mode changed" and name in message
    assert "auto" not in message.replace("autonomy", "")


def test_a_sop_write_by_its_own_writers_is_quiet_and_anyone_elses_alerts(alert):
    records = [
        _s3("sops-b", "ap/a.txt", f"AWS:{COMPOSER}:composer-fn"),
        _s3("sops-b", "ap/b.txt", f"AWS:{SOP_API}:sops-fn"),
        # A session name can be chosen; the role id cannot.
        _s3(
            "sops-b", "ap/c+d.txt", "AWS:AROAADMIN:composer-fn", "ObjectRemoved:Delete"
        ),
    ]
    ((subject, message),) = alert.alerts({"Records": records})
    assert "outside the pack composer" in subject
    assert "s3://sops-b/ap/c d.txt" in message and "AWS:AROAADMIN" in message


def test_every_registry_write_alerts_whoever_wrote_it(alert):
    found = alert.alerts(
        {"Records": [_s3("registry-b", "registry.json", f"AWS:{COMPOSER}:fn")]}
    )
    assert [s for s, _ in found] == ["pack registry written"]


@pytest.fixture(scope="module")
def resources() -> dict:
    result, out = synth_app(
        REPO
        / "use-cases/business-operations/fixtures/contract/business-operations.yaml"
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads((out / f"{MAIN}.template.json").read_text())["Resources"]


def test_the_rule_watches_this_deployments_switch_and_nothing_beside_it(resources):
    (rule,) = [
        r
        for r in resources.values()
        if r["Type"] == "AWS::Events::Rule"
        and "trigger mode" in r["Properties"].get("Description", "")
    ]
    detail = rule["Properties"]["EventPattern"]["detail"]
    # The switch, the emergency stop, and every pack's mode; never the actor records
    # beside them.
    names = [
        PARAM,
        PARAM.replace("trigger-mode", "emergency-stop"),
        {"prefix": PARAM.replace("trigger-mode", "packs/")},
    ]
    assert {"requestParameters": {"name": names}} in detail["$or"]
    assert {"requestParameters": {"names": names}} in detail["$or"]
    assert "DeleteParameter" in detail["eventName"]


def test_both_buckets_notify_the_alert_and_the_registry_only_for_its_index(resources):
    configs = [
        c
        for r in resources.values()
        if r["Type"] == "Custom::S3BucketNotifications"
        for c in r["Properties"]["NotificationConfiguration"][
            "LambdaFunctionConfigurations"
        ]
    ]
    events = sorted(c["Events"][0] for c in configs)
    assert events == ["s3:ObjectCreated:*"] * 2 + ["s3:ObjectRemoved:*"] * 2
    filtered = [c for c in configs if "Filter" in c]
    assert len(filtered) == 2
    for c in filtered:
        assert c["Filter"]["Key"]["FilterRules"] == [
            {"Name": "prefix", "Value": "registry.json"}
        ]


def test_a_bucket_policy_change_names_the_bucket_and_the_principal(alert):
    detail = {
        "eventSource": "s3.amazonaws.com",
        "eventName": "DeleteBucketPolicy",
        "eventTime": "t",
        "userIdentity": {"arn": "arn:aws:sts::1:assumed-role/Admin/someone"},
        "requestParameters": {"bucketName": "sops-b", "policy": "{...}"},
    }
    ((subject, message),) = alert.alerts({"detail": detail})
    assert subject == "pack bucket policy changed"
    assert "DeleteBucketPolicy on s3://sops-b" in message
    assert "assumed-role/Admin/someone" in message and "{...}" not in message


def test_the_policy_rule_watches_both_pack_buckets_and_only_them(resources):
    (rule,) = [
        r
        for r in resources.values()
        if r["Type"] == "AWS::Events::Rule"
        and "bucket's policy" in r["Properties"].get("Description", "")
    ]
    detail = rule["Properties"]["EventPattern"]["detail"]
    assert detail["eventName"] == ["PutBucketPolicy", "DeleteBucketPolicy"]
    buckets = detail["requestParameters"]["bucketName"]
    refs = sorted(b["Ref"] for b in buckets)
    assert len(refs) == 2
    assert all(resources[ref]["Type"] == "AWS::S3::Bucket" for ref in refs)
    assert any("Sop" in ref for ref in refs) and any("Registry" in ref for ref in refs)
