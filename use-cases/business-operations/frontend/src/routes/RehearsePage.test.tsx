// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, vi, afterEach } from "vitest"
import { screen, cleanup, fireEvent, waitFor, within } from "@testing-library/react"
import { renderRoutes, signInAs, serveMockApi } from "@/test/mockApi"
import { clearAllTranscripts, setTranscript } from "@/hooks/transcript"

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

/**
 * The shell's chat, standing in for a run: `rehearse` appends the prompt, then the agent's
 * turn arrives on the transcript as the run channel would write it.
 */
const rehearse = vi.fn(async (_pack: string, _pt: string, prompt: string) => {
  const now = new Date().toISOString()
  setTranscript("general", prev => [...prev, { role: "user", content: prompt, timestamp: now }])
  setTranscript("general", prev => [
    ...prev,
    {
      role: "assistant",
      content: "",
      timestamp: now,
      segments: [
        {
          type: "tool",
          toolCall: {
            toolUseId: "t1",
            name: "sap-s4___odata_read",
            input: JSON.stringify({ entity_set: "A_SupplierInvoice" }),
            status: "complete",
          },
        },
        { type: "text", content: "Within both price tolerances, so the SOP releases the invoice." },
      ],
    },
  ])
})
vi.mock("@/hooks/useAgentChat", () => ({
  useAssistant: () => ({ ready: true, slotKey: "general", rehearse, isRunning: () => false }),
}))

import RehearsePage from "@/routes/RehearsePage"
import { toast } from "sonner"

serveMockApi()

const open = (path: string) =>
  renderRoutes(path, [
    { path: "/packs/:packId/rehearse", element: <RehearsePage /> },
    { path: "/packs/:packId", element: <div>pack-page</div> },
  ])

describe("Rehearse", () => {
  afterEach(() => {
    cleanup()
    localStorage.clear()
    clearAllTranscripts()
    rehearse.mockClear()
  })

  it("says what a rehearsal withholds, always", async () => {
    signInAs("analyst")
    open("/packs/finance_ops/rehearse")
    expect(
      await screen.findByText(/tools that change SAP, a case or a ticket are withheld/)
    ).toBeInTheDocument()
  })

  it("starts from an example case, runs, and shows the steps on the page", async () => {
    signInAs("analyst")
    open("/packs/finance_ops/rehearse?sop=price_variance")
    const from = await screen.findByLabelText(/Start from an example case/)
    await waitFor(() => expect(within(from).getAllByRole("option").length).toBeGreaterThan(1))
    fireEvent.change(from, { target: { value: "ap_price_variance_within_tolerance" } })
    expect((screen.getByLabelText("The case") as HTMLTextAreaElement).value).toMatch(
      /PO amount: 50000/
    )
    fireEvent.click(screen.getByRole("button", { name: "Rehearse" }))

    await waitFor(() =>
      expect(rehearse).toHaveBeenCalledWith("finance_ops", "price_variance", expect.any(String))
    )
    const steps = await screen.findByRole("log", { name: "Steps" })
    expect(within(steps).getByText(/A_SupplierInvoice/)).toBeInTheDocument()
    expect(screen.getByText(/the SOP releases the invoice/)).toBeInTheDocument()
  })

  it("goes back to the form with the API's words when the start is refused", async () => {
    const words = "An emergency stop stands. Nothing new starts until an administrator clears it."
    rehearse.mockImplementationOnce((async () => words) as never)
    signInAs("analyst")
    open("/packs/finance_ops/rehearse?sop=price_variance")
    fireEvent.change(await screen.findByLabelText("The case"), { target: { value: "A case" } })
    fireEvent.click(screen.getByRole("button", { name: "Rehearse" }))
    expect(await screen.findByText(words)).toHaveAttribute("role", "alert")
    expect(screen.getByLabelText("The case")).toBeInTheDocument()
  })

  it("saves a rehearsal started from an example case, keeping its seed", async () => {
    signInAs("analyst")
    open("/packs/finance_ops/rehearse?example=boundary_line_amount_tolerance_usd_at")
    await waitFor(() =>
      expect((screen.getByLabelText("The case") as HTMLTextAreaElement).value).not.toBe("")
    )
    fireEvent.click(screen.getByRole("button", { name: "Rehearse" }))
    fireEvent.click(await screen.findByRole("button", { name: "Save as example case" }))
    const dialog = await screen.findByRole("dialog")
    fireEvent.change(within(dialog).getByLabelText("Why"), {
      target: { value: "Within 2.2 and 2.3." },
    })
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }))
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith("Saved as an example case", expect.anything())
    )
  })

  it("will not save a described case, which has no seed inputs, and says where seeded cases come from", async () => {
    signInAs("analyst")
    open("/packs/finance_ops/rehearse?sop=price_variance")
    fireEvent.change(await screen.findByLabelText("The case"), {
      target: { value: "Invoice 10 units at 5,010.00 against 5,000.00. Goods receipt posted." },
    })
    fireEvent.click(screen.getByRole("button", { name: "Rehearse" }))
    const save = await screen.findByRole("button", { name: "Save as example case" })
    expect(save).toHaveAttribute("aria-disabled", "true")
    fireEvent.click(save)
    expect(screen.queryByRole("dialog")).toBeNull()
    fireEvent.pointerEnter(save)
    fireEvent.mouseEnter(save)
    expect(await screen.findByText(/create one on the test data page/)).toBeInTheDocument()
  })

  it("keeps Rehearse disabled, with the reason, for someone who cannot rehearse", async () => {
    signInAs("approver")
    open("/packs/finance_ops/rehearse")
    fireEvent.change(await screen.findByLabelText("The case"), { target: { value: "Something" } })
    const button = screen.getByRole("button", { name: "Rehearse" })
    expect(button).toHaveAttribute("aria-disabled", "true")
    fireEvent.click(button)
    expect(rehearse).not.toHaveBeenCalled()
  })
})
