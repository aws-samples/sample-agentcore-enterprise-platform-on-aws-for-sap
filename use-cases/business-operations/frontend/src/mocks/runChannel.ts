// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The run channel in mock mode: an AppSync Events socket served by MSW, speaking the same
 * `aws-appsync-event-ws` frames `@business-operations/run-client` subscribes with, and a
 * replay that publishes a recorded run's AG-UI events on it at a steady pace. So the console
 * follows a mock run with the code it follows a real one with: the channel, the run follower
 * and the transcript store.
 */
import { ws } from "msw"
import type { TraceSegment } from "@/types/cases"

export const EVENTS_HTTP_DOMAIN = "events-http.mock.invalid"
export const EVENTS_REALTIME_DOMAIN = "events.mock.invalid"

const link = ws.link(`wss://${EVENTS_REALTIME_DOMAIN}/event/realtime`)

/** Each socket's subscribed channels. */
const subscriptions = new Map<{ send: (data: string) => void }, Set<string>>()

export const runChannel = link.addEventListener("connection", ({ client }) => {
  const channels = new Set<string>()
  subscriptions.set(client, channels)
  client.addEventListener("message", event => {
    let message: { type?: string; id?: string; channel?: string }
    try {
      message = JSON.parse(String(event.data))
    } catch {
      return
    }
    if (message.type === "connection_init")
      client.send(JSON.stringify({ type: "connection_ack", connectionTimeoutMs: 300_000 }))
    else if (message.type === "subscribe" && message.channel) {
      channels.add(message.channel)
      client.send(JSON.stringify({ type: "subscribe_success", id: message.id }))
    }
  })
  client.addEventListener("close", () => subscriptions.delete(client))
})

/** Whether a subscribed channel (`/runs/ap/*`, `/runs/<session>`) carries this slot. */
function carries(channel: string, slot: string): boolean {
  if (channel.endsWith("/*")) {
    const prefix = channel.slice("/runs/".length, -2)
    return slot.startsWith(`${prefix}-`)
  }
  return channel === `/runs/${slot}`
}

/** Other listeners on every envelope published, for a test. */
const listeners = new Set<(slot: string, payload: string) => void>()
export function listen(fn: (slot: string, payload: string) => void): () => void {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

function publish(slot: string, runId: string, seq: number, event: Record<string, unknown>) {
  const payload = JSON.stringify({ caseId: slot, runId, seq, event })
  for (const fn of listeners) fn(slot, payload)
  for (const [client, channels] of subscriptions)
    if ([...channels].some(c => carries(c, slot)))
      client.send(JSON.stringify({ type: "data", event: [payload] }))
}

/** Milliseconds between published steps; a text streams a few words per tick. */
export const PACE = { step: 420, word: 45 }

export interface Replay {
  cancel: () => void
}

/**
 * Publish one run's segments as AG-UI events, a step at a time: a tool call's start and
 * arguments, then its result a beat later; a text a few words at a time. `onSegment` fires
 * when each segment is complete, `onEnd` after `RUN_FINISHED`.
 */
export function replay(
  slot: string,
  runId: string,
  segments: TraceSegment[],
  onSegment: (segment: TraceSegment) => void,
  onEnd: () => void
): Replay {
  const messageId = `msg-${runId}`
  let seq = 0
  let cancelled = false
  const timers: ReturnType<typeof setTimeout>[] = []
  let at = 0
  const at_ = (ms: number, fn: () => void) => {
    at += ms
    timers.push(
      setTimeout(() => {
        if (!cancelled) fn()
      }, at)
    )
  }
  const send = (event: Record<string, unknown>) => publish(slot, runId, ++seq, event)

  at_(0, () => send({ type: "RUN_STARTED", runId, threadId: slot }))
  for (const segment of segments) {
    if (segment.type === "text") {
      const words = (segment.content ?? "").split(/(?<=\s)/)
      at_(PACE.step / 2, () => send({ type: "TEXT_MESSAGE_START", messageId, role: "assistant" }))
      for (let i = 0; i < words.length; i += 3) {
        const delta = words.slice(i, i + 3).join("")
        at_(PACE.word, () => send({ type: "TEXT_MESSAGE_CONTENT", messageId, delta }))
      }
      at_(0, () => {
        send({ type: "TEXT_MESSAGE_END", messageId })
        onSegment(segment)
      })
    } else {
      const toolCallId = segment.tool_call_id ?? `tool-${seq}`
      at_(PACE.step, () => {
        send({
          type: "TOOL_CALL_START",
          toolCallId,
          toolCallName: segment.tool_name,
          parentMessageId: messageId,
        })
        send({ type: "TOOL_CALL_ARGS", toolCallId, delta: segment.tool_input ?? "" })
        send({ type: "TOOL_CALL_END", toolCallId })
      })
      at_(PACE.step, () => {
        send({
          type: "TOOL_CALL_RESULT",
          toolCallId,
          content: segment.tool_result ?? "",
          status: segment.status,
        })
        onSegment(segment)
      })
    }
  }
  at_(PACE.step / 2, () => {
    send({ type: "RUN_FINISHED", runId, threadId: slot })
    onEnd()
  })

  return {
    cancel: () => {
      if (cancelled) return
      cancelled = true
      for (const t of timers) clearTimeout(t)
      send({ type: "RUN_FINISHED", runId, threadId: slot })
    },
  }
}
