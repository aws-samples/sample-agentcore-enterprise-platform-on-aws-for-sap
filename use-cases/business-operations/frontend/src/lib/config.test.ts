// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest"

describe("getConfig", () => {
  beforeEach(() => {
    vi.resetModules() // clear the cached/pending singleton between tests
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  const stubExports = (body: object) =>
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => body })
    )

  it("fetches aws-exports.json and strips the API's trailing slash", async () => {
    stubExports({
      apiUrl: "https://api.test/",
      ticketingEnabled: true,
      adminsGroup: "business-operations-admins",
    })
    const { getConfig } = await import("@/lib/config")
    const cfg = await getConfig()
    expect(fetch).toHaveBeenCalledWith("/aws-exports.json")
    expect(cfg.apiUrl).toBe("https://api.test")
    expect(cfg.ticketingEnabled).toBe(true)
    expect(cfg.adminsGroup).toBe("business-operations-admins")
  })

  it("reads ticketing as off when the flag is absent", async () => {
    stubExports({ apiUrl: "https://api.test/" })
    const { getConfig } = await import("@/lib/config")
    const cfg = await getConfig()
    expect(cfg.ticketingEnabled).toBe(false)
  })

  it("caches the result — fetch is called only once across calls", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ apiUrl: "https://api.test" }),
    })
    vi.stubGlobal("fetch", fetchMock)

    const { getConfig } = await import("@/lib/config")
    await getConfig()
    await getConfig()

    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it("throws when aws-exports.json fails to load", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: false, status: 404, json: async () => ({}) })
    )

    const { getConfig } = await import("@/lib/config")
    await expect(getConfig()).rejects.toThrow(/Failed to load aws-exports.json: 404/)
  })

  it("retries after a failed load instead of caching the failure", async () => {
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({ apiUrl: "https://api.test" }),
      })
    vi.stubGlobal("fetch", fetchMock)

    const { getConfig } = await import("@/lib/config")
    await expect(getConfig()).rejects.toThrow("Failed to fetch")
    expect((await getConfig()).apiUrl).toBe("https://api.test")
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })
})
