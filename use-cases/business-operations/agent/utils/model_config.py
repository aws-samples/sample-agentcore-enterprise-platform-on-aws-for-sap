# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""BedrockModel construction kwargs shared by every agent in this runtime.

The agent (basic_agent) and the write verifier (utils/write_verifier) both build a
BedrockModel, and both have to make the same decisions: whether the target model
accepts sampling params, whether a guardrail was provisioned, and what output
ceiling to run under. One home for them means a guardrail added to the agent
cannot miss the verifier.

## Sampling

Sonnet 5/5.5, Haiku 5.5 and the Opus 4.7+ line (Opus 5.5 included) reject `temperature`/`top_p`/`top_k` —
Amazon Bedrock returns ValidationException "`temperature` is deprecated for this model",
so a model swap alone turns every agent call into a 400. Strands forwards
whatever `temperature` it is constructed with and omits the field entirely when
unset, so the guard belongs at construction time, here, rather than in each
caller.

Older models (Sonnet 4.5, Haiku 4.5) still accept it, and the low value is a
deliberate determinism nudge for financial reasoning, so it is kept where it
still works. Note it never guaranteed identical outputs on those models either.

## Thinking and effort

Nothing here sets `thinking` or `effort`, deliberately. The current models run
adaptive thinking by default, and Sonnet 5.5 returns a 400 on `thinking:
{"type": "disabled"}` while both 5.5 models reject `budget_tokens`, so the
model default is the only setting valid on every tier. Effort is the model
default (`high` on Sonnet 5.5, `medium` on Haiku 5.5) unless a pack declares
`model_effort` — see `request_fields_kwargs`.
"""

import os

# Substrings of model IDs that reject sampling params. Matched on the ID rather
# than enumerated in full because Amazon Bedrock IDs carry region prefixes
# (`us.`/`global.`) and, for the newer models, no date suffix to pin.
# "sonnet-5" also covers Sonnet 5.5; "haiku-5" covers Haiku 5.5; "opus-5" covers Opus 5.5.
_NO_SAMPLING_PARAMS = (
    "sonnet-5",
    "haiku-5",
    "opus-5",
    "opus-4-7",
    "opus-4-8",
    "fable-5",
)


def sampling_kwargs(model_id: str, temperature: float) -> dict:
    """`{"temperature": ...}` when the model accepts it, else `{}`."""
    if any(marker in model_id for marker in _NO_SAMPLING_PARAMS):
        return {}
    return {"temperature": temperature}


# Output ceiling for every agent this runtime builds. Set explicitly because
# leaving it unset does not mean "unlimited" — Amazon Bedrock applies the model's own
# default, which is low enough that a verbose closing answer is cut mid-word. The
# agent then never reaches its final `update_case_state`, so a case whose research
# was complete lands in `manual_review_required` with an empty `action_log`,
# indistinguishable from one the agent failed.
#
# A ceiling alone does not fix that — a truncated turn at 8192 loses the terminal
# write exactly as one at the model default does. What it buys is that the ordinary
# closing answer stops being truncated at all; the *reported* half is
# `status_reason`, written by agent_invoker when it parks the case.
_DEFAULT_MAX_TOKENS = 8192


def max_tokens_kwargs() -> dict:
    """`{"max_tokens": N}` for every model this runtime constructs.

    Unconditional: unlike `temperature`, no Amazon Bedrock model rejects `max_tokens`.
    AGENT_MAX_TOKENS overrides the default; the deploy sets it from the use case's
    `runtime.max_tokens`. A value that is unset,
    non-numeric or non-positive falls back to the default rather than propagating
    into the model call, because an ignored ceiling is the bug this exists to close.
    """
    raw = os.environ.get("AGENT_MAX_TOKENS", "")
    try:
        configured = int(raw)
    except ValueError:
        configured = 0
    if configured <= 0:
        if raw:
            print(
                f"[MODEL] Ignoring unusable AGENT_MAX_TOKENS={raw!r}; "
                f"using {_DEFAULT_MAX_TOKENS}"
            )
        configured = _DEFAULT_MAX_TOKENS
    return {"max_tokens": configured}


_CONTEXT_MANAGEMENT_BETA = "context-management-2025-06-27"


def request_fields_kwargs(config: dict) -> dict:
    """`additional_request_fields` for a pack's `model_effort` and `context_editing`, or {}.

    Both ride Converse's `additionalModelRequestFields`, which Amazon Bedrock forwards to the
    model as Messages API body fields. Verified live on Sonnet 5.5 and Haiku 5.5: an
    unknown effort or edit type is a 400 naming the allowed values, so both are parsed
    rather than ignored, and `context_management` without the beta is refused outright.

    Context editing is server-side: the client's history keeps every tool result, so
    AgentCore Memory, the resume path and the evidence hook see what they always did;
    only what the model is sent loses the older results. It clears from the oldest
    tool use forward and never touches the system prompt or the tool list, so that
    cached prefix survives every clear; the message-level cache is rewritten from the
    first cleared block, which `clear_at_least_tokens` keeps from happening for a
    trivial saving.
    """
    fields: dict = {}
    effort = config.get("model_effort")
    if effort:
        fields["output_config"] = {"effort": effort}
    editing = config.get("context_editing")
    if editing:
        edit: dict = {
            "type": "clear_tool_uses_20250919",
            "keep": {"type": "tool_uses", "value": editing["keep_tool_uses"]},
        }
        if "trigger_input_tokens" in editing:
            edit["trigger"] = {
                "type": "input_tokens",
                "value": editing["trigger_input_tokens"],
            }
        if "clear_at_least_tokens" in editing:
            edit["clear_at_least"] = {
                "type": "input_tokens",
                "value": editing["clear_at_least_tokens"],
            }
        fields["anthropic_beta"] = [_CONTEXT_MANAGEMENT_BETA]
        fields["context_management"] = {"edits": [edit]}
    return {"additional_request_fields": fields} if fields else {}


def guardrail_kwargs() -> dict:
    """Amazon Bedrock Guardrail kwargs for BedrockModel, or {} when not configured.

    The platform's RuntimeStack provisions the guardrail when
    security.require_guardrails is set and passes GUARDRAIL_ID / GUARDRAIL_VERSION.
    When absent, the agent runs without a guardrail and relies on input
    sanitization and Cedar.

    Every model this runtime constructs gets these: each is handed SAP text, the
    untrusted input the guardrail exists to filter.
    """
    guardrail_id = os.environ.get("GUARDRAIL_ID")
    if not guardrail_id:
        return {}
    return {
        "guardrail_id": guardrail_id,
        "guardrail_version": os.environ.get("GUARDRAIL_VERSION", "DRAFT"),
        "guardrail_trace": "enabled",
    }
