// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, afterEach, vi } from "vitest"
import { render, screen, cleanup, fireEvent, waitFor } from "@testing-library/react"
import { MemoryRouter, useLocation } from "react-router"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"

// Routing and the shell's assistant rules, not page internals: pages and the sidebar are
// stubbed, and the chat is a fixed object.
vi.mock("@/components/layout/AppSidebar", () => ({ AppSidebar: () => <nav>sidebar-stub</nav> }))
vi.mock("@/components/layout/CommandMenu", () => ({ CommandMenu: () => null }))
vi.mock("react-oidc-context", () => ({ useAuth: () => ({ isAuthenticated: false }) }))
vi.mock("@/hooks/useRegistry", () => ({ useRegistry: () => ({ packs: [], byId: {}, scopes: [] }) }))
const send = vi.fn(async () => {})
const chatState: { rehearsal: { packId: string; processType: string } | null } = { rehearsal: null }
vi.mock("@/hooks/useAgentChat", () => ({
  useAgentChat: () => ({
    ...chatState,
    caseId: null,
    slotKey: "general",
    input: "",
    setInput: () => {},
    isLoading: false,
    contextCaseCount: 0,
    error: null,
    send,
    stop: async () => {},
    clear: () => {},
  }),
}))
vi.mock("@/routes/CasesPage", () => ({ default: () => <div>cases-stub</div> }))
vi.mock("@/routes/CasePage", () => ({ default: () => <div>case-stub</div> }))
vi.mock("@/routes/TicketsPage", () => ({ default: () => <div>tickets-stub</div> }))
vi.mock("@/routes/PacksIndexPage", () => ({ default: () => <div>packs-stub</div> }))
vi.mock("@/routes/PackPage", () => ({ default: () => <div>pack-stub</div> }))
vi.mock("@/routes/DeploymentSettingsPage", () => ({ default: () => <div>deployment-stub</div> }))
vi.mock("@/hooks/useDemoEnabled", () => ({
  useDemoFeatures: vi.fn(() => ({ ticketing: false })),
}))

import AppRoutes from "@/routes"
import { TooltipProvider } from "@/components/ui/tooltip"
import { useDemoFeatures } from "@/hooks/useDemoEnabled"

function Where() {
  const { pathname, search } = useLocation()
  return <output aria-label="location">{pathname + search}</output>
}

function renderAt(path: string) {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter initialEntries={[path]}>
        <TooltipProvider>
          <AppRoutes />
          <Where />
        </TooltipProvider>
      </MemoryRouter>
    </QueryClientProvider>
  )
}

describe("AppRoutes", () => {
  afterEach(() => {
    cleanup()
    localStorage.clear()
    send.mockClear()
    vi.mocked(useDemoFeatures).mockReturnValue({ ticketing: false })
  })

  it("lands on Cases: there is no Home page (decision 19)", async () => {
    renderAt("/")
    expect(await screen.findByText("cases-stub")).toBeTruthy()
    expect(screen.getByLabelText("location").textContent).toBe("/cases")
  })

  it("keeps /workspace links working, with the case they name", async () => {
    renderAt("/workspace?case=ap-abc")
    expect(await screen.findByText("cases-stub")).toBeTruthy()
    expect(screen.getByLabelText("location").textContent).toBe("/cases?case=ap-abc")
  })

  it("renders packs, a pack and deployment settings", async () => {
    renderAt("/packs")
    expect(await screen.findByText("packs-stub")).toBeTruthy()
    cleanup()
    renderAt("/packs/finance_ops")
    expect(await screen.findByText("pack-stub")).toBeTruthy()
    cleanup()
    renderAt("/deployment")
    expect(await screen.findByText("deployment-stub")).toBeTruthy()
  })

  it("gates /tickets on sample ticketing", async () => {
    renderAt("/tickets")
    expect(await screen.findByText("cases-stub")).toBeTruthy()
    cleanup()
    vi.mocked(useDemoFeatures).mockReturnValue({ ticketing: true })
    renderAt("/tickets")
    expect(await screen.findByText("tickets-stub")).toBeTruthy()
  })

  it("wraps every route in the sidebar and the header", async () => {
    renderAt("/deployment")
    expect(await screen.findByText("sidebar-stub")).toBeTruthy()
    expect(screen.getByRole("button", { name: /Search cases, packs, SOPs/ })).toBeTruthy()
  })
})

describe("the assistant in the shell", () => {
  afterEach(() => {
    cleanup()
    localStorage.clear()
    send.mockClear()
  })

  it("starts closed, with the ask bar on the page and no panel", async () => {
    renderAt("/cases")
    await screen.findByText("cases-stub")
    expect(screen.getByRole("form", { name: "Ask the assistant" })).toBeTruthy()
    expect(screen.queryByRole("complementary", { name: "Assistant" })).toBeNull()
  })

  it("sends nothing and opens nothing from an empty bar", async () => {
    renderAt("/cases")
    await screen.findByText("cases-stub")
    fireEvent.submit(screen.getByRole("form", { name: "Ask the assistant" }))
    // An empty bar with no conversation sends nothing and opens nothing.
    expect(send).not.toHaveBeenCalled()
    expect(screen.queryByRole("complementary", { name: "Assistant" })).toBeNull()
  })

  it("does not open for a rehearsal, whose steps show on the Rehearse page (decision 20)", async () => {
    chatState.rehearsal = { packId: "finance_ops", processType: "price_variance" }
    renderAt("/cases")
    await screen.findByText("cases-stub")
    expect(screen.queryByRole("complementary", { name: "Assistant" })).toBeNull()
    chatState.rehearsal = null
  })

  it("does not open by itself on navigation", async () => {
    renderAt("/packs")
    await screen.findByText("packs-stub")
    expect(screen.queryByRole("complementary", { name: "Assistant" })).toBeNull()
  })

  it("closes with ⌘J and reopens only when there is a conversation", async () => {
    localStorage.setItem("ui.assistant.open", "true")
    renderAt("/cases")
    expect(await screen.findByRole("complementary", { name: "Assistant" })).toBeTruthy()
    fireEvent.keyDown(window, { key: "j", metaKey: true })
    await waitFor(() =>
      expect(screen.queryByRole("complementary", { name: "Assistant" })).toBeNull()
    )
    // No transcript in this slot: ⌘J puts the cursor in the bar instead of opening an empty panel.
    fireEvent.keyDown(window, { key: "j", metaKey: true })
    expect(screen.queryByRole("complementary", { name: "Assistant" })).toBeNull()
    expect(document.activeElement).toBe(screen.getByRole("textbox", { name: "Ask the assistant" }))
  })
})
