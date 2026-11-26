// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, vi, afterEach } from "vitest"
import { screen, cleanup, fireEvent, waitFor, within } from "@testing-library/react"
import { renderRoutes, signInAs, serveMockApi } from "@/test/mockApi"

vi.mock("@/lib/config", async () => {
  const { MOCK_API_URL } = await import("@/test/mockApi")
  return {
    getConfig: vi.fn(async () => ({ apiUrl: MOCK_API_URL, features: {}, ticketingEnabled: true })),
  }
})
vi.mock("@/hooks/useApi", () => {
  const token = async () => "mock-token"
  return { useApi: () => token }
})
vi.mock("react-oidc-context", () => ({
  useAuth: () => ({ isAuthenticated: true, user: { profile: { sub: "mock-approver" } } }),
}))
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

import TicketsPage from "@/routes/TicketsPage"
import { resetMockCases } from "@/mocks/handlers"
import { CASES } from "@/mocks/fixtures"
import { toast } from "sonner"

serveMockApi()

const open = (path = "/tickets") =>
  renderRoutes(path, [{ path: "/tickets", element: <TicketsPage /> }])
const where = () => screen.getByLabelText("location").textContent

describe("Tickets", () => {
  afterEach(() => {
    cleanup()
    localStorage.clear()
    resetMockCases()
    vi.clearAllMocks()
  })

  it("opens on the open tickets, with a count on every status tab", async () => {
    signInAs("approver")
    open()
    expect(await screen.findByText("TKT-1003")).toBeTruthy()
    expect(screen.queryByText("TKT-1001")).toBeNull()
    expect(screen.getByRole("tab", { name: /Open\s*1/ })).toBeTruthy()
    expect(screen.getByRole("tab", { name: /Assigned\s*2/ })).toBeTruthy()
    expect(screen.getByRole("tab", { name: /All\s*3/ })).toBeTruthy()
  })

  it("filters by a case, as a chip that removes the filter", async () => {
    signInAs("approver")
    const c = CASES.find(x => x.document_number === "5100001976")!
    open(`/tickets?status=all&case_id=${c.case_id}`)
    expect(await screen.findByText("TKT-1001")).toBeTruthy()
    expect(screen.queryByText("TKT-1002")).toBeNull()
    fireEvent.click(screen.getByRole("button", { name: "Remove the case filter" }))
    expect(where()).toBe("/tickets?status=all")
  })

  it("decides a ticket in the sheet, through the ticket's action", async () => {
    signInAs("approver")
    open("/tickets?status=assigned")
    fireEvent.click(await screen.findByRole("button", { name: "TKT-1001" }))
    const sheet = await screen.findByRole("dialog")
    expect(within(sheet).getByRole("link", { name: /Open the case/ })).toBeTruthy()
    const reason = await within(sheet).findByRole("textbox", { name: /Reason/ })
    fireEvent.change(reason, { target: { value: "Price agreed with the supplier." } })
    fireEvent.click(within(sheet).getByRole("button", { name: "Approve as billed" }))
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith("Approve as billed: sent", expect.anything())
    )
    // The ticket moved to Approved.
    expect(await screen.findByRole("tab", { name: /Approved\s*1/, hidden: true })).toBeTruthy()
  })

  it("shows the agent's question on an open ticket and takes one answer", async () => {
    signInAs("approver")
    open("/tickets?ticket=TKT-1003")
    const sheet = await screen.findByRole("dialog")
    expect(await within(sheet).findByText("The agent asks")).toBeTruthy()
    expect(await within(sheet).findByRole("button", { name: "Send and resume" })).toBeTruthy()
  })
})
