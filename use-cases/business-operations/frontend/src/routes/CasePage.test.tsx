// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, vi, afterEach } from "vitest"
import { render, screen, cleanup, fireEvent, waitFor } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { MemoryRouter, Route, Routes } from "react-router"
import { CaseStatus, type WorkItem } from "@/types/cases"
import { AP_PACK } from "@/test/packs"

vi.mock("@/services/casesService", async importOriginal => ({
  ...(await importOriginal<typeof import("@/services/casesService")>()),
  fetchCases: vi.fn(async () => []),
  fetchCaseCounts: vi.fn(async () => ({})),
  fetchCase: vi.fn(),
}))
vi.mock("@/hooks/useApi", () => ({ useApi: () => async () => "id" }))
vi.mock("react-oidc-context", () => ({
  useAuth: () => ({ isAuthenticated: true, user: { id_token: "id", profile: { sub: "me" } } }),
}))
const registryPacks: { byId: Record<string, typeof AP_PACK> } = { byId: { finance_ops: AP_PACK } }
vi.mock("@/hooks/useRegistry", () => ({
  useRegistry: () => ({
    packs: Object.values(registryPacks.byId),
    byId: registryPacks.byId,
    scopes: Object.values(registryPacks.byId),
  }),
}))
const ACCESS = {
  see: true,
  edit: {
    intake: false,
    procedures_text: false,
    rule_values: false,
    procedures_structure: false,
    controls: false,
    agent_configuration: false,
    evaluations: false,
  },
  lower_limits: false,
  set_trigger_mode: false,
  rehearse: false,
}
const chat = {
  ready: true,
  isLoading: false,
  isRunning: () => false,
  processCase: vi.fn(async () => {}),
  stop: vi.fn(async () => {}),
}
vi.mock("@/hooks/useAgentChat", () => ({ useAssistant: () => chat }))

import CasePage from "@/routes/CasePage"
import { fetchCase } from "@/services/casesService"
import { processReason } from "@/services/controlsService"

function makeCase(status: CaseStatus, extra: Partial<WorkItem> = {}): WorkItem {
  return {
    case_id: "ap-1",
    document_number: "5100001976",
    key: { SupplierInvoice: "5100001976", FiscalYear: "2026" },
    pack_id: "finance_ops",
    process_type: "invoice_matching",
    status,
    created_at: "2026-11-19T10:00:00Z",
    updated_at: "2026-11-19T10:00:00Z",
    attributes: { amount: 52500, currency: "USD" },
    ...extra,
  }
}

function renderCase(c: WorkItem) {
  vi.mocked(fetchCase).mockResolvedValue(c)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/cases/ap-1?view=needs-you"]}>
        <Routes>
          <Route path="/cases/:caseId" element={<CasePage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  )
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe("CasePage", () => {
  it("names the case by its document number, with its status", async () => {
    // An approval is sent too: every park stamps `inquiry_sent_at`.
    renderCase(
      makeCase(CaseStatus.AwaitingHumanInput, {
        exception_type: "price_variance",
        inquiry_sent_at: "2026-11-19T10:00:00Z",
        decision_options: {
          set_id: "s",
          recorded_at: "2026-11-19T10:00:00Z",
          options: [{ id: "approve", label: "Approve as billed", effect: "Releases the block." }],
        },
      })
    )
    const title = await screen.findByRole("heading", { level: 1 })
    expect(title.textContent).toContain("5100001976")
    expect(title.textContent).toContain("Needs approval")
    expect(screen.getByText("Price variance")).toBeTruthy()
  })

  it("offers Process on a case the agent has not started, and starts the run on this page", async () => {
    renderCase(makeCase(CaseStatus.Detected))
    fireEvent.click(await screen.findByRole("button", { name: "Process" }))
    await waitFor(() => expect(chat.processCase).toHaveBeenCalledWith("ap-1", "invoice_matching"))
  })

  it("shows the run on the page while the agent works, with Stop", async () => {
    renderCase(makeCase(CaseStatus.Processing))
    expect(await screen.findByText("The agent is working on this case")).toBeTruthy()
    fireEvent.click(screen.getByRole("button", { name: "Stop" }))
    await waitFor(() => expect(chat.stop).toHaveBeenCalled())
  })

  it("asks for an answer when the agent sent a question and there is no ticket", async () => {
    renderCase(makeCase(CaseStatus.AwaitingHumanInput, { inquiry_sent_at: "2026-11-19T10:00:00Z" }))
    expect(await screen.findByText("The agent asks")).toBeTruthy()
  })

  it("says why a failed run stopped, and offers to run it again", async () => {
    renderCase(
      makeCase(CaseStatus.Error, {
        status_reason: "SAP refused a read of purchase order 4500000188.",
      })
    )
    expect(await screen.findByText("SAP refused a read of purchase order 4500000188.")).toBeTruthy()
    expect(screen.getByRole("button", { name: "Run again" })).toBeTruthy()
  })

  it("says why the API refused to start, in its words", async () => {
    const words = "An emergency stop stands. Nothing new starts until an administrator clears it."
    chat.processCase.mockResolvedValueOnce(words as never)
    renderCase(makeCase(CaseStatus.Detected))
    fireEvent.click(await screen.findByRole("button", { name: "Process" }))
    expect(await screen.findByRole("alert")).toHaveTextContent(words)
  })

  it("renders what the agent found as Markdown, and never its raw HTML", async () => {
    renderCase(
      makeCase(CaseStatus.Complete, {
        agent_traces: [
          {
            trace_id: "t1",
            timestamp: "2026-11-19T10:05:00Z",
            segments: [
              { type: "text", content: "The price is **within tolerance**. <b>raw</b><img src=x>" },
            ],
          } as never,
        ],
      })
    )
    const bold = await screen.findByText("within tolerance")
    expect(bold.tagName).toBe("STRONG")
    expect(screen.queryByText(/\*\*/)).toBeNull()
    const card = bold.closest("[data-slot='card']")!
    expect(card.querySelector("b, img")).toBeNull()
  })

  it("shows the latest run's conclusion once, above the tabs, not again in Activity", async () => {
    renderCase(
      makeCase(CaseStatus.Complete, {
        agent_traces: [
          {
            trace_id: "t1",
            timestamp: "2026-11-19T10:05:00Z",
            segments: [{ type: "text", content: "Released the invoice at the PO price." }],
          } as never,
        ],
      })
    )
    expect(await screen.findAllByText("Released the invoice at the PO price.")).toHaveLength(1)
    fireEvent.click(screen.getByRole("tab", { name: "Activity" }))
    await screen.findByRole("region", { name: "Latest run" })
    expect(screen.getAllByText("Released the invoice at the PO price.")).toHaveLength(1)
  })

  it("disables Process and Run again for a person outside the case workers, naming the group", async () => {
    const access = { ...ACCESS, approve: true, process: false }
    registryPacks.byId = { finance_ops: { ...AP_PACK, access } }
    try {
      renderCase(makeCase(CaseStatus.Detected))
      const button = await screen.findByRole("button", { name: "Process" })
      expect(button.getAttribute("aria-disabled")).toBe("true")
      fireEvent.click(button)
      expect(chat.processCase).not.toHaveBeenCalled()
      cleanup()
      renderCase(makeCase(CaseStatus.Error))
      const again = await screen.findByRole("button", { name: "Run again" })
      expect(again.getAttribute("aria-disabled")).toBe("true")
      expect(processReason(undefined)).toContain("business-operations-case-workers")
    } finally {
      registryPacks.byId = { finance_ops: AP_PACK }
    }
  })
})
