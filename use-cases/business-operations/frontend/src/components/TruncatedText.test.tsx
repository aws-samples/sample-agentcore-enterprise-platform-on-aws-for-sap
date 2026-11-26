// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, afterEach } from "vitest"
import { render, screen, cleanup } from "@testing-library/react"
import { TooltipProvider } from "@/components/ui/tooltip"
import { TruncatedText } from "@/components/TruncatedText"

afterEach(cleanup)

/** jsdom lays nothing out, so a cut-off text is one whose content is wider than its box. */
function width(scroll: number, client: number) {
  Object.defineProperty(HTMLElement.prototype, "scrollWidth", { configurable: true, value: scroll })
  Object.defineProperty(HTMLElement.prototype, "clientWidth", { configurable: true, value: client })
}

describe("A truncated cell", () => {
  // That focus opens the tooltip is a browser test (e2e/flows.spec.ts): jsdom has no
  // :focus-visible, which Base UI's tooltip opens on.
  it("becomes focusable, with its tooltip, once it is cut off", () => {
    width(300, 120)
    render(
      <TooltipProvider delay={0}>
        <TruncatedText text="17300001 Domestic US Supplier 1, Palo Alto" />
      </TooltipProvider>
    )
    const cell = screen.getByText("17300001 Domestic US Supplier 1, Palo Alto")
    expect(cell).toHaveAttribute("tabindex", "0")
    expect(cell).toHaveAttribute("data-slot", "tooltip-trigger")
  })

  it("takes no tab stop when it fits", () => {
    width(100, 120)
    render(<TruncatedText text="Price variance" />)
    expect(screen.getByText("Price variance")).not.toHaveAttribute("tabindex")
  })
})
