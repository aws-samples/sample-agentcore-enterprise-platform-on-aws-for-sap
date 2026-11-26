# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
SAP Exception Processing Agent

Multi-skill agent for processing SAP exceptions. The wired domain is finance_ops
(supplier-invoice three-way-match exceptions); the Skill Router is domain-agnostic and
loads domain expertise + SOP dynamically based on process_type, so additional domains
can be wired in without agent code changes.

Uses AgentCore Gateway Lambda tools instead of a direct MCP server connection.

This module is the runtime's entrypoint and nothing else: the FastAPI app, the agent
it builds per request (``_create_agent``), and the one route that streams a run
(``agent_stream``). Everything a run *uses* lives beside it under ``utils/`` —

* ``utils/payload`` — inbound payload → skill, prompt, session id
* ``utils/hooks`` — the turn-limit, metrics, and evidence hooks
* ``utils/gateway_client`` — the Gateway token and the MCP client
* ``utils/case_persistence`` — the live snapshot, the trace append, the review flag
* ``utils/stream`` — event naming, RUN_ERROR redaction, SSE keepalive

so that reading this file tells you the shape of a run without also telling you how
every part of it works.
"""

import asyncio
import os
import time
import traceback

from ag_ui.core import (
    EventType,
    RunAgentInput,
    RunErrorEvent,
    RunStartedEvent,
)
from ag_ui.encoder import EventEncoder
from ag_ui_strands import StrandsAgent, StrandsAgentConfig, add_ping
from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
from bedrock_agentcore.memory.integrations.strands.session_manager import (
    AgentCoreMemorySessionManager,
)
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from strands import Agent, ModelRetryStrategy
from strands import tool as strands_tool
from strands.hooks import (
    AfterInvocationEvent,
    AfterModelCallEvent,
    AfterToolCallEvent,
    BeforeInvocationEvent,
    BeforeModelCallEvent,
    HookProvider,
    HookRegistry,
)
from strands.models.bedrock import CacheConfig
from strands.vended_plugins.skills import AgentSkills
from utils import registry
from utils.agent_metrics import emit_agent_metrics
from utils.arithmetic import calculator
from utils.auth import (
    extract_user_id_from_headers,
    forwarded_user_token,
    to_memory_actor_id,
)
from utils.case_persistence import (
    _flag_case_for_review,
    _live_run_writer,
    _save_trace_to_ddb,
    identified_exception_type,
)
from utils.conversation import MemorySafeSessionManager, MemorySafeSlidingWindow
from utils.dates import current_time, days_between
from utils.evidence import sop_text_in
from utils.gateway_client import bind_agentcore_workload_token, create_gateway_client
from utils.hooks import EvidenceHook, MaxTurnsHook, MetricsHook
from utils.mcp_topology import RUNTIME_GRANT_HEADER, bare_names
from utils.model_config import (
    guardrail_kwargs,
    max_tokens_kwargs,
    request_fields_kwargs,
    sampling_kwargs,
)
from utils.payload import (
    SCOPE_KEY,
    _build_prompt,
    _case_session_id,
    _input_with_prompt,
    _resolve_system_prompt,
    _runtime_payload,
    _safe_session_id,
    is_ask,
)
from utils.run_channel import run_channel_publisher
from utils.segments import accumulate_segment
from utils.skill_router import SopLoadError, discovery_skills
from utils.snapshot_safe import use_system_random
from utils.stream import (
    _canonical_event,
    _event_name,
    _keepalive_frame,
    _sanitized_run_error,
    _with_keepalive,
)
from utils.tool_order import CaseWriteOrderedExecutor
from utils.tool_result_fence import FencedBedrockModel
from utils.write_gate import wrap_write_gate_tools

# Unconditional: harmless on V1, and a V2 runtime restores this process's RNG state into
# every instance. Runs before the first request, which is before the first span.
use_system_random()

app = FastAPI(title="Business process agent runtime")
add_ping(app, "/ping")

# Model tier → Amazon Bedrock model id. The deploy sets these from platform.yaml (`agents.model_id`
# for sonnet, the use case's `runtime.haiku_model_id` / `opus_model_id`) and checks them
# against `agents.allowed_models`, which scopes this runtime's Amazon Bedrock grant. `opus` is
# absent when the deployment does not serve it; the synth refuses a pack that names it.
MODEL_TIERS = {
    "haiku": os.environ["MODEL_ID_HAIKU"],
    "sonnet": os.environ["MODEL_ID"],
    **(
        {"opus": os.environ["MODEL_ID_OPUS"]} if os.environ.get("MODEL_ID_OPUS") else {}
    ),
}


def _tier_model(tier: str) -> str:
    """The model for a tier, refusing one this runtime does not serve rather than
    running the pack on another model's bill."""
    if tier not in MODEL_TIERS:
        raise ValueError(
            f"model tier {tier!r} is not served by this deployment "
            f"(serves {sorted(MODEL_TIERS)})"
        )
    return MODEL_TIERS[tier]


def _discovery_plugin(skill: dict) -> AgentSkills | None:
    """Build the AgentSkills plugin for the discovery path, or None on the queued path.

    Only the general/chat path gets skills. When `process_type` is known, resolve_skill
    has already injected the right SOP deterministically — swapping that for a model
    decision plus a tool round-trip would trade correctness for nothing.

    None when no SOP could be loaded for any process_type: the plugin would otherwise
    inject "No skills are currently available", contradicting the skills_summary the
    general prompt already carries.
    """
    if not skill.get("discovery"):
        return None
    skills = discovery_skills(
        sop_bucket=os.environ.get("SOP_BUCKET"), packs=skill.get(SCOPE_KEY)
    )
    if not skills:
        return None
    print(f"[SKILL] Discovery mode: {len(skills)} activatable skills")
    return AgentSkills(skills=skills)


def _create_agent(
    skill: dict,
    user_id: str,
    session_id: str,
    process_type: str,
    audit_context: dict | None = None,
    payload: dict | None = None,
    gateway_bearer: str | None = None,
    run_grant: str | None = None,
) -> tuple[
    Agent,
    MetricsHook,
    MaxTurnsHook,
    AgentCoreMemorySessionManager,
    HookProvider,
    EvidenceHook,
]:
    """Create the request-specific ERP agent template and adapter dependencies."""
    config = skill.get("config", {})
    # Stamped by payload._resolve_system_prompt from the run's `rehearse` or `ask` flag: a
    # pack exercised before it goes Live, or a person's question, so no tool that writes
    # SAP, moves a case, cuts a ticket or sends mail is loaded. The run's grant carries
    # `rehearsal` or `ask` too, and the run checks refuse those tools whatever is loaded.
    read_only = bool(skill.get("read_only"))

    model_tier = skill.get("model_tier", "sonnet")
    model_id = _tier_model(model_tier)
    max_turns = skill.get("max_turns", 15)

    # Hoisted so the [COST] line below can report the ceiling without calling
    # max_tokens_kwargs() twice — the second call would re-log its override warning.
    token_ceiling = max_tokens_kwargs()

    # Fenced: every tool result is sanitized and fenced in the model's view (T31).
    bedrock_model = FencedBedrockModel(
        model_id=model_id,
        cache_config=CacheConfig(strategy="auto"),
        **sampling_kwargs(model_id, 0.1),
        **token_ceiling,
        **guardrail_kwargs(),
        **request_fields_kwargs(config),
    )

    memory_id = os.environ.get("MEMORY_ID")
    if not memory_id:
        raise ValueError("MEMORY_ID environment variable is required")

    session_manager = MemorySafeSessionManager(
        agentcore_memory_config=AgentCoreMemoryConfig(
            memory_id=memory_id,
            session_id=session_id,
            actor_id=to_memory_actor_id(user_id),
            # Historical toolUse/toolResult blocks (e.g. large SAP OData responses)
            # are dropped from restored history; only text turns are reloaded. That
            # filtering breaks role alternation, which the manager subclass repairs.
            filter_restored_tool_context=True,
        ),
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
    )

    mcp_client = create_gateway_client(
        config.get("gateway_tools"),
        audit_context=audit_context,
        read_only=read_only,
        sap_system=config.get("sap_system"),
        bearer=gateway_bearer,
        run_grant=run_grant,
    )

    tools = [mcp_client] + [
        strands_tool(f) for f in (calculator, current_time, days_between)
    ]

    print(
        f"[COST] model_tier={model_tier}, model_id={model_id}, max_turns={max_turns}, "
        f"max_tokens={token_ceiling['max_tokens']}, cache=auto, "
        f"effort={config.get('model_effort', 'default')}, "
        f"context_editing={config.get('context_editing') or 'off'}, "
        f"run_budget={config.get('run_budget') or 'default'}"
    )

    turns_hook = MaxTurnsHook(max_turns)
    metrics_hook = MetricsHook(
        process_type, model_tier, model_id, turns_hook, config.get("run_budget")
    )
    evidence_hook = EvidenceHook(
        sop_baseline=sop_text_in(skill.get("system_prompt", ""))
    )

    skills_plugin = _discovery_plugin(skill)
    if skills_plugin:
        # Passed as plain tools rather than via Agent(plugins=...): the plugin registry
        # lives on the template only, and the adapter copies the tool registry at init.
        tools.extend(skills_plugin.tools)

    class _Hooks(HookProvider):
        """Register turn-limit, metrics and evidence callbacks on the adapter's clone."""

        def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
            registry.add_callback(BeforeInvocationEvent, turns_hook.reset)
            registry.add_callback(BeforeModelCallEvent, turns_hook.check)
            registry.add_callback(AfterModelCallEvent, metrics_hook.check_budget)
            registry.add_callback(BeforeInvocationEvent, metrics_hook.on_start)
            registry.add_callback(AfterInvocationEvent, metrics_hook.on_end)
            registry.add_callback(BeforeInvocationEvent, evidence_hook.on_start)
            registry.add_callback(AfterToolCallEvent, evidence_hook.on_tool_call)
            registry.add_callback(AfterToolCallEvent, turns_hook.check_tool)
            # Strands registers a plugin's @hook methods via its own plugin registry,
            # which only exists on the template. `plugins` is not among the params
            # ag_ui_strands._extract_agent_kwargs forwards (Agent keeps no `.plugins`
            # attribute), so without re-registering here the per-thread clone that
            # actually serves the turn would carry the `skills` tool but never inject
            # <available_skills> — the model would be told to activate skills it
            # cannot see.
            for callback in getattr(skills_plugin, "hooks", []):
                for event_type in getattr(callback, "_hook_event_types", []):
                    registry.add_callback(event_type, callback)

    hook_provider = _Hooks()

    # This is a template. ag-ui-strands creates the invocation agent and receives
    # the request-specific Memory session manager and hooks explicitly below.
    agent = Agent(
        name="SAPExceptionAgent",
        system_prompt=skill["system_prompt"],
        tools=tools,
        model=bedrock_model,
        trace_attributes={"user.id": user_id, "session.id": session_id},
        retry_strategy=ModelRetryStrategy(max_attempts=4, initial_delay=2),
        # Forwarded to the adapter's per-thread clone (public `tool_executor`).
        tool_executor=CaseWriteOrderedExecutor(),
        # Stated rather than defaulted: `conversation_manager` IS forwarded to the
        # adapter's per-thread clone, so leaving it unset silently ran window_size=40
        # there — below the message count a max_turns run produces, so mid-run
        # eviction was routine, and eviction is what triggers the restore-offset bug
        # MemorySafeSlidingWindow exists to defuse.
        #
        # A high window because eviction drops the OLDEST messages, the worst possible
        # edit for the CacheConfig(strategy="auto") prefix above: cache reads run
        # 11-14x input tokens, and moving the
        # prefix converts those hits into writes. Oversized tool results are what
        # actually blow the window, and reduce_context truncates those (first/last
        # 200 chars) before it will evict anything.
        conversation_manager=MemorySafeSlidingWindow(
            window_size=120,
            should_truncate_results=True,
        ),
    )

    registered = list(agent.tool_registry.registry.keys())
    print(f"[TOOLS] {len(registered)} tools registered: {registered}")

    # A routed pack holds only its own SAP system's tools (create_gateway_client), so
    # they are bare; two systems' tools on one run keep their targets.
    renames = bare_names(registered)
    for old, new in renames.items():
        tool = agent.tool_registry.registry.pop(old)
        tool._agent_tool_name = new
        agent.tool_registry.registry[new] = tool
    if renames:
        print(f"[TOOLS] Renamed {len(renames)} tools: {list(renames.values())}")

    # The write guards and verifier re-read SAP and ask a model, which the Gateway
    # cannot; the run checks at the Gateway hold everything else. Inert until a pack
    # declares one.
    guarded = wrap_write_gate_tools(
        agent,
        payload or {},
        model_tiers=MODEL_TIERS,
        verifier_log=metrics_hook.verifier_calls,
    )
    if guarded:
        print(f"[WRITE-GUARD] Guard wrappers: {guarded} tool(s)")

    return (
        agent,
        metrics_hook,
        turns_hook,
        session_manager,
        hook_provider,
        evidence_hook,
    )


@app.post("/invocations")
async def agent_stream(
    input_data: RunAgentInput,
    request: Request,
) -> StreamingResponse:
    """Run the request-specific ERP agent and emit the native AG-UI SSE contract."""
    encoder = EventEncoder(accept=request.headers.get("accept"))

    async def event_generator():
        payload = _runtime_payload(input_data)
        # The AG-UI thread id is authoritative when the caller supplies one, but it is
        # caller-controlled, so it is collapsed to the charset AgentCore Memory ids
        # allow. Falling back to the case identity gives a stable, Memory-legal
        # session id — the codec also clears the Runtime's minimum id length, which a
        # bare case_id does not.
        session_id = (
            _safe_session_id(input_data.thread_id)
            or _case_session_id(payload)
            or "default-session"
        )
        process_type = payload.get("process_type", "general")
        case_id = payload.get("case_id", "")
        trigger = payload.get("trigger", "manual")
        model_tier = "sonnet"
        agent_created = False
        started_emitted = False
        terminal_emitted = False
        metrics_hook = None
        turns_hook = None
        evidence_hook = None
        segments: list = []
        live_writer = None
        run_publisher = None
        prompt = ""
        outcome = "unknown"
        audit_context: dict = {}
        # Captured out of `skill` here because the trace is written from the
        # finally: below, where resolution may have raised and `skill` be unbound.
        sop_version = ""
        sop_key = ""
        sop_version_id = ""
        # The routed pack and SOP type, which decide the case's exception type.
        routed_pack = ""
        routed_type = ""
        # A conversation turn (`POST /runs` without `intent: process`): read from the
        # payload, not the skill, so it holds when resolving the skill is what raised.
        asking = is_ask(payload) and not payload.get("rehearse")

        def emit(event):
            """The event to yield, with a copy on the case's channel.

            Every yield in this generator routes through here. `event` is reassigned
            several times below — MAX_TURNS, sanitization, error status — so the copy
            has to be taken from the value actually going out, not from the `canonical`
            dict computed earlier in the loop.

            Defined before the try: because the except SopLoadError handler yields
            through it, and the SOP resolution that raises runs before the publisher
            is built. `run_publisher` is read at call time, so the None above is all
            this needs to be bound on every path.
            """
            # publish() swallows its own failures, but _canonical_event runs out here,
            # so a model_dump that raised would take the event's yield down with it.
            # Losing the copy may never cost the run, whatever shape arrives later.
            # encoder.encode stays outside: that call IS the run, and swallowing a
            # failure there would hide a real defect behind a channel-shaped log line.
            try:
                if run_publisher is not None:
                    run_publisher.publish(_canonical_event(event))
            except Exception as error:  # noqa: BLE001
                # Named, because one run's dark channel is indistinguishable from
                # another's in a log stream that carries every concurrent run.
                print(
                    f"[CHANNEL] event not published for {case_id} "
                    f"run {input_data.run_id}: {error}"
                )
            return encoder.encode(event)

        try:
            bind_agentcore_workload_token(request)
            user_id = extract_user_id_from_headers(request.headers)
            # The person's own token, when POST /runs forwarded one for an obo SAP
            # system: the Gateway bearer for this run instead of the vault token.
            gateway_bearer = forwarded_user_token(request.headers, user_id)
            # The invoker's secret for this run's grant; the browser never holds it.
            run_grant = request.headers.get(RUNTIME_GRANT_HEADER)
            # Here, before anything reads a pack, and never mid-run: a bundle import
            # reaches a warm session on its next invocation instead of its idle timeout.
            # ponytail: one session per microVM, so concurrent invocations sharing this
            # process are one case's own turns; pin a per-invocation snapshot if not.
            registry.refresh_for_request()
            skill = _resolve_system_prompt(payload)
            # A queued case names no process type; the routed skill knows it.
            process_type = skill.get("process_type") or process_type or "general"
            model_tier = skill.get("model_tier", "sonnet")
            sop_version = skill.get("sop_version") or ""
            sop_key = skill.get("sop_key") or ""
            sop_version_id = skill.get("sop_version_id") or ""
            routed_pack = skill.get("pack_id") or ""
            routed_type = skill.get("process_type") or ""
            prompt = _build_prompt(payload)
            runtime_input = _input_with_prompt(input_data, prompt)
            # Publishes the run's shape onto the case while it streams, so a tab that
            # joins or reloads mid-run has something to render. None when there is no
            # table or no parseable case.
            live_writer = _live_run_writer(case_id, input_data.run_id, trigger, prompt)
            # A second copy of this stream on the channel the case names — or, for the
            # case-less conversation, the one its session id names — so a tab that did
            # not start the run can follow it live. None when the Event API is not
            # configured or neither key names a channel.
            run_publisher = run_channel_publisher(
                case_id, input_data.run_id, input_data.thread_id
            )

            audit_context = {
                # `initiator` is the acting subject: the delegated identity header when
                # the invoker forwarded one, and the Runtime-validated bearer's own `sub`
                # otherwise. So a case a human enqueued from the UI attributes to that
                # human here, in the x-audit-initiator header, and in the persisted
                # trace — the same value their interactive turns resolve to. A run with
                # no human behind it (a poller delivery) has no delegated header and
                # still attributes to the machine subject. Why the delegated value is
                # trustworthy — the Runtime allowlists that header only where its
                # authorizer accepts the machine client alone — is in
                # utils/auth.get_inbound_identity_from_headers. Read that before
                # widening either half.
                "correlation_id": input_data.run_id,
                "initiator": user_id,
                "trigger": trigger,
            }

            print(f"[AG-UI] User: {user_id}, Thread: {session_id}")
            print(f"[AG-UI] Run: {input_data.run_id}, process_type: {process_type}")
            print(f"[AG-UI] Prompt preview: {prompt[:200]}...")

            (
                template_agent,
                metrics_hook,
                turns_hook,
                session_manager,
                hook_provider,
                evidence_hook,
            ) = _create_agent(
                skill,
                user_id,
                session_id,
                process_type,
                audit_context=audit_context,
                payload=payload,
                gateway_bearer=gateway_bearer,
                run_grant=run_grant,
            )
            agent_created = True

            agui_agent = StrandsAgent(
                # Constructed AFTER _create_agent, which is load-bearing: StrandsAgent
                # captures the tool registry at init, and _create_agent is what pops and
                # re-registers the SAP tools with the `target___` prefix stripped.
                # Building the adapter first would expose prefixed tool names that no
                # longer match the names SOPs reference. Verified against
                # ag-ui-strands 0.2.3; see agent/tests/test_adapter_version_pin.py.
                agent=template_agent,
                name="SAPExceptionAgent",
                description="Skill-routed SAP exception processing agent",
                config=StrandsAgentConfig(
                    session_manager_provider=lambda _: session_manager,
                    emit_messages_snapshot=True,
                    replay_history_into_strands=False,
                ),
                hooks=[hook_provider],
            )

            # An immediate heartbeat confirms the stream opened before the model has
            # produced anything, and starts the cadence for the idle gaps that follow.
            keepalive_started_at = time.monotonic()
            yield _keepalive_frame(0, keepalive_started_at)

            async for kind, item in _with_keepalive(agui_agent.run(runtime_input)):
                if kind == "keepalive":
                    yield _keepalive_frame(item, keepalive_started_at)
                    continue
                event = item
                event_name = _event_name(event)
                if (
                    event_name == "RUN_FINISHED"
                    and turns_hook is not None
                    and turns_hook.cancelled
                ):
                    reached = {
                        "RUN_BUDGET_EXCEEDED": "The agent reached its configured run budget.",
                        "REPEATED_TOOL_ERRORS": (
                            "The agent stopped after several tool calls failed in a "
                            "row; SAP or a tool may be unavailable."
                        ),
                    }.get(
                        turns_hook.stop_code,
                        "The agent reached its configured processing limit.",
                    )
                    event = RunErrorEvent(
                        type=EventType.RUN_ERROR,
                        message=(
                            f"{reached} Review the case state for actions completed "
                            "before it stopped."
                        ),
                        code=turns_hook.stop_code,
                    )
                    event_name = "RUN_ERROR"

                canonical = _canonical_event(event)
                accumulate_segment(segments, canonical)
                if live_writer is not None:
                    # Sync write on the event loop, ≤1 per 2 s; asyncio.to_thread if it shows up in stream latency
                    live_writer.maybe_write(segments)
                started_emitted = started_emitted or event_name == "RUN_STARTED"
                if event_name == "RUN_FINISHED":
                    outcome = "complete"
                    terminal_emitted = True
                elif event_name == "RUN_ERROR":
                    # Sanitize first. It is what mints MAX_TOKENS_REACHED out of the
                    # adapter's generic STRANDS_ERROR, and the outcome below reads that
                    # code — reversed, a run that stopped on purpose at its configured
                    # limit was recorded as a failure. Keying on the code rather than on
                    # turns_hook.cancelled also covers a real error raised while the
                    # hook happened to have fired.
                    event = _sanitized_run_error(
                        event, canonical, run_id=input_data.run_id
                    )
                    outcome = (
                        # "Stopped early" in the timeline, and not a danger tone in the
                        # traces panel: the agent hit a limit, it did not fail.
                        "cancelled"
                        if getattr(event, "code", "")
                        in (
                            "MAX_TURNS_REACHED",
                            "MAX_TOKENS_REACHED",
                            "RUN_BUDGET_EXCEEDED",
                            "REPEATED_TOOL_ERRORS",
                        )
                        else "error"
                    )
                    terminal_emitted = True
                elif (
                    event_name == "TOOL_CALL_RESULT"
                    and evidence_hook is not None
                    and evidence_hook.status_for(canonical.get("toolCallId")) == "error"
                ):
                    # AG-UI defines no failure field on this event, so the reducer would
                    # render a failed call as complete until a reload picks up the
                    # persisted segment.status. ag_ui's models allow extras, so the
                    # SDK-native status rides along on the same event.
                    event = event.model_copy(update={"status": "error"})
                yield emit(event)

            if not terminal_emitted:
                outcome = "error"
                error_event = RunErrorEvent(
                    type=EventType.RUN_ERROR,
                    message="The agent stream ended without a terminal event.",
                    code="AGENT_STREAM_INCOMPLETE",
                )
                terminal_emitted = True
                yield emit(error_event)

        except (GeneratorExit, asyncio.CancelledError):
            # Neither is an Exception, so the handler below cannot see them and a run
            # torn down from the consumer side used to leave nothing in the log but an
            # OpenTelemetry "Failed to detach context" — no reason, no terminal event.
            # Starlette closes this generator when the client disconnects, so this is
            # the only place that fact is observable. Re-raise: a generator may not
            # yield during GeneratorExit, so no event can be emitted here.
            # The `disconnected` outcome the timeline and traces panel already render,
            # and which until now nothing ever wrote.
            outcome = "disconnected"
            print(
                f"[AG-UI DISCONNECTED] run {input_data.run_id} on session {session_id}: "
                f"stream closed by the consumer after {len(segments)} segment(s); "
                f"terminal_emitted={terminal_emitted}. The client disconnected or "
                "an intermediate hop dropped the connection."
            )
            raise

        except SopLoadError as error:
            print(f"[AG-UI ERROR] SOP load failed: {error}")
            outcome = "error"
            # T29: the case view renders status_reason verbatim to any operator
            # the entitlement floor admits. The full text — which can carry an S3
            # key or a boto3/library error string — already went to the log line
            # above; the case record gets only the exception's class name.
            # A question changes no case, so a failed one is only reported back.
            if not asking:
                _flag_case_for_review(
                    case_id, f"SOP failed to load: {type(error).__name__}"
                )
            emit_agent_metrics(
                process_type=process_type,
                model_tier=model_tier,
                turns=0,
                input_tokens=0,
                output_tokens=0,
                cache_read_tokens=0,
                cache_write_tokens=0,
                latency_ms=0,
                success=False,
            )
            if not started_emitted:
                yield emit(
                    RunStartedEvent(
                        type=EventType.RUN_STARTED,
                        thread_id=input_data.thread_id,
                        run_id=input_data.run_id,
                    )
                )
                started_emitted = True
            if not terminal_emitted:
                yield emit(
                    RunErrorEvent(
                        type=EventType.RUN_ERROR,
                        message=(
                            "The Standard Operating Procedure could not be loaded. "
                            "The case was flagged for manual review."
                        ),
                        code="SOP_LOAD_ERROR",
                    )
                )
                terminal_emitted = True

        except Exception as error:  # noqa: BLE001
            print(f"[AG-UI ERROR] {error}")
            traceback.print_exc()
            outcome = "error"
            if not agent_created:
                emit_agent_metrics(
                    process_type=process_type,
                    model_tier=model_tier,
                    turns=0,
                    input_tokens=0,
                    output_tokens=0,
                    cache_read_tokens=0,
                    cache_write_tokens=0,
                    latency_ms=0,
                    success=False,
                )
            if not started_emitted:
                yield emit(
                    RunStartedEvent(
                        type=EventType.RUN_STARTED,
                        thread_id=input_data.thread_id,
                        run_id=input_data.run_id,
                    )
                )
                started_emitted = True
            if not terminal_emitted:
                yield emit(
                    RunErrorEvent(
                        type=EventType.RUN_ERROR,
                        message="The agent run failed.",
                        code="ERP_AGENT_ERROR",
                    )
                )
                terminal_emitted = True

        finally:
            # Flush the last batch before the trace write, which is the slow part.
            if run_publisher is not None:
                run_publisher.close()
            if segments and case_id:
                metrics = metrics_hook.trace_metrics() if metrics_hook else None
                # From the SOPs the run was given, never from what the model wrote. A
                # question classifies nothing: it may read any SOP to answer.
                exception_type = (
                    identified_exception_type(
                        routed_pack,
                        [routed_type]
                        + (evidence_hook.loaded_sops() if evidence_hook else []),
                    )
                    if routed_pack and not asking
                    else None
                )
                _save_trace_to_ddb(
                    case_id,
                    trigger,
                    prompt,
                    segments,
                    outcome,
                    metrics=metrics,
                    audit_context=audit_context,
                    evidence_by_id=(
                        evidence_hook.evidence_by_tool_call_id()
                        if evidence_hook
                        else None
                    ),
                    sop_version=sop_version,
                    sop_key=sop_key,
                    sop_version_id=sop_version_id,
                    exception_type=exception_type,
                    intent="ask" if asking else None,
                )

    return StreamingResponse(
        event_generator(),
        media_type=encoder.get_content_type(),
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("AGENTCORE_HOST", "0.0.0.0"),  # nosec B104 - container entrypoint, must be reachable from outside
        port=int(os.environ.get("AGENTCORE_PORT", "8080")),
    )
