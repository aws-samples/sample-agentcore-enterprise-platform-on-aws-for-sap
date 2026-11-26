// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useOutletContext } from "react-router"
import { useAuth } from "react-oidc-context"
import { useApiToken } from "@/hooks/useApi"
import { useRegistry } from "@/hooks/useRegistry"
import {
  refusalOf,
  startRun,
  stopRun,
  type StartRunRequest,
  type StartRunResult,
} from "@/services/runControlService"
import {
  RunFollower,
  buildPromptWithHistory,
  isLiveRunStale,
  liveTurn,
  storedTurns,
  subscribeRunEvents,
} from "@business-operations/run-client"
import { getConfig } from "@/lib/config"
import { deriveActivity, setAgentActivity } from "@/hooks/agentActivity"
import { clearTranscript, getTranscript, setTranscript } from "@/hooks/transcript"
import { fetchCase } from "@/services/casesService"
import { caseAttributes } from "@/lib/caseAttributes"
import { describeCase, isCaseId, normalizeCaseId, toRuntimeSessionId } from "@/lib/caseKey"
import type { WorkItem } from "@/types/cases"
import { useCaseInContext } from "@/hooks/agentChat.utils"
import type { AgentChat } from "@/hooks/agentChat.types"

/**
 * The interactive agent conversation, owned once by the layout route.
 *
 * It lives above the router's outlet rather than inside a page because the assistant
 * is ambient: the same run has to survive a move from the workspace to analytics, and
 * a page that owned it would tear the stream down on navigation.
 *
 * There is no context provider. The layout route already has one channel to its
 * children — `<Outlet context>` — so `useAssistant()` below reads it from there.
 *
 * The transcript is deliberately *not* part of that channel. State on a layout route
 * re-renders the outlet context, so holding messages here would re-render the routed
 * page and its case list on every streamed token. It lives in `hooks/transcript`
 * instead, which the assistant subscribes to directly.
 */

export function useAgentChat(): AgentChat {
  const caseId = useCaseInContext()
  const auth = useAuth()
  const getApiToken = useApiToken()
  const queryClient = useQueryClient()

  const [input, setInput] = useState("")
  const [error, setError] = useState<string | null>(null)
  // Which slots have a start request in flight, rather than one "busy" flag. Several cases
  // can run at once — the operator starts one and moves on to the next — and a single flag
  // made every case's Process button and the chat input hostage to whichever run happened
  // to be going. Only ever the POST: once the server has the run, `live_run` says so.
  const [activeSlots, setActiveSlots] = useState<ReadonlySet<string>>(() => new Set())
  const [ready, setReady] = useState(false)
  const [contextCases, setContextCasesState] = useState<WorkItem[]>([])
  // The pack being rehearsed, or null. State rather than a ref because the dock renders
  // from it: a rehearsal is a run the operator did not start from a case and whose writes
  // were withheld, and a transcript that does not say so reads exactly like a real one.
  const [rehearsal, setRehearsal] = useState<{ packId: string; processType: string } | null>(null)

  // Only consulted when no case is in context. With a case, the session *is* the
  // case's, derived below — so no state can drift out of step with the URL, which is
  // what let a "Process" click stream into the previously focused case's session.
  const [ephemeralSessionId, setEphemeralSessionId] = useState(() => crypto.randomUUID())
  const sessionId = caseId ? toRuntimeSessionId(caseId) : ephemeralSessionId
  // One transcript per case, plus one for the case-less conversation.
  const slotKey = caseId ?? ephemeralSessionId
  // The most recent non-null caseId, kept while caseId is null.
  const lastCaseIdRef = useRef<typeof caseId>(null)
  if (caseId !== null) lastCaseIdRef.current = caseId
  const workspaceCaseId = caseId ?? lastCaseIdRef.current

  // Cases already seeded from their record, so a re-render — or a return trip — cannot
  // re-seed over a turn that is still streaming.
  // Seeded once per page session, so a run that happened elsewhere while you
  // were away only shows up on reload. The events channel does not change that, and is not
  // meant to: it renders a run that is still going, and its terminal branch leaves this
  // claim alone deliberately — dropping it re-arms the replay against a record that is
  // still a turn behind, which flickers the just-finished turn away.
  const seededRef = useRef(new Set<string>())
  const activeSlotsRef = useRef(activeSlots)
  activeSlotsRef.current = activeSlots
  // Held in a ref so the follower below — built once, for the life of the hook — can reach
  // the current one without being rebuilt for it.
  const refreshCaseRef = useRef<(slot: string) => void>(() => {})

  /**
   * The run protocol, which is not React's business: `seq` dedup, out-of-order buffering,
   * the per-run projection, and the splice onto whatever is already on screen all live in
   * `@business-operations/run-client`, where a Node client uses the same code. What is left here is the
   * mapping from "a frame was applied" to this app's stores.
   *
   * Built once and never rebuilt: it holds a projection per run in flight, and replacing it
   * mid-run would drop those and re-render the turn from an empty one.
   */
  const [follower] = useState(
    () =>
      new RunFollower({
        readTranscript: getTranscript,
        writeTranscript: setTranscript,
        onFrame: ({ turn }) => {
          // Every run is a watched run now, so this is the only place the rail is driven.
          setAgentActivity(deriveActivity(turn))
        },
        onIdle: () => setAgentActivity({ kind: "idle" }),
        // seededRef is deliberately untouched: dropping a case's seed claim re-arms the
        // replay against a record that is still a turn behind, which flickers the
        // finished turn away.
        onRunEnd: slot => refreshCaseRef.current(slot),
      })
  )

  /** Mark a slot's run as started or finished. */
  const markRun = useCallback((slot: string, running: boolean): void => {
    setActiveSlots(prev => {
      if (prev.has(slot) === running) return prev
      const next = new Set(prev)
      running ? next.add(slot) : next.delete(slot)
      return next
    })
  }, [])

  const isRunning = useCallback((slot: string) => activeSlots.has(slot), [activeSlots])

  // Confirm a run can be started at all: every run starts through POST /runs.
  useEffect(() => {
    getConfig()
      .then(config => setReady(!!config.apiUrl))
      .catch(e => setError(`Config error: ${e}`))
  }, [])

  // The case's prior turns, for replay. Same query key the workspace's detail pane
  // uses, so React Query serves both observers from one request rather than two.
  const caseQuery = useQuery({
    queryKey: ["case", caseId],
    queryFn: async () => {
      const token = await getApiToken()
      if (!token) throw new Error("Not authenticated")
      return fetchCase(caseId!, token)
    },
    enabled: auth.isAuthenticated && !!caseId && isCaseId(caseId),
  })

  // Replay the case's decision chain as the conversation's history, so opening a case
  // continues its thread instead of presenting a blank assistant that has to be told
  // what has already happened. Once per case: the record is a turn behind a run that is
  // still streaming, and seeding from it again would drop the live turn and every
  // client-side notice with it.
  useEffect(() => {
    if (!caseId) return
    if (seededRef.current.has(caseId)) return
    const record = caseQuery.data
    if (!record) return
    // The record's own two halves: the turns already written, plus the snapshot of the
    // one still streaming. A case opened mid-run is otherwise a blank chat until the
    // first channel frame lands, which for a queued run can be a while.
    //
    // A stale snapshot is treated as absent, not as a run. Only the finished-trace write
    // removes `live_run`, so a container killed mid-run (recycle, OOM, timeout) leaves
    // one on the record forever, and its result-less tool segments render as a spinner
    // that nothing will ever settle. `isLiveRunStale` is the reader half of the contract
    // `live_run.py:36` states and `cases.schema.json` promises twice.
    const fresh = record.live_run
    const live = fresh && !isLiveRunStale(fresh) ? fresh : undefined
    setTranscript(caseId, [
      ...storedTurns(record.agent_traces ?? [], live),
      ...(live ? liveTurn(live) : []),
    ])
    seededRef.current.add(caseId)
  }, [caseId, caseQuery.data])

  // A run interrupted by unmount would otherwise leave the rail reading "working".
  useEffect(() => () => setAgentActivity({ kind: "idle" }), [])

  /**
   * Cases a route has put in context beyond the focused one — the workspace's
   * multi-select. Returning `prev` bails out of the re-render, so a route may push
   * this from an effect on every render without looping.
   */
  const setContextCases = useCallback((next: WorkItem[]) => {
    setContextCasesState(prev =>
      prev.length === next.length && prev.every((c, i) => c === next[i]) ? prev : next
    )
  }, [])

  /**
   * Start one run for `slot`. Rendering it is the follower's job, not this function's: the
   * agent publishes every event of every run on the channel, so a run this tab started is
   * watched exactly like one it did not.
   *
   * The slot is a parameter, not read from the current render: the operator may switch
   * cases while this runs, and the turn belongs to the case it was started on. Same
   * reason `runtimeSessionId` is passed in.
   */
  async function runAgentStream(
    prompt: string,
    runtimeSessionId: string,
    slot: string,
    extras?: Record<string, unknown>
  ): Promise<StartRunResult | undefined> {
    if (!ready) return

    const token = await getApiToken()
    const caseId = typeof extras?.case_id === "string" ? extras.case_id : undefined
    const request: StartRunRequest = {
      prompt,
      caseId,
      // Only off a case: with one, the server derives the session from the case id, and
      // it rejects a session id in that namespace. `runtimeSessionId` is the caller's
      // `crypto.randomUUID()` here, which is what the server's length floor needs.
      sessionId: caseId ? undefined : runtimeSessionId,
      processType: typeof extras?.process_type === "string" ? extras.process_type : undefined,
      rehearse: extras?.rehearse === true,
      // Only Process says so. Every other turn is a question, the server's default, whose
      // run is given no tool that changes SAP, a case or a ticket.
      intent: extras?.intent === "process" ? "process" : undefined,
    }
    // A takeover starts a new run on this slot, so the previous run's projection is done.
    follower.takeOver(slot)

    // The id token, not the access token: `/runs` sits behind API Gateway, whose
    // COGNITO_USER_POOLS authorizer declares no `authorizationScopes` and so validates
    // the token as an identity — only an id token passes. An access token is refused at
    // the authorizer, before the Lambda, and that 401 carries no
    // `Access-Control-Allow-Origin` — so the browser reports it as a CORS failure and
    // the start reads as "Failed to fetch" with nothing to show which token was wrong.
    if (!token) throw new Error("Authentication required.")
    // `trigger` is stamped server-side; sending one from here would let a browser
    // claim a run was scheduled.
    const result = await startRun(request, token)
    // The agent owns the run from here: it writes `live_run` and publishes every event
    // on the channel. Await the refetch before the caller's `finally` drops the slot,
    // or `isLoading` briefly reads false between the two.
    if (caseId) await queryClient.invalidateQueries({ queryKey: ["case", caseId] })
    // Handed back rather than swallowed: a 409 is a successful attach to a run that is
    // already going, and the only thing that knows the prompt it refused is the caller.
    return result
  }

  // Keep the callbacks below on the current invocation fn without re-creating them.
  const streamRef = useRef(runAgentStream)
  streamRef.current = runAgentStream

  /** Cases a route has put in context, as a prompt preamble. */
  const contextPrefix = useCallback((): string => {
    if (contextCases.length === 0) return ""
    // Whatever the case's pack put in `attributes`, not a fixed AP triple. The old
    // three lines named `supplier_number` and `amount` outright, so a case from any
    // other pack read as "supplier=none, amount=—" — three tokens of nothing, and a
    // claim that the case had no supplier rather than no such field.
    const items = contextCases.map(c => {
      const attrs = Object.entries(caseAttributes(c)).map(([k, v]) => `${k}=${v}`)
      return [`- ${describeCase(c.case_id, c.key)}: status=${c.status}`, ...attrs].join(", ")
    })
    return `[Context: The user has selected ${contextCases.length} case(s) for processing:\n${items.join("\n")}\n]\n\n`
  }, [contextCases])

  /**
   * Refetch a case after a turn has run against it.
   *
   * The trace is written server-side, so the case is refetched rather than patched.
   * Twice: once now, once after a beat, because the trace reaches DynamoDB after the
   * stream closes and the first read can still miss it.
   *
   * Every turn must do this, chat included. The replay effect seeds the transcript from
   * this query, so a case left holding its pre-turn record replays that record the next
   * time it is opened — which is how switching to another case and back silently
   * discarded everything the user had said.
   */
  const refreshCase = useCallback(
    (key: string) => {
      const refresh = () => {
        void queryClient.invalidateQueries({ queryKey: ["cases"] })
        void queryClient.invalidateQueries({ queryKey: ["case", key] })
      }
      refresh()
      setTimeout(refresh, 3000)
    },
    [queryClient]
  )

  // Assigned after the callback above it, and read by the follower's `onRunEnd`.
  refreshCaseRef.current = refreshCase

  // Kept current without re-registering the subscription — the token getter's identity
  // changes on every refresh, and reconnecting for that would drop live runs.
  const getApiTokenRef = useRef(getApiToken)
  getApiTokenRef.current = getApiToken

  // The channels this operator may follow: every case run in each pack they can see, by
  // the pack's short id, and this tab's own case-less conversation. The channel refuses
  // anything wider (`run_events_auth/`), `/runs/*` included, so the list is the scope.
  const { packs } = useRegistry()
  const channels = useMemo(
    () =>
      [
        ...packs.filter(p => p.short_id).map(p => `/runs/${p.short_id}/*`),
        `/runs/${ephemeralSessionId}`,
      ].join(" "),
    [packs, ephemeralSessionId]
  )

  // The follower outlives a change of channels: a pack published, or a new conversation,
  // must not drop the projection of a run that is still streaming.
  useEffect(() => () => follower.reset(), [auth.isAuthenticated, follower])

  /**
   * One subscription per channel above, for as long as the operator is signed in.
   *
   * A pack's wildcard costs one socket instead of one per case on screen, and each event
   * names its own case — or, off a case, the session id the run was started with — so
   * nothing is resubscribed when the URL changes. A deployment whose config carries no
   * domains has no channel, and so renders nothing until a run's trace lands on the record.
   */
  useEffect(() => {
    if (!auth.isAuthenticated) return
    const subscriptions: { close: () => void }[] = []
    let cancelled = false
    void getConfig()
      .then(config => {
        if (cancelled || !config.runEventsHttpDomain || !config.runEventsRealtimeDomain) return
        // A channel that never subscribes is indistinguishable from a quiet queue: nothing
        // streams, and the finished trace still lands on the record, so the screen looks
        // healthy. The one thing worth saying out loud is a socket that closed without ever
        // having subscribed — an expired id token, a channel the pool cannot reach, or one
        // the subscribe check refused. Once per channel set, and never on our own teardown.
        let warned = false
        // ponytail: one socket per channel; multiplex subscribes on one socket if a
        // deployment's pack count makes that many sockets a cost.
        for (const channel of channels.split(" ")) {
          let subscribed = false
          subscriptions.push(
            subscribeRunEvents({
              httpDomain: config.runEventsHttpDomain,
              realtimeDomain: config.runEventsRealtimeDomain,
              channel,
              getIdToken: () => getApiTokenRef.current(),
              onEvent: envelope => follower.accept(envelope),
              onStatus: status => {
                if (status === "subscribed") subscribed = true
                if (status === "closed" && !subscribed && !warned && !cancelled) {
                  warned = true
                  console.warn(
                    "The live run channel closed before it connected. A run shows here once its record is saved."
                  )
                }
              },
            })
          )
        }
      })
      .catch(() => {
        // No config, no channel. Runs still happen; they just go unrendered until the
        // finished trace reaches the record and a reload replays it.
      })
    return () => {
      cancelled = true
      for (const subscription of subscriptions) subscription.close()
    }
  }, [auth.isAuthenticated, follower, channels])

  /** Append a system statement to a named slot. `processCase` needs this: it starts a run
   *  on a case the URL has not caught up to yet, so `slotKey` here is still the old one —
   *  and that path sets no error state, so a notice in the wrong slot is silence. */
  const postNoticeTo = useCallback((slot: string, content: string) => {
    setTranscript(slot, prev => [
      ...prev,
      { role: "assistant" as const, content, timestamp: new Date().toISOString() },
    ])
  }, [])

  const postNotice = useCallback(
    (content: string) => postNoticeTo(slotKey, content),
    [postNoticeTo, slotKey]
  )

  /** Take back the turn the server refused, and say so.
   *
   *  A 409 is not an error — the browser attaches to the run the case already has — but the
   *  prompt that lost the race was never delivered, and it went on screen before the POST
   *  answered. Left there it reads as sent: the operator watches someone else's run play out
   *  under their own question and never learns it went nowhere. Matched on the content the
   *  caller appended, so this can only ever take back the turn it was called about, and only
   *  the callers that appended one call it — the auth replay does not. */
  const dropRefusedTurn = useCallback(
    (slot: string, content: string) => {
      setTranscript(slot, prev => {
        for (let i = prev.length - 1; i >= 0; i--) {
          if (prev[i].role === "user" && prev[i].content === content) {
            return [...prev.slice(0, i), ...prev.slice(i + 1)]
          }
        }
        return prev
      })
      postNoticeTo(
        slot,
        "This case already has a run in progress, so your message was not sent. " +
          "The run below is that one; stop it first if you want to ask something else."
      )
    },
    [postNoticeTo]
  )

  /** Send a free-form message, with the case in context and any selection attached. */
  const send = useCallback(
    async (userMessage: string): Promise<void> => {
      if (!userMessage.trim() || !ready) return
      setError(null)
      // This turn carries no `rehearse` flag, so the pack is not the rehearsal's. It is a
      // question (no `intent`), so it still writes nothing, but leaving the banner up would
      // say it is a rehearsal.
      setRehearsal(null)
      // Only the plain tool-result path sets a pending resume now, and that raises no
      // server-side interrupt — so there is no stale session to escape. Rotating here
      // would hand the user a new AgentCore Memory thread, discarding the conversation
      // at exactly the moment they finish signing in and expect it to continue.

      // Read at send time rather than closing over the transcript: the store is not
      // React state, so a closure captured on the last render could be a turn behind.
      const slot = slotKey
      const fullPrompt = contextPrefix() + buildPromptWithHistory(userMessage, getTranscript(slot))
      setTranscript(slot, prev => [
        ...prev,
        { role: "user" as const, content: userMessage, timestamp: new Date().toISOString() },
      ])
      setInput("")
      markRun(slot, true)

      try {
        const result = await streamRef.current(
          fullPrompt,
          sessionId,
          slot,
          caseId ? { case_id: caseId } : undefined
        )
        if (result?.alreadyRunning) dropRefusedTurn(slot, userMessage)
      } catch (err) {
        const msg = err instanceof Error ? err.message : "Unknown error"
        const refused = refusalOf(err)
        // A refusal (a standing emergency stop, say) is the API's own sentence, shown as it
        // words it; anything else is a failure to retry.
        setError(refused ? null : `Failed: ${msg}`)
        if (refused) postNoticeTo(slot, refused)
        else
          setTranscript(slot, prev => {
            const updated = [...prev]
            updated[updated.length - 1] = {
              ...updated[updated.length - 1],
              content: "The request failed. Try again.",
            }
            return updated
          })
      } finally {
        markRun(slot, false)
        if (caseId) refreshCase(caseId)
      }
    },
    [
      ready,
      contextPrefix,
      sessionId,
      slotKey,
      caseId,
      refreshCase,
      markRun,
      dropRefusedTurn,
      postNoticeTo,
    ]
  )

  /**
   * Process one case end to end, streaming into the transcript.
   *
   * The session id is computed from the case rather than read off state: the caller
   * has usually just pointed the URL at this case, and state written in the same tick
   * is not visible here — which used to send the run to the previous case's session.
   */
  const processCase = useCallback(
    async (caseId: string, processType?: string): Promise<string | null> => {
      const key = normalizeCaseId(caseId)
      const runtimeSessionId = toRuntimeSessionId(key)
      markRun(key, true)
      setError(null)

      // Claim the replay slot before streaming. The caller usually focuses the case in
      // the same click, so its record lands mid-run — and the replay effect would seed
      // traces that predate this turn straight over it, emptying the transcript.
      seededRef.current.add(key)

      // Snapshot before the append below, so the summary is the conversation up to this
      // turn and not this turn's own "Process case" line restated back to the agent.
      const history = getTranscript(key)

      setTranscript(key, prev => [
        ...prev,
        {
          role: "user" as const,
          content: `Process case ${key}`,
          timestamp: new Date().toISOString(),
        },
      ])

      // History reaches the agent by one client-side route, not two. The transcript is
      // already seeded from this case's agent_traces by the effect above, so the same
      // summarizer the free-text path uses covers replay here too. (Server-side Memory
      // also restores the case's session; this stays as the belt-and-braces for restore
      // lag, which is why buildPromptWithHistory exists at all.)
      // The id alone. It is opaque, but the agent's first act is to retrieve the case
      // state, which carries the natural key — restating fields here only invited the
      // model to treat them as identity.
      const prompt = buildPromptWithHistory(`Process ERP exception case: ${key}`, history)
      let refused: string | null = null
      try {
        const result = await streamRef.current(prompt, runtimeSessionId, key, {
          case_id: key,
          ...(processType ? { process_type: processType } : {}),
          // The one turn that may write: the case's SOP, within the run checks.
          intent: "process",
        })
        if (result?.alreadyRunning) dropRefusedTurn(key, `Process case ${key}`)
      } catch (err) {
        const msg = err instanceof Error ? err.message : "Unknown error"
        refused = refusalOf(err)
        // Addressed to `key` rather than through `postNotice`, which targets `slotKey`: the
        // caller focuses the case in the tick that starts the run, so this closure's
        // `slotKey` is still the previous slot. A throw before streaming — a token refresh
        // that fails — sets no error state either, so the wrong slot means silence.
        postNoticeTo(key, refused ?? `❌ Error processing ${key}: ${msg}`)
      } finally {
        markRun(key, false)
      }

      refreshCase(key)
      return refused
    },
    [postNoticeTo, refreshCase, markRun, dropRefusedTurn]
  )

  /**
   * A run whose prompt the operator never typed.
   *
   * `processCase` minus the visible user line. The seam it needs already exists: `send`
   * prepends `contextPrefix()` to every prompt and displays none of it, so a wire prompt
   * that differs from the transcript is not new here.
   *
   * No slot parameter, for the same reason `send` needs none: it writes where the URL says,
   * and the only state that shows a narration is the case-less one.
   */
  const narrate = useCallback(
    async (prompt: string, shown?: string): Promise<void> => {
      if (!ready) return
      const slot = slotKey
      setError(null)
      // History is read before the shown question goes on screen, so the agent gets it once.
      const history = getTranscript(slot)
      if (shown)
        setTranscript(slot, prev => [
          ...prev,
          { role: "user" as const, content: shown, timestamp: new Date().toISOString() },
        ])
      markRun(slot, true)
      try {
        // History rather than the bare prompt, so an operator who has already been talking
        // gets a briefing that knows it. An assistant-first history windows and collapses
        // correctly in buildPromptWithHistory and emits `Assistant:` lines, so the
        // deterministic sentence posted a moment ago reaches the agent as context too.
        const result = await streamRef.current(
          buildPromptWithHistory(prompt, history),
          sessionId,
          slot,
          undefined
        )
        // Nothing to take back — no user turn was appended — so this only says why the
        // narration is missing rather than reaching for `dropRefusedTurn`.
        if (result?.alreadyRunning) {
          postNoticeTo(slot, "A run is already in progress, so the briefing was not narrated.")
        }
      } catch (err) {
        const msg = err instanceof Error ? err.message : "Unknown error"
        // The deterministic sentence is already on screen and stays there. This only says
        // the prose failed, which is the whole degradation story for the narration.
        setError(`The briefing could not be narrated: ${msg}`)
      } finally {
        markRun(slot, false)
      }
    },
    [ready, slotKey, sessionId, markRun, postNoticeTo]
  )

  /**
   * Rehearse a pack: run its prompt and its SOPs against a case the author made up.
   *
   * `send` with two differences, and both are why this is its own function rather than an
   * options bag on that one. It carries **no case** — `POST /runs` refuses `case_id`
   * beside `rehearse`, because a case-less run is what makes the rehearsal claim no
   * `live_run` and write nothing back to a case row — and it names the `process_type`
   * itself, since with no case there is nothing for the server to resolve the pack from.
   * An **incomplete** pack is reachable this way and no other: nothing routes to it, so the
   * author's own dry run is the only thing that ever exercises it.
   *
   * Authorization is entirely the route's: the author floor, then the pack's own
   * `author_groups`. This sends the flag and shows what comes back.
   */
  const rehearse = useCallback(
    async (packId: string, processType: string, prompt: string): Promise<string | null> => {
      if (!prompt.trim() || !ready) return null
      setError(null)
      const slot = slotKey
      setRehearsal({ packId, processType })
      setTranscript(slot, prev => [
        ...prev,
        { role: "user" as const, content: prompt, timestamp: new Date().toISOString() },
      ])
      markRun(slot, true)
      try {
        // No `contextPrefix()`: a rehearsal is about this pack and nothing the operator
        // happens to have selected elsewhere. History still goes, so an author can
        // follow up on what the pack just did.
        const result = await streamRef.current(prompt, sessionId, slot, {
          process_type: processType,
          rehearse: true,
        })
        if (result?.alreadyRunning) dropRefusedTurn(slot, prompt)
        return null
      } catch (err) {
        const msg = err instanceof Error ? err.message : "Unknown error"
        const refused = refusalOf(err)
        setError(refused ? null : `Rehearsal failed: ${msg}`)
        return refused
      } finally {
        markRun(slot, false)
      }
    },
    [ready, slotKey, sessionId, markRun, dropRefusedTurn]
  )

  /** Stop the run on the case in context. Runs on other cases are left alone — the
   *  button lives under that case's transcript and means that case.
   *
   *  A stopped run writes no trace, so its partial turn is lost on reload — as today. */
  const stop = useCallback(async (): Promise<void> => {
    if (!ready) return
    const token = await getApiToken()
    // The id token, as on `/runs`: `/runs/stop` is an API Gateway route whose
    // COGNITO_USER_POOLS authorizer declares no `authorizationScopes`, so it takes an id
    // token and 401s an access token at the authorizer — with no CORS header on the
    // response, which surfaces as a network error rather than as an auth one.
    if (!token) return
    // The server holds the claim on the case, so it is the only thing that can release
    // it — and it stops a run this tab never started, which an abort controller could not.
    try {
      const stopped = await stopRun(
        { caseId: caseId ?? undefined, sessionId: caseId ? undefined : ephemeralSessionId },
        token
      )
      // The other way a stop fails, and the quieter one: the route released the claim and
      // answered 202, but the Runtime would not end the session. Nothing throws, the case
      // clears, and the agent's next write puts it straight back — so the operator gets the
      // one fact that explains it, and no diagnosis this side does not have.
      if (!stopped) {
        postNoticeTo(slotKey, "Couldn't confirm the run stopped. It may still be finishing.")
      }
    } catch (err) {
      // Said out loud rather than swallowed: a failed
      // stop means the run is still going, and this is the only control the operator has
      // over a run the server owns. `stop` is wired straight to an onClick, so without
      // this the failure is an unhandled rejection and nothing reaches the screen.
      const msg = err instanceof Error ? err.message : String(err)
      postNoticeTo(slotKey, `Couldn't stop the run. It may still be running: ${msg}`)
    }
    // Outside the catch: the Stop button's own state reads the case record, so the failure
    // path is exactly when it must be refetched.
    if (caseId) await queryClient.invalidateQueries({ queryKey: ["case", caseId] })
  }, [ready, getApiToken, caseId, ephemeralSessionId, queryClient, postNoticeTo, slotKey])

  const clear = useCallback(() => {
    clearTranscript(slotKey)
    // Every run keeps publishing after the transcript is emptied, and Clear cannot stop
    // one. `isLoading` disables the button once the case's `live_run` is on the record, but
    // a run in the window before that first write is still clearable. So tell the follower
    // to drop that run's remaining frames rather than render half a turn into a cleared
    // case; it no-ops when no run is on screen, which `clearTranscript` plus the standing
    // seed claim have already covered.
    follower.dismiss(slotKey)
    // The seed claim stays: un-marking the case would re-arm the replay effect, and the
    // next record to arrive — the workspace's poll mid-run, or the refetch after the next
    // turn — would seed stored traces over a live turn. Cleared until reload.
    setError(null)
    setRehearsal(null)
    // Only meaningful off a case. On a case the session belongs to the case, and
    // rotating it would strand the conversation the operator is auditing.
    if (!caseId) setEphemeralSessionId(crypto.randomUUID())
  }, [slotKey, caseId, follower])

  const live = caseQuery.data?.live_run
  // Two sources, because a run this browser did not start is still a run: the slot covers
  // the POST, `live_run` covers everything from the agent's first write until its terminal —
  // including a run that started before this page loaded.
  const isLoading = activeSlots.has(slotKey) || (!!live && !isLiveRunStale(live))

  return {
    caseId,
    workspaceCaseId,
    input,
    setInput,
    isLoading,
    isRunning,
    error,
    setError,
    sessionId,
    slotKey,
    ready,
    contextCaseCount: contextCases.length,
    setContextCases,
    send,
    processCase,
    stop,
    clear,
    postNotice,
    narrate,
    rehearse,
    rehearsal,
  }
}

/**
 * The shell's chat, from inside a routed page. Throws rather than returning a stub:
 * a page that reads this outside the layout route is a routing mistake, and a silent
 * no-op assistant is harder to notice than a failure.
 */
export function useAssistant(): AgentChat {
  return useShell().chat
}

/** What the shell gives a routed page: the chat, and the two ways to bring the panel up. */
export interface ShellContext {
  chat: AgentChat
  /** Send a question to the assistant and open its panel. */
  ask: (question: string) => void
  /** Open the panel without sending anything, for a page that has just started a narration. */
  openAssistant: () => void
}

export function useShell(): ShellContext {
  const shell = useOutletContext<ShellContext | null>()
  if (!shell?.chat) throw new Error("useAssistant must be used inside the AppShell layout route")
  return shell
}
