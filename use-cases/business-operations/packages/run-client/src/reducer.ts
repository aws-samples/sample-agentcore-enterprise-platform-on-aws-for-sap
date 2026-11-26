// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import type { Message, MessageSegment, ToolCall } from "./types.ts"

/** A canonical AG-UI event as emitted by the Runtime's SSE stream. */
export interface AguiEvent {
  type: string
  timestamp?: number
  [key: string]: unknown
}

export interface AguiProjection {
  messages: Message[]
}

function cloneMessages(messages: Message[]): Message[] {
  return messages.map(message => ({
    ...message,
    segments: message.segments?.map(segment =>
      segment.type === "tool" ? { type: "tool", toolCall: { ...segment.toolCall } } : { ...segment }
    ),
  }))
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null ? (value as Record<string, unknown>) : null
}

function asString(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined
}

function renderedValue(value: unknown): string {
  if (typeof value === "string") return value
  try {
    return JSON.stringify(value) ?? String(value)
  } catch {
    return String(value)
  }
}

function eventTimestamp(event: AguiEvent, fallback: string): string {
  if (typeof event.timestamp !== "number" || !Number.isFinite(event.timestamp)) return fallback
  const milliseconds = event.timestamp < 10_000_000_000 ? event.timestamp * 1000 : event.timestamp
  const value = new Date(milliseconds)
  return Number.isNaN(value.getTime()) ? fallback : value.toISOString()
}

function ensureAssistantMessage(
  messages: Message[],
  messageId: string,
  timestamp: string
): Message {
  let message = messages.find(candidate => candidate.id === messageId)
  if (!message) {
    message = { id: messageId, role: "assistant", content: "", timestamp, segments: [] }
    messages.push(message)
  }
  if (!message.segments) message.segments = []
  return message
}

function latestAssistantMessage(messages: Message[]): Message | undefined {
  return [...messages].reverse().find(message => message.role === "assistant")
}

function findToolCall(messages: Message[], toolCallId: string): ToolCall | undefined {
  for (const message of messages) {
    for (const segment of message.segments ?? []) {
      if (segment.type === "tool" && segment.toolCall.toolUseId === toolCallId) {
        return segment.toolCall
      }
    }
  }
  return undefined
}

function addToolCall(
  message: Message,
  toolCallId: string,
  name: string,
  input = "",
  status: ToolCall["status"] = "streaming"
): ToolCall {
  const existing = findToolCall([message], toolCallId)
  if (existing) return existing
  const toolCall: ToolCall = { toolUseId: toolCallId, name, input, status }
  const segment: MessageSegment = { type: "tool", toolCall }
  message.segments = [...(message.segments ?? []), segment]
  return toolCall
}

function appendText(message: Message, delta: string): void {
  message.content += delta
  const segments = message.segments ?? []
  const last = segments[segments.length - 1]
  if (last?.type === "text") last.content += delta
  else segments.push({ type: "text", content: delta })
  message.segments = segments
}

function markUnresolvedToolsIncomplete(messages: Message[]): void {
  for (const message of messages) {
    for (const segment of message.segments ?? []) {
      if (
        segment.type === "tool" &&
        (segment.toolCall.status === "streaming" || segment.toolCall.status === "executing")
      ) {
        segment.toolCall.status = "incomplete"
      }
    }
  }
}

function applyMessagesSnapshot(
  messages: Message[],
  event: AguiEvent,
  timestamp: string,
  runId: string
): void {
  // Assistant content is ASSIGNED, not appended, and each snapshot carries the whole
  // accumulated message list. That is only correct because the adapter issues a fresh
  // message id after every snapshot splice, so an id appears once and carries only its
  // own fragment. An adapter that reused an id across splices would silently truncate
  // assistant text to the latest fragment. Verified against ag-ui-strands 0.2.3; see
  // agent/tests/test_adapter_version_pin.py, which fails if that pin moves.
  //
  // The two writers of assistant text are not equally forgiving, and the asymmetry is
  // load-bearing. `appendText` does `content += delta`: neither idempotent nor
  // order-tolerant, so a `TEXT_MESSAGE_CONTENT` delivered twice duplicates a fragment and
  // one delivered out of order transposes two. The assign here is what repairs that — it
  // overwrites whatever the deltas accumulated with the server's whole message.
  //
  // The delta path is made safe one layer up rather than here. AppSync Events documents no
  // delivery or ordering guarantee, so the wire envelope carries a per-run sequence number
  // (`seq`, agent/utils/run_channel.py) and `RunFollower.accept` in
  // ./runFollower.ts hands this reducer one frame at a time, in the order the agent
  // published them: an envelope this run has already applied is dropped, and one that
  // arrives before the frame it belongs behind is held until that frame lands. So neither a
  // duplicated nor a transposed delta can reach `appendText`. The assign below no longer has
  // to repair either, which matters because on most runs it never gets the chance — the
  // adapter publishes no snapshot at all on a delta payload, which is every run after a
  // case's first. `appendText` stays non-idempotent on purpose: the reducer projects events
  // and knows nothing about how they arrived, so ordering belongs at the envelope boundary,
  // where a redelivered terminal frame's other side effects are caught too.
  if (!Array.isArray(event.messages)) return
  for (const rawMessage of event.messages) {
    const snapshot = asRecord(rawMessage)
    if (!snapshot) continue
    const role = asString(snapshot.role)
    const messageId = asString(snapshot.id) ?? asString(snapshot.messageId)
    if (role === "assistant") {
      const assistant = ensureAssistantMessage(
        messages,
        messageId ?? `assistant-${runId}`,
        timestamp
      )
      if (snapshot.content !== undefined) {
        const content = renderedValue(snapshot.content)
        assistant.content = content
        const tools = (assistant.segments ?? []).filter(segment => segment.type === "tool")
        assistant.segments = content ? [{ type: "text", content }, ...tools] : tools
      }
      const toolCalls = Array.isArray(snapshot.toolCalls) ? snapshot.toolCalls : []
      for (const rawToolCall of toolCalls) {
        const tool = asRecord(rawToolCall)
        const fn = asRecord(tool?.function)
        const toolCallId = asString(tool?.id) ?? asString(tool?.toolCallId)
        if (!toolCallId) continue
        const name = asString(fn?.name) ?? asString(tool?.name) ?? "tool"
        const rawInput = fn?.arguments ?? tool?.args ?? ""
        addToolCall(assistant, toolCallId, name, renderedValue(rawInput), "executing")
      }
    } else if (role === "tool") {
      const toolCallId = asString(snapshot.toolCallId)
      if (!toolCallId) continue
      const toolCall = findToolCall(messages, toolCallId)
      if (toolCall) {
        toolCall.result = renderedValue(snapshot.content ?? "")
        // The adapter splices this snapshot in immediately after TOOL_CALL_RESULT and
        // the ToolMessage carries no status, so overwriting unconditionally would erase
        // a failure the event just reported.
        if (toolCall.status !== "error") toolCall.status = "complete"
      }
    }
  }
}

/**
 * RUN_ERROR codes that report a deliberate stop rather than a failure.
 *
 * AG-UI has no cancelled terminal event — the protocol defines only RUN_STARTED,
 * RUN_FINISHED and RUN_ERROR — so the agent reports hitting its turn limit or its
 * run budget as a RUN_ERROR carrying one of these codes. Work completed before the
 * limit still stands, so it reads as a warning rather than something to investigate.
 */
const DELIBERATE_STOP_CODES = new Set([
  "MAX_TURNS_REACHED",
  "RUN_BUDGET_EXCEEDED",
  "REPEATED_TOOL_ERRORS",
])

function appendRunError(
  messages: Message[],
  event: AguiEvent,
  timestamp: string,
  runId: string
): void {
  const id = `run-error-${runId}`
  if (messages.some(message => message.id === id)) return
  const detail = asString(event.message) ?? "The agent run failed."
  const code = asString(event.code)
  const content =
    code !== undefined && DELIBERATE_STOP_CODES.has(code)
      ? `⚠️ ${detail}`
      : `❌ ${detail}${code ? ` (${code})` : ""}`
  messages.push({
    id,
    role: "assistant",
    content,
    timestamp,
    segments: [{ type: "text", content }],
  })
}

/**
 * Splice one turn's projected messages into the surrounding chat history.
 *
 * `previous` must be the live state, never a prefix captured when the turn began: a
 * caller may still have an uncommitted append in flight (its own user message), and
 * rendering against a stale prefix would drop it.
 *
 * `ownedIds` accumulates every id the turn has rendered, so a placeholder shown before
 * the first event is removed once the agent supplies its own message id. Messages
 * without an id are never owned by a turn and always survive.
 */
export function spliceTurnIntoHistory(
  previous: Message[],
  turn: Message[],
  ownedIds: Set<string>
): Message[] {
  return [...previous.filter(message => !message.id || !ownedIds.has(message.id)), ...turn]
}

/** Deterministically project one canonical AG-UI event into the existing chat model. */
export function reduceAguiEvent(
  projection: AguiProjection,
  event: AguiEvent,
  journalTimestamp: string,
  runId: string
): AguiProjection {
  const messages = cloneMessages(projection.messages)
  const timestamp = eventTimestamp(event, journalTimestamp)

  switch (event.type) {
    case "TEXT_MESSAGE_START": {
      const messageId = asString(event.messageId)
      const role = asString(event.role)
      if (messageId && (!role || role === "assistant")) {
        ensureAssistantMessage(messages, messageId, timestamp)
      }
      break
    }
    case "TEXT_MESSAGE_CONTENT": {
      const messageId = asString(event.messageId) ?? `assistant-${runId}`
      const delta = asString(event.delta)
      if (delta) appendText(ensureAssistantMessage(messages, messageId, timestamp), delta)
      break
    }
    case "TOOL_CALL_START": {
      const toolCallId = asString(event.toolCallId)
      if (!toolCallId) break
      const parentMessageId = asString(event.parentMessageId)
      const message = parentMessageId
        ? ensureAssistantMessage(messages, parentMessageId, timestamp)
        : (latestAssistantMessage(messages) ??
          ensureAssistantMessage(messages, `assistant-${runId}`, timestamp))
      addToolCall(message, toolCallId, asString(event.toolCallName) ?? "tool")
      break
    }
    case "TOOL_CALL_ARGS": {
      const toolCallId = asString(event.toolCallId)
      const delta = asString(event.delta)
      const toolCall = toolCallId ? findToolCall(messages, toolCallId) : undefined
      if (toolCall && delta) toolCall.input += delta
      break
    }
    case "TOOL_CALL_END": {
      const toolCallId = asString(event.toolCallId)
      const toolCall = toolCallId ? findToolCall(messages, toolCallId) : undefined
      if (toolCall && toolCall.status === "streaming") toolCall.status = "executing"
      break
    }
    case "TOOL_CALL_RESULT": {
      const toolCallId = asString(event.toolCallId)
      if (!toolCallId) break
      let toolCall = findToolCall(messages, toolCallId)
      if (!toolCall) {
        const message =
          latestAssistantMessage(messages) ??
          ensureAssistantMessage(messages, `assistant-${runId}`, timestamp)
        toolCall = addToolCall(message, toolCallId, "tool", "", "executing")
      }
      toolCall.result = renderedValue(event.content ?? event.result ?? "")
      // AG-UI has no failure field on this event; the agent attaches the SDK's
      // ToolResult.status as an extra so a failed call renders as failed while
      // streaming, not only after a reload reads the persisted segment.
      toolCall.status = asString(event.status) === "error" ? "error" : "complete"
      break
    }
    case "MESSAGES_SNAPSHOT":
      applyMessagesSnapshot(messages, event, timestamp, runId)
      break
    case "RUN_ERROR":
      markUnresolvedToolsIncomplete(messages)
      appendRunError(messages, event, timestamp, runId)
      break
    case "RUN_FINISHED":
      markUnresolvedToolsIncomplete(messages)
      break
  }

  return { messages }
}
