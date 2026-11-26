// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from "vitest"
import { parseSop } from "@/components/packs/SopProse"
import { diffLines, diffCounts } from "@/lib/lineDiff"

const RULE = "=".repeat(80)
const SOP = [
  "SOME SOP",
  "Version 1.2",
  "",
  RULE,
  "STEP 2: CALCULATE",
  RULE,
  "",
  "Before it starts, this MUST be true:",
  "",
  "1. A precondition.",
  "2.1  The agent MUST compute:",
  "       pct = |a - b| / b",
  "     Breaching one is WITHIN tolerance,",
  "     not above it.",
  "2.2  IF it is above:",
  "     - The agent MUST raise a ticket to",
  "       {{CONTACT_AP_TEAM}}.",
  "Process Type: x",
].join("\n")

describe("parseSop", () => {
  it("reads sections, clauses, wrapped lines, bullets and formulas", () => {
    const sop = parseSop(SOP)
    expect(sop.version).toBe("1.2")
    expect(sop.sections.map(s => s.title)).toEqual(["Step 2: calculate"])
    const [lead, one, c21, c22, tail] = sop.sections[0].clauses
    expect(lead).toEqual({
      number: null,
      blocks: [{ type: "p", text: "Before it starts, this MUST be true:" }],
    })
    expect(one.number).toBe("1")
    expect(c21.number).toBe("2.1")
    expect(c21.blocks).toEqual([
      { type: "p", text: "The agent MUST compute:" },
      { type: "code", lines: ["  pct = |a - b| / b"] },
      { type: "p", text: "Breaching one is WITHIN tolerance, not above it." },
    ])
    expect(c22.blocks[1]).toEqual({
      type: "bullet",
      text: "The agent MUST raise a ticket to {{CONTACT_AP_TEAM}}.",
    })
    expect(tail.blocks).toEqual([{ type: "p", text: "Process Type: x" }])
  })
})

describe("diffLines", () => {
  it("shows an added line with context and folds the rest", () => {
    const before = ["a", "b", "c", "d", "e", "f", "g"].join("\n")
    const after = ["a", "b", "c", "d", "X", "e", "f", "g"].join("\n")
    const diff = diffLines(before, after, 1)
    expect(diff).toEqual([
      { kind: "fold", count: 3 },
      { kind: "same", text: "d" },
      { kind: "added", text: "X" },
      { kind: "same", text: "e" },
      { kind: "fold", count: 2 },
    ])
    expect(diffCounts(diff)).toEqual({ added: 1, removed: 0 })
  })
})
