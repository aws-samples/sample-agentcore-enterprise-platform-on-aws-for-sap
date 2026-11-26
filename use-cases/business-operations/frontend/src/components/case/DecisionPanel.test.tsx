// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, vi, afterEach } from "vitest"
import { screen, cleanup, fireEvent, waitFor } from "@testing-library/react"
import { useQuery } from "@tanstack/react-query"
import { renderRoutes, signInAs, serveMockApi } from "@/test/mockApi"
import { CASES } from "@/mocks/fixtures"
import { resetMockCases } from "@/mocks/handlers"
import type { WorkItem } from "@/types/cases"

const config = { ticketingEnabled: false }
vi.mock("@/lib/config", async () => {
  const { MOCK_API_URL } = await import("@/test/mockApi")
  return { getConfig: vi.fn(async () => ({ apiUrl: MOCK_API_URL, features: {}, ...config })) }
})
vi.mock("@/hooks/useApi", () => {
  const token = async () => "mock-token"
  return { useApi: () => token }
})
const profile = { sub: "mock-approver" }
vi.mock("react-oidc-context", () => ({
  useAuth: () => ({ isAuthenticated: true, user: { profile } }),
}))
// The registry as mock mode serves it, with the pack's SAP system's system role switchable.
const env = { system_role: "" }
vi.mock("@/hooks/useRegistry", async importOriginal => {
  const real = await importOriginal<typeof import("@/hooks/useRegistry")>()
  return {
    ...real,
    useRegistry: () => {
      const r = real.useRegistry()
      if (!env.system_role) return r
      const byId = Object.fromEntries(
        Object.entries(r.byId).map(([id, p]) => [
          id,
          { ...p, sap_system: { name: "s4", system_role: env.system_role } },
        ])
      )
      return { ...r, byId }
    },
  }
})

import { DecisionPanel } from "@/components/case/DecisionPanel"
import { fetchCase, replyToCase } from "@/services/casesService"
import { stopDeployment } from "@/services/deploymentService"

serveMockApi()

const byDoc = (doc: string) => CASES.find(c => c.document_number === doc)!
const decided = vi.fn()

/** The panel over the case as the mock API serves it now, or over a copy the caller holds. */
function Panel({ id, stale }: { id: string; stale?: WorkItem }) {
  const q = useQuery({
    queryKey: ["case", id],
    queryFn: () => fetchCase(id, "mock-token"),
    enabled: !stale,
  })
  const c = stale ?? q.data
  return c ? (
    <DecisionPanel
      c={c}
      onDecided={decided}
      onRefresh={() => void q.refetch()}
      after="the next case in Needs you opens"
    />
  ) : null
}

const open = (doc: string, stale = false) => {
  const c = byDoc(doc)
  return renderRoutes("/", [
    { path: "/", element: <Panel id={c.case_id} stale={stale ? structuredClone(c) : undefined} /> },
  ])
}

const reason = () => screen.getByRole("textbox", { name: /Reason/ })

describe("The decision panel", () => {
  afterEach(() => {
    cleanup()
    resetMockCases()
    localStorage.clear()
    decided.mockReset()
    env.system_role = ""
    config.ticketingEnabled = false
    profile.sub = "mock-approver"
  })

  it("asks the agent's question and takes one answer", async () => {
    signInAs("approver")
    open("5100001981")
    expect(await screen.findByText("The agent asks")).toBeInTheDocument()
    expect(screen.queryByRole("radio")).not.toBeInTheDocument()
    fireEvent.change(screen.getByRole("textbox", { name: /Your answer/ }), {
      target: { value: "Tier 2 pricing applies from 1 Nov." },
    })
    fireEvent.click(screen.getByRole("button", { name: "Send and resume" }))
    await waitFor(() =>
      expect(decided).toHaveBeenCalledWith(
        expect.objectContaining({ outcome: expect.objectContaining({ decision: "replied" }) })
      )
    )
  })

  it("says why a person decides, shows the change, and needs a reason before it approves", async () => {
    signInAs("approver")
    open("5100001976")
    expect(
      await screen.findByText(
        "52,500 USD is above this deployment's auto-write limit of 50,000 USD."
      )
    ).toBeInTheDocument()
    expect(screen.getByText(/the agent makes this change in s4, a sandbox/)).toBeInTheDocument()
    // The clause by the SOP's name and its section (round five).
    expect(screen.getByText("Price variance 3.3")).toBeInTheDocument()
    expect(screen.getByText("SAP block reason")).toBeInTheDocument()
    expect(
      screen.getByText(/the checks re-read SAP: PO reference, no duplicate, GR coverage/)
    ).toBeInTheDocument()

    const submit = screen.getByRole("button", { name: "Approve as billed" })
    expect(submit).toHaveAttribute("aria-disabled", "true")
    fireEvent.click(submit)
    expect(decided).not.toHaveBeenCalled()

    fireEvent.change(reason(), { target: { value: "Price agreed with the supplier on 3 Nov." } })
    fireEvent.click(screen.getByRole("button", { name: "Approve as billed" }))
    await waitFor(() =>
      expect(decided).toHaveBeenCalledWith(
        expect.objectContaining({
          awaitingSecond: false,
          outcome: expect.objectContaining({ decision: "approved", optionId: "release" }),
        })
      )
    )
  })

  it("offers the case's own options, each with what it does, and names the one chosen", async () => {
    signInAs("approver")
    open("5100001976")
    const shortPay = await screen.findByRole("radio", { name: /Short pay/ })
    expect(screen.getByText(/Pay what the PO supports/)).toBeInTheDocument()
    expect(screen.getByRole("radio", { name: /Reply only/ })).toBeInTheDocument()
    fireEvent.click(shortPay)
    expect(screen.getByText("Short pay makes no change in SAP.")).toBeInTheDocument()
    fireEvent.change(reason(), { target: { value: "Supplier agreed to a credit memo." } })
    fireEvent.click(screen.getByRole("button", { name: "Short pay" }))
    await waitFor(() =>
      expect(decided).toHaveBeenCalledWith(
        expect.objectContaining({
          outcome: expect.objectContaining({ decision: "chose", optionId: "short_pay" }),
        })
      )
    )
  })

  it("approves or denies from the proposed write on a case without options", async () => {
    signInAs("approver")
    const c = { ...structuredClone(byDoc("5100001976")), decision_options: undefined }
    renderRoutes("/", [{ path: "/", element: <Panel id={c.case_id} stale={c} /> }])
    fireEvent.click(await screen.findByRole("radio", { name: /Deny/ }))
    expect(screen.getByText("Denying changes nothing in SAP.")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Deny" })).toBeInTheDocument()
    fireEvent.click(screen.getByRole("radio", { name: /Approve/ }))
    expect(screen.getByRole("button", { name: "Approve and release" })).toBeInTheDocument()
  })

  it("says so on the button when the write lands in production", async () => {
    signInAs("approver")
    env.system_role = "production"
    open("5100002011", true)
    expect(
      await screen.findByRole("button", { name: "Approve and release in production" })
    ).toBeInTheDocument()
    expect(screen.getByText(/this change in s4, the production system/)).toBeInTheDocument()
  })

  it("keeps the decision from a person the pack does not let approve, and says why", async () => {
    signInAs("worker")
    profile.sub = "mock-worker"
    open("5100002011", true)
    const submit = await screen.findByRole("button", { name: "Approve and release" })
    expect(submit).toHaveAttribute("aria-disabled", "true")
    fireEvent.focus(submit)
    expect(
      await screen.findByText(/Only the pack's approvers decide: ap-manager/)
    ).toBeInTheDocument()
  })

  it("counts two approvers, and shows the API's answer to the same approver twice", async () => {
    signInAs("approver")
    open("5100002011")
    expect(await screen.findByText("1 of 2 approvals")).toBeInTheDocument()
    expect(
      screen.getByText(/You approved .* A different approver must approve too\./)
    ).toBeInTheDocument()
    fireEvent.change(reason(), { target: { value: "Still agreed." } })
    fireEvent.click(screen.getByRole("button", { name: "Approve and release" }))
    expect(
      await screen.findByText(/You already approved this change. A different approver/)
    ).toBeInTheDocument()
    expect(decided).not.toHaveBeenCalled()
    cleanup()

    // A different approver: the second approval resumes the run.
    signInAs("operator")
    profile.sub = "mock-operator"
    open("5100002011")
    expect(await screen.findByText(/Priya Shah approved/)).toBeInTheDocument()
    fireEvent.change(reason(), { target: { value: "Second approval." } })
    fireEvent.click(screen.getByRole("button", { name: "Approve and release" }))
    await waitFor(() =>
      expect(decided).toHaveBeenCalledWith(expect.objectContaining({ awaitingSecond: false }))
    )
  })

  it("offers Refresh when the case moved on before the decision reached it", async () => {
    signInAs("approver")
    // Someone else decides first; this panel still holds the copy it opened with.
    const c = byDoc("5100001976")
    await replyToCase(c.case_id, "Decided elsewhere", "approved", "mock-token", "deny")
    open("5100001976", true)
    fireEvent.change(await screen.findByRole("textbox", { name: /Reason/ }), {
      target: { value: "Agreed." },
    })
    fireEvent.click(screen.getByRole("button", { name: "Approve as billed" }))
    expect(await screen.findByText("This case moved on")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Refresh" })).toBeInTheDocument()
  })

  it("shows a standing stop's refusal in its words, with nothing recorded and no Refresh", async () => {
    signInAs("operator")
    profile.sub = "mock-operator"
    await stopDeployment("Check releases", "mock-token")
    open("5100001976")
    fireEvent.change(await screen.findByRole("textbox", { name: /Reason/ }), {
      target: { value: "Agreed." },
    })
    fireEvent.click(screen.getByRole("button", { name: "Approve as billed" }))
    expect(await screen.findByText("Not sent")).toBeInTheDocument()
    expect(screen.getByText(/An emergency stop stands: Check releases/)).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Refresh" })).toBeNull()
    expect((await fetchCase(byDoc("5100001976").case_id, "mock-token")).action_log).toEqual(
      byDoc("5100001976").action_log
    )
    expect(decided).not.toHaveBeenCalled()
  })

  it("has nothing to decide on a case that is not waiting for a person", async () => {
    signInAs("approver")
    open("5100002031")
    expect(await screen.findByText(/Nothing to decide yet/)).toBeInTheDocument()
  })
})
