// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import type { PackField, UseCasePack } from "@/lib/registry"
import { shortDate } from "@/lib/timeAgo"

/** One field the agent read in SAP, as the Evidence tab shows it. */
export interface EvidenceRow {
  /** A sans label: the pack's for a field it declares, else the SAP name in words. */
  label: string
  /** The SAP field name, for the label's tooltip. */
  sapName: string
  value: string
  /** Mono: the value is an identifier (a document, order or supplier number). */
  mono: boolean
  /** Right-aligned tabular figures: an amount or a quantity. */
  numeric: boolean
}

const AMOUNT_NAME = /(Amount|Price|Value)$/
const QUANTITY_NAME = /Quantity/
const CURRENCY_NAME = /Currency$/
const ID_NAME = /(Invoice|Order|Item|Supplier|Party|Document|Number|Code|ID|Id|Key|ByInvcgParty)$/
const SAP_DATE = /^\/Date\((-?\d+)\)\/$/
const ISO_DATE = /^\d{4}-\d{2}-\d{2}(T|$)/

/** "A_SupplierInvoice" → "Supplier invoice": an entity set without its API prefix. */
export function entityWords(entity: string): string {
  return sapWords(entity.replace(/^A_/, ""))
}

/** "InvoiceGrossAmount" → "Invoice gross amount"; acronyms stay upper case ("PO"). */
export function sapWords(name: string): string {
  const words = name
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/([A-Z]+)([A-Z][a-z])/g, "$1 $2")
    .split(/[\s_]+/)
    .filter(Boolean)
  return words
    .map((w, i) =>
      /^[A-Z]{2,}$/.test(w) ? w : i === 0 ? w.charAt(0).toUpperCase() + w.slice(1) : w.toLowerCase()
    )
    .join(" ")
}

const figures = new Intl.NumberFormat(undefined, {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})
const plain = new Intl.NumberFormat(undefined, { maximumFractionDigits: 3 })

/** "52,500.00 USD": two decimals, the ISO currency after the number. */
export function money(value: string | number, currency?: string | null): string | null {
  const n = typeof value === "number" ? value : Number(value)
  if (!Number.isFinite(n)) return null
  return currency ? `${figures.format(n)} ${currency}` : figures.format(n)
}

function asDate(value: string): string | null {
  const sap = SAP_DATE.exec(value)
  const d = sap ? new Date(Number(sap[1])) : ISO_DATE.test(value) ? new Date(value) : null
  return d && !Number.isNaN(d.getTime()) ? shortDate(d) : null
}

function lastSegment(path: string): string {
  return path ? (path.split(".").pop() ?? "") : ""
}

/**
 * The fields of one SAP read, with the pack's labels where it declares the field and plain
 * words where it does not. An amount takes the currency read beside it (or the case's), and
 * that currency field is folded into it rather than shown again.
 */
export function evidenceRows(
  fields: { name: string; value?: string }[],
  pack: UseCasePack | undefined,
  caseCurrency?: string | null
): EvidenceRow[] {
  const declared = new Map<string, PackField>()
  for (const f of pack?.fields ?? []) {
    const name = lastSegment(f.sap_field)
    if (name) declared.set(name, f)
  }
  const byAttribute = new Map((pack?.fields ?? []).map(f => [f.attribute, f]))
  const currencyRead = fields.find(f => CURRENCY_NAME.test(f.name))
  const currency = currencyRead?.value || caseCurrency || null
  const folded = new Set<string>()

  const rows = fields.map(f => {
    const field = declared.get(f.name)
    const raw = f.value ?? ""
    const label = field?.label ?? sapWords(f.name)
    const isAmount = field ? field.type === "amount" : AMOUNT_NAME.test(f.name)
    if (isAmount) {
      const cf = field?.currency_field ? byAttribute.get(field.currency_field) : undefined
      const ownCurrency = cf
        ? (fields.find(x => x.name === lastSegment(cf.sap_field))?.value ?? currency)
        : currency
      const shown = money(raw, ownCurrency)
      if (shown) {
        if (currencyRead) folded.add(currencyRead.name)
        return { label, sapName: f.name, value: shown, mono: false, numeric: true }
      }
    }
    if (field?.type === "decimal" || QUANTITY_NAME.test(f.name)) {
      const n = Number(raw)
      if (raw !== "" && Number.isFinite(n))
        return { label, sapName: f.name, value: plain.format(n), mono: false, numeric: true }
    }
    const date = field?.type === "date" || /Date$/.test(f.name) ? asDate(raw) : null
    if (date) return { label, sapName: f.name, value: date, mono: false, numeric: false }
    const mono = raw !== "" && ID_NAME.test(f.name) && /^[\w/-]+$/.test(raw)
    return { label, sapName: f.name, value: raw || "—", mono, numeric: false }
  })
  return rows.filter(r => !folded.has(r.sapName))
}
