// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import type { WorkItem } from "@/types/cases"

/**
 * Reading a case's pack attributes.
 *
 * `attributes` is an open map (types/cases.schema.json): the poller writes whatever
 * its pack's `field_map` declared, so the schema does not name `amount` or
 * `supplier_number` and neither does `WorkItem`. Every read goes through here because
 * the values are typed `string | number | boolean | null`, and a panel that wants a
 * money figure or a date needs one narrowed value, not a union it has to re-check at
 * every call site.
 *
 * There is no fallback to a top-level position: every case keeps these values in
 * `attributes`.
 */

/** A text attribute, or null when it is absent, empty, or not text. */
export function attrString(c: WorkItem, name: string): string | null {
  const v = c.attributes?.[name]
  if (typeof v === "string") return v || null
  if (typeof v === "number" || typeof v === "boolean") return String(v)
  return null
}

/**
 * A numeric attribute, or null when it is absent or not a number.
 *
 * Numeric strings are parsed: DynamoDB round-trips a Decimal through the API as a
 * JSON number, but a case seeded by the eval test harness writes the amount as a string,
 * and a figure that arrived as `"1234.50"` is still a figure.
 */
export function attrNumber(c: WorkItem, name: string): number | null {
  const v = c.attributes?.[name]
  if (typeof v === "number") return Number.isFinite(v) ? v : null
  if (typeof v === "string" && v.trim() !== "") {
    const n = Number(v)
    return Number.isFinite(n) ? n : null
  }
  return null
}

/** A date attribute reduced to its `YYYY-MM-DD` head, or null. */
export function attrDate(c: WorkItem, name: string): string | null {
  return attrString(c, name)?.split("T")[0] ?? null
}

/**
 * Every attribute the case actually carries a value for.
 *
 * For readers that render or narrate a case without knowing its pack: absent and
 * empty values are dropped, because a label over nothing is noise in a panel and a
 * wasted token in a prompt.
 */
export function caseAttributes(c: WorkItem): Record<string, string | number | boolean> {
  const out: Record<string, string | number | boolean> = {}
  for (const [name, v] of Object.entries(c.attributes ?? {})) {
    if (v === null || v === undefined || v === "") continue
    if (typeof v === "string" || typeof v === "number" || typeof v === "boolean") out[name] = v
  }
  return out
}
