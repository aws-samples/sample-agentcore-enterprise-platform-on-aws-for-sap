// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, afterEach } from "vitest"
import { render, screen, fireEvent, cleanup } from "@testing-library/react"
import { ToolCallDisplay } from "@/components/chat/ToolCallDisplay"

describe("ToolCallDisplay result", () => {
  afterEach(cleanup)

  it("renders the result on expand", () => {
    const normalResult = JSON.stringify({ po_number: "4500001234", amount: 25000 })
    render(
      <ToolCallDisplay
        name="sap_get_purchase_order"
        args="{}"
        status="complete"
        result={normalResult}
      />
    )
    expect(screen.queryByText(/4500001234/)).toBeNull()
    fireEvent.click(screen.getByRole("button"))
    expect(screen.getByText(/4500001234/)).toBeTruthy()
  })

  // No browser sign-in to SAP: an auth_url in a result is data, never a link to open.
  it("offers no sign-in control for an authentication_required result", () => {
    const result = JSON.stringify({
      data: {
        error_type: "authentication_required",
        auth_url: "https://bedrock-agentcore.us-east-1.amazonaws.com/identities/oauth2/authorize",
      },
    })
    render(<ToolCallDisplay name="odata_read" args="{}" status="complete" result={result} />)
    expect(screen.getAllByRole("button")).toHaveLength(1)
  })
})

describe("ToolCallDisplay recorded failure", () => {
  it("renders an error status as failed, not as complete", () => {
    render(<ToolCallDisplay name="odata_update" args="{}" status="error" result="500 boom" />)
    expect(screen.getByLabelText("Tool call failed")).toBeInTheDocument()
    expect(screen.queryByLabelText("Tool outcome not confirmed")).not.toBeInTheDocument()
    expect(screen.queryByLabelText("Tool call succeeded")).not.toBeInTheDocument()
  })

  it("names the success state for assistive tech", () => {
    render(<ToolCallDisplay name="odata_read" args="{}" status="complete" result="ok" />)
    expect(screen.getByLabelText("Tool call succeeded")).toBeInTheDocument()
  })

  it("keeps the unconfirmed wording for an abandoned call", () => {
    render(<ToolCallDisplay name="odata_update" args="{}" status="incomplete" />)
    expect(screen.getByLabelText("Tool outcome not confirmed")).toBeInTheDocument()
    expect(screen.queryByLabelText("Tool call failed")).not.toBeInTheDocument()
  })
})
