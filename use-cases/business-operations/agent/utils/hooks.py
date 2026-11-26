# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The three Strands hooks a run carries: turn limit, metrics, and evidence.

Each is a plain object holding per-run state, constructed in ``_create_agent`` and
registered on the adapter's per-thread clone by the ``_Hooks`` HookProvider that
stays in basic_agent.py. That split is deliberate: the hooks are what a run
observes, and ``_Hooks`` is *where* the observation is attached, which is a fact
about ag-ui-strands' cloning behaviour rather than about any one hook.

All three are read-only with respect to the agent loop except MaxTurnsHook, whose
whole job is to call ``agent.cancel()``. MetricsHook's run-budget check stops a run
through that same call (``MaxTurnsHook.stop``), so there is one cancel path and one
place the stream reads why a run stopped.
"""

import os
import time
from datetime import UTC

from strands import Agent
from strands.hooks import (
    AfterInvocationEvent,
    AfterModelCallEvent,
    AfterToolCallEvent,
    BeforeInvocationEvent,
    BeforeModelCallEvent,
)

from .agent_metrics import _estimate_cost, emit_agent_metrics, emit_failure_metric
from .evidence import (
    _SOP_HEADER_RE,
    _tool_key,
    extract_evidence,
    result_text,
    tool_failed,
)

#: What a run may spend when its pack's `run_budget` leaves a limit unset. The same
#: values are the `default`s in packs/pack.schema.json. They sit several times above any
#: normal run, so they stop only a runaway, and the p90 cost alarm fires first.
DEFAULT_RUN_BUDGET = {"max_tokens": 2_000_000, "max_cost_usd": 0.50}
#: Failed tool calls in a row that stop a run. An unreachable SAP fails every call, and
#: without this a case spent its turns and three whole runs before it reached a person.
MAX_CONSECUTIVE_TOOL_ERRORS = 3
#: Every token the model processes, cached or not: cache reads are most of a run.
_TOKEN_KEYS = (
    "inputTokens",
    "outputTokens",
    "cacheReadInputTokens",
    "cacheWriteInputTokens",
)


class MaxTurnsHook:
    """Cancels the agent after max_turns model calls to prevent runaway costs.

    Also the one cancel path for every other run limit: `stop` records which limit it
    was, as the RUN_ERROR code the stream reports in place of RUN_FINISHED.
    """

    def __init__(self, max_turns: int):
        self._max_turns = max_turns
        self.turn_count = 0
        self.cancelled = False
        self.stop_code = ""
        self._tool_errors = 0

    def reset(self, event: BeforeInvocationEvent) -> None:
        self.turn_count = 0
        self.cancelled = False
        self.stop_code = ""
        self._tool_errors = 0

    def stop(self, agent: Agent, code: str) -> None:
        self.cancelled = True
        self.stop_code = code
        agent.cancel()

    def check(self, event: BeforeModelCallEvent) -> None:
        self.turn_count += 1
        if self.turn_count > self._max_turns:
            print(f"[COST] Max turns ({self._max_turns}) reached — cancelling agent")
            self.stop(event.agent, "MAX_TURNS_REACHED")

    def check_tool(self, event: AfterToolCallEvent) -> None:
        """Stop after MAX_CONSECUTIVE_TOOL_ERRORS failed calls in a row; a success resets.

        One failure the agent recovers from never trips it, so a validation error it
        corrects on the next call costs nothing.
        """
        result = event.result if isinstance(event.result, dict) else None
        self._tool_errors = self._tool_errors + 1 if tool_failed(result) else 0
        if self._tool_errors >= MAX_CONSECUTIVE_TOOL_ERRORS and not self.cancelled:
            print(
                f"[RUN] {self._tool_errors} tool calls failed in a row — cancelling agent"
            )
            emit_failure_metric("RepeatedToolErrors", "consecutive")
            self.stop(event.agent, "REPEATED_TOOL_ERRORS")


class MetricsHook:
    """Emits CloudWatch metrics after every invocation (success or failure).

    Uses Strands' accumulated_usage from event_loop_metrics for accurate
    cumulative token counts across all turns (not just the last message).
    """

    def __init__(
        self,
        process_type: str,
        model_tier: str,
        model_id: str,
        max_turns_hook: MaxTurnsHook,
        run_budget: dict | None = None,
    ):
        self._process_type = process_type
        self._model_tier = model_tier
        self._model_id = model_id
        self._max_turns_hook = max_turns_hook
        self.run_budget = {**DEFAULT_RUN_BUDGET, **(run_budget or {})}
        self._run_tokens = 0
        self._run_model_cost = 0.0
        self._start_time = 0.0
        # Last-run metrics — populated by on_end, read by agent_stream
        self.last_latency_ms: float = 0.0
        self.last_input_tokens: int = 0
        self.last_output_tokens: int = 0
        self.last_cache_read_tokens: int = 0
        self.last_cache_write_tokens: int = 0
        self.last_estimated_cost_usd: float = 0.0
        # Appended to by the write gate's verifier, one record per call. Priced into
        # the run's cost here, but kept out of the token totals: those are priced at the
        # agent's model downstream, and the verifier can be a different one.
        self.verifier_calls: list[dict] = []

    def on_start(self, event: BeforeInvocationEvent) -> None:
        self._start_time = time.time()
        self.verifier_calls.clear()
        self._run_tokens = 0
        self._run_model_cost = 0.0

    def check_budget(self, event: AfterModelCallEvent) -> None:
        """Stop the run once it is over its `run_budget`, before its next tool call.

        This event fires when a model response is complete and before any tool it asked
        for has started, so the stop never lands mid-call: Strands answers each requested
        tool with "Tool execution cancelled" and does not call the model again. A
        response that asked for no tool ends the run anyway, so it is left alone rather
        than turning a finished case into a parked one.

        Usage is read off the response: Strands adds it to `event_loop_metrics` only
        after this event. A retried response is counted too, since it was billed.
        """
        response = event.stop_response
        if response is None:
            return
        usage = (response.message.get("metadata") or {}).get("usage") or {}
        self._run_tokens += sum(usage.get(key, 0) for key in _TOKEN_KEYS)
        self._run_model_cost += _estimate_cost(
            self._model_id,
            usage.get("inputTokens", 0),
            usage.get("outputTokens", 0),
            usage.get("cacheReadInputTokens", 0),
            usage.get("cacheWriteInputTokens", 0),
        )
        if response.stop_reason != "tool_use" or self._max_turns_hook.cancelled:
            return
        cost = self._run_model_cost + sum(
            c.get("cost_usd", 0.0) for c in self.verifier_calls
        )
        spent = {"max_tokens": self._run_tokens, "max_cost_usd": cost}
        over = [
            limit for limit, value in spent.items() if value > self.run_budget[limit]
        ]
        if not over:
            return
        print(
            f"[COST] Run budget exceeded ({over[0]}: {spent[over[0]]} > "
            f"{self.run_budget[over[0]]}) — cancelling agent"
        )
        emit_failure_metric("RunBudgetExceeded", over[0])
        self._max_turns_hook.stop(event.agent, "RUN_BUDGET_EXCEEDED")

    def trace_metrics(self) -> dict:
        """Metrics dict for _save_trace_to_ddb, populated by on_end."""
        return {
            "latency_ms": self.last_latency_ms,
            "input_tokens": self.last_input_tokens,
            "output_tokens": self.last_output_tokens,
            "cache_read_tokens": self.last_cache_read_tokens,
            "cache_write_tokens": self.last_cache_write_tokens,
            "estimated_cost_usd": self.last_estimated_cost_usd,
            "verifier_calls": list(self.verifier_calls),
        }

    @staticmethod
    def _get_invocation_usage(agent: Agent) -> dict:
        """Extract cumulative usage from the agent's latest invocation."""
        try:
            inv = agent.event_loop_metrics.latest_agent_invocation
            if inv and inv.usage:
                return inv.usage
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            if agent.messages:
                return agent.messages[-1].get("usage", {})
        except Exception:  # noqa: BLE001, S110
            pass
        return {}

    def on_end(self, event: AfterInvocationEvent) -> None:
        latency_ms = (time.time() - self._start_time) * 1000
        success = not getattr(event, "exception", None)

        # Cumulative across all turns
        usage = self._get_invocation_usage(event.agent)
        input_tokens = usage.get("inputTokens", 0)
        output_tokens = usage.get("outputTokens", 0)
        cache_read_tokens = usage.get("cacheReadInputTokens", 0)
        cache_write_tokens = usage.get("cacheWriteInputTokens", 0)

        cost = _estimate_cost(
            self._model_id,
            input_tokens,
            output_tokens,
            cache_read_tokens,
            cache_write_tokens,
        )

        cost += sum(c.get("cost_usd", 0.0) for c in self.verifier_calls)

        # Store for trace enrichment
        self.last_latency_ms = latency_ms
        self.last_input_tokens = input_tokens
        self.last_output_tokens = output_tokens
        self.last_cache_read_tokens = cache_read_tokens
        self.last_cache_write_tokens = cache_write_tokens
        self.last_estimated_cost_usd = cost

        emit_agent_metrics(
            process_type=self._process_type,
            model_tier=self._model_tier,
            turns=self._max_turns_hook.turn_count,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            latency_ms=latency_ms,
            success=success,
            estimated_cost_usd=cost,
        )


class EvidenceHook:
    """Collects structured evidence per tool call, keyed by toolUseId.

    AfterToolCallEvent is the right seam: `ToolUse.input` is still a real dict
    here, whereas segments.py only ever sees tool_input as concatenated AG-UI
    delta strings and would have to re-parse JSON that was already flattened.

    The hook is read-only — it never sets `event.retry` and never touches the
    result. (AfterToolCallEvent is not interruptible anyway.)
    """

    def __init__(self, sop_baseline: str = ""):
        self._mode = os.environ.get("CEDAR_ENFORCEMENT_MODE", "OFF")
        self._by_tool_call_id: dict[str, dict] = {}
        # What a quoted citation is checked against: the SOP text the run was
        # given. Starts as the injected document and grows with each load_sop /
        # search_sap_sops result, which is what makes the discovery path — where
        # nothing is injected up front — gradeable on the same footing.
        self._injected_sop = sop_baseline
        self._sop_baseline = sop_baseline
        # The process types `load_sop` served this run, in order: what the case's
        # `exception_type` is decided from (case_persistence.identified_exception_type).
        self._loaded_sops: list[str] = []

    def on_start(self, event: BeforeInvocationEvent) -> None:
        # Containers are warm and reused; without this, one case's evidence
        # merges into the next case's trace.
        self._by_tool_call_id = {}
        self._sop_baseline = self._injected_sop
        self._loaded_sops = []

    def on_tool_call(self, event: AfterToolCallEvent) -> None:
        from datetime import datetime

        try:
            tool_use = event.tool_use or {}
            tool_call_id = tool_use.get("toolUseId")
            if not tool_call_id:
                return
            result = event.result if isinstance(event.result, dict) else None
            # A retried tool call fires this hook once per attempt with the same
            # toolUseId, so the last attempt — the one whose result is kept in
            # conversation history — wins. That is the correct record.
            evidence = extract_evidence(
                tool_use.get("name", ""),
                tool_use.get("input"),
                result,
                at=datetime.now(UTC).isoformat(),
                mode=self._mode,
                sop_baseline=self._sop_baseline,
            )
            # A fetched SOP joins the baseline for every later call. Retrieved text
            # renders differently from the injected copy (the retrieval path leaves
            # `{{SYMBOL}}` constants unresolved by design), so both renderings have
            # to be present for a quote of either to verify.
            if evidence.get("kind") == "sop_lookup":
                self._sop_baseline = f"{self._sop_baseline}\n{result_text(result)}"
            self._note_loaded_sop(tool_use, result)
            # `error` for a call SAP or a Lambda refused as well as one that raised,
            # so a trace and an eval read a SAP 401 as the failure it is.
            self._by_tool_call_id[tool_call_id] = {
                "evidence": evidence,
                "status": "error" if tool_failed(result) else "success",
            }
        except (
            Exception  # noqa: BLE001
        ) as error:  # never fail a tool call over telemetry
            print(f"[EVIDENCE] Extraction failed: {error}")

    def _note_loaded_sop(self, tool_use: dict, result: dict | None) -> None:
        """Remember the process type of a `load_sop` call the Gateway served.

        Served means the call did not fail and its reply opens with the
        `[sop <key> version <id>]` header `sop_tools/handler.py` writes, which only a
        reply the run checks admitted and the SOP bucket answered carries. The model chose
        the argument; whether it names an exception SOP of this case's pack is decided
        later, against the pack, not here.
        """
        if _tool_key(tool_use.get("name", "")) != "load_sop" or tool_failed(result):
            return
        # Searched, not matched: the reply can arrive JSON-encoded (evidence.py).
        if not _SOP_HEADER_RE.search(result_text(result)):
            return
        process_type = (tool_use.get("input") or {}).get("process_type")
        if isinstance(process_type, str) and process_type.strip():
            self._loaded_sops.append(process_type.strip())

    def loaded_sops(self) -> list[str]:
        """The process types `load_sop` served this run, oldest first."""
        return list(self._loaded_sops)

    def evidence_by_tool_call_id(self) -> dict[str, dict]:
        """Merge material for _save_trace_to_ddb, keyed by the same
        `tool_call_id` that segments.py already records."""
        return self._by_tool_call_id

    def status_for(self, tool_call_id: str | None) -> str | None:
        """A call's status (`error` when `tool_failed`), or None if this hook never saw it.

        The AG-UI protocol has no error field on TOOL_CALL_RESULT, so a failed tool
        streams as an ordinary result and renders green until the page is reloaded and
        the persisted `segment.status` takes over. This hook is the only place the
        SDK-native status exists mid-stream: Strands fires AfterToolCallEvent before
        re-emitting the ToolResultEvent the adapter turns into TOOL_CALL_RESULT.
        """
        return (self._by_tool_call_id.get(tool_call_id or "") or {}).get("status")
