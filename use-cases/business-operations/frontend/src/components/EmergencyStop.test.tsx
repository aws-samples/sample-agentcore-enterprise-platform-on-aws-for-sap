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
const admin = { value: true }
vi.mock("@/hooks/useIsAdmin", () => ({ useIsAdmin: () => admin.value }))
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

import DeploymentSettingsPage from "@/routes/DeploymentSettingsPage"
import { StopBand, reasonProblem, stopStands, stopperName } from "@/components/EmergencyStop"
import { resetMockCases } from "@/mocks/handlers"
import { AskBar, AssistantPanel } from "@/components/layout/Assistant"
import { stopDeployment } from "@/services/deploymentService"

serveMockApi()

const open = () =>
  renderRoutes("/deployment", [
    {
      path: "/deployment",
      element: (
        <>
          <StopBand />
          <DeploymentSettingsPage />
        </>
      ),
    },
  ])

const STOP = {
  stopped_by: "sam@example.com (8f0c)",
  stopped_at: "2026-11-19T10:00:00+00:00",
  reason: "r",
  trigger_mode_set: true,
  runs_total: 0,
  runs: [],
  complete: true,
  stands: true,
}

describe("The emergency stop", () => {
  afterEach(() => {
    cleanup()
    localStorage.clear()
    resetMockCases()
    admin.value = true
  })

  it("asks for a reason, names the runs in progress, then stops and shows the band", async () => {
    signInAs("operator")
    open()
    fireEvent.click(await screen.findByRole("button", { name: "Emergency stop" }))
    const dialog = await screen.findByRole("alertdialog")
    expect(await within(dialog).findByText(/No case has a run in progress now/)).toBeInTheDocument()
    expect(within(dialog).getByText(/sets the trigger mode to manual for every pack/)).toBeInTheDocument()
    const confirm = within(dialog).getByRole("button", { name: "Stop every run" })
    expect(confirm).toBeDisabled()
    fireEvent.change(within(dialog).getByLabelText("Reason"), { target: { value: "   " } })
    expect(confirm).toBeDisabled()
    fireEvent.change(within(dialog).getByLabelText("Reason"), {
      target: { value: "Wrong supplier released" },
    })
    fireEvent.click(confirm)

    const band = await screen.findByRole("region", { name: "Emergency stop" })
    expect(band).toHaveTextContent("sam@example.com stopped every run")
    expect(band).toHaveTextContent("Wrong supplier released")
    expect(within(band).getByRole("link", { name: "Deployment settings" })).toHaveAttribute(
      "href",
      "/deployment"
    )
    await waitFor(() => expect(screen.getByText("manual")).toBeInTheDocument())
    expect(
      await screen.findByText(/The emergency stop stands: sam@example.com/)
    ).toBeInTheDocument()
  })

  it("is disabled with its reason for a caller who is not an administrator", async () => {
    admin.value = false
    signInAs("approver")
    open()
    const stop = await screen.findByRole("button", { name: "Emergency stop" })
    expect(stop).toHaveAttribute("aria-disabled", "true")
    fireEvent.click(stop)
    expect(screen.queryByRole("alertdialog")).toBeNull()
  })

  it("stands as the API says, whatever the trigger mode reads", () => {
    expect(stopStands(null)).toBe(false)
    expect(stopStands({ ...STOP, stands: true })).toBe(true)
    expect(stopStands({ ...STOP, stands: false, cleared_by: "ana@example.com (c1)" })).toBe(false)
  })

  it("holds until an administrator clears it with a reason, and leaves the trigger mode manual", async () => {
    signInAs("operator")
    open()
    fireEvent.click(await screen.findByRole("button", { name: "Emergency stop" }))
    const dialog = await screen.findByRole("alertdialog")
    fireEvent.change(within(dialog).getByLabelText("Reason"), {
      target: { value: "Wrong supplier released" },
    })
    fireEvent.click(within(dialog).getByRole("button", { name: "Stop every run" }))
    const band = await screen.findByRole("region", { name: "Emergency stop" })
    expect(band).toHaveTextContent("Nothing new starts until an administrator clears the stop")
    // The trigger mode waits for the clear, and says so.
    expect(await screen.findByRole("button", { name: "Set to auto" })).toHaveAttribute(
      "aria-disabled",
      "true"
    )

    fireEvent.click(await screen.findByRole("button", { name: "Clear the stop" }))
    const clear = await screen.findByRole("alertdialog")
    expect(within(clear).getByText(/The trigger mode stays manual/)).toBeInTheDocument()
    const confirm = within(clear).getByRole("button", { name: "Clear the stop" })
    expect(confirm).toBeDisabled()
    fireEvent.change(within(clear).getByLabelText("Reason"), {
      target: { value: "Supplier master fixed" },
    })
    fireEvent.click(confirm)

    await waitFor(() => expect(screen.queryByRole("region", { name: "Emergency stop" })).toBeNull())
    expect(
      await screen.findByText(/Cleared by sam@example.com.*Supplier master fixed/)
    ).toBeInTheDocument()
    expect(screen.getByText("manual")).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Clear the stop" })).toBeNull()
  })

  it("tells the ask bar and the panel that questions wait while the stop stands", async () => {
    signInAs("operator")
    await stopDeployment("Check releases", "mock-token")
    const chat = {
      slotKey: "general",
      input: "",
      setInput: () => {},
      isLoading: false,
      caseId: null,
    }
    renderRoutes("/", [
      {
        path: "/",
        element: <AskBar chat={chat as never} onOpen={() => {}} inputRef={{ current: null }} />,
      },
    ])
    expect(
      await screen.findByPlaceholderText("Questions wait until an administrator clears the stop")
    ).toBeInTheDocument()
    cleanup()

    // And so does the open panel's own input.
    const panelChat = {
      ...chat,
      contextCaseCount: 0,
      rehearsal: null,
      clear: () => {},
      stop: async () => {},
    }
    renderRoutes("/", [
      {
        path: "/",
        element: <AssistantPanel chat={panelChat as never} onClose={() => {}} />,
      },
    ])
    expect(
      await screen.findByPlaceholderText("Questions wait until an administrator clears the stop")
    ).toBeInTheDocument()
  })

  it("takes a one-line reason, and names the person without their subject id", () => {
    expect(reasonProblem("  ")).toBe("Say why you are stopping.")
    expect(reasonProblem("a\nb")).toBe("One line, with no special characters.")
    expect(reasonProblem("x".repeat(501))).toBe("At most 500 characters.")
    expect(reasonProblem("SAP shows releases no case explains")).toBeNull()
    expect(stopperName("ops@example.com (8f0c…)")).toBe("ops@example.com")
  })
})
