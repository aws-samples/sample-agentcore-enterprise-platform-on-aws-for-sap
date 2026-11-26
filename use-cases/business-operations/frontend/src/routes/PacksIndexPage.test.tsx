// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, vi, afterEach } from "vitest"
import { screen, cleanup, fireEvent, waitFor, within } from "@testing-library/react"
import { MOCK_API_URL, renderRoutes, signInAs, serveMockApi } from "@/test/mockApi"

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
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

import PacksIndexPage from "@/routes/PacksIndexPage"
import { AppSidebar } from "@/components/layout/AppSidebar"
import { SidebarProvider } from "@/components/ui/sidebar"

serveMockApi()

const page = () =>
  renderRoutes("/packs", [
    {
      path: "/packs",
      element: (
        <>
          <SidebarProvider>
            <AppSidebar />
            <PacksIndexPage />
          </SidebarProvider>
        </>
      ),
    },
    { path: "/packs/:packId", element: <div>pack-page</div> },
  ])

describe("Packs", () => {
  afterEach(() => {
    cleanup()
    localStorage.clear()
  })

  it("lists each pack you can see with its version, SOPs, waiting cases and your access", async () => {
    expect(MOCK_API_URL).toContain("/__mock_api")
    signInAs("analyst")
    page()
    const row = await screen.findByRole("row", { name: /Finance — Accounts Payable/ })
    expect(await within(row).findByText("Live v14")).toBeInTheDocument()
    // Eight SOP files: triage, six exception types and one shared; aliases are not SOPs.
    expect(within(row).getByText("8")).toBeInTheDocument()
    expect(within(row).getByText("Edit")).toBeInTheDocument()
  })

  it("stars a pack, which puts it in the sidebar at once and keeps it there", async () => {
    signInAs("approver")
    page()
    const star = await screen.findByRole("button", { name: "Star Finance — Accounts Payable" })
    // An approver can edit no part, so nothing is in the sidebar until they star it.
    expect(screen.queryByRole("link", { name: /^Finance — Accounts Payable/ })).toBeNull()
    fireEvent.click(star)
    expect(
      screen.getByRole("button", { name: "Unstar Finance — Accounts Payable" })
    ).toHaveAttribute("aria-pressed", "true")
    expect(screen.getByRole("link", { name: /^Finance — Accounts Payable/ })).toBeInTheDocument()
    expect(JSON.parse(localStorage.getItem("ui.packs.starred")!)).toEqual(["finance_ops"])
  })

  it("opens a pack from its row", async () => {
    signInAs("operator")
    page()
    fireEvent.click(await screen.findByRole("row", { name: /Finance — Accounts Payable/ }))
    expect(screen.getByLabelText("location").textContent).toBe("/packs/finance_ops")
  })

  it("keeps New pack visible but disabled, with the reason, for someone who cannot create one", async () => {
    signInAs("approver")
    page()
    await screen.findByText("Finance — Accounts Payable")
    const button = screen.getByRole("button", { name: "New pack" })
    expect(button).toHaveAttribute("aria-disabled", "true")
    fireEvent.click(button)
    expect(screen.queryByRole("dialog")).toBeNull()
  })

  it("enables New pack from the API's may_create_pack", async () => {
    signInAs("analyst")
    page()
    await screen.findByRole("button", { name: "New pack" })
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "New pack" })).not.toHaveAttribute("aria-disabled")
    )
  })

  it("says a promotion-only stack takes packs by promotion and refuses New pack", async () => {
    signInAs("analyst")
    localStorage.setItem("mock.stack", "quality")
    page()
    expect(await screen.findByText("This stack takes packs by promotion only")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "New pack" })).toHaveAttribute(
      "aria-disabled",
      "true"
    )
  })

  it("shows nothing but an explanation to someone no pack admits", async () => {
    signInAs("none")
    page()
    expect(await screen.findByText("No packs you can see")).toBeInTheDocument()
  })
})
