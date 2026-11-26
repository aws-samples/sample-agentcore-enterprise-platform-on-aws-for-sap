// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest"
import { entityWords, evidenceRows, sapWords } from "@/lib/evidence"
import { timeAgo } from "@/lib/timeAgo"
import type { UseCasePack } from "@/lib/registry"

const PACK = {
  id: "finance_ops",
  display_name: "Finance",
  fields: [
    {
      attribute: "amount",
      label: "Amount",
      type: "amount",
      sap_field: "parent.InvoiceGrossAmount",
      currency_field: "currency",
    },
    {
      attribute: "currency",
      label: "Currency",
      type: "string",
      sap_field: "parent.DocumentCurrency",
    },
    {
      attribute: "sap_block_reason",
      label: "SAP block reason",
      type: "string",
      sap_field: "parent.PaymentBlockingReason",
      role: "reason",
    },
  ],
} as unknown as UseCasePack

describe("evidenceRows", () => {
  it("labels a declared field with the pack's label and keeps the SAP name for the tooltip", () => {
    const [row] = evidenceRows([{ name: "PaymentBlockingReason", value: "R" }], PACK)
    expect(row).toMatchObject({ label: "SAP block reason", sapName: "PaymentBlockingReason" })
    expect(row.mono).toBe(false)
  })

  it("formats an amount with its currency and folds the currency field into it", () => {
    const rows = evidenceRows(
      [
        { name: "InvoiceGrossAmount", value: "52500.00" },
        { name: "DocumentCurrency", value: "EUR" },
      ],
      PACK
    )
    expect(rows).toHaveLength(1)
    expect(rows[0].value).toBe("52,500.00 EUR")
    expect(rows[0].numeric).toBe(true)
  })

  it("puts words on a field the pack does not declare, with the case's currency", () => {
    const rows = evidenceRows(
      [
        { name: "NetPriceAmount", value: "5000" },
        { name: "OrderQuantity", value: "10" },
      ],
      PACK,
      "USD"
    )
    expect(rows.map(r => [r.label, r.value])).toEqual([
      ["Net price amount", "5,000.00 USD"],
      ["Order quantity", "10"],
    ])
  })

  it("sets identifiers in mono and nothing else", () => {
    const rows = evidenceRows(
      [
        { name: "SupplierInvoice", value: "5100001976" },
        { name: "PurchaseOrder", value: "4500000123" },
      ],
      PACK
    )
    expect(rows.every(r => r.mono)).toBe(true)
    expect(rows[0].label).toBe("Supplier invoice")
  })
})

describe("words", () => {
  it("keeps acronyms and drops the API prefix of an entity set", () => {
    expect(sapWords("POItemNumber")).toBe("PO item number")
    expect(entityWords("A_SupplierInvoice")).toBe("Supplier invoice")
  })
})

describe("timeAgo", () => {
  const now = Date.parse("2026-11-21T12:00:00Z")
  it("is relative within a day and a short date after", () => {
    expect(timeAgo("2026-11-21T11:59:40Z", now)).toBe("just now")
    expect(timeAgo("2026-11-21T11:48:00Z", now)).toBe("12 min ago")
    expect(timeAgo("2026-11-21T11:00:00Z", now)).toBe("1 hour ago")
    expect(timeAgo("2026-11-21T07:00:00Z", now)).toBe("5 hours ago")
    expect(timeAgo("2026-11-19T05:00:00Z", now)).toMatch(/Nov 19/)
    expect(timeAgo("2025-11-19T05:00:00Z", now)).toMatch(/2025/)
  })
})
