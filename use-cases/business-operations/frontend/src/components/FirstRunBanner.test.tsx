// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, vi, afterEach } from "vitest"
import { screen, cleanup, fireEvent, within } from "@testing-library/react"
import { renderRoutes, signInAs, serveMockApi } from "@/test/mockApi"

vi.mock("@/lib/config", async () => {
  const { MOCK_API_URL } = await import("@/test/mockApi")
  return { getConfig: vi.fn(async () => ({ apiUrl: MOCK_API_URL, features: {} })) }
})
vi.mock("@/hooks/useApi", () => {
  const token = async () => "mock-token"
  return { useApi: () => token }
})
vi.mock("react-oidc-context", () => ({
  useAuth: () => ({ isAuthenticated: true, user: { profile: {} } }),
}))

import { FirstRunBanner } from "@/components/FirstRunBanner"

serveMockApi()

const open = (counts: { total: number; started: number; done: number }) =>
  renderRoutes("/", [{ path: "/", element: <FirstRunBanner {...counts} /> }])

describe("The first-run banner", () => {
  afterEach(() => {
    cleanup()
    localStorage.clear()
  })

  it("reads each step from what is true now, and points at the next one", async () => {
    signInAs("operator")
    open({ total: 0, started: 0, done: 0 })
    const banner = await screen.findByRole("region", { name: "First case" })
    const steps = within(banner).getAllByRole("listitem")
    expect(steps[0]).toHaveTextContent("Connect SAP(done)")
    expect(steps[1]).toHaveAttribute("aria-current", "step")
    expect(steps[1]).toHaveTextContent("Seed an exception")
  })

  it("moves on once a case exists and has started", async () => {
    signInAs("operator")
    open({ total: 3, started: 1, done: 0 })
    const banner = await screen.findByRole("region", { name: "First case" })
    const current = within(banner)
      .getAllByRole("listitem")
      .find(li => li.getAttribute("aria-current") === "step")
    expect(current).toHaveTextContent("Decide on it")
    expect(within(current!).getByRole("link", { name: "Decide on it" })).toHaveAttribute(
      "href",
      "/cases?view=needs-you"
    )
  })

  it("goes once a case is done, and when dismissed", async () => {
    signInAs("operator")
    open({ total: 3, started: 3, done: 1 })
    await new Promise(r => setTimeout(r, 50))
    expect(screen.queryByRole("region", { name: "First case" })).toBeNull()
    cleanup()
    open({ total: 0, started: 0, done: 0 })
    fireEvent.click(await screen.findByRole("button", { name: "Dismiss the first case steps" }))
    expect(screen.queryByRole("region", { name: "First case" })).toBeNull()
  })
})
