// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, vi, afterEach } from "vitest"
import { screen, cleanup, fireEvent, waitFor, within } from "@testing-library/react"
import { renderRoutes, signInAs, serveMockApi } from "@/test/mockApi"

vi.mock("@/lib/config", async () => {
  const { MOCK_API_URL } = await import("@/test/mockApi")
  return { getConfig: vi.fn(async () => ({ apiUrl: MOCK_API_URL })) }
})
vi.mock("@/hooks/useApi", () => {
  const token = async () => "mock-token"
  return { useApi: () => token }
})
vi.mock("react-oidc-context", () => ({
  useAuth: () => ({ isAuthenticated: true, user: { profile: {} } }),
}))
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

import PackPage from "@/routes/PackPage"
import { toast } from "sonner"

serveMockApi()

const open = (path = "/packs/finance_ops") =>
  renderRoutes(path, [
    { path: "/packs/:packId", element: <PackPage /> },
    { path: "/packs/:packId/rehearse", element: <div>rehearse-page</div> },
  ])

const selected = () => screen.getByRole("tab", { selected: true }).textContent

describe("A pack", () => {
  afterEach(() => {
    cleanup()
    localStorage.clear()
  })

  it("lands a process analyst on Procedures and an operator on Overview, then on the last tab used", async () => {
    signInAs("analyst")
    open()
    await screen.findByRole("heading", { name: /Finance — Accounts Payable/ })
    expect(selected()).toBe("Procedures")
    cleanup()

    signInAs("operator")
    open()
    await screen.findByRole("heading", { name: /Finance — Accounts Payable/ })
    expect(selected()).toBe("Overview")
    fireEvent.click(screen.getByRole("tab", { name: "Controls" }))
    cleanup()

    open()
    await screen.findByRole("heading", { name: /Finance — Accounts Payable/ })
    expect(selected()).toBe("Controls")
  })

  it("badges each part with what you may do there", async () => {
    signInAs("analyst")
    open("/packs/finance_ops?tab=procedures")
    expect(await screen.findByText("You can edit")).toBeInTheDocument()
    fireEvent.click(screen.getByRole("tab", { name: "Intake" }))
    expect(await screen.findByText("Read only")).toBeInTheDocument()
  })

  it("keeps a control you cannot use focusable, disabled, with its reason", async () => {
    signInAs("analyst")
    open("/packs/finance_ops?tab=controls")
    const edit = await screen.findByRole("button", { name: "Edit controls" })
    expect(edit).toHaveAttribute("aria-disabled", "true")
    edit.focus()
    expect(edit).toHaveFocus()
    fireEvent.mouseEnter(edit)
    fireEvent.pointerEnter(edit)
    expect(
      await screen.findByText(/Only a pack administrator changes controls/)
    ).toBeInTheDocument()
    // The content around it is readable as ever.
    expect(screen.getByText("Approve changes to SAP")).toBeInTheDocument()
  })

  it("shows this deployment's own settings on the pack, marked Set here", async () => {
    signInAs("operator")
    open("/packs/finance_ops?tab=overview")
    const card = (await screen.findByText("In this deployment")).closest(
      "[data-slot=card]"
    )! as HTMLElement
    expect(within(card).getByText("Set here")).toBeInTheDocument()
    expect(
      await within(card).findByText(/manual: each new case waits for a person/)
    ).toBeInTheDocument()
    expect(await within(card).findByText(/50,000 USD, lowered from 100,000/)).toBeInTheDocument()
  })

  it("lowers a limit for this deployment and refuses one above the pack's", async () => {
    signInAs("operator")
    open("/packs/finance_ops?tab=controls")
    fireEvent.click(await screen.findByRole("button", { name: "Lower a limit" }))
    const input = screen.getByLabelText("Two-approver limit in this deployment")
    fireEvent.change(input, { target: { value: "300000" } })
    expect(screen.getByText(/may only lower a limit/)).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled()
    fireEvent.change(input, { target: { value: "200000" } })
    fireEvent.click(screen.getByRole("button", { name: "Save" }))
    expect(await screen.findByText(/lowered from 250,000/)).toBeInTheDocument()
  })

  it("names the limit owners group to anyone who may not lower a limit", async () => {
    signInAs("analyst")
    open("/packs/finance_ops?tab=controls")
    const lower = await screen.findByRole("button", { name: "Lower a limit" })
    expect(lower).toHaveAttribute("aria-disabled", "true")
    expect(
      await screen.findByText(
        "Read only. In this deployment, members of business-operations-limit-owners may lower a limit."
      )
    ).toBeInTheDocument()
  })

  it("edits a rule's value as the pack, and says when a write check reads it", async () => {
    signInAs("analyst")
    open("/packs/finance_ops?tab=procedures")
    fireEvent.click(await screen.findByRole("button", { name: "Edit values" }))
    fireEvent.change(screen.getByLabelText("Price variance"), { target: { value: "3" } })
    expect(screen.getByText("A check before every write reads this rule")).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "Save" }))
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith("Saved 1 rule", expect.anything())
    )
    expect(await screen.findByText("3 %")).toBeInTheDocument()
  })

  it("groups the SOPs by the part they play", async () => {
    signInAs("analyst")
    open("/packs/finance_ops?tab=procedures")
    // Each group is a heading over its rows, in the order the agent meets them.
    const groups = ["Every case", "Triage", "Exception types", "Shared", "Other names"]
    for (const group of groups)
      expect(await screen.findByRole("columnheader", { name: group })).toHaveAttribute(
        "scope",
        "colgroup"
      )
    expect(screen.getByRole("link", { name: "General instructions" })).toBeInTheDocument()
  })

  it("offers Rehearse on Overview and Procedures only, and no release request", async () => {
    signInAs("operator")
    open("/packs/finance_ops?tab=overview")
    expect(await screen.findByRole("button", { name: "Rehearse" })).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Request release" })).toBeNull()
    fireEvent.click(screen.getByRole("tab", { name: "Intake" }))
    expect(screen.queryByRole("button", { name: "Rehearse" })).toBeNull()
    cleanup()

    open("/packs/finance_ops?tab=procedures")
    expect(await screen.findByRole("button", { name: "Rehearse" })).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Request release" })).toBeNull()
  })

  it("shows a failed example case in detail", async () => {
    signInAs("analyst")
    open("/packs/finance_ops?tab=evaluations")
    fireEvent.click(
      await screen.findByRole("link", { name: "Boundary line amount tolerance usd at" })
    )
    expect(await screen.findByText("What the last run did")).toBeInTheDocument()
    expect(screen.getByText(/expected auto_release, got awaiting_human_input/)).toBeInTheDocument()
    expect(screen.getByRole("link", { name: "All example cases" })).toBeInTheDocument()
    // Renders the example cases table and the case's run: slow under a full parallel run.
  }, 15_000)

  it("lists the stored versions, newest first, and opens one read only", async () => {
    signInAs("worker")
    open("/packs/finance_ops?tab=versions")
    const first = await screen.findByRole("button", { name: "Version 14" })
    expect(screen.getByText("Active")).toBeInTheDocument()
    expect(screen.getAllByRole("row")[1]).toHaveTextContent("Procedures")
    expect(screen.getByRole("button", { name: "Load more" })).toBeInTheDocument()
    fireEvent.click(first)
    const sheet = await screen.findByRole("dialog")
    expect(within(sheet).getByText(/Read only/)).toBeInTheDocument()
    expect(await within(sheet).findByText("pack.json")).toBeInTheDocument()
    // Version 14 changed the price variance SOP: a line diff against version 13.
    expect(
      await within(sheet).findByRole("heading", { name: "Changes from version 13" })
    ).toBeInTheDocument()
    expect(within(sheet).getByText("Price variance SOP")).toBeInTheDocument()
    expect(within(sheet).getAllByText(/^Added:/).length).toBeGreaterThan(0)
  })

  it("shows a rule value a version changed, as the rule reads", async () => {
    signInAs("worker")
    open("/packs/finance_ops?tab=versions")
    fireEvent.click(await screen.findByRole("button", { name: "Version 13" }))
    const sheet = await screen.findByRole("dialog")
    const procedures = await within(sheet).findByRole("heading", { name: "Procedures" })
    const change = procedures.parentElement!
    expect(change).toHaveTextContent("Price variance")
    expect(change).toHaveTextContent("PRICE_VARIANCE_PCT")
    expect(change).toHaveTextContent("3 % → 2 %")
  })

  it("names the SOPs that cite each rule", async () => {
    signInAs("analyst")
    open("/packs/finance_ops?tab=procedures")
    const row = await screen.findByRole("row", { name: /PRICE_VARIANCE_PCT/ })
    await waitFor(() => expect(row).toHaveTextContent(/Price variance/))
  })
})
