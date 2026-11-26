// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, afterEach } from "vitest"
import { render, screen, cleanup } from "@testing-library/react"
import { JsonBlock } from "@/components/ui/json-block"

describe("JsonBlock", () => {
  afterEach(cleanup)

  it("prints a payload indented", () => {
    const { container } = render(
      <JsonBlock text='{"case_id":"5100001510-2026","amount":"40000"}' />
    )
    expect(container.textContent).toContain('  "case_id": "5100001510-2026"')
    expect(screen.queryByText(/Cut short in storage/)).toBeNull()
  })

  it("says so when it had to close a payload cut short in storage", () => {
    render(<JsonBlock text='{"case_id":"5100001510-2026","domain":"finance_ops","exce' />)
    expect(screen.getByText(/Cut short in storage/)).toBeTruthy()
  })

  it("renders a non-JSON tool result as the text it is", () => {
    // A SAP error reaches the same pane as a payload. Unformattable is not unreadable.
    const message = "FY2026 is closed. Reopen the period, or post to FY2027."
    const { container } = render(<JsonBlock text={message} />)
    expect(container.textContent).toBe(message)
  })
})
