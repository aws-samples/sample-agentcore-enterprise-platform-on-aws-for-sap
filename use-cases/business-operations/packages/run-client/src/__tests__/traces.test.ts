// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from "vitest"
import { tracesToMessages, liveTurn, storedTurns } from "../traces.ts"
import { reduceAguiEvent } from "../reducer.ts"
import type { AgentTrace, LiveRun } from "../types.ts"

// The wire values of the generated `Type` enum, restated. This package types
// `TraceSegment.type` as the literal union rather than importing the generated enum, so
// the fixtures below name the strings the record actually carries.
const TraceSegmentType = { Text: "text", Tool: "tool" } as const

function makeTrace(overrides: Partial<AgentTrace> = {}): AgentTrace {
  return {
    trace_id: "trace-1",
    timestamp: "2026-07-01T00:00:00Z",
    segments: [{ type: TraceSegmentType.Text, content: "Hello from the agent" }],
    ...overrides,
  }
}

describe("tracesToMessages", () => {
  it("maps a text-only trace to a single assistant message", () => {
    const messages = tracesToMessages([makeTrace()])
    expect(messages).toEqual([
      {
        id: "trace-1",
        role: "assistant",
        content: "Hello from the agent",
        timestamp: "2026-07-01T00:00:00Z",
        segments: [{ type: "text", content: "Hello from the agent" }],
      },
    ])
  })

  it("emits a preceding user message when prompt is present", () => {
    const messages = tracesToMessages([makeTrace({ prompt: "Process case DOC-1" })])
    expect(messages[0]).toEqual({
      role: "user",
      content: "Process case DOC-1",
      timestamp: "2026-07-01T00:00:00Z",
    })
    expect(messages[1].role).toBe("assistant")
  })

  it("replays a chat turn's prompt as what the operator typed", () => {
    // A chat prompt is stored as sent — history summary and case-context preamble and
    // all — so replaying it verbatim showed the operator a bubble full of scaffolding
    // they never wrote.
    const messages = tracesToMessages([
      makeTrace({
        prompt:
          "[Context: The user has selected 1 case(s) for processing:\n- DOC-1: status=open\n]\n\n" +
          "<conversation_history>\nUser: process case DOC-1\nAssistant: done\n</conversation_history>\n\n" +
          "User: what changed?",
      }),
    ])
    expect(messages[0]).toEqual({
      role: "user",
      content: "what changed?",
      timestamp: "2026-07-01T00:00:00Z",
    })
  })

  it("maps tool segments into a toolCall with status complete", () => {
    const messages = tracesToMessages([
      makeTrace({
        segments: [
          {
            type: TraceSegmentType.Tool,
            tool_name: "get_po",
            tool_input: '{"po":"123"}',
            tool_result: '{"status":"ok"}',
          },
        ],
      }),
    ])
    expect(messages[0].segments).toEqual([
      {
        type: "tool",
        toolCall: {
          toolUseId: "trace-1-0",
          name: "get_po",
          input: '{"po":"123"}',
          result: '{"status":"ok"}',
          status: "complete",
        },
      },
    ])
    expect(messages[0].content).toBe("")
  })

  it("carries the stored SDK toolUseId through as the card's id", () => {
    // The id the wire will use for this call's TOOL_CALL_RESULT. Synthesizing one instead
    // is what opened a second card for a tool spanning a mid-run join; the fallback above
    // only covers a segment written before the field existed.
    const messages = tracesToMessages([
      makeTrace({
        segments: [
          {
            type: TraceSegmentType.Tool,
            tool_call_id: "tooluse_abc123",
            tool_name: "get_po",
          },
        ],
      }),
    ])
    expect(messages[0].segments).toEqual([
      {
        type: "tool",
        toolCall: {
          toolUseId: "tooluse_abc123",
          name: "get_po",
          input: "",
          result: undefined,
          status: "complete",
        },
      },
    ])
  })

  it("concatenates multiple text segments space-joined into content", () => {
    const messages = tracesToMessages([
      makeTrace({
        segments: [
          { type: TraceSegmentType.Text, content: "First." },
          { type: TraceSegmentType.Tool, tool_name: "get_po" },
          { type: TraceSegmentType.Text, content: "Second." },
        ],
      }),
    ])
    expect(messages[0].content).toBe("First. Second.")
    expect(messages[0].segments).toHaveLength(3)
  })

  it("preserves trace order (oldest first, matching storage order)", () => {
    const messages = tracesToMessages([
      makeTrace({ trace_id: "t1", timestamp: "2026-07-01T00:00:00Z" }),
      makeTrace({ trace_id: "t2", timestamp: "2026-07-02T00:00:00Z" }),
    ])
    expect(messages.map(m => m.id)).toEqual(["t1", "t2"])
  })

  it("skips a trace with missing segments", () => {
    const messages = tracesToMessages([
      { trace_id: "bad", timestamp: "2026-07-01T00:00:00Z" } as unknown as AgentTrace,
    ])
    expect(messages).toEqual([])
  })

  it("skips a trace with empty segments array", () => {
    const messages = tracesToMessages([makeTrace({ segments: [] })])
    expect(messages).toEqual([])
  })

  it("skips a tool segment with no tool_name", () => {
    const messages = tracesToMessages([
      makeTrace({
        segments: [
          { type: TraceSegmentType.Text, content: "Before" },
          { type: TraceSegmentType.Tool },
          { type: TraceSegmentType.Text, content: "After" },
        ],
      }),
    ])
    expect(messages[0].segments).toEqual([
      { type: "text", content: "Before" },
      { type: "text", content: "After" },
    ])
  })

  it("skips a text segment with empty content", () => {
    const messages = tracesToMessages([
      makeTrace({
        segments: [
          { type: TraceSegmentType.Text, content: "" },
          { type: TraceSegmentType.Text, content: "Kept" },
        ],
      }),
    ])
    expect(messages[0].segments).toEqual([{ type: "text", content: "Kept" }])
  })

  it("returns an empty array for an empty traces list", () => {
    expect(tracesToMessages([])).toEqual([])
  })

  it("renders a result-less tool call as executing while the run is live", () => {
    // A live run's last tool call has been requested but not answered. Rendering it
    // "complete" shows the operator a finished step that is still running.
    const messages = tracesToMessages(
      [
        makeTrace({
          segments: [
            { type: TraceSegmentType.Tool, tool_name: "get_po", tool_input: '{"po":"123"}' },
          ],
        }),
      ],
      { live: true }
    )
    expect(messages[0].segments).toEqual([
      {
        type: "tool",
        toolCall: {
          toolUseId: "trace-1-0",
          name: "get_po",
          input: '{"po":"123"}',
          result: undefined,
          status: "executing",
        },
      },
    ])
  })

  it("still renders an answered tool call as complete while the run is live", () => {
    const messages = tracesToMessages(
      [
        makeTrace({
          segments: [
            { type: TraceSegmentType.Tool, tool_name: "get_po", tool_result: '{"status":"ok"}' },
          ],
        }),
      ],
      { live: true }
    )
    expect((messages[0].segments?.[0] as { toolCall: { status: string } }).toolCall.status).toBe(
      "complete"
    )
  })

  it("treats an empty-string result as an answer while the run is live", () => {
    const messages = tracesToMessages(
      [
        makeTrace({
          segments: [{ type: TraceSegmentType.Tool, tool_name: "get_po", tool_result: "" }],
        }),
      ],
      { live: true }
    )
    expect((messages[0].segments?.[0] as { toolCall: { status: string } }).toolCall.status).toBe(
      "complete"
    )
  })
})

function liveRunFor(runId: string): LiveRun {
  return {
    run_id: runId,
    segments: [],
    started_at: "2026-08-20T10:00:00Z",
    updated_at: "2026-08-20T10:00:04Z",
  }
}

describe("liveTurn", () => {
  const live: LiveRun = {
    ...liveRunFor("run-9"),
    prompt: "Process case 5100001506-2026",
    segments: [
      { type: TraceSegmentType.Text, content: "Reading the invoice. " },
      { type: TraceSegmentType.Tool, tool_name: "sap_read", tool_input: "{}" },
    ],
  }

  it("renders the run in flight, with its unfinished tool still executing", () => {
    const messages = liveTurn(live)

    expect(messages.map(m => m.role)).toEqual(["user", "assistant"])
    expect(messages[0].content).toBe("Process case 5100001506-2026")
    // The channel's projection replaces this turn by id, so it has to be the run id.
    expect(messages[1].id).toBe("run-9")
    expect(messages[1].segments?.[1]).toMatchObject({
      type: "tool",
      toolCall: { status: "executing" },
    })
  })

  it("says so when the cap dropped the start of the run", () => {
    const messages = liveTurn({ ...live, segments_dropped: 3 })

    expect(messages[1].content).toContain("3 earlier steps")
  })

  it("renders an empty run as a turn in flight rather than nothing", () => {
    // A run that has produced no segment yet is still a run: dropping it would leave
    // the case looking idle for the first seconds of every queued run.
    const messages = liveTurn({ ...live, segments: [] })

    expect(messages.map(m => m.role)).toEqual(["user", "assistant"])
    expect(messages[1].id).toBe("run-9")
  })

  it("hands the reducer a tool card the wire's own result can settle", () => {
    // The mid-run join, end to end: a tab that opens a case already running seeds this
    // turn from `live_run` and then keeps rendering the same run from the channel. Its
    // TOOL_CALL_RESULT has to land on the card already on screen — matched by id — or the
    // reducer opens a second one and the same call shows twice, once unnamed and complete,
    // once named and settled incomplete at RUN_FINISHED.
    const seeded = liveTurn({
      ...live,
      segments: [
        { type: TraceSegmentType.Text, content: "Reading the invoice. " },
        {
          type: TraceSegmentType.Tool,
          tool_call_id: "tooluse_abc123",
          tool_name: "sap_read",
          tool_input: "{}",
        },
      ],
    })
    // What `applyFrame` seeds a channel entry with: the messages this run already owns,
    // which is the assistant turn under the bare run id — the user bubble carries none.
    const projection = { messages: seeded.filter(m => m.id) }

    const next = reduceAguiEvent(
      projection,
      { type: "TOOL_CALL_RESULT", toolCallId: "tooluse_abc123", content: '{"status":"ok"}' },
      "2026-08-20T10:00:05Z",
      "run-9"
    )

    const tools = (next.messages[0].segments ?? []).filter(s => s.type === "tool")
    expect(tools).toHaveLength(1)
    expect(tools[0]).toMatchObject({
      toolCall: { name: "sap_read", result: '{"status":"ok"}', status: "complete" },
    })
  })
})

describe("storedTurns", () => {
  const trace = makeTrace({ correlation_id: "run-9" })

  it("drops the stored copy of the run that is still live", () => {
    // Not a state today's writer can reach: appending the trace and REMOVEing `live_run`
    // are one UpdateItem, so a record holds one copy or the other. The filter is belt and
    // braces against a writer that stops being atomic — two copies would render the same
    // turn twice — and this pins the direction it exists for.
    expect(storedTurns([trace], liveRunFor("run-9"))).toEqual([])
  })

  it("keeps the case's history while a different run is live", () => {
    // The half that runs on every case opened mid-run, including the pre-channel traces
    // that carry no correlation_id at all. A filter keyed off a live run being *present*
    // rather than off its id would drop the whole decision chain here and leave the
    // operator the one turn in flight.
    const earlier = makeTrace({ trace_id: "trace-0", correlation_id: "run-8" })
    const untagged = makeTrace({ trace_id: "trace-1" })

    expect(storedTurns([earlier, untagged], liveRunFor("run-9")).map(m => m.id)).toEqual([
      "trace-0",
      "trace-1",
    ])
  })

  it("keeps every trace when nothing is live", () => {
    expect(storedTurns([trace]).map(m => m.role)).toEqual(["assistant"])
  })
})
