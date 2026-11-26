// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { afterEach, describe, expect, it } from "vitest"
import { MOCK_API_URL, serveMockApi, signInAs } from "@/test/mockApi"
import { resetMockCases } from "@/mocks/handlers"
import { CASES } from "@/mocks/fixtures"
import { CaseStatus } from "@/types/cases"

const NEW = CASES.find(c => c.status === CaseStatus.Detected)!.case_id

serveMockApi()
afterEach(() => resetMockCases())

const get = async (path: string) => (await fetch(`${MOCK_API_URL}${path}`)).json()
const post = (path: string, body: unknown) =>
  fetch(`${MOCK_API_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  })

describe("Mock mode's Process group (7.1)", () => {
  it("lets a case worker process without approving, and an analyst neither", async () => {
    signInAs("worker")
    const worker = (await get("/registry")).packs[0].access
    expect(worker).toMatchObject({ process: true, approve: false })
    signInAs("analyst")
    const analyst = (await get("/registry")).packs[0].access
    expect(analyst).toMatchObject({ process: false, approve: false })
  })

  it("refuses Process and enqueue outside the case workers, with the route's words", async () => {
    signInAs("analyst")
    const run = await post("/runs", { case_id: NEW, intent: "process", prompt: "p" })
    expect(run.status).toBe(403)
    expect((await run.json()).error).toBe(
      "Only this pack's case workers may start the agent on its cases"
    )
    const queued = await post("/cases/enqueue", { case_ids: [NEW] })
    expect(queued.status).toBe(403)
  })
})

describe("Mock mode's standing stop (7.2)", () => {
  it("refuses every start until an administrator clears it, then leaves the trigger mode manual", async () => {
    signInAs("operator")
    expect((await post("/deployment/stop", { reason: "Check releases" })).status).toBe(200)
    expect((await get("/deployment/settings")).last_stop.stands).toBe(true)
    const starts = [
      post("/runs", { case_id: NEW, intent: "process", prompt: "p" }),
      post("/runs", { prompt: "What waits on me?", session_id: "s-1" }),
      post("/runs", { prompt: "p", process_type: "price_variance", rehearse: true }),
      post("/cases/enqueue", { case_ids: [NEW] }),
    ]
    for (const res of await Promise.all(starts)) {
      expect(res.status).toBe(409)
      const body = await res.json()
      expect(body.error).toMatch(/^An emergency stop stands: Check releases \(sam@example.com/)
      expect(body.emergency_stop).toMatchObject({ stands: true, reason: "Check releases" })
    }
    expect((await get("/deployment/settings")).last_stop).not.toHaveProperty("cleared_at")
    expect((await post("/deployment/stop/clear", { reason: " " })).status).toBe(400)
    signInAs("approver")
    expect((await post("/deployment/stop/clear", { reason: "Fixed" })).status).toBe(403)
    signInAs("operator")
    const cleared = await (await post("/deployment/stop/clear", { reason: "Fixed" })).json()
    expect(cleared.stop).toMatchObject({ stands: false, clear_reason: "Fixed" })
    expect((await get("/deployment/trigger-mode"))["trigger-mode"]).toBe("manual")
    expect((await post("/runs", { case_id: NEW, intent: "process", prompt: "p" })).status).toBe(202)
    expect((await post("/deployment/stop/clear", { reason: "Again" })).status).toBe(409)
  })
})
