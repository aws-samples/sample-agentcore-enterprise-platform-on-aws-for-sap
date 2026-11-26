# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""A run over its `run_budget` stops before its next tool call, through the same cancel
path as the turn limit.

Driven through a real Strands Agent with a stub model, because the property that
matters, that no tool runs after the stop, belongs to Strands' event loop and not to the
hook.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from strands import Agent, tool
from strands.hooks import (
    AfterModelCallEvent,
    AfterToolCallEvent,
    BeforeInvocationEvent,
    BeforeModelCallEvent,
)
from strands.models.model import Model
from utils import hooks

SCHEMA = Path(__file__).resolve().parents[2] / "packs" / "pack.schema.json"
# Haiku 5.5 regional: 0.11 in, 0.55 out per million.
HAIKU = "us.anthropic.claude-haiku-5-5"


class _StubModel(Model):
    """Asks for one `post_invoice` call per turn, reporting `usage` each time, for
    `turns` turns; then answers with text."""

    def __init__(self, usage: dict, turns: int = 5):
        self._usage = usage
        self._turns = turns
        self.calls = 0

    def get_config(self):
        return {}

    def update_config(self, **k):
        pass

    async def structured_output(self, *a, **k):
        raise NotImplementedError

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kw):
        self.calls += 1
        yield {"messageStart": {"role": "assistant"}}
        if self.calls > self._turns:
            yield {"contentBlockStart": {"start": {}}}
            yield {"contentBlockDelta": {"delta": {"text": "Done."}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}
        else:
            tu = {"toolUseId": f"tu-{self.calls}", "name": "post_invoice", "input": {}}
            yield {"contentBlockStart": {"start": {"toolUse": tu}}}
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": "{}"}}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        yield {"metadata": {"usage": self._usage, "metrics": {"latencyMs": 1}}}


def _run(monkeypatch, usage: dict, budget: dict | None, turns: int = 5):
    metrics = []
    monkeypatch.setattr(
        hooks, "emit_failure_metric", lambda name, kind: metrics.append((name, kind))
    )
    posted = []

    @tool
    def post_invoice() -> str:
        """Write SAP."""
        posted.append(1)
        return "ok"

    turns_hook = hooks.MaxTurnsHook(15)
    metrics_hook = hooks.MetricsHook("p", "haiku", HAIKU, turns_hook, budget)
    model = _StubModel(usage, turns)
    agent = Agent(model=model, tools=[post_invoice], callback_handler=None)
    agent.hooks.add_callback(BeforeInvocationEvent, turns_hook.reset)
    agent.hooks.add_callback(BeforeInvocationEvent, metrics_hook.on_start)
    agent.hooks.add_callback(BeforeModelCallEvent, turns_hook.check)
    agent.hooks.add_callback(AfterModelCallEvent, metrics_hook.check_budget)
    result = agent("go")
    return result, turns_hook, posted, model, metrics


USAGE = {"inputTokens": 1000, "outputTokens": 100, "totalTokens": 1100}


def test_a_run_under_budget_runs_to_the_end(monkeypatch):
    _, turns, posted, model, metrics = _run(monkeypatch, USAGE, None)
    assert len(posted) == 5 and model.calls == 6
    assert not turns.cancelled and metrics == []


def test_a_run_over_its_token_budget_stops_before_the_next_tool_call(monkeypatch):
    # 1100 tokens a turn: turn 2 crosses 2000, and its tool call never runs.
    result, turns, posted, model, metrics = _run(
        monkeypatch, USAGE, {"max_tokens": 2000}
    )
    assert len(posted) == 1 and model.calls == 2
    assert turns.cancelled and turns.stop_code == "RUN_BUDGET_EXCEEDED"
    assert result.stop_reason == "cancelled"
    assert metrics == [("RunBudgetExceeded", "max_tokens")]


def test_a_run_over_its_cost_budget_stops_before_the_next_tool_call(monkeypatch):
    # 200k output tokens a turn at Haiku's list price: the third turn crosses the 0.30
    # cost budget while the run is still well under the default token budget.
    usage = {"inputTokens": 0, "outputTokens": 200_000, "totalTokens": 200_000}
    _, turns, posted, model, metrics = _run(monkeypatch, usage, {"max_cost_usd": 0.30})
    assert len(posted) == 2 and model.calls == 3
    assert turns.stop_code == "RUN_BUDGET_EXCEEDED"
    assert metrics == [("RunBudgetExceeded", "max_cost_usd")]


def test_the_final_answer_is_never_turned_into_a_stop(monkeypatch):
    # Over budget only on the closing text turn: nothing is left to stop, and parking
    # the case would undo the outcome the run already wrote.
    _, turns, posted, _, metrics = _run(
        monkeypatch, USAGE, {"max_tokens": 1500}, turns=1
    )
    assert len(posted) == 1 and not turns.cancelled and metrics == []


def test_the_verifiers_cost_counts_against_the_budget(monkeypatch):
    hook = hooks.MetricsHook("p", "haiku", HAIKU, hooks.MaxTurnsHook(15))
    hook.on_start(None)
    hook.verifier_calls.append({"cost_usd": 0.60})
    stopped = []
    hook._max_turns_hook.stop = lambda agent, code: stopped.append(code)
    monkeypatch.setattr(hooks, "emit_failure_metric", lambda *a: None)

    response = SimpleNamespace(
        stop_reason="tool_use", message={"metadata": {"usage": USAGE}}
    )
    hook.check_budget(SimpleNamespace(stop_response=response, agent=None))
    assert stopped == ["RUN_BUDGET_EXCEEDED"]


def test_the_defaults_are_the_schemas():
    props = json.loads(SCHEMA.read_text())["properties"]["run_budget"]["properties"]
    assert {k: v["default"] for k, v in props.items()} == hooks.DEFAULT_RUN_BUDGET


@pytest.mark.parametrize("budget", [None, {}, {"max_tokens": 10}])
def test_an_unset_limit_takes_its_default(budget):
    hook = hooks.MetricsHook("p", "haiku", HAIKU, hooks.MaxTurnsHook(15), budget)
    assert hook.run_budget["max_cost_usd"] == hooks.DEFAULT_RUN_BUDGET["max_cost_usd"]


def _run_tools(monkeypatch, outcomes: list[bool]):
    """Five tool calls whose results succeed or fail as `outcomes` says, in order."""
    metrics = []
    monkeypatch.setattr(
        hooks, "emit_failure_metric", lambda name, kind: metrics.append((name, kind))
    )
    calls = []

    @tool
    def post_invoice() -> str:
        """Write SAP."""
        ok = outcomes[len(calls)]
        calls.append(ok)
        return "ok" if ok else '{"success": false, "message": "ConnectionError"}'

    turns_hook = hooks.MaxTurnsHook(15)
    agent = Agent(model=_StubModel(USAGE), tools=[post_invoice], callback_handler=None)
    agent.hooks.add_callback(BeforeInvocationEvent, turns_hook.reset)
    agent.hooks.add_callback(AfterToolCallEvent, turns_hook.check_tool)
    agent("go")
    return turns_hook, calls, metrics


def test_three_failed_tool_calls_in_a_row_stop_the_run(monkeypatch):
    turns, calls, metrics = _run_tools(monkeypatch, [False] * 5)
    assert len(calls) == 3
    assert turns.cancelled and turns.stop_code == "REPEATED_TOOL_ERRORS"
    assert metrics == [("RepeatedToolErrors", "consecutive")]


def test_a_success_between_failures_resets_the_count(monkeypatch):
    turns, calls, _ = _run_tools(monkeypatch, [False, False, True, False, False])
    assert len(calls) == 5 and not turns.cancelled
