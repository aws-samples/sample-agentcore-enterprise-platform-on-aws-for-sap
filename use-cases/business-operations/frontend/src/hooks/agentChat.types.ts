// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The assistant's public contract.
 *
 * Held apart from `useAgentChat` so a consumer can depend on the shape without importing
 * the implementation — `AssistantDock` renders one of these and never calls the hook.
 */

import type { WorkItem } from "@/types/cases"

export interface AgentChat {
  /** The case in context, or null on a route that is not looking at one. */
  caseId: string | null
  /** `caseId`, or the last case that was in context if the current route has none —
   *  for a nav link back to a case view to resume where the user left off rather
   *  than landing contextless (see SideRail's Workspace link). */
  workspaceCaseId: string | null
  input: string
  setInput: (value: string) => void
  /** Whether the case on screen has a run in flight, wherever it was started. Per-slot,
   *  not per-app: a run on another case must not disable the input for the one being read. */
  isLoading: boolean
  /**
   * A POST for this slot is still in flight. Not "the agent is working" — that is
   * `isLoading`, which also covers a run started elsewhere or before this reload.
   * Used only to keep a second Process click from racing the first; the server's 409
   * makes one getting through harmless anyway.
   */
  isRunning: (slot: string) => boolean
  error: string | null
  setError: (value: string | null) => void
  /** AgentCore Memory session — the case's own when there is one. */
  sessionId: string
  /** The transcript slot this chat reads and writes — the case, or the ephemeral
   *  conversation when no case is in context. */
  slotKey: string
  /** False until config confirms somewhere to start a run — the API on the server path,
   *  the runtime ARN on the legacy one. Every run is gated on it. */
  ready: boolean
  /** How many cases a route has put in context beyond the focused one. */
  contextCaseCount: number
  setContextCases: (items: WorkItem[]) => void
  send: (text: string) => Promise<void>
  /**
   * Run one case. The id is opaque, so the caller has nothing to take apart. Answers the
   * API's refusal in its own words (a standing emergency stop, say), or null when it started.
   */
  processCase: (caseId: string, processType?: string) => Promise<string | null>
  stop: () => Promise<void>
  clear: () => void
  /** Append a system statement to the transcript — an enqueue confirmation, say. */
  postNotice: (content: string) => void
  /**
   * Stream a run that produces an assistant turn and **no user turn**.
   *
   * `send` cannot do this: it appends the prompt as a user turn before streaming. The
   * briefing's opening is the agent simply speaking, because the operator never asked a
   * question. Always writes to the current slot, which is the case-less one whenever the
   * briefing is on screen.
   */
  /** A run on a prompt the person did not type; `shown`, when given, is the question they
   *  see in the transcript in its place (the Briefing button's). */
  narrate: (prompt: string, shown?: string) => Promise<void>
  /**
   * Run a pack against a case its author made up, before anything routes to it.
   *
   * Case-less and `process_type`-addressed, which is what `POST /runs` requires of a
   * rehearsal: it writes to no case row and the runtime withholds the side-effecting
   * tools. The only way an **incomplete** pack is ever exercised — nothing routes to one.
   */
  rehearse: (packId: string, processType: string, prompt: string) => Promise<string | null>
  /**
   * The pack whose rehearsal produced what is on screen, or null for an ordinary
   * conversation. Cleared by `clear` and by the next `send`, because a follow-up turn
   * carries no `rehearse` flag and so is a real run.
   */
  rehearsal: { packId: string; processType: string } | null
}
