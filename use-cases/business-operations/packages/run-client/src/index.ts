// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * `@business-operations/run-client` — the run protocol, with no framework attached.
 *
 * Everything a client needs to follow an agent run: subscribe to the events channel, put
 * its envelopes back in order, project them into a chat transcript, and seed that
 * transcript from a case record. No React, no DOM beyond `WebSocket`, no dependencies.
 *
 * The REST side of the contract — `POST /runs`, `POST /runs/stop`, `GET /cases/{case_id}`
 * — is not wrapped here. It is four fetches, described in `openapi.yaml` at the repo root;
 * see `examples/follow-run.ts` for one that starts a run and follows it to completion.
 */

export { subscribeRunEvents } from "./channel.ts"
export type { RunChannelStatus, RunEventEnvelope, SubscribeRunEventsOptions } from "./channel.ts"

export { reduceAguiEvent, spliceTurnIntoHistory } from "./reducer.ts"
export type { AguiEvent, AguiProjection } from "./reducer.ts"

export { RunFollower } from "./runFollower.ts"
export type { AppliedFrame, RunFollowerHost } from "./runFollower.ts"

export {
  LIVE_RUN_STALE_SECONDS,
  isLiveRunStale,
  liveTurn,
  storedTurns,
  tracesToMessages,
} from "./traces.ts"

export { buildPromptWithHistory, stripPromptScaffolding } from "./prompt.ts"

export type {
  AgentTrace,
  LiveRun,
  Message,
  MessageRole,
  MessageSegment,
  ToolCall,
  ToolCallStatus,
  TraceSegment,
} from "./types.ts"
