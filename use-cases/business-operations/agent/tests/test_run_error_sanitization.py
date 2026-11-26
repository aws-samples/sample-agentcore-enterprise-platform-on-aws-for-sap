# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""RUN_ERROR messages reach the browser verbatim, so adapter detail must be stripped.

`aguiReducer.appendRunError` renders `RUN_ERROR.message` directly into the chat. A
failure raised inside the agent loop arrives formatted by whichever library raised
it and can carry ARNs, endpoint hostnames, or SAP response fragments. Errors this
runtime builds itself are written for the user and pass through.

`_sanitized_run_error` lives in `utils.stream`, which imports only asyncio, time,
and ag_ui — so these tests exercise the real function.
"""

from pathlib import Path

import pytest
from ag_ui.core import EventType, RunErrorEvent
from utils.stream import _OWN_ERROR_CODES, _sanitized_run_error

_AGENT = Path(__file__).resolve().parents[1] / "basic_agent.py"


def _event(message, code=None):
    event = RunErrorEvent(type=EventType.RUN_ERROR, message=message, code=code)
    return event, event.model_dump(mode="json", by_alias=True)


@pytest.mark.parametrize("code", sorted(_OWN_ERROR_CODES))
def test_our_own_errors_pass_through_untouched(code):
    # These messages are written for the user; rewriting them would lose meaning.
    event, canonical = _event(
        "The agent reached its configured processing limit.", code
    )

    assert _sanitized_run_error(event, canonical, run_id="r1") is event


def test_exact_strands_max_tokens_error_becomes_safe_owned_code():
    event, canonical = _event(
        "Model stopped generating due to maximum token limit. "
        "The partial message has been added to the conversation history.",
        code="STRANDS_ERROR",
    )

    sanitized = _sanitized_run_error(event, canonical, run_id="r1")

    assert sanitized is not event
    assert sanitized.code == "MAX_TOKENS_REACHED"
    assert "conversation history" not in sanitized.message
    assert "case state" in sanitized.message.lower()


def test_other_strands_errors_remain_generic_and_sanitized():
    event, canonical = _event(
        "Model stopped generating for a different reason.", code="STRANDS_ERROR"
    )

    sanitized = _sanitized_run_error(event, canonical, run_id="r1")

    assert sanitized.code == "AGENT_INTERNAL_ERROR"
    assert "different reason" not in sanitized.message


def test_adapter_detail_is_replaced():
    leak = (
        "ClientError calling bedrock-agentcore at "
        "https://internal.endpoint.example/mcp: arn:aws:iam::111122223333:role/Secret"
    )
    event, canonical = _event(leak, code="SomeLibraryError")

    sanitized = _sanitized_run_error(event, canonical, run_id="r1")

    assert sanitized is not event
    assert sanitized.code == "AGENT_INTERNAL_ERROR"
    assert "internal.endpoint.example" not in sanitized.message
    assert "arn:aws" not in sanitized.message
    assert "111122223333" not in sanitized.message


def test_an_error_with_no_code_is_replaced():
    # The adapter is not obliged to set a code, and an absent one is not ours.
    event, canonical = _event("s3://private-bucket/key exploded", code=None)

    sanitized = _sanitized_run_error(event, canonical, run_id="r1")

    assert sanitized.code == "AGENT_INTERNAL_ERROR"
    assert "private-bucket" not in sanitized.message


def _run_error_branch() -> str:
    """The stream loop's ``elif event_name == "RUN_ERROR"`` branch, and only it.

    basic_agent.py imports strands and mcp and cannot be imported here, so this reads
    the source — the same approach as test_live_run_wiring.py, for the same reason:
    the property is an ordering that no importable seam exposes.
    """
    text = _AGENT.read_text()
    start = text.find('elif event_name == "RUN_ERROR":')
    assert start != -1, "the RUN_ERROR branch left basic_agent's stream loop"
    end = text.find("elif event_name ==", start + 1)
    return text[start : end if end != -1 else len(text)]


def test_the_caller_sanitizes_before_it_decides_the_run_outcome():
    # Sanitizing is what mints MAX_TOKENS_REACHED, so a caller that sets `outcome`
    # first can only ever see the adapter's STRANDS_ERROR and records a deliberate
    # token-limit stop as a failure.
    branch = _run_error_branch()
    assert branch.index("_sanitized_run_error(") < branch.index("outcome ="), (
        "_sanitized_run_error must be called before the outcome is assigned"
    )


def test_a_deliberate_stop_is_not_recorded_as_an_error():
    branch = _run_error_branch()
    for code in ("MAX_TURNS_REACHED", "MAX_TOKENS_REACHED", "RUN_BUDGET_EXCEEDED"):
        assert code in branch, (
            f"the outcome must recognise {code} as a deliberate stop and record "
            '"cancelled" — the timeline renders that as "Stopped early", while "error" '
            "reads as a failure the operator has to investigate"
        )
    assert '"cancelled"' in branch


def test_the_replacement_still_tells_the_user_what_to_do():
    _, canonical = _event("boom", code="X")
    sanitized = _sanitized_run_error(
        RunErrorEvent(type=EventType.RUN_ERROR, message="boom", code="X"),
        canonical,
        run_id="r1",
    )

    assert "case state" in sanitized.message.lower()
    assert sanitized.type == EventType.RUN_ERROR, "it must remain a terminal RUN_ERROR"
