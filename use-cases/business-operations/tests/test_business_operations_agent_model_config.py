# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""business-operations agent: what every Amazon Bedrock model is built with, and what it costs."""

import importlib
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
UC = REPO / "use-cases" / "business-operations"
AGENT = UC / "agent"
sys.path.insert(0, str(UC))

from business_operations import runtime as uc_runtime


def _agent_utils(*names):
    """Import agent/utils modules without leaving `utils` bound in sys.modules.

    The agent imports its helpers as the package `utils`, and scripts/ has a
    `utils` module of its own that other tests import, so the binding is held
    only for the import.
    """

    def ours():
        return [k for k in sys.modules if k == "utils" or k.startswith("utils.")]

    held = {k: sys.modules.pop(k) for k in ours()}
    sys.path.insert(0, str(AGENT))
    try:
        return [importlib.import_module(f"utils.{n}") for n in names]
    finally:
        sys.path.remove(str(AGENT))
        for k in ours():
            del sys.modules[k]
        sys.modules.update(held)


model_config, agent_metrics = _agent_utils("model_config", "agent_metrics")
_DEFAULT_MAX_TOKENS = model_config._DEFAULT_MAX_TOKENS
guardrail_kwargs = model_config.guardrail_kwargs
max_tokens_kwargs = model_config.max_tokens_kwargs
request_fields_kwargs = model_config.request_fields_kwargs
sampling_kwargs = model_config.sampling_kwargs
_estimate_cost = agent_metrics._estimate_cost
model_prices = agent_metrics.model_prices


# ── The three decisions every BedrockModel in this runtime has to make. ──


_AGENT = AGENT / "basic_agent.py"

REJECTS_SAMPLING = [
    "us.anthropic.claude-sonnet-5-5",
    "global.anthropic.claude-sonnet-5-5",
    "us.anthropic.claude-haiku-5-5",
    "global.anthropic.claude-haiku-5-5",
    "us.anthropic.claude-sonnet-5",
    "global.anthropic.claude-sonnet-5",
    "us.anthropic.claude-opus-5",
    # Verified live 2026-10-26: `temperature` is a ValidationException on Opus 5.5.
    "us.anthropic.claude-opus-5-5",
    "global.anthropic.claude-opus-5-5",
    "us.anthropic.claude-opus-4-7",
    "us.anthropic.claude-opus-4-8",
]

ACCEPTS_SAMPLING = [
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
]


def _shipped_tier_defaults() -> list[str]:
    """The tier defaults the deploy sets when platform.yaml names no model."""
    return [uc_runtime.DEFAULT_SONNET, uc_runtime.DEFAULT_HAIKU]


@pytest.mark.parametrize("model_id", REJECTS_SAMPLING)
def test_temperature_omitted_for_models_that_reject_it(model_id):
    assert sampling_kwargs(model_id, 0.1) == {}


@pytest.mark.parametrize("model_id", ACCEPTS_SAMPLING)
def test_temperature_kept_where_still_accepted(model_id):
    assert sampling_kwargs(model_id, 0.1) == {"temperature": 0.1}


def test_every_shipped_tier_default_is_explicitly_classified():
    """A tier bump must be classified here, since only Amazon Bedrock is the real oracle.

    Whether a model rejects sampling params is not derivable from the ID — it is
    a per-model API fact. Pinning the shipped defaults to the lists above forces
    anyone bumping MODEL_TIERS to confirm the new model's behaviour rather than
    discovering it as a 400 in deployment.
    """
    defaults = _shipped_tier_defaults()
    assert defaults

    classified = set(REJECTS_SAMPLING) | set(ACCEPTS_SAMPLING)
    for model_id in defaults:
        assert model_id in classified, (
            f"tier default {model_id!r} is unclassified — verify against Amazon Bedrock "
            f"whether it accepts `temperature`, then add it to the list in this test"
        )


def _no_guardrail_env(monkeypatch):
    for name in ("GUARDRAIL_ID", "GUARDRAIL_VERSION"):
        monkeypatch.delenv(name, raising=False)


def test_no_guardrail_configured_yields_no_kwargs(monkeypatch):
    # The sample's default deployment. A partial dict here — a version or a trace
    # flag with no id — is a ValidationException on every single model call.
    _no_guardrail_env(monkeypatch)

    assert guardrail_kwargs() == {}


def test_an_empty_guardrail_id_is_treated_as_unconfigured(monkeypatch):
    # CloudFormation resolves an unset output to "", so the env var can be present
    # and empty. Passing that through would reference a guardrail that isn't there.
    _no_guardrail_env(monkeypatch)
    monkeypatch.setenv("GUARDRAIL_ID", "")

    assert guardrail_kwargs() == {}


def test_a_configured_guardrail_is_passed_with_tracing_on(monkeypatch):
    _no_guardrail_env(monkeypatch)
    monkeypatch.setenv("GUARDRAIL_ID", "gr-abc123")
    monkeypatch.setenv("GUARDRAIL_VERSION", "3")

    assert guardrail_kwargs() == {
        "guardrail_id": "gr-abc123",
        "guardrail_version": "3",
        # Tracing is what makes an intervention visible in the trace rather than
        # arriving as an unexplained truncated answer.
        "guardrail_trace": "enabled",
    }


def test_a_guardrail_without_an_explicit_version_falls_back_to_draft(monkeypatch):
    # A guardrail always has DRAFT; a numbered version only exists once published.
    _no_guardrail_env(monkeypatch)
    monkeypatch.setenv("GUARDRAIL_ID", "gr-abc123")

    assert guardrail_kwargs()["guardrail_version"] == "DRAFT"


def test_a_ceiling_is_always_passed(monkeypatch):
    # The bug this closes is the *absent* kwarg, not a wrong number: unset lets Amazon Bedrock
    # apply the model's own default and truncate the closing answer.
    monkeypatch.delenv("AGENT_MAX_TOKENS", raising=False)

    assert max_tokens_kwargs() == {"max_tokens": _DEFAULT_MAX_TOKENS}


def test_an_env_override_wins_over_the_default(monkeypatch):
    # The deploy sets AGENT_MAX_TOKENS from `runtime.max_tokens`; a set value wins.
    monkeypatch.setenv("AGENT_MAX_TOKENS", "16384")

    assert max_tokens_kwargs() == {"max_tokens": 16384}


@pytest.mark.parametrize("raw", ["", "unlimited", "0", "-1", "8192.5"])
def test_an_unusable_override_falls_back_rather_than_reaching_the_model(
    monkeypatch, raw
):
    # An override that is empty, non-numeric or non-positive must not propagate: a
    # `max_tokens` of 0 or a string is a ValidationException on every call, and an
    # ignored ceiling is the exact bug this helper exists to close. `""` is its own
    # case — CloudFormation resolves an unset value to the empty string.
    monkeypatch.setenv("AGENT_MAX_TOKENS", raw)

    assert max_tokens_kwargs() == {"max_tokens": _DEFAULT_MAX_TOKENS}


def test_the_agent_takes_the_ceiling_from_here():
    """The agent may not construct a BedrockModel without it. Read from source rather
    than by constructing the agent, which needs the whole runtime."""
    # The call, not the splat: basic_agent.py hoists it into `token_ceiling` so the
    # [COST] line can report the number without invoking the helper twice.
    assert "max_tokens_kwargs()" in _AGENT.read_text(encoding="utf-8")


def test_an_unset_effort_and_editing_send_no_request_fields():
    """The model default, and Haiku/Sonnet 5.5 defaults differ — never send one by accident."""
    assert request_fields_kwargs({}) == {}
    assert request_fields_kwargs({"model_tier": "haiku"}) == {}


def test_a_pack_effort_reaches_the_model_as_output_config():
    fields = request_fields_kwargs({"model_effort": "low"})["additional_request_fields"]
    assert fields == {"output_config": {"effort": "low"}}


def test_context_editing_sends_the_clear_tool_uses_edit_with_its_beta():
    """Without the beta Amazon Bedrock 400s on `context_management` ("Extra inputs are not permitted")."""
    fields = request_fields_kwargs(
        {
            "context_editing": {
                "keep_tool_uses": 3,
                "trigger_input_tokens": 30000,
                "clear_at_least_tokens": 5000,
            }
        }
    )["additional_request_fields"]
    assert fields == {
        "anthropic_beta": ["context-management-2025-06-27"],
        "context_management": {
            "edits": [
                {
                    "type": "clear_tool_uses_20250919",
                    "keep": {"type": "tool_uses", "value": 3},
                    "trigger": {"type": "input_tokens", "value": 30000},
                    "clear_at_least": {"type": "input_tokens", "value": 5000},
                }
            ]
        },
    }


def test_context_editing_omits_the_thresholds_it_was_not_given():
    """An absent threshold is the API default, not a zero."""
    edit = request_fields_kwargs({"context_editing": {"keep_tool_uses": 2}})[
        "additional_request_fields"
    ]["context_management"]["edits"][0]
    assert edit == {
        "type": "clear_tool_uses_20250919",
        "keep": {"type": "tool_uses", "value": 2},
    }


def test_the_orchestrator_takes_its_request_fields_from_the_pack():
    assert "**request_fields_kwargs(config)" in _AGENT.read_text(encoding="utf-8")


# ── The runtime's cost estimate is priced by model id, never by tier. ──


M = 1_000_000


@pytest.mark.parametrize(
    "model_id, want",
    [
        ("us.anthropic.claude-sonnet-5-5", 2.20),
        ("global.anthropic.claude-sonnet-5-5", 2.00),
        ("us.anthropic.claude-haiku-5-5", 0.11),
        ("global.anthropic.claude-haiku-5-5", 0.10),
        ("us.anthropic.claude-opus-5-5", 4.40),
        ("global.anthropic.claude-opus-5-5", 4.00),
        ("us.anthropic.claude-sonnet-5", 2.20),
        ("us.anthropic.claude-haiku-4-5-20251001-v1:0", 1.10),
    ],
)
def test_input_rate_is_the_models_own(model_id, want):
    assert model_prices(model_id)["input"] == want


def test_sonnet_5_5_is_not_priced_as_sonnet_5():
    # Same input/output rate, but Sonnet 5.5 cache reads are half Sonnet 5's.
    assert model_prices("us.anthropic.claude-sonnet-5-5")["cache_read"] == 0.11
    assert model_prices("us.anthropic.claude-sonnet-5")["cache_read"] == 0.22


def test_cache_reads_are_not_billed_twice_as_input():
    cost = _estimate_cost("us.anthropic.claude-haiku-5-5", M, 0, M // 2, 0)
    assert cost == pytest.approx(0.5 * 0.11 + 0.5 * 0.011)


def test_an_unlisted_model_falls_back_without_raising():
    assert (
        model_prices("us.anthropic.claude-opus-9")
        is agent_metrics._PRICING[agent_metrics._FALLBACK_MODEL]
    )


def test_opus_5_5_rates_are_the_price_list_rates():
    # AWS Price List, AmazonBedrockFoundationModels us-west-2, publication 2026-10-12.
    assert model_prices("us.anthropic.claude-opus-5-5") == {
        "input": 4.40,
        "output": 22.00,
        "cache_read": 0.22,
        "cache_write": 5.50,
    }
