// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useQuery, type QueryClient } from "@tanstack/react-query"
import { useAuth } from "react-oidc-context"
import {
  Activity,
  Circle,
  CircleAlert,
  Hand,
  HelpCircle,
  Loader2,
  type LucideIcon,
} from "lucide-react"
import { CaseStatus } from "@/types/cases"
import { countOf, fetchCaseCounts, type CaseCounts } from "@/services/casesService"
import { useApi } from "@/hooks/useApi"
import { useAgentActivity, type AgentActivity } from "@/hooks/agentActivity"
import type { StatusTone } from "@/lib/statusTone"

/**
 * Case counts (`GET /cases/counts`) and the one statement of what the agent is doing that
 * the sidebar shows on every route.
 *
 * Six states, in precedence order: a live interactive run, an unknown case status, a
 * live background run, cases blocked, cases waiting on a person, idle. A live run wins
 * over a standing count because "it is working right now" is the thing a count cannot say.
 */

// While work is in flight, the view is stale in seconds rather than minutes.
const POLL_ACTIVE_MS = 5_000
const POLL_QUIET_MS = 30_000

/**
 * Query key for the rail's case counts (`GET /cases/counts`). Exported so the page that
 * enqueues work can invalidate it: an enqueue changes what the rail should say, and
 * nothing else tells it.
 */
export const AGENT_PULSE_KEY = ["agent-pulse"] as const

/**
 * How long after an enqueue the rail stays on the fast poll.
 *
 * An invalidation alone does not close the gap. `agent_invoker` stamps `processing`
 * only when it picks the SQS message up, so the refetch that follows an enqueue
 * usually still reads `pending`, no case is `processing`, and the interval recomputes
 * straight back to quiet — leaving the operator on a 30 s wait for the state they
 * just caused. This window keeps the fast poll running until the queue is picked up.
 */
const HANDOFF_WINDOW_MS = 20_000

export type Pulse = {
  label: string
  detail: string
  /** Live agent work, so violet rather than a tone. */
  live: boolean
  /** The interactive run this operator started, which is the only state that spins. */
  interactive: boolean
  tone: StatusTone
  /**
   * Collapsed, the label is `sr-only`, so a dot would leave hue as the only signal a
   * sighted operator gets — and blocked/needs-you/unknown are red, orange, orange.
   * Each state therefore carries a distinct shape too.
   */
  icon: LucideIcon
}

/**
 * When work was last handed to the queue from this tab. `-Infinity` rather than 0 so
 * the elapsed time is never inside the window before an enqueue has happened.
 */
let enqueuedAt = -Infinity

/**
 * Tell the rail that work was just queued. The caller owns the enqueue; the rail owns
 * what it says about it, and without this it says nothing until its next quiet poll.
 */
export function notifyWorkEnqueued(queryClient: QueryClient): void {
  enqueuedAt = Date.now()
  void queryClient.invalidateQueries({ queryKey: AGENT_PULSE_KEY })
}

/** Elapsed time since the last local enqueue; `Infinity` if there has not been one. */
export function msSinceEnqueue(): number {
  return Date.now() - enqueuedAt
}

/**
 * Fast while anything is running, and through the window after a local enqueue.
 * Split out from the query so the two reasons to poll fast are testable without
 * driving timers through a mounted component.
 */
export function pollIntervalMs(counts: CaseCounts | undefined, sinceEnqueueMs: number): number {
  if (sinceEnqueueMs < HANDOFF_WINDOW_MS) return POLL_ACTIVE_MS
  return countOf(counts, CaseStatus.Processing) > 0 ? POLL_ACTIVE_MS : POLL_QUIET_MS
}

export function useCaseCounts(refetchInterval?: (counts: CaseCounts | undefined) => number) {
  const auth = useAuth()
  const getToken = useApi()
  return useQuery<CaseCounts>({
    queryKey: AGENT_PULSE_KEY,
    queryFn: async () => fetchCaseCounts(await getToken()),
    enabled: auth.isAuthenticated,
    refetchInterval: refetchInterval ? query => refetchInterval(query.state.data) : false,
  })
}

/**
 * `unknown` is a state, not the absence of one: until the first poll settles there is
 * no basis for claiming idle, and after a failure there is no basis for the counts.
 */
export function pulseFrom(activity: AgentActivity, counts: CaseCounts, known: boolean): Pulse {
  if (activity.kind === "tool") {
    return {
      // The tool's own name rather than a guess at what it talks to: most of these
      // tools never touch SAP, and claiming a SAP write that isn't happening is the
      // one lie this console cannot afford.
      label: `Calling ${activity.name}`,
      detail: `Running ${activity.name} right now`,
      live: true,
      interactive: true,
      tone: "progress",
      icon: Loader2,
    }
  }
  if (activity.kind === "reasoning") {
    return {
      label: "Reasoning",
      detail: "Working through the current request",
      live: true,
      interactive: true,
      tone: "progress",
      icon: Loader2,
    }
  }

  // Only after the live states: an unsettled or failed poll says nothing about the run
  // in front of this operator, but it does mean the counts below are not yet knowable.
  if (!known) {
    return {
      label: "Status unknown",
      // True whether the first poll is still in flight or the last one failed: the
      // interval retries either way, so this state is always "still waiting".
      detail: "Waiting for the case counts, which are not available yet",
      live: false,
      interactive: false,
      tone: "attention",
      icon: HelpCircle,
    }
  }

  const processing = countOf(counts, CaseStatus.Processing)
  if (processing > 0) {
    return {
      label: `Working · ${processing} case${processing === 1 ? "" : "s"}`,
      detail: "Background runs in flight",
      live: true,
      interactive: false,
      tone: "progress",
      // Not the spinner: a background run outlives the operator's attention, and an
      // indefinite spin in the rail for something they did not start is noise.
      icon: Activity,
    }
  }

  const blocked = countOf(counts, CaseStatus.ManualReviewRequired)
  if (blocked > 0) {
    return {
      label: `Blocked · ${blocked}`,
      detail: "Cases the agent could not clear",
      live: false,
      interactive: false,
      tone: "danger",
      icon: CircleAlert,
    }
  }

  const waiting = countOf(counts, CaseStatus.AwaitingHumanInput)
  if (waiting > 0) {
    return {
      label: `Needs you · ${waiting}`,
      detail: "Cases awaiting a human decision",
      live: false,
      interactive: false,
      tone: "attention",
      icon: Hand,
    }
  }

  return {
    // Not "Idle": beside a rail row that says whether the trigger mode is auto, "idle" reads as a
    // mode the operator could have set rather than the absence of work.
    label: "Nothing running",
    detail: "Nothing in flight",
    live: false,
    interactive: false,
    tone: "neutral",
    icon: Circle,
  }
}

/** The agent's state now, polled fast while anything is in flight. */
export function useAgentPulse(): Pulse & { counts: CaseCounts | undefined } {
  const activity = useAgentActivity()
  const query = useCaseCounts(counts => pollIntervalMs(counts, msSinceEnqueue()))
  return { ...pulseFrom(activity, query.data ?? {}, query.isSuccess), counts: query.data }
}
