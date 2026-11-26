# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The context a use case's build() receives is interface: fields are only added."""

import dataclasses
import inspect
from pathlib import Path

import pytest

from infra_utils.use_case_context import UseCaseContext
from stacks.runtime_stack import RuntimeStack

INTERFACE = Path(__file__).resolve().parents[1] / "docs" / "PLATFORM_INTERFACE.md"


def test_the_names_follow_project_and_environment():
    ctx = UseCaseContext("acme", "dev", None)
    assert (ctx.prefix, ctx.ssm_prefix) == ("acme-dev", "/acme/dev")


def test_it_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        UseCaseContext("acme", "dev", None).project = "other"


def test_dict_access_reads_the_same_values_for_interface_2_use_cases():
    ctx = UseCaseContext("acme", "dev", None, region="eu-west-1")
    for key in ("project", "environment", "prefix", "ssm_prefix", "region", "cdk_env"):
        assert ctx[key] == getattr(ctx, key)
    for key in ("runtime_kwargs", "ssm_value", "_missing"):
        with pytest.raises(KeyError):
            ctx[key]
    with pytest.raises(TypeError):
        ctx["project"] = "other"


def test_runtime_kwargs_are_runtime_stack_arguments_and_follow_production():
    ctx = UseCaseContext(
        "acme", "dev", None, production=True, network={"network_mode": "VPC"}
    )
    kwargs = ctx.runtime_kwargs()
    assert set(kwargs) <= set(inspect.signature(RuntimeStack.__init__).parameters)
    assert kwargs["retain_data"] and kwargs["guardrail_floor"]
    assert kwargs["network_mode"] == "VPC"


def test_every_field_is_documented():
    doc = INTERFACE.read_text()
    for name in [f.name for f in dataclasses.fields(UseCaseContext)] + [
        "prefix",
        "ssm_prefix",
        "runtime_kwargs()",
    ]:
        assert f"`{name}`" in doc, f"ctx.{name} is not in PLATFORM_INTERFACE.md"
