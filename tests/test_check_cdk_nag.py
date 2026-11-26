# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""scripts/check_cdk_nag.py: what a suppression entry may hide.

The scan itself needs Node and cdk-nag and runs in CI's cdk-nag job; these pin the
matching rules, which decide whether a new finding is hidden or fails the gate."""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_cdk_nag.py"
_spec = importlib.util.spec_from_file_location("check_cdk_nag", SCRIPT)
nag = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nag)

T = "agentcore-production-prod-uc-business-operations.template.json"


def _entries(tmp_path, body: str) -> list[dict]:
    path = tmp_path / ".cdk-nag.yaml"
    path.write_text(body)
    return nag.load_config(path)


def _policy(*statements) -> dict:
    return {
        "Type": "AWS::IAM::Policy",
        "Properties": {"PolicyDocument": {"Statement": list(statements)}},
    }


def test_the_repo_config_loads():
    for path in nag.default_configs():
        assert nag.load_config(path), path


def test_each_use_case_brings_its_own_suppressions(tmp_path):
    (tmp_path / ".cdk-nag.yaml").write_text("suppressions: []\n")
    own = tmp_path / "use-cases" / "demo" / ".cdk-nag.yaml"
    own.parent.mkdir(parents=True)
    own.write_text("suppressions: []\n")
    assert nag.default_configs(tmp_path) == [tmp_path / ".cdk-nag.yaml", own]
    own.unlink()
    assert nag.default_configs(tmp_path) == [tmp_path / ".cdk-nag.yaml"]


@pytest.mark.parametrize(
    "body",
    [
        "suppressions: [{rule: AwsSolutions-L1, stacks: [auth], reason: r}]",  # no expiration
        "suppressions: [{rule: AwsSolutions-L1, stacks: ['*'], reason: r, expiration: '2099-01-01'}]",
        "suppressions: [{rule: 'AwsSolutions-*', stacks: [auth], reason: r, expiration: '2099-01-01'}]",
    ],
)
def test_an_unscoped_or_undated_entry_is_refused(tmp_path, body):
    with pytest.raises(SystemExit):
        _entries(tmp_path, body)


def test_stacks_match_by_template_suffix_only(tmp_path):
    (e,) = _entries(
        tmp_path,
        "suppressions: [{rule: AwsSolutions-S1, stacks: [uc-business-operations], reason: r, expiration: '2099-01-01'}]",
    )
    assert nag.in_stacks(e, T)
    assert not nag.in_stacks(e, T.replace(".template", "-console.template"))


def test_applies_to_narrows_to_the_granular_finding(tmp_path):
    (e,) = _entries(
        tmp_path,
        "suppressions: [{rule: AwsSolutions-IAM5, stacks: [uc-business-operations], "
        "applies_to: ['Action::s3:GetObject*'], reason: r, expiration: '2099-01-01'}]",
    )
    assert nag.finding_matches(e, T, "P", "AwsSolutions-IAM5", "Action::s3:GetObject*")
    assert not nag.finding_matches(e, T, "P", "AwsSolutions-IAM5", "Action::s3:*")
    assert nag._js_regex("Action::s3:GetObject*") == r"/^Action::s3:GetObject.*$/"


def test_star_actions_refuse_a_policy_granting_anything_else_on_star(tmp_path):
    (e,) = _entries(
        tmp_path,
        "suppressions: [{rule: AwsSolutions-IAM5, stacks: [uc-business-operations], applies_to: ['Resource::*'], "
        "star_actions: ['xray:Put*'], reason: r, expiration: '2099-01-01'}]",
    )
    xray = {"Effect": "Allow", "Action": ["xray:PutTraceSegments"], "Resource": "*"}
    assert nag.covers_resource(e, "P", _policy(xray))
    # A Deny on * grants nothing, so it never blocks the suppression.
    deny = {"Effect": "Deny", "Action": "bedrock:InvokeModel", "Resource": "*"}
    assert nag.covers_resource(e, "P", _policy(xray, deny))
    s3 = {"Effect": "Allow", "Action": "s3:GetObject", "Resource": ["*"]}
    assert not nag.covers_resource(e, "P", _policy(xray, s3))


def test_where_pins_a_property(tmp_path):
    (e,) = _entries(
        tmp_path,
        "suppressions: [{rule: AwsSolutions-L1, stacks: [uc-business-operations], resources: ['Fn*'], "
        "where: {Runtime: python3.13}, reason: r, expiration: '2099-01-01'}]",
    )
    fn = {"Type": "AWS::Lambda::Function", "Properties": {"Runtime": "python3.13"}}
    assert nag.covers_resource(e, "FnA", fn)
    assert not nag.covers_resource(e, "Other", fn)
    assert not nag.covers_resource(
        e, "FnA", {**fn, "Properties": {"Runtime": "python3.12"}}
    )
