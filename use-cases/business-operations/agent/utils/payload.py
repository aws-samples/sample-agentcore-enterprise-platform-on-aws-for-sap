# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Turning an inbound payload into the three things a run needs.

Every run arrives as an AG-UI ``RunAgentInput`` with a legacy ERP envelope tucked
into ``forwarded_props``, and three questions have to be answered before an agent
can be built:

* **Which skill?** ``_resolve_system_prompt`` — the process type decides the system
  prompt, model tier, and turn budget. When the caller does not state one, the case
  id is recovered from whatever the payload does carry and looked up.
* **Which prompt?** ``_build_prompt`` — one branch per trigger. This is where
  untrusted text (a ticket reply, a webhook body) is sanitized and fenced, so the
  branch that reads external content is the same branch that neutralizes it.
* **Which session?** ``_safe_session_id`` / ``_case_session_id`` — AgentCore Memory
  restricts the id charset, and a caller-supplied thread id does not respect it.

Nothing here talks to the agent loop or the stream; it is all payload-in,
value-out, which is why it is testable without importing strands.
"""

import os
import re

import boto3
from ag_ui.core import RunAgentInput, UserMessage

from .case_key import (
    describe_case,
    to_case_key,
    to_runtime_session_id,
    try_normalize_case_id,
)
from .content_filter import fence_data, sanitize_external_content
from .registry import process_type_pack_map
from .skill_router import list_skills, resolve_skill

#: The caller's entitled packs, stamped by `POST /runs` from the group claim it
#: verified. Absent means unrestricted: an unattended queued run, or a deployment that
#: declares no entitlement. The run checks hold the run's tools to the same set at the
#: Gateway; routing is the one place it bites in the agent, because nothing else sees
#: which SOP a prompt-named case resolves to.
SCOPE_KEY = "visible_packs"


def scoped_packs(payload: dict) -> frozenset[str] | None:
    """The entitled pack set stamped by the invoke route, or None when unrestricted."""
    raw = payload.get(SCOPE_KEY)
    if not isinstance(raw, list):
        return None
    return frozenset(str(p) for p in raw)


def may_see_process_type(process_type: str, scope: frozenset[str]) -> bool:
    """Whether `scope` covers the pack that declares `process_type`. A process type no
    pack declares is refused, matching `entitlement.may_see`."""
    pack = process_type_pack_map().get(process_type)
    return pack is not None and pack in scope


# Fallback prompt when no process_type is provided (chat mode)
GENERAL_PROMPT = """You are an expert SAP exception handling specialist. You autonomously
investigate and resolve SAP exceptions across all domains.

Available skills: {skills_summary}

## AUTONOMOUS EXECUTION (CRITICAL)
When a user asks you to process a case:
1. IMMEDIATELY call get_case_state to retrieve the case details.
2. Identify the process_type from the case data.
3. If an <available_skills> block lists a skill matching that process_type, activate it
   with the skills tool — it returns the full Standard Operating Procedure along with the
   service and entity names to use. Otherwise use search_sap_sops to find the SOP, and
   discover the service once with find_sap_services and get_metadata.
4. Follow the SOP step by step WITHOUT asking the user for permission.
5. NEVER present a menu of options or ask "What would you like me to do first?"
6. Only stop for explicit approval gates defined in the SOP.

Use odata_read/odata_count for SAP reads and odata_create/odata_update/odata_function_import for SAP writes.
Pass `select` on every odata_read, naming only the fields you need — an unselected read returns
every field of the entity and is re-read on every later turn.
Use update_case_state to record the case's status and the note that goes with it. The
case's own attributes (amounts, dates, supplier, exception type) are what the poller read
out of SAP — correct them in SAP, not on the case.
"""


#: A ticket category reaches the prompt unfenced, so it must be an identifier, not prose.
_TICKET_CATEGORY = re.compile(r"[a-z][a-z0-9_]{0,63}")


#: Appended to whatever prompt a rehearsal run assembles. The withholding itself is done
#: by `mcp_topology.build_tool_filters` — telling the agent is what stops it planning a
#: write, failing on a tool that is simply absent, and reporting that as a blocked case.
REHEARSAL_NOTE = """

## THIS RUN IS A REHEARSAL
This pack is being exercised before it goes live. No tool that writes SAP, moves a case,
cuts a ticket or sends a notification is loaded — do not plan around one being available.
Read what you need, follow the SOP to the point of the decision, and then say plainly what
you WOULD post: the entity, the fields and the values. That statement is the deliverable.
"""


#: Appended to every conversation turn's prompt: `POST /runs` without `intent: process`.
#: The tools are withheld as for a rehearsal (`mcp_topology.build_tool_filters`) and the
#: run's grant carries `ask`, so the run checks refuse them too. Telling the agent is what
#: turns a request to act into an answer that says where the action is, instead of a
#: failed call or a bare refusal.
ASK_NOTE = """

## THIS TURN ANSWERS A QUESTION
A person is asking you something. No tool that writes SAP, changes a case, opens a ticket or
sends a notification is loaded in this turn, and none will be: read what you need and answer.
Any procedure above describes a full run; follow it here only to explain or to look things up.
If the person asks you to act (post, release, change or close the case, open a ticket, notify
someone), do not just decline. Say what you would do and why, then say where they do it: Process
on the case runs the procedure, which writes within its limits; a change already waiting for a
person is approved or denied in the case's decision.
"""

#: The payload key `POST /runs` and the invoker stamp on a conversation turn.
ASK_KEY = "ask"


#: A decision option's id, admitted outside the fence only in this form.
_OPTION_ID = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


def _approves(decision: object) -> str:
    if decision == "approved":
        return "It approves the write that option names."
    return "It approves no SAP write."


def _option_lines(option: object, *, source: str) -> tuple[str, str] | None:
    """`(id, fenced label)` for the option a person chose, or None for no option.

    The id is server-checked against the case's set and is an identifier, so it is
    the instruction; the label is the agent's own prose, so it is data.
    """
    if not isinstance(option, dict) or not _OPTION_ID.fullmatch(
        str(option.get("id", ""))
    ):
        return None
    label = option.get("label")
    block = (
        fence_data(sanitize_external_content(label, source=source), source=source)
        + "\n"
        if isinstance(label, str) and label
        else ""
    )
    return option["id"], block


def is_ask(payload: dict) -> bool:
    """Whether this run is a conversation turn, which reads only."""
    return payload.get(ASK_KEY) is True


def _resolve_system_prompt(payload: dict) -> dict:
    """Resolve skill config: skill-routed if process_type present, general defaults otherwise.

    Also where the caller's pack entitlement first bites. Routing is the gap the API floor
    cannot cover: a chat turn needs no `case_id` to pass `POST /runs`, and the case id
    recovered from prompt text below then resolves a `process_type` — so naming an AP case
    in a sentence would hand an AR-only operator the AP SOP with its tolerances and
    contacts already substituted. A `process_type` outside the entitled set is dropped
    rather than refused: the run continues in general mode, where the run checks refuse the
    case data itself.
    """
    process_type = payload.get("process_type")
    inner = payload.get("payload", {})
    # None when the invocation carries no set at all: the floor is inert.
    scope = scoped_packs(payload)
    # Authorized by `POST /runs`, which refuses a rehearsal to anyone who may not author
    # the pack. Stamped on BOTH returns below: a rehearsal whose process type was dropped
    # by the scope check falls to general mode, and must not get its tools back on the way.
    rehearsal = bool(payload.get("rehearse"))
    # Stamped the same way, by the invoker, on every run `POST /runs` did not start as
    # Process. Also on both returns, for the same reason.
    ask = not rehearsal and is_ask(payload)

    # If no process_type, try to extract case_id and look it up in DynamoDB
    if not process_type:
        case_id = try_normalize_case_id(payload.get("case_id"))
        if not case_id:
            user_prompt = (
                payload.get("user_prompt")
                or payload.get("prompt")
                or inner.get("user_prompt")
                or ""
            )
            case_id = _extract_case_id(user_prompt)
        if case_id:
            process_type, case_natural_key = _lookup_process_type(case_id)
            if process_type:
                print(
                    f"[ROUTING] Resolved process_type={process_type} from "
                    f"{describe_case(case_id, case_natural_key)}"
                )

    # Checked wherever the process type came from — the caller may state one directly.
    if (
        process_type
        and scope is not None
        and not may_see_process_type(process_type, scope)
    ):
        print(
            f"[SCOPE] Not routing to process_type={process_type}: outside the caller's "
            f"entitled packs {sorted(scope)}"
        )
        process_type = None

    if process_type:
        sop_bucket = os.environ.get("SOP_BUCKET")
        skill = resolve_skill(process_type, sop_bucket=sop_bucket)
        # No sop_loaded flag to report: resolve_skill raises rather than return a
        # skill without its SOP, and basic_agent turns that into a case flagged for
        # review. A routed run that got this far has its procedure.
        print(f"[SKILL] Routed to pack={skill['pack_id']}, sop={skill['sop_key']}")
        skill[SCOPE_KEY] = scope
        return _mark(skill, rehearsal=rehearsal, ask=ask)

    # General mode — the process type is unknown, so the agent has to classify before
    # it can follow a procedure. AgentSkills advertises one skill per process_type and
    # serves the full SOP on activation; the skills_summary below is the fallback for
    # when no skill has a loadable SOP (see _discovery_plugin).
    #
    # Narrowed to the entitled packs: the summary names every use case and its process
    # types, which is the shape of the estate, and an unentitled operator asking "what
    # can you do" should not be told about a domain they cannot touch.
    skills = list_skills(packs=scope)
    summary = "\n".join(
        f"- {s['display_name']} ({', '.join(s['process_types'])}): {s['description']}"
        for s in skills
    )
    general = {
        "system_prompt": GENERAL_PROMPT.format(skills_summary=summary),
        "model_tier": "sonnet",
        "max_turns": 15,
        "discovery": True,
        SCOPE_KEY: scope,
    }
    return _mark(general, rehearsal=rehearsal, ask=ask)


def _mark(skill: dict, *, rehearsal: bool, ask: bool) -> dict:
    """Flag a read-only run, and tell the agent so in its own prompt.

    `basic_agent._create_agent` reads `read_only` and withholds every side-effecting
    tool. `ask` is kept beside it so the trace can say the turn was a question.
    """
    if not (rehearsal or ask):
        return skill
    note = REHEARSAL_NOTE if rehearsal else ASK_NOTE
    print(
        f"[{'REHEARSAL' if rehearsal else 'ASK'}] Side-effecting tools withheld "
        "for this run"
    )
    skill["read_only"] = True
    skill["rehearsal" if rehearsal else ASK_KEY] = True
    skill["system_prompt"] = (skill.get("system_prompt") or "") + note
    return skill


def _extract_case_id(text: str) -> str | None:
    """Extract a case_id from a free-text prompt: "case ap-bchzfkch6kxm6uwqvqe5hhlkvx".

    `try_normalize_case_id` is what decides whether the match is really an id.
    """
    m = re.search(r"(?i)case[:\s]+([a-z0-9][a-z0-9-]+)", text)
    return try_normalize_case_id(m.group(1)) if m else None


def _safe_session_id(thread_id: str | None) -> str | None:
    """Collapse a caller-supplied thread id to the charset Memory session ids allow.

    Same shape of control as ``to_memory_actor_id`` applies to actor ids: the value
    reaches AgentCore Memory, so an out-of-charset character (historically `#`) has
    to be replaced rather than forwarded.
    """
    if not thread_id:
        return None
    return re.sub(r"[^a-zA-Z0-9\-_/:]", "-", str(thread_id))


def _case_session_id(payload: dict) -> str | None:
    """Derive a stable Memory session id from the payload's case identity, if any."""
    case_id = try_normalize_case_id(payload.get("case_id"))
    return to_runtime_session_id(case_id) if case_id else None


def _lookup_process_type(case_id: str) -> tuple[str | None, dict | None]:
    """Look up ``(process_type, key)`` from the DynamoDB cases table.

    The natural key comes back with it because this is the read that resolves an
    opaque id: routing is the first thing an operator reads when a case goes to the
    wrong skill, and a digest alone does not say which SAP object it was. `key` is a
    DynamoDB reserved word, hence the expression-name alias.
    """
    table_name = os.environ.get("CASES_TABLE")
    if not table_name:
        return None, None
    try:
        table = boto3.resource("dynamodb").Table(table_name)
        resp = table.get_item(
            Key=to_case_key(case_id),
            ProjectionExpression="process_type, #k",
            ExpressionAttributeNames={"#k": "key"},
        )
        item = resp.get("Item", {})
        return item.get("process_type"), item.get("key")
    except Exception as e:  # noqa: BLE001
        print(f"[ROUTING] Case lookup failed for {case_id}: {e}")
        return None, None


def _build_prompt(payload: dict) -> str:
    """Build agent prompt by dispatching on trigger type.

    SQS-invoked payloads have: {case_id, trigger, payload: {...}}
    Chat/frontend payloads have: {case_id, user_prompt, process_type, ...}
    """
    case_id = payload.get("case_id", "")
    trigger = payload.get("trigger", "")
    ctx = payload.get("payload", {})

    # Ticket action — reviewer approved/denied/replied
    if trigger == "ticket-action":
        ticket_id = ctx.get("ticket_id", "")
        decision = ctx.get("ticket_decision", "")
        response_text = ctx.get("response_text", "")
        if decision == "replied":
            if response_text:
                fenced_response = fence_data(
                    sanitize_external_content(response_text, source="ticket-reply"),
                    source="ticket-reply",
                    ticket_id=ticket_id,
                )
                return (
                    f"Resume case: {case_id}\n"
                    f"Ticket {ticket_id} received a free-text reply from a human reviewer.\n\n"
                    f"{fenced_response}\n\n"
                    f"Call demo_get_ticket for full details, then incorporate this response and continue per SOP."
                )
            return (
                f"Resume case: {case_id}\n"
                f"Ticket {ticket_id} received a free-text reply from a human reviewer.\n"
                f"Call demo_get_ticket to read the durable response, then continue per SOP."
            )
        # `category` is outside the fence because an SOP clause keys on it, so it is
        # admitted only as a bare identifier. The title is agent-written prose: data.
        category = ctx.get("category", "")
        category_line = (
            f"Ticket category: {category}\n"
            if isinstance(category, str) and _TICKET_CATEGORY.fullmatch(category)
            else ""
        )
        title = ctx.get("title", "")
        title_block = (
            fence_data(
                sanitize_external_content(title, source="ticket-title"),
                source="ticket-title",
                ticket_id=ticket_id,
            )
            + "\n"
            if isinstance(title, str) and title
            else ""
        )
        chosen = _option_lines(ctx.get("option"), source="ticket-option")
        if chosen:
            line, block = chosen
            return (
                f"Resume case: {case_id}\n"
                f"A human reviewer chose the option '{line}' on ticket {ticket_id}. "
                f"{_approves(decision)}\n"
                f"{category_line}{block}"
                f"Retrieve case state, then carry out that outcome per SOP."
            )
        return (
            f"Resume case: {case_id}\n"
            f"Ticket {ticket_id} has been {decision} by a human reviewer.\n"
            f"{category_line}{title_block}"
            f"Call demo_get_ticket for full details, then continue per SOP.\n"
            f"If approved, proceed with next steps. If denied, update case status and close."
        )

    # Console action — an operator decided inline, with no ticket in between. Same trust
    # split as the ticket branch above, and for the same reason: a decision is an
    # instruction to proceed, so it cannot live inside a block the agent is told to treat
    # as data, while the operator's own prose is untrusted human input like any other.
    if trigger == "console-action":
        # Both server-asserted: `case_id` off the request path, `decision` off the reply
        # route's three-member allowlist. No IdP-supplied display name reaches this
        # sentence — the acting human is the message's top-level `subject`, which is what
        # the audit trail reads.
        decision = ctx.get("decision", "") or "replied"
        instruction = {
            "approved": "Retrieve case state, then proceed with the next steps per SOP.",
            "denied": "Retrieve case state, then update case status and close.",
        }.get(
            decision,
            # Anything else is a reply, which authorizes nothing on its own.
            "Retrieve case state, then incorporate this response and continue per SOP.",
        )
        message = ctx.get("message", "")
        # A decision with no prose is valid — a bare approve — so no empty fence.
        fenced = (
            fence_data(
                sanitize_external_content(message, source="console"), source="console"
            )
            + "\n\n"
            if message
            else ""
        )
        chosen = _option_lines(ctx.get("option"), source="console-option")
        if chosen:
            line, block = chosen
            return (
                f"Resume case: {case_id}\n"
                f"An authorized operator chose the option '{line}' in the console. "
                f"{_approves(decision)}\n"
                f"{block}\n{fenced}"
                "Retrieve case state, then carry out that outcome per SOP."
            )
        return (
            f"Resume case: {case_id}\n"
            f"An authorized operator recorded the decision '{decision}' in the console.\n\n"
            f"{fenced}"
            f"{instruction}"
        )

    # Webhook — inbound message from SES/Jira/ServiceNow
    if trigger.startswith("webhook"):
        channel = ctx.get("source", trigger.removeprefix("webhook-") or "unknown")
        sender = ctx.get("sender", "")
        message = ctx.get("message", "")
        subject = ctx.get("subject", "")

        # Build fenced data block from untrusted webhook content
        data_parts = []
        if subject:
            data_parts.append(
                f"Subject: {sanitize_external_content(subject, source=channel)}"
            )
        if sender:
            data_parts.append(
                f"From: {sanitize_external_content(sender, source=channel)}"
            )
        if message:
            data_parts.append(f"\n{sanitize_external_content(message, source=channel)}")
        fenced = fence_data("\n".join(data_parts), source=channel) if data_parts else ""

        instruction = (
            f"Process case {case_id} per SOP. Retrieve case state, then follow each step."
            if case_id
            else "Identify the relevant case from the message content, then process per SOP."
        )
        return f"Inbound {channel} message received.\n\n{fenced}\n\n{instruction}"

    # An explicit UI prompt must win over the generic case-processing prompt.
    # The durable Runs API carries caseId separately for skill routing, audit, and
    # trace persistence; dropping the message here would turn focused-case chat
    # into an unintended autonomous "process case" request.
    #
    # Interactive chat and the /cases/enqueue route share trigger="manual" — both
    # are a human initiating work. The presence of a prompt is what separates them:
    # the invoker deliberately sends none so an enqueued case falls through to the
    # SOP instruction below.
    #
    # The case is named in the turn as well, when the caller sent one. `case_id` reaches
    # the agent for the claim, the session id and skill routing, but none of that is in
    # the message, and the message is all the model sees — so a prompt about "this
    # invoice" arrives with no referent and the model answers by asking which case it is
    # on, having already been given it. The browser prefixed its own messages
    # (`contextPrefix`, useAgentChat.ts), which left every other
    # caller of the Runs API — the run client, any headless consumer — without it.
    user_prompt = payload.get("user_prompt") or payload.get("prompt")
    if trigger == "manual" and user_prompt:
        return f"Case: {case_id}\n\n{user_prompt}" if case_id else user_prompt

    # Poller or generic SQS trigger with a case_id
    if case_id:
        return f"Process case: {case_id}\nRetrieve case state, follow SOP, update state at each step."

    # Chat/frontend — direct user prompt
    if user_prompt:
        return user_prompt

    return "No case or prompt provided."


def _runtime_payload(input_data: RunAgentInput) -> dict:
    """Extract the legacy ERP envelope carried inside canonical AG-UI input."""
    forwarded_props = input_data.forwarded_props
    if isinstance(forwarded_props, dict):
        candidate = forwarded_props.get("erpPayload")
        if isinstance(candidate, dict):
            payload = dict(candidate)
        else:
            payload = {
                key: value
                for key, value in forwarded_props.items()
                if key != "erpPayload"
            }
    else:
        payload = {}

    payload["runtimeSessionId"] = input_data.thread_id
    return payload


def _input_with_prompt(input_data: RunAgentInput, prompt: str) -> RunAgentInput:
    """Put the sanitized trigger-specific prompt into the AG-UI message history."""
    messages = list(input_data.messages or [])
    for index in range(len(messages) - 1, -1, -1):
        if getattr(messages[index], "role", None) == "user":
            messages[index] = messages[index].model_copy(update={"content": prompt})
            break
    else:
        messages.append(
            UserMessage(
                id=f"input-{input_data.run_id}",
                role="user",
                content=prompt,
            )
        )

    # Client-defined proxy tools are intentionally disabled. All ERP tools are selected
    # server-side by the skill router and enforced by Gateway/Cedar.
    return input_data.model_copy(update={"messages": messages, "tools": []})
