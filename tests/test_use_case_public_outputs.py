# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Only the outputs a use case's manifest lists in `public_outputs` are
printed or exported, and the synth refuses one that resolves a secret."""

from __future__ import annotations

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from dashboard.monitor import SAFE_EXPORT_STACK_OUTPUTS, safe_stack_outputs
from infra_utils.platform_config import UseCaseManifest
from infra_utils.public_outputs import secret_public_outputs


def _outputs(**values) -> dict:
    app = cdk.App()
    stack = cdk.Stack(app, "proj-dev-uc-tenant")
    for key, value in values.items():
        cdk.CfnOutput(stack, key, value=value)
    return {stack.stack_name: Template.from_stack(stack).to_json()}


def test_a_declared_output_that_resolves_a_secret_is_refused():
    templates = _outputs(
        Url="https://example.com",
        Password=cdk.SecretValue.secrets_manager("app/password").unsafe_unwrap(),
        Token=cdk.SecretValue.ssm_secure("/app/token", "1").unsafe_unwrap(),
    )
    assert secret_public_outputs(templates, ["Url", "Password", "Token"]) == [
        "proj-dev-uc-tenant/Password",
        "proj-dev-uc-tenant/Token",
    ]
    # Undeclared outputs are never printed, so they are not this guard's business.
    assert secret_public_outputs(templates, ["Url"]) == []


def test_only_declared_use_case_outputs_are_printed_or_exported():
    outputs = [
        {"OutputKey": "Greeting", "OutputValue": "hello"},
        {"OutputKey": "ExportsOutputRefSecret", "OutputValue": "s3cr3t"},
    ]
    assert safe_stack_outputs("uc-hello-platform", outputs) == {"Greeting": "hello"}
    assert safe_stack_outputs(
        "uc-hello-platform", outputs, allowlist=SAFE_EXPORT_STACK_OUTPUTS
    ) == {"Greeting": "hello"}
    assert safe_stack_outputs("uc-no-such-stack", outputs) == {}


def test_public_outputs_must_be_output_keys():
    with pytest.raises(ValueError, match="public_outputs"):
        UseCaseManifest.model_validate(
            {
                "name": "leaky",
                "owner": "x",
                "summary": "s",
                "stacks": ["uc-leaky"],
                "public_outputs": ["Not a key"],
            }
        )
