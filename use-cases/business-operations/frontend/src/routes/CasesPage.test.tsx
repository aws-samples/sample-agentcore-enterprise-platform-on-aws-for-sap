// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, vi, afterEach } from "vitest"
import { screen, cleanup, fireEvent, waitFor, within } from "@testing-library/react"
import { renderRoutes, signInAs, serveMockApi } from "@/test/mockApi"

vi.mock("@/lib/config", async () => {
  const { MOCK_API_URL } = await import("@/test/mockApi")
  return { getConfig: vi.fn(async () => ({ apiUrl: MOCK_API_URL, features: {} })) }
})
vi.mock("@/hooks/useApi", () => {
  const token = async () => "mock-token"
  return { useApi: () => token, useApiToken: () => token }
})
const profile = { sub: "mock-operator", email: "procurement@example.com" }
vi.mock("react-oidc-context", () => ({
  useAuth: () => ({ isAuthenticated: true, user: { profile } }),
}))
vi.mock("@/hooks/useDemoEnabled", () => ({ useDemoFeatures: () => ({ ticketing: false }) }))
// The registry as mock mode serves it, plus a second pack when a test asks for one.
const extra = { packs: [] as Record<string, unknown>[] }
vi.mock("@/hooks/useRegistry", async importOriginal => {
  const real = await importOriginal<typeof import("@/hooks/useRegistry")>()
  return {
    ...real,
    useRegistry: () => {
      const r = real.useRegistry()
      if (!extra.packs.length) return r
      const packs = [...r.packs, ...extra.packs] as typeof r.packs
      return { ...r, packs, scopes: packs, byId: Object.fromEntries(packs.map(p => [p.id, p])) }
    },
  }
})
const narrate = vi.fn(async () => {})
const openAssistant = vi.fn()
vi.mock("@/hooks/useAgentChat", () => ({
  useShell: () => ({ chat: { ready: true, narrate }, openAssistant, ask: vi.fn() }),
}))
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

import CasesPage from "@/routes/CasesPage"
import { resetMockCases } from "@/mocks/handlers"
import { toast } from "sonner"

serveMockApi()

const open = (path = "/cases") =>
  renderRoutes(path, [
    { path: "/cases", element: <CasesPage /> },
    { path: "/cases/:caseId", element: <div>case-page</div> },
  ])
const where = () => screen.getByLabelText("location").textContent

/** Base UI's checkbox toggles through its hidden input, which is what a click reaches. */
function check(name: string) {
  fireEvent.click(screen.getByRole("checkbox", { name }).parentElement!.querySelector("input")!)
}

describe("Cases", () => {
  afterEach(() => {
    cleanup()
    localStorage.clear()
    resetMockCases()
    extra.packs = []
    profile.sub = "mock-operator"
    vi.clearAllMocks()
  })

  it("opens on Needs you, with the server's count on every view", async () => {
    signInAs("operator")
    open()
    expect(await screen.findByText("5100001976")).toBeTruthy()
    expect(screen.queryByText("5100002031")).toBeNull()
    expect(await screen.findByRole("tab", { name: /Needs you\s*4/ })).toBeTruthy()
    expect(screen.getByRole("tab", { name: /Not started\s*5/ })).toBeTruthy()
    expect(screen.getByRole("tab", { name: /All\s*11/ })).toBeTruthy()
  })

  it("shows everything waiting on a person beside what the caller may decide", async () => {
    // A case worker may not approve in the pack: nothing needs them, the team view has it all.
    signInAs("worker")
    open()
    expect(await screen.findByRole("tab", { name: /Waiting on a person\s*4/ })).toBeTruthy()
    expect(screen.getByRole("tab", { name: /Needs you\s*0/ })).toBeTruthy()
    expect(await screen.findByText("Nothing needs you")).toBeTruthy()
    fireEvent.click(screen.getByRole("tab", { name: /Waiting on a person/ }))
    expect(await screen.findByText("5100001976")).toBeTruthy()
    expect(where()).toBe("/cases?view=team")
    expect(screen.getByRole("columnheader", { name: "Waiting for" })).toBeTruthy()
  })

  it("leaves a case out of Needs you once its second approver is someone else", async () => {
    // Priya gave the first of two approvals on 5100002011: it waits on a different approver.
    signInAs("approver")
    profile.sub = "mock-approver"
    open()
    expect(await screen.findByText("5100001976")).toBeTruthy()
    expect(await screen.findByRole("tab", { name: /Needs you\s*3/ })).toBeTruthy()
    expect(screen.queryByText("5100002011")).toBeNull()
    expect(screen.getByRole("tab", { name: /Waiting on a person\s*4/ })).toBeTruthy()
    fireEvent.click(screen.getByRole("tab", { name: /Waiting on a person/ }))
    expect(await screen.findByText("5100002011")).toBeTruthy()
  })

  it("keeps such a case in Needs you for an approver who has not approved it", async () => {
    signInAs("operator")
    open()
    expect(await screen.findByText("5100002011")).toBeTruthy()
    expect(await screen.findByRole("tab", { name: /Needs you\s*4/ })).toBeTruthy()
  })

  it("sorts by amount from the Amount column, largest first and then smallest", async () => {
    signInAs("operator")
    open()
    await screen.findByText("5100001976")
    const header = screen.getByRole("columnheader", { name: /Amount/ })
    expect(header).not.toHaveAttribute("aria-sort")
    fireEvent.click(within(header).getByRole("button", { name: "Amount" }))
    await waitFor(() => expect(where()).toBe("/cases?sort=amount"))
    await waitFor(() =>
      expect(within(screen.getAllByRole("row")[1]).queryByText("5100002011")).toBeTruthy()
    )
    expect(screen.getByRole("columnheader", { name: /Amount/ })).toHaveAttribute(
      "aria-sort",
      "descending"
    )
    fireEvent.click(screen.getByRole("button", { name: "Amount" }))
    await waitFor(() => expect(where()).toBe("/cases?sort=amount-asc"))
    await waitFor(() =>
      expect(within(screen.getAllByRole("row")[1]).queryByText("5100001994")).toBeTruthy()
    )
  })

  it("says whether a case needs an approval or a reply", async () => {
    signInAs("operator")
    open()
    const approval = (await screen.findByText("5100001976")).closest("tr")!
    expect(within(approval).getByText("Needs approval")).toBeTruthy()
    const reply = screen.getByText("5100001981").closest("tr")!
    expect(within(reply).getByText("Needs a reply")).toBeTruthy()
  })

  it("shows the fiscal year only when it is not this year", async () => {
    signInAs("operator")
    open("/cases?view=not-started")
    const old = (await screen.findByText("5100001859")).closest("td")!
    expect(old.textContent).toContain("2025")
  })

  it("offers checkboxes only where there is a bulk action, and starts runs there", async () => {
    signInAs("operator")
    open()
    await screen.findByText("5100001976")
    expect(screen.queryByRole("checkbox")).toBeNull()
    cleanup()
    open("/cases?view=not-started")
    await screen.findByRole("checkbox", { name: "Select 5100002031" })
    check("Select 5100002031")
    check("Select 5100002032")
    fireEvent.click(screen.getByRole("button", { name: "Start 2 runs" }))
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith("Started 2 runs", expect.anything())
    )
  })

  it("asks the server for what was typed, and says so when nothing matches", async () => {
    signInAs("operator")
    open()
    await screen.findByText("5100001976")
    fireEvent.change(screen.getByLabelText("Search cases"), { target: { value: "51000019" } })
    await waitFor(() => expect(screen.queryByText("5100002011")).toBeNull())
    expect(screen.getByText("5100001976")).toBeTruthy()
    expect(await screen.findByRole("tab", { name: /Needs you\s*3/ })).toBeTruthy()
    fireEvent.change(screen.getByLabelText("Search cases"), { target: { value: "nothing" } })
    expect(await screen.findByText("Nothing matches")).toBeTruthy()
  })

  it("pages with Load more", async () => {
    signInAs("operator")
    open("/cases?view=all")
    await screen.findByText("5100001976")
    expect(screen.getAllByRole("row")).toHaveLength(1 + 8)
    fireEvent.click(screen.getByRole("button", { name: "Load more" }))
    await waitFor(() => expect(screen.getAllByRole("row")).toHaveLength(1 + 11))
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull()
  })

  it("opens a case carrying the list's state, for its stepper", async () => {
    signInAs("operator")
    open("/cases?q=5100")
    fireEvent.click(await screen.findByText("5100001976"))
    expect(await screen.findByText("case-page")).toBeTruthy()
    expect(where()).toMatch(/^\/cases\/ap-[a-z2-7]+\?q=5100&view=needs-you$/)
  })

  it("sends an old ?case= link to the case page", async () => {
    signInAs("operator")
    open("/cases?case=ap-abc")
    expect(await screen.findByText("case-page")).toBeTruthy()
  })

  it("runs the briefing into the assistant, showing the question asked", async () => {
    signInAs("operator")
    open()
    await screen.findByText("5100001976")
    fireEvent.click(screen.getByRole("button", { name: "Briefing" }))
    await waitFor(() => expect(narrate).toHaveBeenCalledTimes(1))
    expect(openAssistant).toHaveBeenCalled()
    // The person sees the question they asked, not the prompt behind it (decision 20).
    expect(narrate).toHaveBeenCalledWith(expect.any(String), "Give me a briefing")
  })

  it("filters by pack as a removable chip, and by exception type", async () => {
    signInAs("operator")
    open("/cases?pack=finance_ops&exception=price_variance")
    expect(await screen.findByText("5100001976")).toBeTruthy()
    expect(screen.getByText(/Exception: Price variance/)).toBeTruthy()
    fireEvent.click(
      screen.getByRole("button", { name: "Remove filter Finance — Accounts Payable" })
    )
    expect(where()).toBe("/cases?exception=price_variance")
  })

  it("shows a Pack column and sorts by amount only within one pack, with several", async () => {
    extra.packs = [
      {
        id: "hr_ops",
        display_name: "HR operations",
        short_id: "hr",
        fields: [],
        process_types: [],
        source: {},
      },
    ]
    signInAs("operator")
    open()
    expect(await screen.findByRole("columnheader", { name: "Pack" })).toBeTruthy()
    const amount = screen.getByRole("option", { name: /Largest amount/ }) as HTMLOptionElement
    expect(amount.disabled).toBe(true)
    expect(amount.textContent).toContain("one pack only")
  })

  it("leads Done with what closed in the window, from the summary route", async () => {
    signInAs("operator")
    open("/cases?view=done")
    expect(await screen.findByRole("combobox", { name: "Summary window" })).toHaveValue("24")
    expect(
      await screen.findByText("1 done · 4,400.00 USD released · 0.18 USD agent cost")
    ).toBeTruthy()
    fireEvent.change(screen.getByRole("combobox", { name: "Summary window" }), {
      target: { value: "168" },
    })
    expect(where()).toBe("/cases?view=done&window=168")
  })

  it("says so when a view is empty", async () => {
    signInAs("operator")
    open("/cases?view=working")
    expect(await screen.findByText("The agent is not working on anything")).toBeTruthy()
  })
})
