// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from "vitest"
import { versionDiff } from "@/lib/versionDiff"

const base = {
  pack: {
    display_name: "AP",
    write_guards: ["po_reference"],
    constants: { PRICE_VARIANCE_PCT: 2, AUTO_WRITE_LIMIT_USD: 100000 },
  },
  base_prompt: "Be brief.\n",
  sops: { "price_variance.txt": "Version 1.0\nStep one.\n" },
  evals: [{ test_id: "a" }, { test_id: "b", x: 1 }],
}
const PARTS = {
  display_name: "procedures",
  write_guards: "controls",
  constants: "procedures",
} as const
const CONSTANT_PARTS = { AUTO_WRITE_LIMIT_USD: "controls" } as const

describe("versionDiff", () => {
  it("finds nothing between a version and itself", () => {
    expect(versionDiff(base, structuredClone(base), PARTS, CONSTANT_PARTS)).toEqual([])
  })

  it("groups the changes by part in the parts' order, splitting constants by symbol", () => {
    const after = structuredClone(base)
    after.pack.constants.AUTO_WRITE_LIMIT_USD = 50000
    after.pack.constants.PRICE_VARIANCE_PCT = 3
    after.pack.write_guards = ["po_reference", "no_duplicate"]
    ;(after.pack as Record<string, unknown>).new_key = true
    after.sops["price_variance.txt"] = "Version 1.1\nStep one.\n"
    after.evals = [{ test_id: "b", x: 2 }, { test_id: "c" }]
    const parts = versionDiff(base, after, PARTS, CONSTANT_PARTS)
    expect(parts.map(p => p.part)).toEqual(["procedures", "controls", "evaluations", null])
    const procedures = parts[0].changes
    expect(procedures[0]).toMatchObject({ symbol: "PRICE_VARIANCE_PCT", before: 2, after: 3 })
    expect(procedures[1]).toMatchObject({ kind: "text", file: "price_variance.txt" })
    expect(parts[1].changes.map(c => ("symbol" in c ? c.symbol : c.key))).toEqual([
      "AUTO_WRITE_LIMIT_USD",
      "write_guards",
    ])
    expect(parts[2].changes[0]).toEqual({
      kind: "cases",
      key: "evals",
      added: ["c"],
      removed: ["a"],
      changed: ["b"],
    })
    expect(parts[3].changes[0]).toMatchObject({ key: "new_key", before: undefined, after: true })
  })

  it("diffs the general instructions as text", () => {
    const after = { ...structuredClone(base), base_prompt: "Be brief.\nCite the SOP.\n" }
    const [{ part, changes }] = versionDiff(base, after, PARTS, CONSTANT_PARTS)
    expect(part).toBe("procedures")
    expect(changes[0]).toMatchObject({ kind: "text", key: "base_prompt" })
    const diff = (changes[0] as { diff: { kind: string; text?: string }[] }).diff
    expect(diff.filter(l => l.kind === "added").map(l => l.text)).toEqual(["Cite the SOP."])
  })
})
