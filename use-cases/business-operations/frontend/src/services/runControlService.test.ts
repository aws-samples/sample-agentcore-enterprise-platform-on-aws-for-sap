// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import { describe, it, expect, vi, beforeEach } from "vitest"
import { startRun, stopRun } from "./runControlService"

vi.mock("@/lib/config", () => ({
  getConfig: vi.fn().mockResolvedValue({ apiUrl: "https://api.test" }),
}))

const jsonResponse = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } })

describe("startRun", () => {
  beforeEach(() => vi.unstubAllGlobals())

  it("posts the run and returns the ids to watch", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse(202, { run_id: "run-1", session_id: "session-1" }))
    vi.stubGlobal("fetch", fetchMock)

    const result = await startRun(
      { prompt: "why held?", caseId: "5100001976-2026", processType: "finance_ops" },
      "tok"
    )

    expect(result).toEqual({ runId: "run-1", sessionId: "session-1", alreadyRunning: false })
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe("https://api.test/runs")
    expect(init.headers.Authorization).toBe("Bearer tok")
    expect(JSON.parse(init.body)).toEqual({
      prompt: "why held?",
      case_id: "5100001976-2026",
      session_id: undefined,
      process_type: "finance_ops",
    })
  })

  // A 409 is a success: it names the run already in flight, which is the run the
  // caller wants to watch. This is the reason this function cannot use apiFetch.
  it("treats a 409 as the run to attach to", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse(409, { error: "already_running", run_id: "run-0" }))
    )

    const result = await startRun({ prompt: "p", caseId: "5100001976-2026" }, "tok")

    expect(result).toEqual({ runId: "run-0", sessionId: undefined, alreadyRunning: true })
  })

  it("surfaces the server's message on a real failure", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse(400, { error: "prompt is required" }))
    )

    await expect(startRun({ prompt: " ", caseId: "x" }, "tok")).rejects.toThrow(
      "prompt is required"
    )
  })

  // The case-less conversation is keyed on `session_id` and nothing else: the server
  // 400s a run that arrives with neither field, so this pass-through is the whole
  // mechanism. The case-bearing tests above cannot catch a break in it, because they
  // assert `session_id: undefined` — which passes even with the key absent.
  it("sends a case-less run's session id as session_id", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse(202, { run_id: "run-2", session_id: "browser-session-1" }))
    vi.stubGlobal("fetch", fetchMock)

    const result = await startRun({ prompt: "hello", sessionId: "browser-session-1" }, "tok")

    expect(result).toEqual({
      runId: "run-2",
      sessionId: "browser-session-1",
      alreadyRunning: false,
    })
    const [, init] = fetchMock.mock.calls[0]
    expect(JSON.parse(init.body)).toEqual({
      prompt: "hello",
      case_id: undefined,
      session_id: "browser-session-1",
      process_type: undefined,
    })
  })
})

describe("stopRun", () => {
  it("posts the target to the stop route", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(202, { stopped: true }))
    vi.stubGlobal("fetch", fetchMock)

    await stopRun({ caseId: "5100001976-2026" }, "tok")

    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe("https://api.test/runs/stop")
    expect(JSON.parse(init.body)).toEqual({
      case_id: "5100001976-2026",
      session_id: undefined,
    })
  })

  // Same gap as the start route: a case-less stop is targeted by session id alone.
  it("sends a case-less stop's session id as session_id", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(202, { stopped: true }))
    vi.stubGlobal("fetch", fetchMock)

    await stopRun({ sessionId: "browser-session-1" }, "tok")

    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe("https://api.test/runs/stop")
    expect(JSON.parse(init.body)).toEqual({
      case_id: undefined,
      session_id: "browser-session-1",
    })
  })

  it("reports a stop the server refused", async () => {
    // The route answers 202 either way — it released the claim, and whether the Runtime
    // session actually ended is a separate fact it reports in `stopped`. Discarding the body
    // makes a refusal indistinguishable from a stop, and the operator watches the case go
    // busy again with nothing said.
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(202, { stopped: false })))

    expect(await stopRun({ caseId: "5100001976-2026" }, "tok")).toBe(false)
  })

  it("reads a body with no `stopped` field as a stop", async () => {
    // A deployment whose Lambda predates the flag answers `{}`, and reading that as a
    // refusal would post a warning after every successful stop.
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(202, {})))

    expect(await stopRun({ caseId: "5100001976-2026" }, "tok")).toBe(true)
  })

  it("throws parsed error body on non-ok", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse(400, { error: "case_id or session_id is required" }))
    )

    await expect(stopRun({}, "tok")).rejects.toThrow("case_id or session_id is required")
  })
})
