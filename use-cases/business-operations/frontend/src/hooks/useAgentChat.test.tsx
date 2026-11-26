// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest"
import { render, screen, cleanup, act, fireEvent, waitFor } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { MemoryRouter, useNavigate } from "react-router"
import { CaseStatus, Type as TraceSegmentType } from "@/types/cases"
import type { AgentTrace, WorkItem } from "@/types/cases"
import { fetchCase } from "@/services/casesService"
import { clearAllTranscripts, getTranscript } from "@/hooks/transcript"
import { buildPromptWithHistory } from "@business-operations/run-client"
import { setAgentActivity, useAgentActivity } from "@/hooks/agentActivity"
import { AP_PACK } from "@/test/packs"

/**
 * What a chat turn owes the case it was sent about.
 *
 * A case is seeded from its `agent_traces` at most once per page session, so nothing —
 * a return trip, a Clear, a poll tick — re-seeds the stored record over a turn that is
 * still streaming. Replay from the record is a fresh-mount path now, covered by
 * "replays a case's stored traces on a fresh mount".
 *
 * The case still has to be refetched after every turn, chat included: the trace is
 * written server-side, and a case left holding its pre-turn record hands a reload the
 * conversation as it was before the turn. Every other caller did that refetch; the chat
 * path did not, which is how this shipped.
 */

vi.mock("@/services/casesService", () => ({ fetchCase: vi.fn(async () => null) }))
vi.mock("@/hooks/useApi", () => ({
  useApi: () => async () => "id",
  useApiToken: () => async () => "id",
}))
vi.mock("react-oidc-context", () => ({ useAuth: () => ({ isAuthenticated: true }) }))

let channelHandler: ((envelope: RunEventEnvelope) => void) | undefined
const closeChannel = vi.fn()
// Only the socket is stubbed. `@business-operations/run-client` also exports the reduction this suite is
// here to exercise — `RunFollower`, the reducer, the trace replay — so the real module is
// spread back in and just its transport swapped. Standing in for the whole barrel would
// stub out the code under test.
vi.mock("@business-operations/run-client", async () => ({
  ...(await vi.importActual<typeof import("@business-operations/run-client")>(
    "@business-operations/run-client"
  )),
  subscribeRunEvents: vi.fn((options: { onEvent: (e: RunEventEnvelope) => void }) => {
    channelHandler = options.onEvent
    return { close: closeChannel }
  }),
}))

import { useAgentChat } from "@/hooks/useAgentChat"
import { subscribeRunEvents, type RunEventEnvelope } from "@business-operations/run-client"

const CASE_A = "ap-aaaaaaaaaaaaaaaaaaaaaaaaaa"
const CASE_B = "ap-bbbbbbbbbbbbbbbbbbbbbbbbbb"

/** Ids copied from a real published run: the adapter rotates uuid4s per assistant message,
 *  and tool call ids are the model's own. None is derivable from the run id. `messageId` is
 *  required on TEXT_MESSAGE_CONTENT, so every frame here carries one — a frame without it
 *  routes through a reducer fallback production never reaches, which is what let a
 *  double-render bug live through two rounds of review.
 *
 *  Two message ids because sameness is load-bearing in both directions: one run's frames
 *  share an id whichever path they arrive by, and two runs never share one. */
const AGENT_MESSAGE_ID = "fc22616c-b2f5-474a-a18d-9c095347f61b"
const OTHER_MESSAGE_ID = "86d6f705-1c70-4e9d-adb0-ef25886d1dee"
const TOOL_USE_ID = "tooluse_Fk3VR1TtDShro0M87WDbL5"

/** A tool call the agent started and never finished. Field names come from the reducer
 *  (`packages/run-client/src/reducer.ts`), and
 *  `parentMessageId` is on the real frame — without it the tool opens a bubble of its own
 *  instead of landing on the message the deltas are writing. */
const toolCallStart = {
  type: "TOOL_CALL_START",
  toolCallId: "tool-1",
  toolCallName: "sap_read",
  parentMessageId: AGENT_MESSAGE_ID,
}

/** The natural key each test case id stands for, since a minted id is a digest of it. */
const NATURAL_KEY: Record<string, [string, string]> = {
  "ap-aaaaaaaaaaaaaaaaaaaaaaaaaa": ["5100001976", "2026"],
  "ap-bbbbbbbbbbbbbbbbbbbbbbbbbb": ["5100001977", "2026"],
  "ap-cccccccccccccccccccccccccc": ["5100001978", "2026"],
  "ap-dddddddddddddddddddddddddd": ["5100000001", "2026"],
}

function makeCase(caseId: string, agent_traces?: AgentTrace[]): WorkItem {
  const [document_number, item_id] = NATURAL_KEY[caseId] ?? ["", ""]
  return {
    case_id: caseId,
    document_number,
    item_id,
    key: { SupplierInvoice: document_number, FiscalYear: item_id },
    pack_id: "finance_ops",
    process_type: "price_variance",
    status: CaseStatus.Detected,
    created_at: "2026-07-01T00:00:00Z",
    updated_at: "2026-07-01T00:00:00Z",
    ...(agent_traces ? { agent_traces } : {}),
  }
}

/** The turn as the agent writes it: the prompt exactly as sent, scaffolding included. */
function turnTrace(userMessage: string, answer: string): AgentTrace {
  return {
    trace_id: "trace-1",
    timestamp: "2026-07-01T00:01:00Z",
    prompt: buildPromptWithHistory(userMessage, []),
    segments: [{ type: TraceSegmentType.Text, content: answer }],
  }
}

/** A snapshot's timestamps as the writer would have left them a moment ago, 2 s apart —
 *  its throttle. A snapshot older than LIVE_RUN_STALE_SECONDS is a dead run and is not
 *  rendered, so a fixture pinned to a fixed date replays a live turn only until that date
 *  passes; these two have to be relative to now to keep meaning what they mean. */
function justWritten() {
  const now = Date.now()
  return {
    started_at: new Date(now - 2000).toISOString(),
    updated_at: new Date(now).toISOString(),
  }
}

/** `isLoading` as of every render. A test about the handover from the POST to `live_run`
 *  needs the frames in between, not just the value it settles on. */
const loadingRenders: boolean[] = []

/** The hook, with the controls a test needs: send a message, process a case, change the
 *  case — plus the busy state as the UI reads it, per slot and for the case on screen. */
function Harness() {
  const chat = useAgentChat()
  const navigate = useNavigate()
  loadingRenders.push(chat.isLoading)
  // The rail's own store, read the way AgentHeartbeat reads it: the hook publishes
  // there rather than returning activity, and the channel is now its only writer.
  const activity = useAgentActivity()
  return (
    <div>
      <span data-testid="case">{chat.caseId ?? "none"}</span>
      <span data-testid="activity">{activity.kind === "idle" ? "" : activity.kind}</span>
      <span data-testid="loading">{chat.isLoading ? "busy" : "idle"}</span>
      <span data-testid="running-a">{chat.isRunning(CASE_A) ? "running" : "idle"}</span>
      <span data-testid="running-b">{chat.isRunning(CASE_B) ? "running" : "idle"}</span>
      <span data-testid="slot">{chat.slotKey}</span>
      <span data-testid="error">{chat.error ?? ""}</span>
      <button onClick={() => void chat.send("hello")}>send</button>
      <button onClick={() => void chat.narrate("brief me")}>narrate</button>
      {/* Called the way ChatInput calls it — the returned promise is dropped, so a rejection
          inside `stop` reaches nobody. */}
      <button onClick={() => void chat.stop()}>stop</button>
      {/* One argument: a case id is opaque, so the caller has nothing to take apart. */}
      <button onClick={() => void chat.processCase(CASE_B)}>process-b</button>
      <button onClick={() => navigate(`/?case=${CASE_B}`)}>goto-b</button>
      <button onClick={() => navigate(`/?case=${CASE_A}`)}>goto-a</button>
      <button onClick={() => navigate("/")}>goto-none</button>
      <button onClick={() => chat.clear()}>clear</button>
    </div>
  )
}

function renderChat() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const invalidate = vi.spyOn(client, "invalidateQueries")
  const view = render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/?case=${CASE_A}`]}>
        <Harness />
      </MemoryRouter>
    </QueryClientProvider>
  )
  return { ...view, client, invalidate }
}

async function click(name: string) {
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name }))
  })
}

const AWS_EXPORTS = {
  // Required: the ready gate reads this, and `startRun` builds its URL from it.
  apiUrl: "https://api.test",
  runEventsHttpDomain: "abc.appsync-api.us-east-1.amazonaws.com",
  runEventsRealtimeDomain: "abc.appsync-realtime-api.us-east-1.amazonaws.com",
}

/** The run-control POSTs, with the bearer they were sent under. The token is recorded
 *  because these two routes are the only ones in this hook that go to API Gateway, and its
 *  authorizer is what decides whether the request reaches the Lambda at all. */
let startCalls: Array<{ url: string; body: Record<string, unknown>; token: string }> = []
/** Holds a POST open. The POST being in flight is all `isRunning` reports now, so a test
 *  about two slots at once has to be able to keep one from resolving. */
let runsGate: ((body: Record<string, unknown>) => Promise<void>) | undefined
/** Set to make `POST /runs/stop` refuse. The realistic case is a 403 from the stop route's
 *  narrowed IAM scope, and the operator has to hear about it: the run is still going. */
let stopStatus: number | undefined
/** Set to make the stop route report that the Runtime would not end the session. Not an
 *  error — the route answers 202 either way, because it released the claim regardless — so
 *  nothing throws and only the body says the agent is still running. */
let stopRefused = false
/** Set to make `POST /runs` answer 409 — the claim was already held, by a run this browser
 *  may know nothing about. Not an error, so nothing throws: the turn just went nowhere. */
let startStatus: number | undefined
/** The body that answer carries; the default names the run in flight. */
let startBody: Record<string, unknown> = { error: "already_running", run_id: "run-held" }

beforeEach(() => {
  clearAllTranscripts()
  setAgentActivity({ kind: "idle" })
  // The turn's trace exists from the second read onward — the agent writes it while the
  // run streams, so only a refetch after the run can see it.
  let readsOfA = 0
  vi.mocked(fetchCase)
    .mockReset()
    .mockImplementation(async (caseId: string) => {
      if (caseId !== CASE_A) return makeCase(caseId)
      return readsOfA++ === 0
        ? makeCase(CASE_A)
        : makeCase(CASE_A, [turnTrace("hello", "all three legs match")])
    })
  startCalls = []
  runsGate = undefined
  stopStatus = undefined
  stopRefused = false
  startStatus = undefined
  startBody = { error: "already_running", run_id: "run-held" }
  // One stub, three answers: config, /runs, /runs/stop. A catch-all here would let a
  // test asserting on a POST body pass against the aws-exports response.
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/runs") || url.endsWith("/runs/stop")) {
        const body = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>
        const auth = (init?.headers as Record<string, string> | undefined)?.Authorization
        startCalls.push({ url, body, token: String(auth ?? "").replace("Bearer ", "") })
        await runsGate?.(body)
        if (stopStatus && url.endsWith("/runs/stop")) {
          return new Response(JSON.stringify({ error: "not authorized" }), { status: stopStatus })
        }
        if (url.endsWith("/runs/stop")) {
          return new Response(JSON.stringify({ stopped: !stopRefused }), { status: 202 })
        }
        if (startStatus && url.endsWith("/runs")) {
          return new Response(JSON.stringify(startBody), {
            status: startStatus,
          })
        }
        return new Response(JSON.stringify({ run_id: "run-1", session_id: "session-1" }), {
          status: 202,
        })
      }
      return new Response(JSON.stringify(AWS_EXPORTS))
    })
  )
  // All three belong to the mount under test. A handler left over from the last test
  // would satisfy the wait for one immediately and push events at an unmounted tab.
  channelHandler = undefined
  closeChannel.mockClear()
  vi.mocked(subscribeRunEvents).mockClear()
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

/** Wait until the case's first record has been read and the runtime is confirmed. */
async function settle() {
  await waitFor(() => expect(vi.mocked(fetchCase)).toHaveBeenCalledWith(CASE_A, "id"))
  await act(async () => {
    await Promise.resolve()
  })
}

/** Publish one frame the way the agent does. The channel is the only route from a run to
 *  the transcript now — a run this tab started included — so every test that needs a
 *  mid-run turn goes through here rather than through the start call. */
async function publish(
  slot: string,
  event: Record<string, unknown>,
  runId = "run-1",
  seq?: number
) {
  await waitFor(() => expect(channelHandler).toBeDefined())
  await act(async () => {
    channelHandler!({ caseId: slot, runId, ...(seq === undefined ? {} : { seq }), event })
  })
}

/** One assistant delta, which is all most of these tests need off a run. */
const textFrame = (delta: string, messageId = AGENT_MESSAGE_ID) => ({
  type: "TEXT_MESSAGE_CONTENT",
  messageId,
  delta,
})

describe("useAgentChat chat turns", () => {
  it("refetches the case it just talked about", async () => {
    const { invalidate } = renderChat()
    await settle()

    await click("send")

    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ["case", CASE_A] }))
    // The list carries each case's status and last update, which a turn can move too.
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["cases"] })
  })

  it("still shows the turn after switching to another case and back", async () => {
    renderChat()
    await settle()

    await click("send")
    await waitFor(() => expect(getTranscript(CASE_A).map(m => m.content)).toContain("hello"))
    await publish(CASE_A, textFrame("all three legs match"))

    await click("goto-b")
    await waitFor(() => expect(screen.getByTestId("case")).toHaveTextContent(CASE_B))
    await click("goto-a")
    await waitFor(() => expect(screen.getByTestId("case")).toHaveTextContent(CASE_A))

    // The live turn survives the round trip: the case was seeded once on mount, so the
    // record refetched after the turn never seeds over it. Both halves are still there,
    // and the user's half reads as what they typed rather than as the prompt built around
    // it. (Replay from the record is covered by "replays a case's stored traces on a
    // fresh mount" — this array is the same either way, so it is not evidence about
    // replay.)
    await waitFor(() =>
      expect(getTranscript(CASE_A).map(m => ({ role: m.role, content: m.content }))).toEqual([
        { role: "user", content: "hello" },
        { role: "assistant", content: "all three legs match" },
      ])
    )
  })

  it("refetches the case in context, not the one that was focused first", async () => {
    // The turn is written against the case in the URL now; refreshing anything else
    // leaves the case the operator is actually looking at holding a stale record.
    const { invalidate } = renderChat()
    await settle()

    await click("goto-b")
    await waitFor(() => expect(screen.getByTestId("case")).toHaveTextContent(CASE_B))
    await click("send")

    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ["case", CASE_B] }))
    expect(invalidate).not.toHaveBeenCalledWith({ queryKey: ["case", CASE_A] })
  })

  it("keeps a mid-run turn on its own case while the operator wanders", async () => {
    // The reported bug: one shared transcript meant switching away mid-run re-seeded it
    // from the other case's record while the live run kept appending — so the turn
    // vanished from A and bled into B. A contextless route in between used to reset the
    // seed guard as well, so coming back to A clobbered it a second time.
    //
    // A's record carries a stored exchange of its own, so a re-seed shows up as that
    // exchange *replacing* the live turn rather than as an empty slot. That is also the
    // proof this test is not vacuous: the seed effect early-returns when the record has
    // not loaded, and the stored pair being in the slot is how we know it had.
    vi.mocked(fetchCase).mockImplementation(async (caseId: string) =>
      caseId === CASE_A
        ? makeCase(CASE_A, [turnTrace("what changed here", "the PO was re-priced last week")])
        : makeCase(caseId)
    )
    renderChat()
    await settle()
    await waitFor(() =>
      expect(getTranscript(CASE_A).map(m => m.content)).toContain("the PO was re-priced last week")
    )

    await click("send")

    await click("goto-b")
    await waitFor(() => expect(screen.getByTestId("case")).toHaveTextContent(CASE_B))
    // Mid-run: the frame arrives while B is on screen, and no terminal frame follows.
    await publish(CASE_A, textFrame("all three legs match"))

    // B is what the operator is reading, and A's run has nothing to do with it.
    expect(getTranscript(CASE_B)).toEqual([])
    expect(getTranscript(CASE_A).map(m => m.content)).toContain("all three legs match")

    await click("goto-none")
    await click("goto-a")
    await settle()
    // A is seeded from its record once, so the return trip leaves the stored exchange and
    // the live turn side by side. A re-seed would cut this back to the stored pair.
    expect(getTranscript(CASE_A).map(m => m.content)).toEqual([
      "what changed here",
      "the PO was re-priced last week",
      "hello",
      "all three legs match",
    ])
  })

  it("starts a run on a second case while the first is still streaming", async () => {
    // The reported bug: one busy flag for the whole app. A run on any case disabled every
    // Process button and the chat input, so an operator who started case A and moved on
    // could not touch case B until A finished — which for a real run is minutes.
    //
    // `isRunning` is the start request now, so two slots busy at once means two POSTs held
    // open. That is what `runsGate` is for.
    const held = new Map<string, () => void>()
    runsGate = async body => new Promise<void>(resolve => held.set(String(body.case_id), resolve))

    renderChat()
    await settle()
    await click("send")
    await waitFor(() => expect(held.has(CASE_A)).toBe(true))

    await click("goto-b")
    await waitFor(() => expect(screen.getByTestId("case")).toHaveTextContent(CASE_B))
    await click("process-b")
    await waitFor(() => expect(held.has(CASE_B)).toBe(true))

    // Two runs in flight, and the input gates on the case being read rather than on
    // whether anything anywhere is busy.
    expect(screen.getByTestId("running-a")).toHaveTextContent("running")
    expect(screen.getByTestId("running-b")).toHaveTextContent("running")
    expect(screen.getByTestId("loading")).toHaveTextContent("busy")

    // Two runs, so two message ids — a shared one would let a cross-slot splice pass.
    await publish(CASE_A, textFrame("all three legs match"))
    await publish(CASE_B, textFrame("the GR posted late", OTHER_MESSAGE_ID), "run-2")
    expect(getTranscript(CASE_A).map(m => m.content)).toContain("all three legs match")
    expect(getTranscript(CASE_B).map(m => m.content)).toContain("the GR posted late")
    expect(getTranscript(CASE_A).map(m => m.content)).not.toContain("the GR posted late")

    // B's start returning says nothing about A: each run clears only its own slot.
    await act(async () => {
      held.get(CASE_B)!()
    })
    await waitFor(() => expect(screen.getByTestId("running-b")).toHaveTextContent("idle"))
    expect(screen.getByTestId("running-a")).toHaveTextContent("running")
    // And the input, which belongs to the case on screen: B is done, so B's operator can
    // type again while A keeps running. `isLoading` reading "is anything busy" rather than
    // "is this slot busy" is the reported bug itself, and nothing above catches it — every
    // other assertion here is on `isRunning`, which takes the case as an argument.
    expect(screen.getByTestId("loading")).toHaveTextContent("idle")

    await act(async () => {
      held.get(CASE_A)!()
    })
    await waitFor(() => expect(screen.getByTestId("running-a")).toHaveTextContent("idle"))
  })

  it("keeps Clear from re-arming the replay over the next turn", async () => {
    // Clear must not forget the case's seed claim. Forgetting it re-arms the replay
    // effect, and the very next record to arrive seeds over whatever is in the slot — one
    // further turn is enough, because the refetch that turn fires hands the effect a
    // moved record that does not carry the turn's own trace yet.
    let readsOfA = 0
    const stored = [turnTrace("what changed here", "the PO was re-priced last week")]
    vi.mocked(fetchCase).mockImplementation(async (caseId: string) => {
      if (caseId !== CASE_A) return makeCase(caseId)
      // A distinct `updated_at` per read, so the refetched record is a new value rather
      // than the cached one handed back by React Query's structural sharing.
      readsOfA += 1
      return { ...makeCase(CASE_A, stored), updated_at: `2026-07-01T00:0${readsOfA}:00Z` }
    })
    const { client } = renderChat()
    await settle()
    // The stored pair being in the slot is the proof the seed effect ran with a record in
    // hand — and its content is nothing the run below streams, so a clobber cannot hide.
    await waitFor(() =>
      expect(getTranscript(CASE_A).map(m => m.content)).toEqual([
        "what changed here",
        "the PO was re-priced last week",
      ])
    )
    const seededRecord = client.getQueryData(["case", CASE_A])

    await click("clear")
    expect(getTranscript(CASE_A)).toEqual([])

    await click("send")
    await publish(CASE_A, textFrame("all three legs match"))
    // The post-turn refetch has to actually land, or this test proves nothing: a new
    // record in the cache is exactly what would re-run the replay effect.
    await waitFor(() => expect(client.getQueryData(["case", CASE_A])).not.toBe(seededRecord))
    await act(async () => {
      await Promise.resolve()
    })

    expect(getTranscript(CASE_A).map(m => m.content)).toEqual(["hello", "all three legs match"])
  })

  it("replays a case's stored traces on a fresh mount", async () => {
    // Seeding happens once per case per page session now, so a reload — or a second
    // tab — is what has to fall back on the record.
    vi.mocked(fetchCase).mockImplementation(async (caseId: string) =>
      caseId === CASE_A
        ? makeCase(CASE_A, [turnTrace("hello", "all three legs match")])
        : makeCase(caseId)
    )
    renderChat()
    await waitFor(() =>
      expect(getTranscript(CASE_A).map(m => ({ role: m.role, content: m.content }))).toEqual([
        { role: "user", content: "hello" },
        { role: "assistant", content: "all three legs match" },
      ])
    )
  })
})

describe("useAgentChat narration", () => {
  it("starts a run that leaves no user turn on screen", async () => {
    renderChat()
    await settle()
    // The briefing is the case-less conversation, which is where a narration ever happens.
    await click("goto-none")
    const slot = screen.getByTestId("slot").textContent!

    await click("narrate")

    // Sent, with the history scaffolding every other turn gets.
    await waitFor(() => expect(startCalls).toHaveLength(1))
    expect(String(startCalls[0].body.prompt)).toContain("brief me")
    // The point of the method: the operator never asked, so nothing of theirs is on screen.
    // `send` cannot do this — it appends the prompt as a user turn before streaming.
    expect(getTranscript(slot).filter(m => m.role === "user")).toEqual([])
  })

  it("says the narration failed and leaves the transcript alone", async () => {
    renderChat()
    await settle()
    await click("goto-none")
    const slot = screen.getByTestId("slot").textContent!
    // The start refuses — an expired session, a 5xx, a dropped connection. The briefing's
    // whole degradation story is that the deterministic sentence stays and only the prose
    // is missing, so the failure has to reach the banner and nothing else.
    runsGate = async () => {
      throw new Error("boom")
    }

    await click("narrate")

    await waitFor(() => expect(screen.getByTestId("error").textContent).toMatch(/not be narrated/))
    expect(getTranscript(slot)).toEqual([])
  })
})

describe("useAgentChat server-owned runs", () => {
  it("asks on a message and the briefing, and processes only on Process", async () => {
    // The server reads a start with no `intent` as a question, which is given no tool that
    // changes SAP, a case or a ticket. Only Process may say otherwise.
    renderChat()
    await settle()

    await click("send")
    await waitFor(() => expect(startCalls).toHaveLength(1))
    expect(startCalls[0].body.case_id).toBe(CASE_A)
    expect(startCalls[0].body).not.toHaveProperty("intent")

    await click("goto-none")
    await click("narrate")
    await waitFor(() => expect(startCalls).toHaveLength(2))
    expect(startCalls[1].body).not.toHaveProperty("intent")

    await click("process-b")
    await waitFor(() => expect(startCalls).toHaveLength(3))
    expect(startCalls[2].body.case_id).toBe(CASE_B)
    expect(startCalls[2].body.intent).toBe("process")
  })

  it("starts a run through the API", async () => {
    renderChat()
    await settle()
    await click("send")

    expect(startCalls.map(c => c.url.replace(/^.*\/runs/, "/runs"))).toEqual(["/runs"])
    expect(startCalls[0].body.case_id).toBe(CASE_A)
  })

  it("authorizes both run-control routes with the id token", async () => {
    // `/runs` and `/runs/stop` are API Gateway routes, and the COGNITO_USER_POOLS authorizer
    // in front of them declares no `authorizationScopes` — so it validates the token as an
    // identity and only an id token passes. An access token is refused at the authorizer,
    // before the Lambda, and that 401 carries no `Access-Control-Allow-Origin`: the browser
    // reports "Failed to fetch" and nothing on screen names the token as the reason, so
    // every start and every stop fails with no diagnosis. Which is what shipped — the access
    // token was correct for both calls until these two routes were repointed at the API,
    // and the sibling calls one branch over still want it, so the two live side by side.
    //
    // The token is asserted, not just the call: swapping the two is a one-word edit, and the
    // mock returns distinguishable values for exactly this reason.
    renderChat()
    await settle()

    await click("send")
    await click("stop")

    expect(startCalls.map(c => [c.url.replace(/^.*\/runs/, "/runs"), c.token])).toEqual([
      ["/runs", "id"],
      ["/runs/stop", "id"],
    ])
  })

  it("stays loading from the POST until live_run says the case is busy", async () => {
    // The agent writes `live_run` as it starts, so the case reads busy from the first
    // refetch onward.
    let readsOfA = 0
    vi.mocked(fetchCase).mockImplementation(async (caseId: string) => {
      if (caseId !== CASE_A) return makeCase(caseId)
      readsOfA += 1
      if (readsOfA === 1) return makeCase(CASE_A)
      return {
        ...makeCase(CASE_A),
        updated_at: `2026-07-01T00:0${readsOfA}:00Z`,
        live_run: { run_id: "run-1", ...justWritten(), segments: [] },
      }
    })

    const { invalidate } = renderChat()
    await settle()

    // The case query is refetched before the slot is dropped: otherwise there is a window
    // where neither the slot nor `live_run` reports the case as busy. Asserting only that
    // `invalidate` was called passes either way, so the handover is read off every render
    // instead — one `false` between the POST and the record is the bug.
    loadingRenders.length = 0
    await click("send")

    expect(invalidate).toHaveBeenCalledWith(expect.objectContaining({ queryKey: ["case", CASE_A] }))
    const fromFirstBusy = loadingRenders.slice(loadingRenders.indexOf(true))
    expect(fromFirstBusy[0]).toBe(true)
    expect(fromFirstBusy).not.toContain(false)
  })

  it("reports a case with a fresh live_run as loading after a reload", async () => {
    vi.mocked(fetchCase).mockResolvedValue({
      case_id: CASE_A,
      live_run: { run_id: "run-7", updated_at: new Date().toISOString(), segments: [] },
    } as never)

    renderChat()
    await settle()

    // Nothing was started in this browser session — this is the reload-resume case.
    expect(screen.getByTestId("loading").textContent).toBe("busy")
  })

  it("keeps a takeover from holding less than the run it replaces", async () => {
    renderChat()
    await settle()
    await waitFor(() => expect(channelHandler).toBeDefined())
    await click("send")

    await act(async () => {
      channelHandler!({
        caseId: CASE_A,
        runId: "run-1",
        event: { type: "TEXT_MESSAGE_CONTENT", messageId: AGENT_MESSAGE_ID, delta: "first half " },
      })
    })
    await act(async () => {
      channelHandler!({
        caseId: CASE_A,
        runId: "run-1",
        event: { type: "TEXT_MESSAGE_CONTENT", messageId: AGENT_MESSAGE_ID, delta: "second half" },
      })
    })

    // The same rule on a run agent_invoker owns: a later projection of
    // the same run must never drop content an earlier one already showed.
    expect(getTranscript(CASE_A).map(m => m.content)).toContain("first half second half")
  })

  it("drives the activity rail from the channel, since nothing else does now", async () => {
    renderChat()
    await settle()
    await waitFor(() => expect(channelHandler).toBeDefined())
    await click("send")

    await act(async () => {
      channelHandler!({ caseId: CASE_A, runId: "run-1", event: toolCallStart })
    })
    expect(screen.getByTestId("activity").textContent).not.toBe("")

    await act(async () => {
      channelHandler!({ caseId: CASE_A, runId: "run-1", event: { type: "RUN_FINISHED" } })
    })
    expect(screen.getByTestId("activity").textContent).toBe("")
  })

  it("tells the operator when the server refuses to stop the run", async () => {
    // Stop is the only control anyone has over a run the server owns, and it is wired
    // straight to an onClick — so an unguarded rejection is an unhandled promise and the
    // operator watches a run they think they stopped. A 403 from the stop route's narrowed
    // IAM scope is the case that actually does this.
    stopStatus = 403
    const { invalidate } = renderChat()
    await settle()

    await click("stop")

    expect(
      getTranscript(CASE_A)
        .map(m => m.content)
        .join("\n")
    ).toContain("not authorized")
    // And the refetch is not skipped on the way out: the Stop button's own state reads the
    // case record, so the failure path is exactly when it has to be refreshed.
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["case", CASE_A] })
  })

  it("tells the operator when the run would not stop, even though the stop succeeded", async () => {
    // The quiet half of a stop. The route answers 202 because it released the claim, and
    // separately reports that the Runtime refused to end the session — so nothing throws and
    // the agent is still running. Unreported, the case clears, looks stopped, and goes busy
    // again on the agent's next write with nothing on screen to explain it.
    stopRefused = true
    renderChat()
    await settle()

    await click("stop")

    expect(
      getTranscript(CASE_A)
        .map(m => m.content)
        .join("\n")
    ).toContain("Couldn't confirm the run stopped")
  })

  it("says nothing when the run did stop", async () => {
    renderChat()
    await settle()

    await click("stop")

    expect(
      getTranscript(CASE_A)
        .map(m => m.content)
        .join("\n")
    ).not.toContain("Couldn't confirm the run stopped")
  })

  it("takes back a turn the server refused, rather than leaving it looking sent", async () => {
    // A 409 is the claim already being held — by the poller, by another operator, or by this
    // operator's own second click. It is not an error, so nothing throws and the browser
    // attaches to the run that is already going. But the prompt was never delivered, and the
    // turn went on screen before the POST answered: left there it reads as sent, with someone
    // else's run playing out underneath it as though it were the answer.
    startStatus = 409
    renderChat()
    await settle()

    await click("send")

    await waitFor(() => expect(startCalls).toHaveLength(1))
    const transcript = getTranscript(CASE_A)
    expect(transcript.filter(m => m.role === "user")).toEqual([])
    expect(transcript.map(m => m.content).join("\n")).toContain("already has a run in progress")
  })

  it("shows a refusal to start in the API's own words, keeping the question", async () => {
    // A 409 that names no run is not an attach: a standing emergency stop refuses the turn.
    startStatus = 409
    startBody = {
      error: "An emergency stop stands. Nothing new starts until an administrator clears it.",
    }
    renderChat()
    await settle()

    await click("send")

    await waitFor(() => expect(startCalls).toHaveLength(1))
    await waitFor(() =>
      expect(getTranscript(CASE_A).slice(-1)[0]?.content).toBe(
        "An emergency stop stands. Nothing new starts until an administrator clears it."
      )
    )
    expect(getTranscript(CASE_A).filter(m => m.role === "user")).toHaveLength(1)
  })
})

describe("useAgentChat channel runs", () => {
  it("renders a run this tab never started", async () => {
    // The point of the whole phase: a queued run streams into the case's chat for
    // whoever is looking at it, not just for the browser that pressed the button.
    renderChat()
    await settle()
    await waitFor(() => expect(channelHandler).toBeDefined())

    await act(async () => {
      channelHandler!({
        caseId: CASE_A,
        runId: "run-7",
        event: { type: "RUN_STARTED", runId: "run-7" },
      })
      channelHandler!({
        caseId: CASE_A,
        runId: "run-7",
        event: {
          type: "TEXT_MESSAGE_CONTENT",
          messageId: AGENT_MESSAGE_ID,
          delta: "the GR posted late",
        },
      })
    })

    expect(getTranscript(CASE_A).map(m => m.content)).toContain("the GR posted late")
  })

  it("puts each run's events on its own case", async () => {
    renderChat()
    await settle()
    await waitFor(() => expect(channelHandler).toBeDefined())

    await act(async () => {
      channelHandler!({
        caseId: CASE_B,
        runId: "run-8",
        event: { type: "RUN_STARTED", runId: "run-8" },
      })
      channelHandler!({
        caseId: CASE_B,
        runId: "run-8",
        event: {
          type: "TEXT_MESSAGE_CONTENT",
          messageId: AGENT_MESSAGE_ID,
          delta: "three legs match",
        },
      })
    })

    expect(getTranscript(CASE_B).map(m => m.content)).toContain("three legs match")
    expect(getTranscript(CASE_A)).toEqual([])
  })

  it("appends a redelivered delta once, not twice", async () => {
    // The reported bug, reproduced. A run's rendered reply ended
    // "...investigated and cleared.repancy is invest" — an interior slice of its own last
    // sentence, no console error, corrected by a reload, about one run in five. AppSync
    // Events promises neither at-most-once delivery nor ordering, `appendText` in the
    // reducer is a plain `+=`, and the snapshot that would assign over the damage is
    // suppressed for every run after a case's first in a session. So the envelope's own
    // number is what has to catch it.
    renderChat()
    await settle()

    await publish(CASE_A, textFrame("until the discrepancy is cleared"), "run-7", 1)
    await publish(CASE_A, textFrame("until the discrepancy is cleared"), "run-7", 1)

    expect(getTranscript(CASE_A).map(m => m.content)).toEqual(["until the discrepancy is cleared"])
  })

  it("refetches once for a redelivered terminal frame", async () => {
    // Why the guard sits at the envelope boundary and not inside `appendText`. A duplicated
    // RUN_FINISHED does more than duplicate text: it re-fires the post-run refetch, deletes
    // the channel entry a second time, and re-runs the idle transition. A reducer-level
    // guard leaves every one of those unprotected.
    const { invalidate } = renderChat()
    await settle()
    await publish(CASE_B, textFrame("the GR posted late"), "run-8", 1)
    invalidate.mockClear()

    await publish(CASE_B, { type: "RUN_FINISHED" }, "run-8", 2)
    await publish(CASE_B, { type: "RUN_FINISHED" }, "run-8", 2)

    expect(
      invalidate.mock.calls.filter(
        ([arg]) => (arg as { queryKey: string[] }).queryKey[1] === CASE_B
      )
    ).toHaveLength(1)
  })

  it("starts the count over for the next run on the slot", async () => {
    // The mark has to be per-run, because the sequence is: every run opens at 1. Compared
    // against the run before it, a second run on the same case is entirely below the mark
    // left behind — and since the mark is never evicted, every frame of it is dropped and
    // the case renders nothing at all for the whole run. Worse than the bug being fixed.
    renderChat()
    await settle()
    await publish(CASE_A, textFrame("the first run said this"), "run-7", 1)
    await publish(CASE_A, textFrame(" and this"), "run-7", 2)
    await publish(CASE_A, { type: "RUN_FINISHED" }, "run-7", 3)

    await publish(CASE_A, textFrame("the second run said this", "msg-2"), "run-8", 1)

    expect(getTranscript(CASE_A).map(m => m.content)).toContain("the second run said this")
  })

  it("still applies an envelope that carries no sequence number", async () => {
    // The deploy window. The agent and this bundle do not change over at the same instant,
    // so a run already in flight can publish old-code envelopes to a browser running new
    // code. Rejecting those would blank the transcript for the length of every run that
    // straddles a deploy — so an unnumbered envelope renders as it always did and simply
    // takes no part in dedup.
    renderChat()
    await settle()

    await publish(CASE_A, textFrame("the GR posted late"), "run-7")

    expect(getTranscript(CASE_A).map(m => m.content)).toEqual(["the GR posted late"])
  })

  it("applies a delta that arrives out of order", async () => {
    // The channel promises delivery, not order, and the agent POSTs every five events on a
    // connection of their own — so a transposed pair of POSTs hands this handler a lower
    // number after a higher one. Comparing against a high-water mark drops it, and on a
    // delta payload nothing ever puts it back: `appendText` is the only writer of assistant
    // text there, because the adapter publishes no MESSAGES_SNAPSHOT for
    // `applyMessagesSnapshot` to assign over, and the terminal refetch cannot re-render
    // because a case seeds its transcript once per page session. The reply stays short by
    // that batch until a reload, with nothing logged anywhere.
    renderChat()
    await settle()

    await publish(CASE_A, textFrame("the invoice "), "run-9", 1)
    await publish(CASE_A, textFrame("matches the PO"), "run-9", 3)
    await publish(CASE_A, textFrame("quantity "), "run-9", 2)

    // In sequence order, not arrival order: a delta means nothing except at its own place
    // in the reply.
    expect(getTranscript(CASE_A).map(m => m.content)).toContain(
      "the invoice quantity matches the PO"
    )
  })

  it("renders what it has when the run ends on a hole", async () => {
    // The counterpart risk to holding frames back: a batch the transport actually lost is
    // not late, it is gone, and waiting on it would render less than the gap does. Every run
    // ends on a terminal frame, so that frame releases whatever is still waiting rather than
    // a timer having to guess.
    renderChat()
    await settle()

    await publish(CASE_A, textFrame("the invoice "), "run-9", 1)
    await publish(CASE_A, textFrame("matches the PO"), "run-9", 4)
    await publish(CASE_A, { type: "RUN_FINISHED" }, "run-9", 5)

    expect(getTranscript(CASE_A).map(m => m.content)).toContain("the invoice matches the PO")
  })

  // Retired here: "does not double-render this tab's own run" and "keeps a finished turn
  // when the channel's copy of it lands late". Both pinned the de-duplication between this
  // tab's own SSE body and the channel's copy of the same run. There is one copy now — the
  // channel — so neither has a second render to guard against. What survives of their
  // subject is "keeps a takeover from holding less than the run it replaces", which pins
  // that a later projection of one run never drops what an earlier one showed.

  it("subscribes once to this tab's conversation, at the configured domains", async () => {
    renderChat()
    await settle()
    await waitFor(() => expect(channelHandler).toBeDefined())

    // A per-case channel would cost a socket per case on screen and resubscribe on every
    // URL change; the domains come from the deployment's config, not from this hook. With
    // no pack visible (this suite's registry is empty) there is no pack channel, and never
    // `/runs/*`: the subscribe check refuses it.
    expect(vi.mocked(subscribeRunEvents)).toHaveBeenCalledWith(
      expect.objectContaining({
        channel: expect.stringMatching(/^\/runs\/[0-9a-f-]{36}$/),
        httpDomain: "abc.appsync-api.us-east-1.amazonaws.com",
        realtimeDomain: "abc.appsync-realtime-api.us-east-1.amazonaws.com",
      })
    )

    // The channel authorizes on the *id* token: the API's Cognito authorizer matches
    // `appIdClientRegex` against the token's `aud`, which only the id token carries.
    // Handing it the access token fails the handshake, and swapping the two is a one-word
    // edit — so the token itself is asserted, not just that some getter was passed.
    const options = vi.mocked(subscribeRunEvents).mock.calls[0][0]
    await expect(options.getIdToken()).resolves.toBe("id")

    // Changing the case must not resubscribe — the count alone would stay green if the
    // effect gained a `caseId` dependency, so the URL has to actually move first.
    await click("goto-b")
    expect(vi.mocked(subscribeRunEvents)).toHaveBeenCalledTimes(1)
  })

  it("follows each visible pack's runs by its short id, and never every run", async () => {
    // The subscribe check admits `/runs/<short_id>/*` to the pack's users and refuses
    // `/runs/*`, so the registry this caller is served is the whole scope.
    const served = vi.mocked(fetch).getMockImplementation()!
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) =>
        url.endsWith("/registry")
          ? new Response(JSON.stringify({ packs: [AP_PACK] }))
          : served(url, init)
      )
    )
    renderChat()
    await settle()
    await waitFor(() =>
      expect(vi.mocked(subscribeRunEvents)).toHaveBeenCalledWith(
        expect.objectContaining({ channel: `/runs/${AP_PACK.short_id}/*` })
      )
    )
    const channels = vi.mocked(subscribeRunEvents).mock.calls.map(([o]) => o.channel)
    expect(channels).not.toContain("/runs/*")
  })

  it("says so once when the channel closes without ever subscribing", async () => {
    // The failure this catches renders as nothing at all: no stream, no error, and the
    // finished trace still lands on the record, so a rejected subscribe looks exactly like
    // a quiet queue. `onStatus` had no caller in this app at all, so nothing anywhere said
    // it happened.
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {})
    renderChat()
    await settle()
    await waitFor(() => expect(channelHandler).toBeDefined())
    const onStatus = vi.mocked(subscribeRunEvents).mock.calls[0][0].onStatus!

    onStatus("connecting")
    onStatus("closed")
    // The client reconnects forever on a half-second floor, so warning per close would
    // fill the console rather than report anything.
    onStatus("closed")

    expect(warn).toHaveBeenCalledTimes(1)
    warn.mockRestore()
  })

  it("stays quiet when a subscribed channel drops and reconnects", async () => {
    // A network blip on a working subscription is what the retry loop is for, and it is
    // the common case — warning on it would train the operator to ignore the one warning
    // that means the channel never worked.
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {})
    renderChat()
    await settle()
    await waitFor(() => expect(channelHandler).toBeDefined())
    const onStatus = vi.mocked(subscribeRunEvents).mock.calls[0][0].onStatus!

    onStatus("subscribed")
    onStatus("closed")

    expect(warn).not.toHaveBeenCalled()
    warn.mockRestore()
  })

  it("refetches the case when a channel run finishes", async () => {
    // The trace is written server-side, so the record has moved and nothing else in
    // this tab knows it.
    const { invalidate } = renderChat()
    await settle()
    await waitFor(() => expect(channelHandler).toBeDefined())

    await act(async () => {
      channelHandler!({
        caseId: CASE_B,
        runId: "run-8",
        event: { type: "RUN_STARTED", runId: "run-8" },
      })
      channelHandler!({ caseId: CASE_B, runId: "run-8", event: { type: "RUN_FINISHED" } })
    })

    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ["case", CASE_B] }))
  })

  it("survives that refetch landing a record the finished run is not in yet", async () => {
    // The refetch above is what makes this reachable. The trace is written server-side, so
    // the record that comes back can still be a turn behind — and re-arming this case's
    // replay, by dropping its seed claim alongside the entry in the terminal branch, seeds
    // that pre-run history straight over the turn the operator just watched arrive. It
    // reads like a natural thing to do (the run is over, let the record take back over) and
    // costs the finished turn about a second after it lands, with nothing logged anywhere.
    let readsOfA = 0
    vi.mocked(fetchCase).mockImplementation(async (caseId: string) => {
      if (caseId !== CASE_A) return makeCase(caseId)
      // A distinct `updated_at` per read, so the refetched record is a new value rather
      // than the cached one React Query's structural sharing hands back — an identical
      // record re-runs no effect, and would make this test unfailable.
      readsOfA += 1
      return {
        ...makeCase(CASE_A, [turnTrace("what changed here", "the PO was re-priced last week")]),
        updated_at: `2026-07-01T00:0${readsOfA}:00Z`,
      }
    })
    const { client } = renderChat()
    await settle()
    await waitFor(() => expect(channelHandler).toBeDefined())
    // Seeded, and with content the run below never streams — so a re-seed cannot hide
    // behind matching text.
    await waitFor(() =>
      expect(getTranscript(CASE_A).map(m => m.content)).toEqual([
        "what changed here",
        "the PO was re-priced last week",
      ])
    )
    const seededRecord = client.getQueryData(["case", CASE_A])

    await act(async () => {
      channelHandler!({
        caseId: CASE_A,
        runId: "run-7",
        event: { type: "RUN_STARTED", runId: "run-7" },
      })
      channelHandler!({
        caseId: CASE_A,
        runId: "run-7",
        event: {
          type: "TEXT_MESSAGE_CONTENT",
          messageId: AGENT_MESSAGE_ID,
          delta: "the GR posted late",
        },
      })
      channelHandler!({ caseId: CASE_A, runId: "run-7", event: { type: "RUN_FINISHED" } })
    })
    // The refetch the terminal branch fires has to actually land: a new record in the cache
    // is exactly what would re-run the replay effect.
    await waitFor(() => expect(client.getQueryData(["case", CASE_A])).not.toBe(seededRecord))
    await act(async () => {
      await Promise.resolve()
    })

    expect(getTranscript(CASE_A).map(m => m.content)).toEqual([
      "what changed here",
      "the PO was re-priced last week",
      "the GR posted late",
    ])
  })

  it("stops a tool spinning when the run ends without its result", async () => {
    // The agent died mid-tool. Nothing more will arrive on this run, so an executing
    // spinner would sit there until the operator reloads — the same reason the SSE path
    // settles its projection on every exit.
    renderChat()
    await settle()
    await waitFor(() => expect(channelHandler).toBeDefined())

    // The spinner's own condition, copied from ToolCallDisplay.tsx:74 — a tool call that
    // started and never ended sits at "streaming", which spins just the same.
    const spinning = () =>
      getTranscript(CASE_A)
        .flatMap(m => m.segments ?? [])
        .filter(
          s =>
            s.type === "tool" &&
            (s.toolCall.status === "streaming" || s.toolCall.status === "executing")
        )

    await act(async () => {
      channelHandler!({
        caseId: CASE_A,
        runId: "run-7",
        event: { type: "RUN_STARTED", runId: "run-7" },
      })
      channelHandler!({ caseId: CASE_A, runId: "run-7", event: toolCallStart })
    })
    // Non-vacuous: the spinner has to be there before its absence proves anything.
    expect(spinning()).toHaveLength(1)

    await act(async () => {
      channelHandler!({
        caseId: CASE_A,
        runId: "run-7",
        event: { type: "RUN_ERROR", message: "boom" },
      })
    })
    expect(spinning()).toHaveLength(0)
  })

  it("keeps a cleared case cleared while the run it was watching finishes", async () => {
    // Clear cannot stop a run this tab is only watching, and it cannot even see one: the
    // channel handler never marks the slot busy, so `isLoading` is false and the button is
    // enabled. Without suppression the next frame splices the assistant's half-written turn
    // into the empty transcript, with no user message above it — verbatim the render Clear's
    // own tooltip promises it prevents, repeatable for as long as the run lasts.
    renderChat()
    await settle()
    await waitFor(() => expect(channelHandler).toBeDefined())

    await act(async () => {
      channelHandler!({
        caseId: CASE_A,
        runId: "run-7",
        event: { type: "RUN_STARTED", runId: "run-7" },
      })
      channelHandler!({
        caseId: CASE_A,
        runId: "run-7",
        event: {
          type: "TEXT_MESSAGE_CONTENT",
          messageId: AGENT_MESSAGE_ID,
          delta: "Reading the invoice",
        },
      })
    })
    // Non-vacuous: the watched run has to be on screen before clearing it means anything.
    expect(getTranscript(CASE_A)).toHaveLength(1)

    await click("clear")
    expect(getTranscript(CASE_A)).toEqual([])

    await act(async () => {
      channelHandler!({
        caseId: CASE_A,
        runId: "run-7",
        event: {
          type: "TEXT_MESSAGE_CONTENT",
          messageId: AGENT_MESSAGE_ID,
          delta: ", then the PO",
        },
      })
      channelHandler!({ caseId: CASE_A, runId: "run-7", event: { type: "RUN_FINISHED" } })
    })
    expect(getTranscript(CASE_A)).toEqual([])

    // And the suppression lifts: the next run renders from its own start, or Clear would
    // have blinded this case to every watched run for the rest of the page session.
    await act(async () => {
      channelHandler!({
        caseId: CASE_A,
        runId: "run-8",
        event: { type: "RUN_STARTED", runId: "run-8" },
      })
      channelHandler!({
        caseId: CASE_A,
        runId: "run-8",
        event: {
          type: "TEXT_MESSAGE_CONTENT",
          messageId: OTHER_MESSAGE_ID,
          delta: "Checking the PO",
        },
      })
    })
    expect(getTranscript(CASE_A).map(m => m.content)).toEqual(["Checking the PO"])
  })

  it("idles the rail when a cleared run reaches its terminal frame", async () => {
    // The cleared-run branch returns before the terminal bookkeeping, so it has to do that
    // bookkeeping itself. An empty channel-runs map is the only thing that idles the rail,
    // and a cleared run's entry is dropped nowhere else — so skipping it leaves the rail
    // reading "working" for the rest of the page session, with nothing left to arrive that
    // could ever clear it.
    renderChat()
    await settle()

    await publish(CASE_A, textFrame("Reading the invoice"), "run-7")
    // Non-vacuous: the rail has to be lit before its going out proves anything.
    expect(screen.getByTestId("activity").textContent).toBe("reasoning")

    await click("clear")
    await publish(CASE_A, { type: "RUN_FINISHED" }, "run-7")

    expect(screen.getByTestId("activity").textContent).toBe("")
  })

  it("idles the rail when the start that took the slot never happened", async () => {
    // The gap between "the entry is a newer run's" and "there is no entry". A start deletes
    // the slot's entry before it POSTs, so a refused start — a 409 for a run this browser
    // knows nothing about — leaves the map empty with nothing to rebuild it. The dismissed
    // run's terminal frame is then the last thing that will ever arrive for this slot, and if
    // it does not idle the rail nothing does for the rest of the page session.
    renderChat()
    await settle()

    await publish(CASE_A, textFrame("Reading the invoice"), "run-7")
    expect(screen.getByTestId("activity").textContent).toBe("reasoning")
    await click("clear")

    startStatus = 409
    await click("send")

    await publish(CASE_A, { type: "RUN_FINISHED" }, "run-7")

    expect(screen.getByTestId("activity").textContent).toBe("")
  })

  it("leaves a newer run's entry alone when the run it replaced finishes late", async () => {
    // Clear cannot stop the run it dismissed, so the next run can be under way before the
    // dismissed one's terminal frame arrives — and by then the slot's entry is the new run's.
    // Dropping it unconditionally idles the rail on a run that is still publishing, and
    // rebuilds the next frame's projection with an `ownedIds` that no longer claims what this
    // run has already spliced in.
    renderChat()
    await settle()

    await publish(CASE_A, textFrame("Reading the invoice"), "run-7")
    await click("clear")
    await publish(CASE_A, textFrame("Checking the PO", OTHER_MESSAGE_ID), "run-8")
    // Non-vacuous: the second run has to hold the rail before A's late frame can take it.
    expect(screen.getByTestId("activity").textContent).toBe("reasoning")

    await publish(CASE_A, { type: "RUN_FINISHED" }, "run-7")

    expect(screen.getByTestId("activity").textContent).toBe("reasoning")
    await publish(CASE_A, textFrame(", then the receipt", OTHER_MESSAGE_ID), "run-8")
    expect(getTranscript(CASE_A).map(m => m.content)).toEqual(["Checking the PO, then the receipt"])
  })

  // Retired here: "shows the next watched run even if this tab was busy when it started".
  // Its whole setup was a locally streamed run holding the slot so that the drop-this-tab's-own-
  // frames guard swallowed a second run's RUN_STARTED. That guard is gone with the stream, so
  // there is no longer a state in which a start frame is dropped for being local. The property
  // it was really about — that the mark names a run and not a slot — is pinned by the test
  // below, which needs no local run at all.

  it("shows the next watched run even if its start never arrived", async () => {
    // The same latch on a trigger no guard is involved in: the channel has no replay, so a
    // RUN_STARTED published while the socket is between connections is gone, and nothing
    // re-sends it. A mark that waits for one therefore waits forever, and swallows a run the
    // operator never dismissed. Matching on the run id needs no arrival at all.
    renderChat()
    await settle()
    await waitFor(() => expect(channelHandler).toBeDefined())

    await act(async () => {
      channelHandler!({
        caseId: CASE_A,
        runId: "run-7",
        event: { type: "RUN_STARTED", runId: "run-7" },
      })
      channelHandler!({
        caseId: CASE_A,
        runId: "run-7",
        event: {
          type: "TEXT_MESSAGE_CONTENT",
          messageId: AGENT_MESSAGE_ID,
          delta: "Reading the invoice",
        },
      })
    })
    expect(getTranscript(CASE_A)).toHaveLength(1)
    await click("clear")
    expect(getTranscript(CASE_A)).toEqual([])

    // run-9's frames, with its RUN_STARTED never delivered.
    await act(async () => {
      channelHandler!({
        caseId: CASE_A,
        runId: "run-9",
        event: {
          type: "TEXT_MESSAGE_CONTENT",
          messageId: OTHER_MESSAGE_ID,
          delta: "Checking the PO",
        },
      })
    })
    expect(getTranscript(CASE_A).map(m => m.content)).toEqual(["Checking the PO"])
  })

  it("closes the socket when the hook unmounts", async () => {
    const view = renderChat()
    await settle()
    await waitFor(() => expect(channelHandler).toBeDefined())

    view.unmount()

    expect(closeChannel).toHaveBeenCalled()
  })

  it("replays a live_run so a case opened mid-run is not a blank chat", async () => {
    // Before the first channel frame arrives there is nothing to render but the
    // snapshot the agent has been writing to the record every two seconds.
    vi.mocked(fetchCase).mockImplementation(async (caseId: string) =>
      caseId === CASE_A
        ? {
            ...makeCase(CASE_A),
            live_run: {
              run_id: "run-live",
              prompt: "Process case ap-aaaaaaaaaaaaaaaaaaaaaaaaaa",
              ...justWritten(),
              segments: [{ type: TraceSegmentType.Text, content: "Reading the invoice" }],
            },
          }
        : makeCase(caseId)
    )
    renderChat()

    await waitFor(() =>
      expect(getTranscript(CASE_A).map(m => m.content)).toContain("Reading the invoice")
    )
  })

  it("does not replay a live_run stranded by a container that died mid-run", async () => {
    // Only the finished-trace write removes `live_run`, and a container killed before it
    // (recycle, OOM, timeout) never gets there — so the snapshot stays on the record with
    // nothing left alive to clear it. Replayed, its result-less tool call renders as a
    // spinner for every operator who opens the case, in every tab, with no timeout and no
    // way to dismiss it. Past LIVE_RUN_STALE_SECONDS the snapshot is a dead run, not a run.
    vi.mocked(fetchCase).mockImplementation(async (caseId: string) =>
      caseId === CASE_A
        ? {
            ...makeCase(CASE_A, [turnTrace("what happened", "Posted the credit")]),
            live_run: {
              run_id: "run-stranded",
              prompt: "Process case ap-aaaaaaaaaaaaaaaaaaaaaaaaaa",
              started_at: "2026-08-20T10:00:00Z",
              // Thirty missed writes at the writer's throttle, and counting.
              updated_at: "2026-08-20T10:00:02Z",
              segments: [{ type: TraceSegmentType.Tool, tool_name: "sap_read", tool_input: "{}" }],
            },
          }
        : makeCase(caseId)
    )
    renderChat()

    // Seeding happened — the stored turn is here — and the strand was left out of it, so
    // this is the record's history and nothing else. The stranded run would add two more.
    await waitFor(() =>
      expect(getTranscript(CASE_A).map(m => m.content)).toEqual([
        "what happened",
        "Posted the credit",
      ])
    )
  })

  it("holds the replayed snapshot, then lets a MESSAGES_SNAPSHOT supersede it", async () => {
    // The record's live_run and the channel are two views of one run, and the record's is
    // the older, longer one: the channel only carries what has happened since this tab
    // subscribed. Its frames name the adapter's own message ids — `messageId` is required
    // on TEXT_MESSAGE_CONTENT — so a delta never lands on the replayed bubble. It opens a
    // second one, and the handler has to claim the replayed id while holding its content,
    // or the splice deletes forty segments to make room for one fragment.
    //
    // MESSAGES_SNAPSHOT is what merges them: it carries the whole accumulated turn under
    // the adapter's ids, a superset of the placeholder. Keeping the placeholder past that
    // point renders the same text — and the same tool call — twice, forever.
    //
    // Frame shapes here are copied from a real run's published payloads (323 of them, case
    // 5100001506-2026): rotating uuid4 message ids, `timestamp` and `rawEvent` present and
    // null, model-issued `tooluse_…` ids. Including the one snapshot that is *not* a
    // superset — the first, emitted before any assistant text, carrying only the user's
    // input — because superseding on that one would wipe the replay.
    vi.mocked(fetchCase).mockImplementation(async (caseId: string) =>
      caseId === CASE_A
        ? {
            ...makeCase(CASE_A),
            live_run: {
              run_id: "run-live",
              prompt: "Process case ap-aaaaaaaaaaaaaaaaaaaaaaaaaa",
              ...justWritten(),
              segments: [
                {
                  type: TraceSegmentType.Tool,
                  tool_name: "sap_read",
                  tool_input: "{}",
                  tool_result: "3 legs",
                },
                { type: TraceSegmentType.Text, content: "Reading the invoice" },
              ],
            },
          }
        : makeCase(caseId)
    )
    renderChat()
    await settle()
    await waitFor(() => expect(channelHandler).toBeDefined())

    await act(async () => {
      channelHandler!({
        caseId: CASE_A,
        runId: "run-live",
        event: {
          type: "TEXT_MESSAGE_CONTENT",
          timestamp: null,
          rawEvent: null,
          messageId: AGENT_MESSAGE_ID,
          delta: ", then the PO",
        },
      })
    })

    // Held, not wiped: the placeholder is still on screen beside the new fragment.
    expect(
      getTranscript(CASE_A)
        .filter(m => m.role === "assistant")
        .map(m => m.content)
    ).toEqual(["Reading the invoice", ", then the PO"])

    // The run's first snapshot, verbatim in shape: the client's one message and nothing
    // else. It supersedes nothing, so the placeholder has to survive it.
    await act(async () => {
      channelHandler!({
        caseId: CASE_A,
        runId: "run-live",
        event: {
          type: "MESSAGES_SNAPSHOT",
          timestamp: null,
          rawEvent: null,
          messages: [
            {
              id: "input-run-live",
              role: "user",
              content: "Process case ap-aaaaaaaaaaaaaaaaaaaaaaaaaa",
            },
          ],
        },
      })
    })
    expect(
      getTranscript(CASE_A)
        .filter(m => m.role === "assistant")
        .map(m => m.content)
    ).toEqual(["Reading the invoice", ", then the PO"])

    await act(async () => {
      channelHandler!({
        caseId: CASE_A,
        runId: "run-live",
        event: {
          type: "MESSAGES_SNAPSHOT",
          timestamp: null,
          rawEvent: null,
          messages: [
            {
              id: "input-run-live",
              role: "user",
              content: "Process case ap-aaaaaaaaaaaaaaaaaaaaaaaaaa",
            },
            {
              id: AGENT_MESSAGE_ID,
              role: "assistant",
              content: "Reading the invoice, then the PO",
              toolCalls: [
                {
                  id: TOOL_USE_ID,
                  type: "function",
                  function: { name: "sap_read", arguments: "{}" },
                },
              ],
            },
            {
              id: "81f49bc0-8c9b-4c1e-86a9-5d2c9036f297",
              role: "tool",
              toolCallId: TOOL_USE_ID,
              content: "3 legs",
            },
          ],
        },
      })
    })

    const answers = getTranscript(CASE_A).filter(m => m.role === "assistant")
    expect(answers.map(m => m.content)).toEqual(["Reading the invoice, then the PO"])
    // The placeholder's tool segment went with it. A write tool shown once complete and
    // once incomplete is a misleading audit trail, not a cosmetic duplicate.
    expect(answers.flatMap(m => m.segments ?? []).filter(s => s.type === "tool")).toHaveLength(1)
  })

  it("keeps a channel run's head when its own frames resume after this tab's run", async () => {
    // Starting a run drops the slot's channel entry, so the watched run's next frame builds
    // a new one. That entry claims ids whose content is already on screen, and a claim it
    // cannot re-render is a deletion at the splice.
    renderChat()
    await settle()
    await waitFor(() => expect(channelHandler).toBeDefined())

    await act(async () => {
      channelHandler!({
        caseId: CASE_A,
        runId: "run-7",
        event: {
          type: "TEXT_MESSAGE_CONTENT",
          messageId: AGENT_MESSAGE_ID,
          delta: "Reading the invoice",
        },
      })
    })
    await click("send")
    await act(async () => {
      channelHandler!({
        caseId: CASE_A,
        runId: "run-7",
        event: {
          type: "TEXT_MESSAGE_CONTENT",
          messageId: AGENT_MESSAGE_ID,
          delta: ", then the PO",
        },
      })
    })

    expect(getTranscript(CASE_A).map(m => m.content)).toContain("Reading the invoice, then the PO")
  })
})
