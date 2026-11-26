# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""business-operations: the business dashboard, and model content in the log group.

The console's Observability and Analytics pages and their `/observability/*` routes
are gone; the dashboard and AgentCore's own observability replace them.
"""

from __future__ import annotations

import json

import pytest
from test_business_operations_api import (
    _of,
    _stack,
)

from infra_utils.use_case_context import UseCaseContext

ORIGIN = "https://app.test"


# ── the stack ──


def _methods(template) -> dict[str, dict]:
    resources = {
        k: r["Properties"]
        for k, r in template["Resources"].items()
        if r["Type"] == "AWS::ApiGateway::Resource"
    }

    def path(ref):
        if "Ref" not in ref:
            return ""
        props = resources[ref["Ref"]]
        return f"{path(props['ParentId'])}/{props['PathPart']}"

    return {
        f"{m['HttpMethod']} {path(m['ResourceId'])}": m
        for m in (r["Properties"] for r in _of(template, "AWS::ApiGateway::Method"))
        if m["HttpMethod"] != "OPTIONS"
    }


def test_the_dashboard_is_deployed_and_no_observability_route_is():
    t = _stack("business-operations.yaml")
    assert not any("/observability" in m for m in _methods(t))
    (board,) = _of(t, "AWS::CloudWatch::Dashboard")
    body = json.dumps(board["Properties"]["DashboardBody"])
    assert "agent_invoker" in body and "batch_runner" in body
    params = [p["Properties"]["Name"] for p in _of(t, "AWS::SSM::Parameter")]
    assert any(
        n.endswith("/use-cases/business-operations/dashboard-url") for n in params
    )


# ── model content on spans ──


@pytest.mark.parametrize(
    "mode,setting,expected",
    [
        ("standard", None, True),
        ("production", None, False),
        ("production", True, True),
        ("standard", False, False),
    ],
)
def test_log_model_content_follows_the_mode_unless_set(mode, setting, expected):
    from business_operations import config as uc_config
    from business_operations import runtime as uc_runtime

    ctx = UseCaseContext("p", "dev", None, production=mode == "production")
    cfg = uc_config.load({"runtime": {"log_model_content": setting}})
    assert uc_runtime.log_model_content(ctx, cfg) is expected


def test_log_model_content_has_no_old_name():
    from business_operations import config as uc_config

    assert (
        uc_config.load(
            {"runtime": {"log_model_content": False}}
        ).runtime.log_model_content
        is False
    )
    with pytest.raises(ValueError, match="model_content_on_spans"):
        uc_config.load({"runtime": {"model_content_on_spans": False}})


def test_span_env_routes_to_aws_spans_and_redacts_only_when_off():
    from business_operations import runtime as uc_runtime

    on, off = uc_runtime.span_env(True), uc_runtime.span_env(False)
    # Spans stay in aws/spans, which evals-run checks for before judging.
    assert on == {"UNIFIED_TRACES_DESTINATION_ENABLED": "false"}
    assert off["UNIFIED_TRACES_DESTINATION_ENABLED"] == "false"
    # An empty allowlist is what makes Strands redact every message field.
    assert off["OTEL_SEMCONV_STABILITY_OPT_IN"] == "gen_ai_unredacted_attributes="
    assert off["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] == "false"
