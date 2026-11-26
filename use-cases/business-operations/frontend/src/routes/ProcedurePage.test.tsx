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
  return { useApi: () => token }
})
vi.mock("react-oidc-context", () => ({
  useAuth: () => ({ isAuthenticated: true, user: { profile: {} } }),
}))
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }))
// CodeMirror needs layout jsdom does not have; the page's flow is what is under test here.
vi.mock("@/components/packs/SopCodeEditor", () => ({
  default: ({
    value,
    onChange,
    label,
  }: {
    value: string
    onChange: (v: string) => void
    label: string
  }) => <textarea aria-label={label} value={value} onChange={e => onChange(e.target.value)} />,
}))

import ProcedurePage from "@/routes/ProcedurePage"
import { toast } from "sonner"

serveMockApi()

const open = (path = "/packs/finance_ops/procedures/price_variance") =>
  renderRoutes(path, [
    { path: "/packs/:packId/procedures/:processType", element: <ProcedurePage /> },
    { path: "/packs/:packId/instructions", element: <ProcedurePage instructions /> },
    { path: "/packs/:packId", element: <div>pack-page</div> },
    { path: "/packs/:packId/rehearse", element: <div>rehearse-page</div> },
  ])

const editor = () => screen.getByLabelText("Price variance SOP text") as HTMLTextAreaElement

describe("A procedure", () => {
  afterEach(() => {
    cleanup()
    localStorage.clear()
  })

  it("reads as a document, with each rule shown as its value", async () => {
    signInAs("analyst")
    open()
    expect(
      await screen.findByRole("heading", { level: 1, name: "Price variance" })
    ).toBeInTheDocument()
    expect(
      screen.getByText(/Exception SOP\. Used when triage finds a price variance/)
    ).toBeInTheDocument()
    expect(
      await screen.findByRole("heading", { name: "Step 2: calculate variance" })
    ).toBeInTheDocument()
    const cites = (await screen.findByText("Rules this cites")).closest(
      "[data-slot=card]"
    ) as HTMLElement
    expect(within(cites).getByText("Line amount tolerance")).toBeInTheDocument()
    expect(within(cites).getAllByText("Checked before every write").length).toBeGreaterThan(0)
    // Hands over to the SOPs it loads.
    const over = screen.getByText("Hands over to").closest("[data-slot=card]") as HTMLElement
    expect(within(over).getByRole("link", { name: "Missing goods receipt" })).toBeInTheDocument()
  })

  it("edits, checks as you type, shows the diff, and saves", async () => {
    signInAs("analyst")
    open()
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }))
    await waitFor(() => expect(editor()).toBeInTheDocument())
    expect(await screen.findByText("Found nothing")).toBeInTheDocument()

    // A rule the pack does not declare: the check names it, and Save stays off.
    fireEvent.change(editor(), { target: { value: `${editor().value}\nUses {{NOT_A_RULE}}.` } })
    expect(
      await screen.findByText(/\{\{NOT_A_RULE\}\} is not a rule or contact/)
    ).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled()

    const text = editor().value.replace(
      "\nUses {{NOT_A_RULE}}.",
      "\n2.6  The agent MUST leave tax lines out."
    )
    fireEvent.change(editor(), { target: { value: text } })
    await screen.findByText("Found nothing")
    fireEvent.click(screen.getByRole("button", { name: "Save" }))

    const dialog = await screen.findByRole("dialog")
    expect(within(dialog).getByText(/1 line added, 0 removed/)).toBeInTheDocument()
    expect(within(dialog).getByRole("region", { name: "Changes" }).textContent).toContain(
      "Added: 2.6  The agent MUST leave tax lines out."
    )
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }))
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith("Saved Price variance", expect.anything())
    )
    expect(await screen.findByRole("button", { name: "Edit" })).toBeInTheDocument()
  })

  it("holds navigation while there are unsaved changes", async () => {
    signInAs("analyst")
    open()
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }))
    await waitFor(() => expect(editor()).toBeInTheDocument())
    fireEvent.change(editor(), { target: { value: `${editor().value}\nMore.` } })
    fireEvent.click(screen.getByRole("link", { name: "All SOPs" }))
    expect(
      await screen.findByRole("alertdialog", { name: "Leave without saving?" })
    ).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "Keep editing" }))
    expect(screen.getByLabelText("location").textContent).toBe(
      "/packs/finance_ops/procedures/price_variance"
    )

    fireEvent.click(screen.getByRole("link", { name: "All SOPs" }))
    fireEvent.click(await screen.findByRole("button", { name: "Discard changes" }))
    await waitFor(() =>
      expect(screen.getByLabelText("location").textContent).toBe(
        "/packs/finance_ops?tab=procedures"
      )
    )
  })

  it("is read only, with the reason, for someone who cannot author", async () => {
    signInAs("approver")
    open()
    const edit = await screen.findByRole("button", { name: "Edit" })
    await waitFor(() => expect(edit).toHaveAttribute("aria-disabled", "true"))
  })

  it("opens an alias as the SOP it stands for", async () => {
    signInAs("analyst")
    open("/packs/finance_ops/procedures/three_way_match")
    expect(
      await screen.findByRole("heading", { level: 1, name: "Invoice matching" })
    ).toBeInTheDocument()
    expect(screen.getByText(/Also answers to Three way match and AP exception/)).toBeInTheDocument()
  })

  it("opens the general instructions", async () => {
    signInAs("analyst")
    open("/packs/finance_ops/instructions")
    expect(
      await screen.findByRole("heading", { level: 1, name: "General instructions" })
    ).toBeInTheDocument()
  })
})
