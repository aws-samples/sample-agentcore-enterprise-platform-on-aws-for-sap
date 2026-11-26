// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, vi, afterEach } from "vitest"
import { screen, cleanup, fireEvent, waitFor } from "@testing-library/react"
import { QueryClient } from "@tanstack/react-query"
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
vi.mock("sonner", () => ({ toast: vi.fn() }))

import { CaseMarker, MARKERS_KEY, SandboxBand, SandboxMarker } from "@/components/SandboxMarker"
import { useRegistry } from "@/hooks/useRegistry"
import { clearOperatorContext } from "@/lib/signOut"
import { CASES } from "@/mocks/fixtures"

serveMockApi()

function Markers() {
  const { packs } = useRegistry()
  const testData = CASES.find(c => c.test_data)!
  const plain = CASES.find(c => !c.test_data)!
  return (
    <>
      <SandboxBand />
      <div data-testid="pack">
        <SandboxMarker pack={packs[0]} />
      </div>
      <div data-testid="test-data-case">
        <CaseMarker c={testData} pack={packs[0]} />
      </div>
      <div data-testid="case">
        <CaseMarker c={plain} pack={packs[0]} />
      </div>
    </>
  )
}

const open = () => renderRoutes("/", [{ path: "/", element: <Markers /> }])

describe("Sandbox markers (decision 22)", () => {
  afterEach(() => {
    cleanup()
    localStorage.clear()
  })

  it("shows the band when every SAP system is non-production, and a flask on the pack and on test data", async () => {
    signInAs("approver")
    open()
    expect(await screen.findByRole("region", { name: "Sandbox" })).toHaveTextContent(
      "SAP system s4 is a sandbox. Nothing here reaches a production system."
    )
    expect(
      await screen.findByRole("img", { name: /s4 is a sandbox\. Not production/ })
    ).toBeInTheDocument()
    expect(screen.getByRole("img", { name: /Test data/ })).toBeInTheDocument()
    // The band already says it for every case, so a plain case carries no flask of its own.
    expect(screen.getByTestId("case")).toBeEmptyDOMElement()
  })

  it("hides every marker in this browser, and keeps that choice across sign-out", async () => {
    signInAs("approver")
    open()
    fireEvent.click(await screen.findByRole("button", { name: "Hide markers" }))
    await waitFor(() => expect(screen.queryByRole("region", { name: "Sandbox" })).toBeNull())
    expect(screen.queryByRole("img")).toBeNull()
    clearOperatorContext(new QueryClient())
    expect(localStorage.getItem(MARKERS_KEY)).toBe("false")
  })
})
