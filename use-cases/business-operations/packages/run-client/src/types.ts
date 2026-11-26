// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The chat model a run projects into, and the record shapes it is seeded from.
 *
 * Structural on purpose. The record shapes below are a hand-written subset of
 * `types/cases.schema.json`, narrowed to the fields the reduction actually reads, so
 * this package never imports the repo's generated types: it is framework-free and has
 * to compile outside the frontend's path aliases. The generated interfaces are
 * assignable to these — a wider object satisfies a narrower one — so a caller holding
 * `AgentTrace` from `@/types/cases` passes it straight in. Widen these only when the
 * reduction starts reading a new field.
 */

export type MessageRole = "user" | "assistant"

// "incomplete" means the run ended without a result — outcome unknown. "error" means
// the tool reported failure. Rendering the second as the first says "unknown" about
// something we were told.
export type ToolCallStatus = "streaming" | "executing" | "complete" | "incomplete" | "error"

export interface ToolCall {
  toolUseId: string
  name: string
  input: string
  result?: string
  status: ToolCallStatus
}

export type MessageSegment =
  { type: "text"; content: string } | { type: "tool"; toolCall: ToolCall }

export interface Message {
  id?: string
  role: MessageRole
  content: string
  timestamp: string
  segments?: MessageSegment[]
}

/** One step of a stored or in-flight turn, as the record holds it. */
export interface TraceSegment {
  type: "text" | "tool"
  content?: string
  tool_call_id?: string
  tool_input?: string
  tool_name?: string
  tool_result?: string
}

/** A turn already written to the case record. */
export interface AgentTrace {
  trace_id: string
  timestamp: string
  segments: TraceSegment[]
  prompt?: string
  correlation_id?: string
}

/** The turn still in flight — a trace that has not been written yet. */
export interface LiveRun {
  run_id: string
  segments: TraceSegment[]
  started_at: string
  updated_at: string
  prompt?: string
  segments_dropped?: number
}
