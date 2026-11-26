// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, vi, beforeEach } from "vitest"
import { createPack, fetchPack, keysOfPart, patchPack } from "@/services/packsService"
import { checkSop, fetchSop, saveSop } from "@/services/sopsService"
import { fetchRules } from "@/services/rulesService"
import { fetchControls, lowerLimits } from "@/services/controlsService"
import {
  fetchDeploymentTriggerMode,
  fetchPackTriggerMode,
  saveDeploymentTriggerMode,
  savePackTriggerMode,
} from "@/services/triggerModeService"
import { fetchDeploymentSettings, saveContacts } from "@/services/deploymentService"
import { fetchEvaluations, saveExampleCase } from "@/services/evaluationsService"

/**
 * The console on the API's resource tree: a pack's parts under `/packs/{pack_id}/…`, what
 * applies to every pack under `/deployment/…`, and no old path (`/config`, `/autonomy`,
 * `/sops?key=`, `/packs/publish`) anywhere.
 */
vi.mock("@/lib/config", () => ({
  getConfig: vi.fn().mockResolvedValue({ apiUrl: "https://api.test" }),
}))

let fetchMock: ReturnType<typeof vi.fn>
beforeEach(() => {
  fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({}) })
  vi.stubGlobal("fetch", fetchMock)
})

const call = () => {
  const [url, init] = fetchMock.mock.calls[0]
  return { url, method: init.method ?? "GET", body: init.body ? JSON.parse(init.body) : undefined }
}

describe("the pack resource tree", () => {
  it.each([
    ["pack document", () => fetchPack("p_1", "t"), "GET", "/packs/p_1"],
    [
      "a SOP",
      () => fetchSop("p_1", "price_variance", "t"),
      "GET",
      "/packs/p_1/sops/price_variance",
    ],
    ["rules", () => fetchRules("p_1", "t"), "GET", "/packs/p_1/rules"],
    ["controls", () => fetchControls("p_1", "t"), "GET", "/packs/p_1/controls"],
    ["pack trigger mode", () => fetchPackTriggerMode("p_1", "t"), "GET", "/packs/p_1/trigger-mode"],
    ["evaluations", () => fetchEvaluations("p_1", "t"), "GET", "/packs/p_1/evaluations"],
    ["deployment trigger mode", () => fetchDeploymentTriggerMode("t"), "GET", "/deployment/trigger-mode"],
    ["deployment settings", () => fetchDeploymentSettings("t"), "GET", "/deployment/settings"],
  ])("reads %s", async (_, run, method, path) => {
    await run()
    expect(call()).toMatchObject({ url: `https://api.test${path}`, method })
  })

  it("creates with POST /packs", async () => {
    await createPack("p_1", { display_name: "P" } as never, "t")
    expect(call()).toMatchObject({ url: "https://api.test/packs", method: "POST" })
    expect(call().body).toMatchObject({ pack_id: "p_1", create: { display_name: "P" } })
  })

  it("edits a pack with PATCH, carrying the version it read", async () => {
    await patchPack("p_1", { pack: { constants: { A_PCT: 3 } } }, "t", 14)
    expect(call()).toMatchObject({ url: "https://api.test/packs/p_1", method: "PATCH" })
    expect(call().body).toEqual({ pack: { constants: { A_PCT: 3 } }, expected_version: 14 })
  })

  it("saves a SOP by its process type, and checks one without writing", async () => {
    await saveSop("p_1", "price_variance", "text", "t", 3)
    expect(call()).toMatchObject({
      url: "https://api.test/packs/p_1/sops/price_variance",
      method: "PUT",
    })
    expect(call().body).toEqual({ content: "text", expected_version: 3 })
    fetchMock.mockClear()
    await checkSop("p_1", "price_variance", "text", "t")
    expect(call()).toMatchObject({
      url: "https://api.test/packs/p_1/sops/price_variance/check",
      method: "POST",
    })
  })

  it("lowers limits on the pack's controls, and sends null to undo", async () => {
    await lowerLimits("p_1", { A_LIMIT_USD: null }, "t")
    expect(call()).toMatchObject({ url: "https://api.test/packs/p_1/controls", method: "PUT" })
    expect(call().body).toEqual({ limits: { A_LIMIT_USD: null } })
  })

  it("sets each deployment trigger mode on its own path", async () => {
    await savePackTriggerMode("p_1", "auto", "t")
    expect(call()).toMatchObject({ url: "https://api.test/packs/p_1/trigger-mode", method: "PUT" })
    expect(call().body).toEqual({ "trigger-mode": "auto" })
    fetchMock.mockClear()
    await saveDeploymentTriggerMode("manual", "t")
    expect(call()).toMatchObject({ url: "https://api.test/deployment/trigger-mode", method: "PUT" })
  })

  it("saves contacts on the deployment, and an example case on the pack", async () => {
    await saveContacts({ ap_team: null }, "t")
    expect(call()).toMatchObject({ url: "https://api.test/deployment/settings", method: "PUT" })
    fetchMock.mockClear()
    await saveExampleCase(
      "p_1",
      "my_case",
      { description: "d", process_type: "x", expected: { outcome: "auto_release", reason: "r" } },
      "t",
      14
    )
    expect(call()).toMatchObject({
      url: "https://api.test/packs/p_1/evaluations/my_case",
      method: "PUT",
    })
    expect(call().body.expected_version).toBe(14)
  })

  it("names every field the API refused", async () => {
    fetchMock.mockResolvedValue({
      ok: false,
      status: 400,
      json: async () => ({ error: "Invalid limits", details: ["A may be lowered, never raised"] }),
    })
    await expect(lowerLimits("p_1", { A: 9 }, "t")).rejects.toThrow(
      "A may be lowered, never raised"
    )
  })

  it("groups a document's keys by part", () => {
    const doc = {
      pack_id: "p",
      version: 1,
      pack: { $schema: "x", source: {}, constants: {}, model_tier: "haiku" },
      parts: {
        source: "intake",
        constants: "procedures",
        model_tier: "agent_configuration",
      } as const,
    }
    expect(keysOfPart(doc, "intake")).toEqual(["source"])
    expect(keysOfPart(doc, "controls")).toEqual([])
  })
})
