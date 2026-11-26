// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, afterEach } from "vitest"
import { render, screen, cleanup } from "@testing-library/react"
import { MemoryRouter } from "react-router"
import { Citation, citationNote } from "@/components/chat/Citation"

// Shaped on `Citation` in types/cases.schema.json: the two required properties, and the
// index signature that lets extra keys ride along. No live case is needed to pin the render.
const QUOTE = "Variance up to 5% of the PO line value MAY be released without escalation."

afterEach(cleanup)

describe("Citation", () => {
  it("fills a verified quote with the marker", () => {
    render(<Citation citation={{ quote: QUOTE, verified: true }} />)
    const mark = screen.getByText(QUOTE)
    // The gradient, not a flat colour: `animate-marker-wipe` animates background-size,
    // which a background-color cannot do.
    expect(mark).toHaveClass("bg-marker")
    expect(mark.className).toContain("animate-marker-wipe")
  })

  it("underlines an unverified quote instead of filling it", () => {
    render(<Citation citation={{ quote: QUOTE, verified: false }} />)
    const mark = screen.getByText(QUOTE)
    expect(mark).toHaveClass("decoration-dotted")
    expect(mark).not.toHaveClass("bg-marker")
  })

  it("does not accuse an unverified quote of breaking the SOP", () => {
    // `verified: false` also catches a compliant agent that paraphrased. Rendering it in
    // `held` — or wording it as a violation — reports a finding the data does not support.
    render(<Citation citation={{ quote: QUOTE, verified: false }} />)
    const mark = screen.getByText(QUOTE)
    expect(mark.className).not.toContain("held")
    expect(mark.className).not.toContain("destructive")
    expect(citationNote(false, false)).toMatch(/paraphrase/i)
    expect(citationNote(false, false)).not.toMatch(/violat|breach/i)
  })

  it("links to the SOP behind the quote as an anchor, not a button", () => {
    // A `<Link>` rather than a click handler on purpose: this is a navigation, so
    // ctrl-click and "open in new tab" have to work. An operator checking a quoted rule
    // wants the SOP beside the case, not instead of it — which a button cannot give them.
    render(
      <MemoryRouter>
        <Citation citation={{ quote: QUOTE, verified: true }} href="/packs?doc=a%2Fb.txt" />
      </MemoryRouter>
    )
    expect(screen.getByRole("link", { name: QUOTE })).toHaveAttribute(
      "href",
      "/packs?doc=a%2Fb.txt"
    )
    expect(screen.queryByRole("button")).not.toBeInTheDocument()
  })

  it("keeps the mark honest about what the link opens", () => {
    // Not "where this quote is from": the baseline a quote is graded against also absorbs
    // whatever the agent looked up mid-run, and nothing records which document that was.
    const title = citationNote(true, true)
    expect(title).toMatch(/the SOP this run was given/i)
    expect(title).not.toMatch(/where this quote/i)
  })

  it("offers no control when there is nothing behind the quote", () => {
    render(<Citation citation={{ quote: QUOTE, verified: true }} />)
    expect(screen.queryByRole("button")).not.toBeInTheDocument()
    expect(screen.queryByRole("link")).not.toBeInTheDocument()
  })
})
