// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import type { AgentTrace, LiveRun, Message, MessageSegment, TraceSegment } from "./types.ts"
import { stripPromptScaffolding } from "./prompt.ts"
import { LIVE_RUN_STALE_SECONDS } from "./generated-contract.ts"

/**
 * How long a `live_run` snapshot is still a run, in seconds: the schema's
 * `LiveRun.x-stale-after-seconds`, generated beside this file. At the writer's 2 s
 * throttle that is 30 missed writes, past which the container is gone.
 */
export { LIVE_RUN_STALE_SECONDS }

/**
 * Whether a `live_run` snapshot is too old to still be a run.
 *
 * `live_run` is removed by the update that appends the finished trace, so a container
 * killed before that update (AgentCore recycle, OOM, timeout) leaves a snapshot with no
 * writer left to remove it. Rendering it regardless shows a tool card spinning
 * "executing" to every operator who opens the case, in every tab, until that case's
 * *next* successful run — with no timeout, no error, and no way to clear it. An
 * unparseable `updated_at` counts as stale: it cannot be shown to be fresh.
 */
export function isLiveRunStale(live: LiveRun, now: number = Date.now()): boolean {
  const updated = Date.parse(live.updated_at)
  if (Number.isNaN(updated)) return true
  return now - updated >= LIVE_RUN_STALE_SECONDS * 1000
}

function mapSegment(
  segment: TraceSegment,
  index: number,
  traceId: string,
  live: boolean
): MessageSegment | null {
  if (segment.type === "text") {
    if (!segment.content) return null
    return { type: "text", content: segment.content }
  }
  if (segment.type === "tool") {
    if (!segment.tool_name) return null
    return {
      type: "tool",
      toolCall: {
        // The stored SDK toolUseId where there is one, because a seeded turn and the wire
        // have to share one id space. A mid-run join renders the case from `live_run` and
        // then keeps rendering it from the channel: the reducer matches a TOOL_CALL_RESULT
        // to its card by this id, so a synthesized one misses and opens a second card —
        // the same call twice, once unnamed and complete, once named and left incomplete
        // at RUN_FINISHED. The fallback covers a segment written before the field existed,
        // and is position-dependent in a way the real id is not: `liveTurn` prepends a
        // notice segment when a run starts dropping segments, which shifts every index
        // under it mid-run. `runFollower.ts` depends on this sharing: a mid-run join seeds
        // the turn from `live_run` and then keeps rendering it from the channel.
        toolUseId: segment.tool_call_id ?? `${traceId}-${index}`,
        name: segment.tool_name,
        input: segment.tool_input ?? "",
        result: segment.tool_result,
        // Any result-less tool segment renders executing when live, not just the trailing one.
        // An empty-string result is an answer, so the check is on absence.
        // Make it position-aware if a mid-run gap ever appears.
        status: live && segment.tool_result === undefined ? "executing" : "complete",
      },
    }
  }
  return null
}

/**
 * Replays traces as chat messages, oldest first (storage order).
 *
 * `live` renders the run in flight rather than a stored one: a result-less tool call is
 * rendered as executing, not finished.
 */
export function tracesToMessages(
  traces: AgentTrace[],
  options: { live?: boolean } = {}
): Message[] {
  const live = options.live === true
  const messages: Message[] = []

  for (const trace of traces) {
    if (!Array.isArray(trace.segments) || trace.segments.length === 0) continue

    // The prompt is stored as sent, so a chat turn's carries the history and context
    // preamble the client wrapped around it — not something to show back to the user.
    const asked = trace.prompt ? stripPromptScaffolding(trace.prompt) : ""
    if (asked) {
      messages.push({ role: "user", content: asked, timestamp: trace.timestamp })
    }

    const segments = trace.segments
      .map((segment, index) => mapSegment(segment, index, trace.trace_id, live))
      .filter((s): s is MessageSegment => s !== null)

    const content = segments
      .filter((s): s is { type: "text"; content: string } => s.type === "text")
      .map(s => s.content)
      .join(" ")

    messages.push({
      id: trace.trace_id,
      role: "assistant",
      content,
      timestamp: trace.timestamp,
      segments,
    })
  }

  return messages
}

/**
 * The run in flight, as a chat turn.
 *
 * A `LiveRun` is a trace that has not been written yet — same segments, a run id
 * instead of a trace id — so this adapts the shape and lets `tracesToMessages` do the
 * rendering. The assistant message is id'd by `run_id` on purpose: a subscriber's own
 * projection of the same run replaces this turn by that id rather than appending
 * beside it.
 */
export function liveTurn(live: LiveRun): Message[] {
  const dropped =
    live.segments_dropped && live.segments_dropped > 0
      ? `_[${live.segments_dropped} earlier steps trimmed]_\n`
      : ""
  const segments = live.segments ?? []
  return tracesToMessages(
    [
      {
        trace_id: live.run_id,
        timestamp: live.started_at,
        prompt: live.prompt,
        // An empty run still owes the reader a turn, and `tracesToMessages` skips a trace
        // with no segments. So a text segment carrying the trim notice (or nothing at all)
        // stands in until the first real segment lands: it clears that skip, and an empty
        // one is then dropped by the falsy-content check, leaving a bare turn in flight.
        segments:
          dropped || segments.length === 0
            ? [{ type: "text" as const, content: dropped }, ...segments]
            : segments,
      },
    ],
    { live: true }
  )
}

/**
 * The case's stored turns, minus the one that is still streaming.
 *
 * `live_run` is REMOVEd in the same statement that appends the finished trace, so no
 * reader ever sees both copies of one run — that swap is a single atomic `UpdateItem`.
 * The filter is belt and braces against a writer that stops being atomic, one pass over
 * an already capped list; it is not a guard on any state today's writer can produce.
 *
 * The strand that *does* happen is the other one: a snapshot left behind by a container
 * that died before the swap, which no trace write will ever remove. Rendering that is
 * `isLiveRunStale`'s job to prevent, applied by the caller before a live turn is built
 * at all — a stranded snapshot has no matching stored trace for this filter to catch.
 */
export function storedTurns(traces: AgentTrace[], live?: LiveRun): Message[] {
  const streaming = live?.run_id
  return tracesToMessages(
    streaming ? traces.filter(trace => trace.correlation_id !== streaming) : traces
  )
}
