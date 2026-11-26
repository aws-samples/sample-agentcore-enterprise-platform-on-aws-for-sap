// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Start a run and follow it to completion, on bare Node — no browser, no bundler.
 *
 * The point of this file is that it is not special. It reaches the same two routes the
 * browser reaches (`POST /runs`, and the events channel behind `subscribeRunEvents`) and
 * hands every envelope to the same `RunFollower`. There is no code path here that the UI
 * does not also take, and none in the UI that this cannot.
 *
 * Two things it does in a deliberate order, because the protocol requires it:
 *
 *  1. Subscribe *before* starting the run. The channel has no replay, so anything the
 *     agent publishes before the subscription is acknowledged is gone — including
 *     RUN_STARTED and, on a fast run, the opening deltas.
 *  2. Follow the run the server names, not the one it asked for. A 409 means a run is
 *     already in flight on that case; its id is the one to watch.
 *
 * Run it:
 *
 *   node packages/run-client/examples/follow-run.ts --help
 *
 * Node 22.18+ runs this file as-is: type stripping has been unflagged since then, which
 * is also why the import below names a `.ts` file.
 */

import { RunFollower, type AppliedFrame, type Message } from "../src/index.ts"
import { subscribeRunEvents } from "../src/channel.ts"

const USAGE = `Start an agent run and follow it to completion.

Usage:
  node examples/follow-run.ts [options] <prompt>

Options:
  --api <url>               API base URL, no trailing slash (env ERP_API_URL)
  --http-domain <domain>    AppSync Events HTTP domain (env ERP_EVENTS_HTTP_DOMAIN)
  --realtime-domain <d>     AppSync Events realtime domain (env ERP_EVENTS_REALTIME_DOMAIN)
  --token <jwt>             An id token for the API's authorizer (env ERP_ID_TOKEN)
  --case <case_id>          Run on a case. Omitted, this is a case-less conversation.
  --process                 Process the case: run its SOP, which may write to SAP within
                            the run checks. Needs --case. Omitted, the run is a question and
                            changes nothing.
  --timeout <seconds>       Give up waiting for the run to end (default 600)
  -h, --help                This text

Every value above is in the deployment's own /aws-exports.json — apiUrl,
runEventsHttpDomain, runEventsRealtimeDomain — or in the backend stack's outputs. The
token is whatever your IdP issues for the API: the same one the browser sends.

Example:
  ERP_ID_TOKEN=$(...) node examples/follow-run.ts \\
    --api https://example.execute-api.us-east-1.amazonaws.com/prod \\
    --http-domain abc123.appsync-api.us-east-1.amazonaws.com \\
    --realtime-domain abc123.appsync-realtime-api.us-east-1.amazonaws.com \\
    --case 5100001976-2026 "Summarise the block reason and propose a fix."`

export interface Options {
  api: string
  httpDomain: string
  realtimeDomain: string
  token: string
  caseId: string
  /** Process the case (`intent: "process"`) rather than ask about it. */
  process: boolean
  prompt: string
  timeoutMs: number
}

export type Parsed = { ok: true; options: Options } | { ok: false; message: string; help: boolean }

/**
 * `argv` and the environment, as options — or as the message to print instead.
 *
 * Separate from `main` and pure so the flag handling has one test rather than a manual
 * run per branch. Env fallbacks so a token, which is long and sensitive, need not sit in
 * a shell history.
 */
export function parseArgs(argv: string[], env: Record<string, string | undefined>): Parsed {
  const flags = new Map<string, string>()
  const positional: string[] = []
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index]
    if (argument === "--help" || argument === "-h") return { ok: false, message: USAGE, help: true }
    if (argument === "--process") {
      flags.set(argument, "true")
      continue
    }
    if (!argument.startsWith("--")) {
      positional.push(argument)
      continue
    }
    const split = argument.indexOf("=")
    const name = split === -1 ? argument : argument.slice(0, split)
    // `--flag=value` as well as `--flag value`, because a JWT on a command line is long
    // enough that people paste it either way.
    const value = split === -1 ? argv[(index += 1)] : argument.slice(split + 1)
    if (value === undefined) return { ok: false, message: `${name} needs a value.`, help: false }
    flags.set(name, value)
  }

  const options: Options = {
    api: (flags.get("--api") ?? env.ERP_API_URL ?? "").replace(/\/+$/, ""),
    httpDomain: flags.get("--http-domain") ?? env.ERP_EVENTS_HTTP_DOMAIN ?? "",
    realtimeDomain: flags.get("--realtime-domain") ?? env.ERP_EVENTS_REALTIME_DOMAIN ?? "",
    token: flags.get("--token") ?? env.ERP_ID_TOKEN ?? "",
    caseId: flags.get("--case") ?? "",
    process: flags.has("--process"),
    prompt: positional.join(" "),
    timeoutMs: Number(flags.get("--timeout") ?? 600) * 1000,
  }
  // `string[]` and not the tuple's own literal union: `<prompt>` joins this list below, and
  // it is not a flag.
  const missing: string[] = (
    [
      ["--api", options.api],
      ["--http-domain", options.httpDomain],
      ["--realtime-domain", options.realtimeDomain],
      ["--token", options.token],
    ] as const
  )
    .filter(([, value]) => !value)
    .map(([name]) => name)
  if (!options.prompt) missing.push("<prompt>")
  if (missing.length > 0) {
    return { ok: false, message: `Missing ${missing.join(", ")}.\n\n${USAGE}`, help: false }
  }
  if (options.process && !options.caseId) {
    return { ok: false, message: "--process needs --case: Process runs a case's SOP.", help: false }
  }
  if (!Number.isFinite(options.timeoutMs) || options.timeoutMs <= 0) {
    return { ok: false, message: "--timeout must be a positive number of seconds.", help: false }
  }
  return { ok: true, options }
}

/** What has already been printed, so each frame prints only what it added. */
export interface RenderState {
  /** Message id → how many characters of its text have been written. */
  text: Map<string, number>
  /** Tool calls already announced, by `toolUseId`. */
  tools: Set<string>
}

/**
 * The lines this frame added, given everything printed before it.
 *
 * Reads the follower's projection rather than the raw `delta` on the event, which is the
 * whole reason the package exists: the projection is already deduplicated, already in
 * order, and already folded, so a naive "print every delta" client would render a
 * redelivered batch twice and a transposed one backwards.
 *
 * Per message id rather than per turn: a MESSAGES_SNAPSHOT can add a second assistant
 * message, and a single high-water mark over "the last message" would then slice the new
 * one by the old one's length.
 */
export function renderFrame(frame: AppliedFrame, state: RenderState): string[] {
  const out: string[] = []
  for (const message of frame.turn) {
    for (const segment of message.segments ?? []) {
      if (segment.type !== "tool") continue
      const { toolUseId, name, status } = segment.toolCall
      if (state.tools.has(toolUseId)) continue
      // Announced once the tool is actually running, so the name is settled: a card opens
      // on TOOL_CALL_START, whose name the adapter can still be assembling.
      if (status === "streaming") continue
      state.tools.add(toolUseId)
      out.push(`\n[tool] ${name}`)
    }
    if (message.role !== "assistant") continue
    const id = message.id ?? ""
    const printed = state.text.get(id) ?? 0
    if (message.content.length > printed) {
      out.push(message.content.slice(printed))
      state.text.set(id, message.content.length)
    }
  }
  return out
}

/** POST /runs, and the id of the run to actually watch. */
async function startRun(options: Options, sessionId: string): Promise<string> {
  const response = await fetch(`${options.api}/runs`, {
    method: "POST",
    headers: { Authorization: `Bearer ${options.token}`, "Content-Type": "application/json" },
    body: JSON.stringify({
      prompt: options.prompt,
      case_id: options.caseId || undefined,
      session_id: options.caseId ? undefined : sessionId,
      // Without it the run is a question: the route's default, which writes nothing.
      ...(options.process ? { intent: "process" } : {}),
    }),
  })
  const body = (await response.json().catch(() => ({}))) as Record<string, string>
  // A 409 is not a failure: a run already holds this case, and its id is what to follow.
  // Retrying would be the wrong move — the case is busy, and this run is the reason.
  if (response.status === 409) {
    if (!body.run_id) throw new Error("A run already holds this case, and it was not named.")
    process.stderr.write(`A run already holds this case; following ${body.run_id}.\n`)
    return body.run_id
  }
  if (!response.ok) throw new Error(body.error || `POST /runs failed: ${response.status}`)
  return body.run_id
}

async function main(): Promise<number> {
  const parsed = parseArgs(process.argv.slice(2), process.env)
  if (!parsed.ok) {
    process.stdout.write(`${parsed.message}\n`)
    return parsed.help ? 0 : 2
  }
  const options = parsed.options

  // The channel key a run publishes under is the case id, or the session id when there is
  // no case — so the slot is known before the run is, and it is what the subscription and
  // the follower are both keyed by. A UUID clears the server's length floor on a
  // caller-supplied session id and is outside the namespace it reserves for cases.
  const sessionId = crypto.randomUUID()
  const slot = options.caseId || sessionId

  const state: RenderState = { text: new Map(), tools: new Set() }
  let transcript: Message[] = []
  let runId = ""
  let settle: (code: number) => void = () => {}
  const ended = new Promise<number>(resolve => {
    settle = resolve
  })
  // Terminal frames seen, by run id, with the code each one exits on. A subscription over
  // one slot can carry a run this client did not start — a case another operator is
  // working — so a terminal frame is not necessarily ours. And ours can land before the
  // POST that named it has returned, which is why this is a map and not just a flag.
  const finished = new Map<string, number>()

  const follower = new RunFollower({
    readTranscript: () => transcript,
    writeTranscript: (_slot, update) => {
      transcript = update(transcript)
    },
    onFrame: frame => {
      for (const chunk of renderFrame(frame, state)) process.stdout.write(chunk)
      if (!frame.terminal) return
      // RUN_ERROR is a failed run, and a headless client's exit code is its result. The
      // message itself is already in the projection — the reducer appends it as assistant
      // text — so it has been printed above.
      const code = frame.event.type === "RUN_ERROR" ? 1 : 0
      finished.set(frame.runId, code)
      if (frame.runId === runId) settle(code)
    },
  })

  const subscribed = new Promise<void>(resolve => {
    const subscription = subscribeRunEvents({
      httpDomain: options.httpDomain,
      realtimeDomain: options.realtimeDomain,
      // A case's channel names its pack by the short id its case ids start with.
      channel: options.caseId ? `/runs/${options.caseId.split("-")[0]}/${slot}` : `/runs/${slot}`,
      getIdToken: async () => options.token,
      onEvent: envelope => follower.accept(envelope),
      onStatus: status => {
        if (status === "subscribed") resolve()
      },
    })
    void ended.then(() => subscription.close())
  })

  // Armed before the subscription rather than after the POST: an unreachable or
  // misconfigured channel never acknowledges, and the wait below would then hang in
  // silence forever instead of failing.
  const deadline = setTimeout(() => {
    process.stdout.write(`\n[timeout] gave up after ${options.timeoutMs / 1000}s.\n`)
    settle(1)
  }, options.timeoutMs)

  // The subscription comes first, and this is the one ordering constraint in the protocol:
  // the channel has no replay, so an event published before the subscription is
  // acknowledged is lost — including RUN_STARTED, and on a fast run the opening deltas.
  // Raced against the deadline so a channel that never acknowledges still ends the process.
  const handshake = await Promise.race([
    subscribed.then(() => "subscribed" as const),
    ended.then(() => "over" as const),
  ])
  if (handshake === "subscribed") {
    try {
      runId = await startRun(options, sessionId)
      // The run can be over before its own id got back here: the reply to the POST crosses
      // the channel's frames, and nothing orders the two.
      const already = finished.get(runId)
      if (already !== undefined) settle(already)
    } catch (error) {
      // Settled, not rethrown. `ended` resolving is what closes the subscription and lets
      // the deadline be cleared, so a throw here would print the error and then hold the
      // process open for the full timeout with nothing left to wait for.
      process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`)
      settle(1)
    }
  }

  const code = await ended
  clearTimeout(deadline)
  process.stdout.write("\n")
  return code
}

// Only when run, not when imported: the tests next door import `parseArgs` and
// `renderFrame`, and `import.meta.main` is Node 24 and later.
if (process.argv[1]?.endsWith("follow-run.ts")) {
  // `exitCode` rather than `process.exit`, which would truncate stdout when it is a pipe
  // rather than a terminal. Closing the subscription clears its socket and its timers, so
  // there is nothing left holding the loop open.
  main().then(
    code => {
      process.exitCode = code
    },
    error => {
      process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`)
      process.exitCode = 1
    }
  )
}
