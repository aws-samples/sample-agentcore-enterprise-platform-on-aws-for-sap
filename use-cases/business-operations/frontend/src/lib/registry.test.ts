// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from "vitest"
import {
  caseFields,
  caseMoney,
  caseReason,
  describePack,
  formatAmount,
  humanise,
  indexPacks,
  isIncomplete,
  moneyField,
  sourceProvenance,
  type PackField,
  type UseCasePack,
} from "@/lib/registry"
import type { WorkItem } from "@/types/cases"
import { AP_PACK as AP, AP_PACKS as PACKS } from "@/test/packs"
import apPack from "@/test/fixtures/finance_ops.pack.json"

const apSource = apPack.source

/** A pack with no money and no reason, to pin what those callers do without them. */
const MINIMAL: UseCasePack = {
  id: "material_master",
  display_name: "Material Master",
  label: "Material Data Issues",
  short_id: "mm",
  process_types: ["field_completeness"],
  source: { kind: "filter", service: "API_PRODUCT_SRV", entity: "A_Product" },
  fields: [{ attribute: "plant", label: "Plant", type: "string", sap_field: "parent.Plant" }],
}

/** `{label: value}`, for assertions that do not care about attribution. */
const valueMap = (rows: ReturnType<typeof caseFields>) =>
  Object.fromEntries(rows.map(r => [r.label, r.value]))

// Minimal WorkItem fixture; cast to satisfy the type without filling every field.
// Everything the pack knows sits in `attributes`, which is where the poller writes it.
function makeItem(attributes: Record<string, unknown> = {}, overrides: Partial<WorkItem> = {}) {
  return {
    case_id: "ap-abc123",
    pack_id: "finance_ops",
    created_at: "2026-01-01T00:00:00",
    updated_at: "2026-01-02T00:00:00",
    attributes: {
      company_code: "1000",
      supplier_number: "SUP-1",
      amount: 1234.5,
      currency: "USD",
      sap_block_reason: "R",
      document_date: "2026-01-15T00:00:00",
      posting_date: "2026-01-16T00:00:00",
      external_reference: "INV-9",
      ...attributes,
    },
    ...overrides,
  } as unknown as WorkItem
}

describe("humanise", () => {
  it("makes an attribute key readable without guessing at a label", () => {
    expect(humanise("material_master")).toBe("Material master")
    expect(humanise("plant")).toBe("Plant")
  })
})

describe("describePack", () => {
  it("uses the pack's short_id as the tab, so a tab matches its case-id prefix", () => {
    expect(describePack("finance_ops", PACKS)).toEqual({
      id: "finance_ops",
      label: apPack.display_name,
      short: "AP",
      known: true,
    })
  })

  // The old behaviour here was a `Record` lookup with a default, which rendered an
  // unknown pack under AP's label. A newer poller's pack, or one renamed since this
  // build, has to read as itself.
  it("humanises an unrecognised id and says it is unrecognised", () => {
    expect(describePack("legacy_thing", PACKS)).toEqual({
      id: "legacy_thing",
      label: "Legacy thing",
      short: "Legacy thing",
      known: false,
    })
  })

  it("reports a missing or blank pack as unknown rather than as the first pack", () => {
    expect(describePack(undefined, PACKS).known).toBe(false)
    expect(describePack("  ", PACKS).short).toBe("—")
  })

  // A pack with no source produces no cases, so it has no id prefix to show.
  it("falls back to the display name when a pack has no short_id", () => {
    const noSource = { ...MINIMAL, short_id: "" }
    expect(describePack("material_master", indexPacks([noSource])).short).toBe("Material Master")
  })
})

describe("sourceProvenance", () => {
  it("names the service and entity when the pack declares one", () => {
    expect(sourceProvenance("finance_ops", PACKS)).toBe(`${apSource.service} · ${apSource.entity}`)
  })

  // The case that used to render as a blank <p>. A pack published with `kind`, `service`
  // and `entity` all null joins to the empty string, which beside fields that populated
  // fine reads as a load failure.
  it("says so when a pack resolves but declares no source at all", () => {
    const noSource: UseCasePack = { ...MINIMAL, source: {} }
    expect(sourceProvenance("material_master", indexPacks([noSource]))).toBe(
      "Material Master · no upstream service declared"
    )
  })

  it("distinguishes a pack this build has never heard of", () => {
    expect(sourceProvenance("legacy_thing", PACKS)).toBe(
      "Legacy thing · not in this deployment's registry"
    )
  })

  // `registry.public_view()` iterates every pack with no marker filter, so a pack minted
  // through `POST /packs` really is served here — and would otherwise render its
  // own sentinel as though it were a service name.
  it("explains an incomplete pack instead of rendering its sentinel", () => {
    const marked: UseCasePack = {
      ...MINIMAL,
      incomplete: true,
      source: { kind: "filter", service: "<AWAITING_SURVEY>", entity: "<AWAITING_SURVEY>" },
    }
    expect(sourceProvenance("material_master", indexPacks([marked]))).toBe(
      "Awaiting a SAP survey, so nothing is polled yet"
    )
  })
})

describe("isIncomplete", () => {
  it("reads the registry's flag, and nothing else", () => {
    expect(isIncomplete(PACKS.finance_ops)).toBe(false)
    expect(isIncomplete(undefined)).toBe(false)
    expect(isIncomplete({ ...MINIMAL, incomplete: true })).toBe(true)
    // A marker in the source is the server's business: unflagged is complete.
    expect(isIncomplete({ ...MINIMAL, source: { service: "<AWAITING_SURVEY>" } })).toBe(false)
  })
})

describe("formatAmount", () => {
  it("pads to two decimals so a column of figures lines up", () => {
    expect(formatAmount(1200)).toBe("1,200.00")
    expect(formatAmount(1200.5)).toBe("1,200.50")
  })

  it("renders a non-USD total with its own currency, never a dollar sign", () => {
    expect(formatAmount(1000, "EUR")).toBe("1,000.00 EUR")
    expect(formatAmount(1000, "USD")).toBe("1,000.00 USD")
  })

  it("em-dashes an absent figure — no money is not zero money", () => {
    expect(formatAmount(null)).toBe("—")
    expect(formatAmount(undefined)).toBe("—")
  })
})

describe("caseMoney", () => {
  it("reads the attribute the pack declared as its amount, with its currency", () => {
    expect(moneyField(AP)?.attribute).toBe("amount")
    expect(caseMoney(makeItem(), PACKS)).toEqual({ amount: 1234.5, currency: "USD" })
  })

  // A pack that carries no money must render no figure rather than a zero: the case was
  // never told what it was worth, which is not the same as being worth nothing.
  it("reports no money for a pack that declares none", () => {
    expect(moneyField(MINIMAL)).toBeUndefined()
    const item = makeItem({}, { pack_id: "material_master" })
    expect(caseMoney(item, indexPacks([MINIMAL]))).toEqual({ amount: null, currency: null })
  })

  it("reports no money for a case whose pack this deployment does not have", () => {
    expect(caseMoney(makeItem({}, { pack_id: "gone" }), PACKS)).toEqual({
      amount: null,
      currency: null,
    })
  })
})

describe("caseReason", () => {
  // AP's reason is the payment block, which is the field the digest quotes per row.
  it("reads the field the pack marked role: reason", () => {
    expect(caseReason(makeItem(), PACKS)).toBe("R")
  })

  it("returns null when the pack declares no reason, so a caller can fall back", () => {
    const item = makeItem({}, { pack_id: "material_master" })
    expect(caseReason(item, indexPacks([MINIMAL]))).toBeNull()
    expect(caseReason(makeItem({}, { pack_id: "gone" }), PACKS)).toBeNull()
  })

  it("returns null when the reason field is declared but empty on this case", () => {
    expect(caseReason(makeItem({ sap_block_reason: null }), PACKS)).toBeNull()
  })
})

describe("caseFields", () => {
  it("renders the pack's fields in declared order, typed, then our own bookkeeping", () => {
    const rows = caseFields(makeItem(), PACKS)
    const map = valueMap(rows)

    expect(map["Supplier"]).toBe("SUP-1")
    expect(map["Amount"]).toBe("1,234.50 USD")
    expect(map["Currency"]).toBe("USD")
    expect(map["SAP block reason"]).toBe("R")
    expect(map["Company code"]).toBe("1000")
    // Relative within a day, then a short date, so assert the shape: the string moves with
    // the wall clock. The stamp it stands for stays recoverable, which is what `exact` carries.
    expect(map["Created"]).toMatch(/ago$|^just now$|^[A-Z][a-z]{2} \d{1,2}(, \d{4})?$/)
    expect(rows.find(r => r.label === "Updated")?.exact).toBe("2026-01-02T00:00:00")
  })

  /**
   * The exit condition for making the frontend read from the registry: AP looks exactly
   * as it did when these eight labels were compiled into this build. Order included —
   * `pack.json`'s `fields` block is insertion-ordered on purpose, and that order is the panel's order.
   */
  it("shows AP the same eight fields, in the same order, plus Created and Updated", () => {
    expect(caseFields(makeItem(), PACKS).map(r => r.label)).toEqual([
      "Supplier",
      "Amount",
      "Currency",
      "SAP block reason",
      "Document date",
      "Posting date",
      "External reference",
      "Company code",
      "Created",
      "Updated",
    ])
  })

  it("leaves out fields the pack marked panel: false", () => {
    const map = valueMap(caseFields(makeItem({ purchase_order: "4500001" }), PACKS))
    // Declared so the poller writes them and the agent can read them — not so they
    // compete for space in a panel.
    expect(map["Purchase order"]).toBeUndefined()
    expect(map["Invoice quantity"]).toBeUndefined()
  })

  it("attributes each field to its SAP name without the poller's own scope prefix", () => {
    const origin = Object.fromEntries(caseFields(makeItem(), PACKS).map(r => [r.label, r.sapField]))
    // `parent.`/`child.` says which record of an expanded response a value came from,
    // which is this repo's vocabulary — not a name an operator can look up in SAP.
    expect(origin["Supplier"]).toBe("InvoicingParty")
    expect(origin["SAP block reason"]).toBe("PaymentBlockingReason")
    // Inventing a field name for our own bookkeeping is the same defect in reverse.
    expect(origin["Created"]).toBeUndefined()
    expect(origin["Updated"]).toBeUndefined()
  })

  it("splits a date at T and em-dashes a missing one", () => {
    const map = valueMap(caseFields(makeItem({ posting_date: undefined }), PACKS))
    expect(map["Document date"]).toBe("2026-01-15")
    expect(map["Posting date"]).toBe("—")
  })

  it("em-dashes every absent value rather than rendering 'null'", () => {
    const map = valueMap(
      caseFields(
        makeItem({
          supplier_number: null,
          amount: null,
          currency: null,
          sap_block_reason: null,
          external_reference: null,
          company_code: null,
        }),
        PACKS
      )
    )
    expect(map["Supplier"]).toBe("—")
    expect(map["Amount"]).toBe("—")
    expect(map["SAP block reason"]).toBe("—")
    expect(map["External reference"]).toBe("—")
    expect(map["Company code"]).toBe("—")
  })

  /**
   * These values live in `attributes`, so the browser needs no fallback to a top-level
   * column.
   *
   * The assertion is that the fallback is *gone*: a top-level column read here would be
   * a second implementation of the shim, drifting against the one that has an expiry.
   */
  it("ignores a top-level column, because the API lifts before it serialises", () => {
    const unlifted = {
      case_id: "ap-legacy",
      pack_id: "finance_ops",
      created_at: "2026-01-01T00:00:00",
      updated_at: "2026-01-02T00:00:00",
      supplier_number: "SUP-OLD",
      amount: "999.99",
      currency: "EUR",
    } as unknown as WorkItem
    const map = valueMap(caseFields(unlifted, PACKS))
    expect(map["Supplier"]).toBe("—")
    expect(map["Amount"]).toBe("—")
    expect(caseMoney(unlifted, PACKS)).toEqual({ amount: null, currency: null })
  })

  // And the shape the API actually serves: the same case, lifted. The eval test harness seeds
  // amounts as strings, so a figure that arrived as `"999.99"` is still a figure.
  it("reads a lifted record's attributes", () => {
    const lifted = makeItem({
      supplier_number: "SUP-OLD",
      amount: "999.99",
      currency: "EUR",
    })
    const map = valueMap(caseFields(lifted, PACKS))
    expect(map["Supplier"]).toBe("SUP-OLD")
    expect(map["Amount"]).toBe("999.99 EUR")
    expect(caseMoney(lifted, PACKS)).toEqual({ amount: 999.99, currency: "EUR" })
  })

  /**
   * The other half of the exit condition. A case whose pack is not in this deployment's
   * registry — unpublished since, or a registry this build could not fetch — renders the
   * attributes the poller actually wrote. Borrowing AP's labels would put a plant under
   * the heading "Supplier".
   */
  it("renders an unrecognised pack's own attributes rather than another pack's labels", () => {
    const rows = caseFields(
      makeItem({ plant: "1710", base_unit: "EA" }, { pack_id: "material_master" }),
      PACKS
    )
    const map = valueMap(rows)
    expect(map["Plant"]).toBe("1710")
    expect(map["Base unit"]).toBe("EA")
    expect(map["Supplier"]).toBeUndefined()
    // No invented origin: the SAP path is a pack declaration, so with no pack there is
    // nothing to attribute to.
    expect(rows.every(r => r.sapField === undefined)).toBe(true)
  })

  it("renders every case generically when the registry could not be reached at all", () => {
    const map = valueMap(caseFields(makeItem(), {}))
    // The humanised attribute key, not the pack's label — the label lives in a registry
    // this build could not fetch, so there is nothing to render but what the record says.
    expect(map["Supplier number"]).toBe("SUP-1")
    expect(map["Supplier"]).toBeUndefined()
  })
})

// Read against the config rather than a copy of it. A label over an attribute no poller
// writes renders as an em-dash forever, and a `sap_field` that no longer matches the path
// it came from is an attribution an operator cannot check — both look like data problems.
describe("finance_ops's declarations track its poller config", () => {
  it("maps every declared field to a field_map entry", () => {
    const fieldMap = apSource.field_map as Record<string, { path: string }>
    for (const field of AP.fields) {
      expect(
        fieldMap[field.attribute],
        `field_map has no '${field.attribute}' for the "${field.label}" field`
      ).toBeDefined()
    }
  })

  it("declares exactly one reason field — a case has one reason", () => {
    const reasons = AP.fields.filter((f: PackField) => f.role === "reason")
    expect(reasons.map(f => f.attribute)).toEqual(["sap_block_reason"])
  })

  it("pairs its amount with a currency it also declares", () => {
    const money = moneyField(AP)
    expect(money?.currency_field).toBe("currency")
    expect(AP.fields.some(f => f.attribute === money?.currency_field)).toBe(true)
  })
})
