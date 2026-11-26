# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Agent Metrics — CloudWatch custom metrics emitted after each agent run.

Emits:
  - AgentTurns (count per run, dimensioned by process_type + model_tier)
  - AgentInputTokens / AgentOutputTokens / AgentCacheReadTokens
  - AgentLatencyMs (wall-clock time)
  - AgentEstimatedCost (USD estimate based on token counts)
  - AgentSuccess (1 or 0)

`emit_failure_metric` is the other entry point: one count on a path that swallowed its
exception, dimensioned by Kind. A swallowed failure emits no AWS/Lambda datapoint and
raises nothing, so without it the only trace is a log line nobody is reading.

Every metric is emitted twice: once dimensioned — by process_type + model_tier, or by
Kind — and once with no dimensions, because CloudWatch cannot aggregate across a
dimension subset. See the comment in emit_agent_metrics.

All metrics go to a custom namespace so they can be dashboarded alongside
Lambda/SQS/DDB metrics without polluting AWS/ namespaces.
"""

import os

import boto3

_cw = None
NAMESPACE = os.environ.get("METRICS_NAMESPACE", "ERPAgent")

# Amazon Bedrock on-demand USD per 1M tokens, keyed by model id fragment, read from the AWS
# Price List API (AmazonBedrockFoundationModels, us-west-2, publication 2026-10-12;
# the US Regions price alike). A `us.` geo profile bills the regional rate and a
# `global.` profile the global one, so both are listed for the current models.
# Keyed by model rather than tier so a tier's default moving to a new model can never
# be priced at the old model's rates. These are estimates — for exact costs, use AWS
# Cost Explorer with the 'project' cost-allocation tag.
#
# ponytail: Haiku 5.5 bills a higher rate on a request whose prompt exceeds 100K
# tokens, but the usage this sees is summed across a run's requests, so the threshold
# cannot be applied here; the ≤100K rate is used. Agent requests run ~25–37K tokens.
# Per-request pricing needs per-cycle usage from the event loop.
_PRICING = {
    "anthropic.claude-sonnet-5-5": {
        "input": 2.20,
        "output": 11.00,
        "cache_read": 0.11,
        "cache_write": 2.75,
    },
    "global.anthropic.claude-sonnet-5-5": {
        "input": 2.00,
        "output": 10.00,
        "cache_read": 0.10,
        "cache_write": 2.50,
    },
    "anthropic.claude-haiku-5-5": {
        "input": 0.11,
        "output": 0.55,
        "cache_read": 0.011,
        "cache_write": 0.1375,
    },
    "global.anthropic.claude-haiku-5-5": {
        "input": 0.10,
        "output": 0.50,
        "cache_read": 0.01,
        "cache_write": 0.125,
    },
    "anthropic.claude-opus-5-5": {
        "input": 4.40,
        "output": 22.00,
        "cache_read": 0.22,
        "cache_write": 5.50,
    },
    "global.anthropic.claude-opus-5-5": {
        "input": 4.00,
        "output": 20.00,
        "cache_read": 0.20,
        "cache_write": 5.00,
    },
    "anthropic.claude-sonnet-5": {
        "input": 2.20,
        "output": 11.00,
        "cache_read": 0.22,
        "cache_write": 2.75,
    },
    "anthropic.claude-haiku-4-5": {
        "input": 1.10,
        "output": 5.50,
        "cache_read": 0.11,
        "cache_write": 1.375,
    },
}
# What an unlisted model is priced at: the sonnet tier's default, the most common case.
_FALLBACK_MODEL = "anthropic.claude-sonnet-5-5"


def model_prices(model_id: str) -> dict:
    """Per-1M rates for an Amazon Bedrock model id, by the longest listed fragment it contains.

    Longest wins so `us.anthropic.claude-sonnet-5-5` is never priced as Sonnet 5 and a
    `global.` profile takes its own row. An unlisted model logs and falls back rather
    than raising: a metric must never fail the run it describes.
    """
    matches = [key for key in _PRICING if key in model_id]
    if not matches:
        print(
            f"[METRICS] No price for model {model_id!r}; estimating at {_FALLBACK_MODEL}"
        )
        return _PRICING[_FALLBACK_MODEL]
    return _PRICING[max(matches, key=len)]


def _get_cw():
    global _cw
    if _cw is None:
        region = os.environ.get(
            "AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        )
        _cw = boto3.client("cloudwatch", region_name=region)
    return _cw


def _estimate_cost(
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int = 0,
) -> float:
    prices = model_prices(model_id)
    billable_input = max(0, input_tokens - cache_read_tokens)
    return (
        billable_input * prices["input"]
        + output_tokens * prices["output"]
        + cache_read_tokens * prices["cache_read"]
        + cache_write_tokens * prices["cache_write"]
    ) / 1_000_000


def emit_agent_metrics(
    process_type: str,
    model_tier: str,
    turns: int,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    latency_ms: float,
    success: bool,
    estimated_cost_usd: float = 0.0,
):
    """Emit a batch of CloudWatch metrics for one agent run.

    The cost arrives priced rather than recomputed here: it includes the write
    verifier's calls, which can run a different model, so only the caller holding
    both usages can price them (MetricsHook).
    """
    dims = [
        {"Name": "ProcessType", "Value": process_type},
        {"Name": "ModelTier", "Value": model_tier},
    ]

    metric_data = [
        {
            "MetricName": "AgentTurns",
            "Value": turns,
            "Unit": "Count",
            "Dimensions": dims,
        },
        {
            "MetricName": "AgentInputTokens",
            "Value": input_tokens,
            "Unit": "Count",
            "Dimensions": dims,
        },
        {
            "MetricName": "AgentOutputTokens",
            "Value": output_tokens,
            "Unit": "Count",
            "Dimensions": dims,
        },
        {
            "MetricName": "AgentCacheReadTokens",
            "Value": cache_read_tokens,
            "Unit": "Count",
            "Dimensions": dims,
        },
        {
            "MetricName": "AgentCacheWriteTokens",
            "Value": cache_write_tokens,
            "Unit": "Count",
            "Dimensions": dims,
        },
        {
            "MetricName": "AgentLatencyMs",
            "Value": latency_ms,
            "Unit": "Milliseconds",
            "Dimensions": dims,
        },
        {
            "MetricName": "AgentEstimatedCostUSD",
            "Value": estimated_cost_usd,
            "Unit": "None",
            "Dimensions": dims,
        },
        {
            "MetricName": "AgentSuccess",
            "Value": 1 if success else 0,
            "Unit": "Count",
            "Dimensions": dims,
        },
    ]

    # CloudWatch does not roll up dimension subsets: a metric emitted only with
    # [ProcessType, ModelTier] is invisible to an alarm or widget that asks for it
    # with no dimensions. Every ERPAgent alarm and widget in
    # business_operations/alarms.py asks with none — so the failure-rate and
    # cost alarms sat at INSUFFICIENT_DATA forever. Emit a dimensionless copy rather
    # than teaching ~15 widgets a SEARCH() expression, which cannot be alarmed on
    # directly and would make AgentSuccess an average-of-averages and the cost alarm
    # a p90-of-p90s.
    metric_data += [{**m, "Dimensions": []} for m in metric_data]

    try:
        _get_cw().put_metric_data(Namespace=NAMESPACE, MetricData=metric_data)
    except Exception as e:  # noqa: BLE001
        # Metrics are best-effort — never fail the agent run
        print(f"[METRICS] Failed to emit: {e}")


def emit_failure_metric(metric_name: str, kind: str) -> None:
    """Count one failure on a path that swallows its own exception.

    Separate from emit_agent_metrics rather than a ninth entry in it: that one runs once,
    at the end of a run, and describes the run. This one fires mid-run and describes a
    fault the run survived — which is exactly the fault nothing else reports, since a
    swallowed exception produces no AWS/Lambda Errors datapoint and no failed run.

    `Kind` says which fault; the dimensionless copy is the one an alarm can query, for the
    reason spelled out in emit_agent_metrics. Never raises: every caller is already
    handling a failure.
    """
    metric_data = [
        {
            "MetricName": metric_name,
            "Value": 1,
            "Unit": "Count",
            "Dimensions": [{"Name": "Kind", "Value": kind}],
        }
    ]
    metric_data += [{**m, "Dimensions": []} for m in metric_data]

    try:
        _get_cw().put_metric_data(Namespace=NAMESPACE, MetricData=metric_data)
    except Exception as e:  # noqa: BLE001
        print(f"[METRICS] Failed to emit {metric_name}: {e}")
