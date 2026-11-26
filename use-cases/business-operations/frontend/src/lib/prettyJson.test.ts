// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from "vitest"
import { prettyJson } from "@/lib/prettyJson"

/**
 * The repair path is the reason this file exists. Every stored `tool_result`
 * over 512 bytes reaches the UI cut on a character boundary, so "does it print"
 * is decided by whether a fragment can be closed — and the one thing that must
 * never happen is a repaired preview showing a value the payload did not carry.
 */
describe("prettyJson", () => {
  it("indents payload that parses, and reports no repair", () => {
    const result = prettyJson('{"case_id":"5100001510-2026","amount":"40000"}')
    expect(result).toEqual({
      text: '{\n  "case_id": "5100001510-2026",\n  "amount": "40000"\n}',
      repaired: false,
    })
  })

  it("closes a payload cut mid-string and drops the half-read entry", () => {
    // Exactly what the 512-byte budget produces: the cut lands inside a value.
    const result = prettyJson('{"case_id":"5100001510-2026","domain":"finance_ops","exce')
    expect(result?.repaired).toBe(true)
    // `exce` is gone rather than shown as an empty or guessed value, and the two
    // complete fields survive verbatim.
    expect(JSON.parse(result!.text)).toEqual({
      case_id: "5100001510-2026",
      domain: "finance_ops",
    })
  })

  it("closes nested structures at the depth the cut left open", () => {
    const result = prettyJson('{"action_log":[{"action":"manual_review_required","timesta')
    expect(result?.repaired).toBe(true)
    expect(JSON.parse(result!.text)).toEqual({
      action_log: [{ action: "manual_review_required" }],
    })
  })

  it("does not let a trailing backslash escape the quote that closes the string", () => {
    const result = prettyJson('{"note":"line one\\nline two\\')
    expect(JSON.parse(result!.text)).toEqual({ note: "line one\nline two" })
  })

  it("returns null for text that was never JSON, so callers render it as prose", () => {
    // A SAP error reaches the same pane as a payload and must not be mangled.
    expect(prettyJson("FY2026 is closed. Reopen the period, or post to FY2027.")).toBeNull()
    expect(prettyJson("")).toBeNull()
  })

  it("returns null when a fragment has no complete value in it at all", () => {
    // Better an unformatted line than a `{}` that implies the tool returned nothing.
    expect(prettyJson('{"case')).toBeNull()
  })
})
