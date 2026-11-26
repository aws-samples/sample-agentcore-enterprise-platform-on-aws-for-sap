# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Every Lambda log group the app creates carries the platform retention.

Lambda otherwise creates /aws/lambda/<fn> on first invocation, with no retention
and outside any stack. app.py turns on the managed-log-group flag and applies
LambdaLogRetention; a group whose author chose a retention keeps it.
"""

from __future__ import annotations

import json
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import custom_resources as cr

from infra_utils.log_retention import MANAGED_LOG_GROUP_FLAG, LambdaLogRetention
from tests.app_synth import synth_app

REPO = Path(__file__).resolve().parents[1]


def _fn(scope, id, **kw):
    return lambda_.Function(
        scope,
        id,
        runtime=lambda_.Runtime.PYTHON_3_13,
        handler="index.handler",
        code=lambda_.Code.from_inline("def handler(e, c): pass"),
        **kw,
    )


def _resources(retain: bool) -> dict:
    app = cdk.App(post_cli_context={MANAGED_LOG_GROUP_FLAG: True})
    stack = cdk.Stack(app, "S")
    _fn(stack, "Plain")
    _fn(
        stack,
        "Chosen",
        log_group=logs.LogGroup(
            stack, "ChosenLogs", retention=logs.RetentionDays.ONE_WEEK
        ),
    )
    # A singleton, and a CustomResourceProvider handler (an L1 function).
    cr.AwsCustomResource(
        stack,
        "Call",
        on_create=cr.AwsSdkCall(
            service="STS",
            action="getCallerIdentity",
            physical_resource_id=cr.PhysicalResourceId.of("x"),
        ),
        policy=cr.AwsCustomResourcePolicy.from_sdk_calls(
            resources=cr.AwsCustomResourcePolicy.ANY_RESOURCE
        ),
    )
    s3.Bucket(
        stack, "B", auto_delete_objects=True, removal_policy=cdk.RemovalPolicy.DESTROY
    )
    logs.CfnLogGroup(stack, "Bare")
    cdk.Aspects.of(app).add(LambdaLogRetention(30, retain=retain))
    return app.synth().get_stack_by_name("S").template["Resources"]


def _groups(resources: dict) -> dict:
    return {k: v for k, v in resources.items() if v["Type"] == "AWS::Logs::LogGroup"}


def test_every_function_has_a_group_and_every_group_a_retention():
    resources = _resources(retain=False)
    groups = _groups(resources)
    functions = [
        k for k, v in resources.items() if v["Type"] == "AWS::Lambda::Function"
    ]
    named = " ".join(str(v["Properties"].get("LogGroupName")) for v in groups.values())
    for fn in functions:
        explicit = resources[fn]["Properties"].get("LoggingConfig", {}).get("LogGroup")
        assert explicit or f"'Ref': '{fn}'" in named, fn
    assert all("RetentionInDays" in v["Properties"] for v in groups.values())


def test_the_platform_days_apply_and_a_chosen_retention_is_kept():
    groups = _groups(_resources(retain=False))
    days = {k: v["Properties"]["RetentionInDays"] for k, v in groups.items()}
    chosen = next(k for k in days if k.startswith("ChosenLogs"))
    assert days.pop(chosen) == 7
    assert set(days.values()) == {30}
    assert all(
        groups[k]["DeletionPolicy"] == "Delete" for k in days
    )  # what the platform sets outside production


def test_production_retains_the_groups_it_sets():
    groups = _groups(_resources(retain=True))
    assert {
        v["DeletionPolicy"] for k, v in groups.items() if not k.startswith("Chosen")
    } == {"Retain"}


# The app with no platform.yaml (every platform runtime), and every contract fixture.
CONFIGS = [
    REPO / "no-such-platform.yaml",
    *sorted(REPO.glob("tests/fixtures/contract/*.yaml")),
    *sorted(REPO.glob("use-cases/*/fixtures/contract/*.yaml")),
]


def _invoked_on_delete(resources: dict) -> set[str]:
    """The functions a stack's custom resources invoke on its Delete: each
    ServiceToken, and the handlers a Provider framework function calls."""
    found = set()
    for r in resources.values():
        if r["Type"] == "AWS::CloudFormation::CustomResource" or r["Type"].startswith(
            "Custom::"
        ):
            token = r["Properties"].get("ServiceToken")
            if isinstance(token, dict) and "Fn::GetAtt" in token:
                found.add(token["Fn::GetAtt"][0])
    for fn in list(found):
        env = resources[fn]["Properties"].get("Environment", {}).get("Variables", {})
        for key, value in env.items():
            if key.startswith("USER_") and isinstance(value, dict):
                found.add(value.get("Fn::GetAtt", [None])[0])
    return {
        fn
        for fn in found
        if resources.get(fn, {}).get("Type") == "AWS::Lambda::Function"
    }


@pytest.mark.parametrize("config", CONFIGS, ids=lambda p: p.stem)
def test_a_group_lambda_recreates_after_a_delete_never_blocks_a_redeploy(config):
    """A late log line after the stack's Delete makes Lambda recreate the function's
    group, outside the stack. A fixed group name then fails the next create with
    "already exists", so a function invoked on delete writes to one group whose
    name changes with every create."""
    result, out = synth_app(config)
    assert result.returncode == 0, result.stderr[-2000:]
    checked = 0
    for path in out.glob("*.template.json"):
        resources = json.loads(path.read_text()).get("Resources", {})
        groups = {
            k: v["Properties"]
            for k, v in resources.items()
            if v["Type"] == "AWS::Logs::LogGroup"
        }
        for fn in _invoked_on_delete(resources):
            props = resources[fn]["Properties"]
            own = props.get("LoggingConfig", {}).get("LogGroup")
            declared = [
                name
                for name, g in groups.items()
                if f"'Ref': '{fn}'" in str(g.get("LogGroupName"))
                or own == {"Ref": name}
            ]
            assert len(declared) == 1, (path.name, fn, declared)
            group = groups[declared[0]]
            fixed = isinstance(group.get("LogGroupName"), str) or (
                own is None and isinstance(props.get("FunctionName"), str)
            )
            assert not fixed, (path.name, fn)
            checked += 1
    assert checked, "no function invoked on delete in this footprint"
