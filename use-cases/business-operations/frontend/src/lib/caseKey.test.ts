// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The browser's half of the case-id contract. The Python codec mints; this one only
 * reads, so these cases pin the shape the two must agree on.
 */

import { describe, it, expect } from "vitest"
import {
  DERIVED_CASE_ID_PATTERN,
  CaseKeyError,
  RUNTIME_SESSION_MIN_LENGTH,
  caseHeadline,
  describeCase,
  isCaseId,
  normalizeCaseId,
  toRuntimeSessionId,
  tryNormalizeCaseId,
} from "./caseKey"

// The legacy `{document_number}-{item_id}` form, which no case here has.
const LEGACY_CASE_ID = "5100001976-2026"

// What the poller actually mints for that AP case. Nothing here mints, so this is a
// literal — and being a literal is the point: if the Python codec's digest ever
// changes, `test_minting_is_deterministic` and this suite disagree about the same case.
const MINTED_CASE_ID = "ap-bchzfkch6kxm6uwqvqe5hhlkvx"
const AP_KEY = { SupplierInvoice: "5100001976", FiscalYear: "2026" }

/**
 * The same key as it actually comes back off DynamoDB — reversed.
 *
 * A live `Scan` of the beta stack returned `{FiscalYear, SupplierInvoice}` for a case the
 * poller inserted in the declared order, because a DynamoDB map has no order to preserve.
 * Every literal above is in declared order, so nothing here fails when a renderer trusts
 * arrival order; this one is the counterexample that does.
 */
const AP_KEY_AS_STORED = { FiscalYear: "2026", SupplierInvoice: "5100001976" }

/** The AP pack as `GET /registry` publishes it, trimmed to the order. */
const AP_PACK = { source: { key: ["SupplierInvoice", "FiscalYear"] } }

describe("normalizeCaseId", () => {
  it.each([
    ["", "empty"],
    ["   ", "blank"],
    [LEGACY_CASE_ID, "the legacy document/item form"],
    ["5100001976#2026", "the legacy wire form"],
    ["ap-bchzfkch6kxm6uwqvqe5hhlkvx OR 1=1", "injection payload"],
    ["../../ap-bchzfkch6kxm6uwqvqe5hhlkvx", "traversal prefix"],
  ])("rejects %s (%s)", bad => {
    expect(() => normalizeCaseId(bad)).toThrow(CaseKeyError)
  })

  it("returns null from tryNormalizeCaseId instead of throwing", () => {
    expect(tryNormalizeCaseId(`  ${MINTED_CASE_ID}  `)).toBe(MINTED_CASE_ID)
    expect(tryNormalizeCaseId(LEGACY_CASE_ID)).toBeNull()
    expect(tryNormalizeCaseId(null)).toBeNull()
    expect(tryNormalizeCaseId(undefined)).toBeNull()
    expect(isCaseId(LEGACY_CASE_ID)).toBe(false)
  })
})

describe("toRuntimeSessionId", () => {
  it("is distinct across cases", () => {
    expect(toRuntimeSessionId(MINTED_CASE_ID)).not.toBe(
      toRuntimeSessionId("ap-aaaaaaaaaaaaaaaaaaaaaaaaaa")
    )
  })

  it("rejects a malformed case id", () => {
    expect(() => toRuntimeSessionId("not a case")).toThrow(CaseKeyError)
  })
})

describe("a minted id, which the browser only ever reads", () => {
  it("matches the anchored pattern the API accepts", () => {
    expect(DERIVED_CASE_ID_PATTERN.startsWith("^")).toBe(true)
    expect(DERIVED_CASE_ID_PATTERN.endsWith("$")).toBe(true)
    expect(new RegExp(DERIVED_CASE_ID_PATTERN).test(MINTED_CASE_ID)).toBe(true)
  })

  it("normalizes to itself and keys a session", () => {
    expect(normalizeCaseId(MINTED_CASE_ID)).toBe(MINTED_CASE_ID)
    expect(isCaseId(MINTED_CASE_ID)).toBe(true)
    expect(toRuntimeSessionId(MINTED_CASE_ID).length).toBeGreaterThanOrEqual(
      RUNTIME_SESSION_MIN_LENGTH
    )
  })

  it("needs no escaping in a URL or an AppSync channel segment", () => {
    expect(encodeURIComponent(MINTED_CASE_ID)).toBe(MINTED_CASE_ID)
    // The narrowest consumer the browser subscribes through.
    expect(MINTED_CASE_ID).toMatch(/^[A-Za-z0-9-]{1,50}$/)
  })

  it("renders with its natural key, in the order the config declared", () => {
    expect(describeCase(MINTED_CASE_ID, AP_KEY)).toBe(
      `${MINTED_CASE_ID} (SupplierInvoice=5100001976, FiscalYear=2026)`
    )
    // A record with no key to print; the line still has to render.
    expect(describeCase(MINTED_CASE_ID)).toBe(MINTED_CASE_ID)
    expect(describeCase(MINTED_CASE_ID, {})).toBe(MINTED_CASE_ID)
    expect(describeCase(MINTED_CASE_ID, null)).toBe(MINTED_CASE_ID)
  })

  it("heads a row with its key's values, never the digest", () => {
    expect(caseHeadline({ case_id: MINTED_CASE_ID, key: AP_KEY })).toBe("5100001976 · 2026")
    // A single-field key is a bare value, with no separator left dangling.
    expect(caseHeadline({ case_id: MINTED_CASE_ID, key: { SupplierInvoice: "5100001976" } })).toBe(
      "5100001976"
    )
    // Nothing to fall back to: the id, so the row is still clickable rather than blank.
    expect(caseHeadline({ case_id: MINTED_CASE_ID })).toBe(MINTED_CASE_ID)
    expect(caseHeadline({ case_id: MINTED_CASE_ID, key: {} })).toBe(MINTED_CASE_ID)
    expect(caseHeadline({})).toBe("")
  })

  it("orders the key by what the pack declared, not by how the store returned it", () => {
    const stored = { case_id: MINTED_CASE_ID, key: AP_KEY_AS_STORED }
    // The invoice number leads because the pack says so. Without the pack it would be the
    // fiscal year, which is the defect a live read found and no literal above can reproduce.
    expect(caseHeadline(stored, AP_PACK)).toBe("5100001976 · 2026")
    // What the row said before the order was published, kept as an assertion so the fix
    // cannot be quietly reverted: this is the string the operator actually saw.
    expect(caseHeadline(stored)).toBe("2026 · 5100001976")
    expect(describeCase(MINTED_CASE_ID, AP_KEY_AS_STORED, AP_PACK)).toBe(
      `${MINTED_CASE_ID} (SupplierInvoice=5100001976, FiscalYear=2026)`
    )
    // Same case, whichever order it arrived in.
    expect(caseHeadline(stored, AP_PACK)).toBe(
      caseHeadline({ case_id: MINTED_CASE_ID, key: AP_KEY }, AP_PACK)
    )
  })

  it("appends a key field the published order does not name, rather than dropping it", () => {
    // A pack whose key outgrew the last `usecase publish`. Losing a field would make two
    // different cases render as the same row, which is worse than an odd order.
    const key = { Plant: "1000", Material: "M-1", Batch: "B-7" }
    const pack = { source: { key: ["Material"] } }
    expect(caseHeadline({ case_id: MINTED_CASE_ID, key }, pack)).toBe("M-1 · 1000 · B-7")
    // No order published at all: arrival order, and still every field.
    expect(caseHeadline({ case_id: MINTED_CASE_ID, key }, { source: { key: [] } })).toBe(
      "1000 · M-1 · B-7"
    )
  })
})

// The vectors the Python codec is tested against too (tests/test_business_operations_types.py):
// it mints each derived row, and both codecs read it and refuse every invalid id.
import vectors from "../../../types/case-id-vectors.json"

describe("the shared case-id vectors", () => {
  it.each(vectors.derived)("reads $case_id and keys its session", row => {
    expect(normalizeCaseId(row.case_id)).toBe(row.case_id)
    expect(toRuntimeSessionId(row.case_id)).toBe(row.session_id)
  })
  it.each(vectors.invalid)("refuses %j", raw => {
    expect(tryNormalizeCaseId(raw)).toBeNull()
  })
  it.each(vectors.trimmed)("trims $raw", row => {
    expect(normalizeCaseId(row.raw)).toBe(row.case_id)
  })
})
