# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""business-operations: `admin_email` makes a first console user with the stack."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from app_synth import synth_app

REPO = Path(__file__).resolve().parents[3]
FIXTURE = (
    REPO / "use-cases/business-operations/fixtures/contract/business-operations.yaml"
)
MAIN = "agentcore-workshop-dev-uc-business-operations"


def _resources(admin_email: str | None) -> dict:
    config = yaml.safe_load(FIXTURE.read_text())
    if admin_email:
        config["use_cases"]["business-operations"]["admin_email"] = admin_email
    result, out = synth_app(yaml.safe_dump(config))
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads((out / f"{MAIN}.template.json").read_text())["Resources"]


def _of(resources: dict, kind: str) -> list[dict]:
    return [r for r in resources.values() if r["Type"] == kind]


def test_no_admin_email_makes_no_user():
    assert not _of(_resources(None), "AWS::Cognito::UserPoolUser")


def test_admin_email_makes_a_verified_user_who_is_mailed_and_in_every_group():
    resources = _resources("admin@example.com")
    (user,) = _of(resources, "AWS::Cognito::UserPoolUser")
    props = user["Properties"]
    assert props["Username"] == "admin@example.com"
    assert props["DesiredDeliveryMediums"] == ["EMAIL"]
    assert {"Name": "email_verified", "Value": "true"} in props["UserAttributes"]
    groups = {
        g["Properties"]["GroupName"]
        for g in _of(resources, "AWS::Cognito::UserPoolGroup")
    } - {"business-operations-pack-admins"}
    joined = {
        a["Properties"]["GroupName"]
        for a in _of(resources, "AWS::Cognito::UserPoolUserToGroupAttachment")
    }
    assert groups and joined == groups
