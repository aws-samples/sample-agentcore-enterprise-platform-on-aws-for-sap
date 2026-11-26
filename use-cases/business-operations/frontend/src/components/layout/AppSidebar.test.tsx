// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, vi, afterEach } from "vitest"
import { screen, cleanup } from "@testing-library/react"
import { renderRoutes, signInAs, serveMockApi } from "@/test/mockApi"

vi.mock("@/lib/config", async () => {
  const { MOCK_API_URL } = await import("@/test/mockApi")
  return { getConfig: vi.fn(async () => ({ apiUrl: MOCK_API_URL })) }
})
vi.mock("@/hooks/useApi", () => {
  const token = async () => "mock-token"
  return { useApi: () => token }
})
const profile: { sub?: string } = {}
vi.mock("react-oidc-context", () => ({
  useAuth: () => ({ isAuthenticated: true, user: { profile } }),
}))

import { AppSidebar } from "@/components/layout/AppSidebar"
import { SidebarProvider } from "@/components/ui/sidebar"

serveMockApi()

const open = () =>
  renderRoutes("/cases", [
    {
      path: "/cases",
      element: (
        <SidebarProvider>
          <AppSidebar />
        </SidebarProvider>
      ),
    },
  ])

describe("The sidebar's Cases count", () => {
  afterEach(() => {
    cleanup()
    localStorage.clear()
    delete profile.sub
  })

  it("counts what Needs you holds: the cases the caller may decide", async () => {
    signInAs("operator")
    profile.sub = "mock-operator"
    open()
    expect(await screen.findByLabelText("4 need you")).toBeInTheDocument()
    cleanup()

    // Priya gave the first of two approvals on one of them, which now waits on someone else.
    signInAs("approver")
    profile.sub = "mock-approver"
    open()
    expect(await screen.findByLabelText("3 need you")).toBeInTheDocument()
    cleanup()

    // A case worker reads the pack but approves in none of it: nothing needs them.
    signInAs("worker")
    open()
    await screen.findByText(/Agent idle/)
    // Long enough for the registry and the counts to land, as they did for the approver.
    await new Promise(r => setTimeout(r, 500))
    expect(screen.queryByLabelText(/need you/)).toBeNull()
  })
})
