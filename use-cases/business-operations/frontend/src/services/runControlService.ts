// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import { getConfig } from "@/lib/config"
import { apiFetch } from "@/lib/apiFetch"

export interface StartRunRequest {
  prompt: string
  /** Omit for the case-less conversation, and send `sessionId` instead. */
  caseId?: string
  sessionId?: string
  processType?: string
  /**
   * Exercise a pack before anything routes to it — the author's own dry run.
   *
   * Never sent with a `caseId`: the route refuses the pair outright, because a case-less
   * run is what makes a rehearsal claim no `live_run` and write nothing back to a case
   * row. `processType` is required rather than optional here, since a rehearsal names no
   * case for the server to resolve the pack from.
   */
  rehearse?: boolean
  /** `process` runs the case's SOP and may write. Absent is a question, which cannot. */
  intent?: "ask" | "process"
}

export interface StartRunResult {
  runId: string
  /** Absent only on a 409, where the caller falls back to the session id it holds. */
  sessionId?: string
  /** True when a run already held this case (409) — watch `runId`, don't retry. */
  alreadyRunning: boolean
}

/**
 * Ask the server to start a run, and report which run to watch.
 *
 * Deliberately not `apiFetch`: a 409 here is a success, naming the run already in
 * flight on this case, and `apiFetch` throws on every non-2xx.
 */
export async function startRun(request: StartRunRequest, token: string): Promise<StartRunResult> {
  const { apiUrl } = await getConfig()
  const res = await fetch(`${apiUrl}/runs`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify({
      prompt: request.prompt,
      case_id: request.caseId,
      session_id: request.sessionId,
      process_type: request.processType,
      // Only when true: the route reads it as `bool(body.get("rehearse"))`, and sending
      // `false` on every ordinary run would put the flag in every request body for
      // nothing.
      ...(request.rehearse ? { rehearse: true } : {}),
      ...(request.intent ? { intent: request.intent } : {}),
    }),
  })
  const body = await res.json().catch(() => ({}) as Record<string, string>)
  // A 409 that names a run is the case's own run in flight. One that names none is a refusal,
  // such as a standing emergency stop, and reads as the API words it.
  if (res.status === 409 && (body.error === "already_running" || body.run_id)) {
    return { runId: body.run_id ?? "", sessionId: request.sessionId, alreadyRunning: true }
  }
  if (!res.ok)
    throw Object.assign(new Error(body.error || `Failed to start run: ${res.status}`), {
      status: res.status,
      body,
    })
  return { runId: body.run_id, sessionId: body.session_id, alreadyRunning: false }
}

/**
 * The API's refusal to start a run, in its own words: a 4xx with an `error` sentence, such as a
 * standing emergency stop or a caller outside the case workers. Null for anything else.
 */
export function refusalOf(error: unknown): string | null {
  const e = error as { status?: unknown; body?: { error?: unknown } } | null
  const status = typeof e?.status === "number" ? e.status : 0
  const words = e?.body?.error
  return status >= 400 && status < 500 && status !== 401 && typeof words === "string" && words
    ? words
    : null
}

/**
 * Ask the server to end the run on this target, and report whether it agreed.
 *
 * The 202 is about the claim, which the route releases either way; whether the Runtime
 * actually abandoned the session is the separate fact it answers with. False means the agent
 * is still running and its next write will put the case back into the state the release just
 * cleared — the caller's only warning that a case it just cleared is about to look busy.
 *
 * A body with no `stopped` — or one carrying something other than a boolean — reads as a
 * stop: a deployment whose Lambda predates the flag would otherwise report every successful
 * stop as a refusal.
 */
export async function stopRun(
  target: { caseId?: string; sessionId?: string },
  token: string
): Promise<boolean> {
  const { apiUrl } = await getConfig()
  const body = await apiFetch<{ stopped?: unknown } | undefined>(
    `${apiUrl}/runs/stop`,
    { token, method: "POST", body: { case_id: target.caseId, session_id: target.sessionId } },
    "Failed to stop run",
    // The route rejects an unusable target with `{ error }` — "case_id or session_id is
    // required", or a session id in the case-derived namespace. A bare 400 would say the
    // stop failed without saying which half of the target the server refused.
    async res => (await res.json().catch(() => ({}))).error
  )
  return typeof body?.stopped === "boolean" ? body.stopped : true
}
