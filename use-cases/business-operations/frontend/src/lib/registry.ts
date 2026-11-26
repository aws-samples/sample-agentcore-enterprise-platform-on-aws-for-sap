// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import type { WorkItem } from "@/types/cases"
import { attrDate, attrNumber, attrString, caseAttributes } from "@/lib/caseAttributes"
import { timeAgo } from "@/lib/timeAgo"

/**
 * The use case corpus as `GET /registry` serves it, and everything the UI derives
 * from it.
 *
 * These shapes used to be three hard-coded maps in this repo — `DOMAIN_META`,
 * `DOMAIN_ROWS` and `DOMAIN_SOURCE` — so a new use case could not render its own
 * columns without a code edit and a frontend build. They are declarations now, read
 * from the published registry, and a pack this build has never heard of renders its
 * own attributes rather than borrowing another pack's labels.
 *
 * Nothing here fetches. These are pure functions over a resolved pack so a panel can
 * be rendered in a test without a network, and so the same field list backs the case
 * table and the detail panel.
 */

/** What a renderer can do with a value. Mirrors `type` in `packs/<pack>/pack.json`'s `fields`. */
export type FieldType = "string" | "date" | "amount" | "decimal"

/** One attribute a pack declares, joined to the SAP path it was read from. */
export interface PackField {
  /** The key inside a case's `attributes` map. */
  attribute: string
  label: string
  type: FieldType
  /** Empty for an attribute the poller derives rather than reads from a SAP path. */
  sap_field: string
  /** On an `amount`: the attribute holding the currency to render it in. */
  currency_field?: string
  /** What this attribute means to a reader beyond its label. `reason` is why the case is an exception. */
  role?: "reason"
  /** Present, and false, when the field is deliberately kept out of the detail panel. */
  panel?: boolean
}

export interface UseCasePack {
  id: string
  display_name: string
  description?: string
  /** What to call this pack's queue of cases. */
  label: string
  /** The prefix every `case_id` from this pack starts with. Empty if it has no source. */
  short_id: string
  process_types: string[]
  /**
   * Its `source` still carries a survey marker, so nothing polls it and no case routes to
   * it. Published by the registry, which is the one place that looks for the marker.
   */
  incomplete?: boolean
  source: {
    kind?: string
    service?: string
    entity?: string
    /**
     * The natural key's field names, in the order the pack's `source` block declared them.
     *
     * A case carries the same names as a *map*, and DynamoDB does not preserve a map's
     * key order — so this array, not that map, is what says which value comes first.
     * Empty on a pack with no source, and on any stack that has not published since
     * this was added; `caseHeadline` falls back to arrival order either way.
     */
    key?: string[]
  }
  /** Declaration order, which is the column order a table should render. */
  fields: PackField[]
  /** One row per process type the pack maps, with the part its SOP plays. */
  sops?: PackSop[]
  /** The SAP system the pack reads and writes, with its system role; null when it names none
   *  and the use case reaches more than one. */
  sap_system?: { name: string; system_role: string } | null
  /** What the signed-in caller may do in this pack, by part. For badges and disabled
   *  controls only: every route makes its own decision. */
  access?: PackAccess
}

/**
 * The part a SOP plays: `triage` is where every case starts, an `exception` SOP resolves one
 * exception type, a `shared` one is loaded from another SOP, and an `alias` is a second
 * process type for the same file (`alias_of` names the first).
 */
export type SopKind = "triage" | "exception" | "shared" | "alias"

export interface PackSop {
  process_type: string
  kind: SopKind
  /** Bucket key, `<pack_id>/<file>`. */
  sop: string
  alias_of?: string
}

export interface PackAccess {
  see: boolean
  edit: {
    intake: boolean
    procedures_text: boolean
    rule_values: boolean
    procedures_structure: boolean
    controls: boolean
    agent_configuration: boolean
    /** The seed recipe, which is structure. */
    evaluations: boolean
    /** Example cases, which are content. */
    example_cases?: boolean
  }
  lower_limits: boolean
  set_trigger_mode: boolean
  approve: boolean
  /**
   * Process and Start runs on a case: the case workers group, narrowed by the pack's
   * `entitlement.process_groups` (round seven, 7.1). Deciding keeps `approve`.
   */
  process: boolean
  rehearse: boolean
}

/**
 * Whether the caller may start the agent on the pack's cases. With no `access` at all the
 * console does not guess, and the route decides.
 */
export function mayProcess(pack: UseCasePack | undefined): boolean {
  return !pack?.access || pack.access.process
}

/** True when the caller may edit any part of the pack. */
export function canEditPack(pack: UseCasePack): boolean {
  return !!pack.access && Object.values(pack.access.edit).some(Boolean)
}

/** True when the pack's SAP system is not a production one (decision 22). An unknown
 *  system role counts as production, so a missing value can only hide a marker. */
export function isNonProduction(pack: UseCasePack | undefined): boolean {
  const role = pack?.sap_system?.system_role
  return role === "sandbox" || role === "development" || role === "quality"
}

/** Packs by id, for the per-case lookups every renderer does. */
export type PackIndex = Record<string, UseCasePack>

export function indexPacks(packs: UseCasePack[]): PackIndex {
  return Object.fromEntries(packs.map(p => [p.id, p]))
}

/** `material_master` → `Material master`, `uom_mismatch` → `UoM mismatch`: an id made readable, in sentence case. */
export function humanise(id: string): string {
  const words = id.split("_").filter(Boolean)
  return words
    .map((word, i) => {
      const upper = ACRONYMS[word.toLowerCase()]
      if (upper) return upper
      return i === 0 ? word.charAt(0).toUpperCase() + word.slice(1) : word.toLowerCase()
    })
    .join(" ")
}

/** Words an id spells in lower case that a reader knows in capitals. */
const ACRONYMS: Record<string, string> = {
  ap: "AP",
  api: "API",
  ar: "AR",
  gr: "GR",
  odata: "OData",
  pii: "PII",
  po: "PO",
  sap: "SAP",
  sop: "SOP",
  sops: "SOPs",
  uom: "UoM",
  usd: "USD",
}

export interface PackMeta {
  id: string
  label: string
  /** What fits in a tab or a pill. */
  short: string
  /** False when the id is not in the registry — the caller may want to say so. */
  known: boolean
}

/**
 * What to show for a pack id, for any id at all.
 *
 * `short` is the pack's `short_id` — the same prefix its case ids carry, so the tab an
 * operator clicks and the `ap-…` in front of every case are the same two letters. A
 * pack with no source has no prefix and falls back to its display name; an
 * unrecognised id renders as its own humanised self and never borrows another pack's
 * label, which is what a `Record` lookup with a default would do.
 */
export function describePack(id: string | undefined, packs: PackIndex): PackMeta {
  const trimmed = (id ?? "").trim()
  if (!trimmed) return { id: "", label: "Unknown", short: "—", known: false }

  const pack = packs[trimmed]
  if (pack) {
    const label = pack.display_name || humanise(trimmed)
    return {
      id: trimmed,
      label,
      short: pack.short_id ? pack.short_id.toUpperCase() : label,
      known: true,
    }
  }

  const label = humanise(trimmed)
  return { id: trimmed, label, short: label, known: false }
}

/**
 * A pack whose SAP paths are still markers — what `POST /packs` mints, and what
 * a use-case scoping server emits. **It is inert, not broken:** the server keeps it
 * out of `sources()` (never polled) and empties its `sops` (no `process_type` routes to it),
 * so it can hold a whole use case's content while the survey that resolves its markers is
 * still outstanding. Its constants are live and its content is editable.
 *
 * The registry's own `marked` flag, never a scan here: one place decides.
 */
export function isIncomplete(pack: UseCasePack | undefined): boolean {
  return pack?.incomplete === true
}

/**
 * One line saying where a case panel's figures came from. Four outcomes, because
 * there are four states and two of them used to render as noise or as nothing:
 *
 *   pack resolves, declares a source  →  "ZERP_AP_SRV · ApInvoiceHeader"
 *   pack resolves, still marked       →  "Awaiting a SAP survey, so nothing is polled yet"
 *   pack resolves, declares no source →  "Accruals · no upstream service declared"
 *   no pack in the registry           →  "Accruals · not in this deployment's registry"
 *
 * In the third case a pack has `kind`, `service` and `entity` all null, and a
 * `.filter(Boolean).join(" · ")` over two nulls is the empty string. A blank provenance line reads as a load failure
 * beside fields that populated fine; "no upstream service" is a fact about the pack. The
 * marked case reads as `<AWAITING_SURVEY> · <AWAITING_SURVEY>` unless it is named, which
 * is a raw sentinel where the operator is looking for a reason.
 */
export function sourceProvenance(id: string | undefined, packs: PackIndex): string {
  const meta = describePack(id, packs)
  if (!meta.known) return `${meta.label} · not in this deployment's registry`

  const pack = packs[meta.id]
  if (isIncomplete(pack)) return "Awaiting a SAP survey, so nothing is polled yet"
  const named = [pack.source.service, pack.source.entity].filter(Boolean).join(" · ")
  return named || `${meta.label} · no upstream service declared`
}

/**
 * A money figure, or an em-dash when there is none: two decimals always, so a column of
 * amounts lines up, and the ISO currency after the number when there is one ("1,000.00
 * EUR"), as every page shows amounts. Without one the figure stands alone; the caller shows
 * the case's own currency beside it.
 */
export function formatAmount(n?: number | null, currency?: string | null): string {
  if (n == null) return "—"
  const figure = n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  return currency ? `${figure} ${currency}` : figure
}

/**
 * The pack's money field, if it has one: which attribute holds the figure and which
 * holds its currency.
 *
 * The first `amount` declaration wins. A pack that carries no money has none, and
 * every caller renders no figure rather than a zero — the case was not told what it
 * was worth, which is different from being worth nothing.
 */
export function moneyField(pack?: UseCasePack): PackField | undefined {
  return pack?.fields.find(f => f.type === "amount")
}

/** The case's money, resolved through its own pack's declaration. */
export function caseMoney(
  c: WorkItem,
  packs: PackIndex
): { amount: number | null; currency: string | null } {
  const field = moneyField(packs[c.pack_id])
  if (!field) return { amount: null, currency: null }
  return {
    amount: attrNumber(c, field.attribute),
    currency: field.currency_field ? attrString(c, field.currency_field) : null,
  }
}

/**
 * Why this case is an exception, in the pack's own words, or null when it never said.
 *
 * A pack marks one field `"role": "reason"` — for AP that is the payment block. With
 * none declared, callers fall back to the `process_type`, which is a classification
 * rather than a cause but is at least always true.
 */
export function caseReason(c: WorkItem, packs: PackIndex): string | null {
  const field = packs[c.pack_id]?.fields.find(f => f.role === "reason")
  return field ? attrString(c, field.attribute) : null
}

export interface DisplayField {
  label: string
  value: string
  /**
   * The precise time behind an approximated `value`, for its tooltip. A
   * relative age is scannable; the stamp it came from still has to be recoverable.
   */
  exact?: string
  /**
   * The SAP field this figure was read from, or `undefined` when the case record is
   * itself the source. A figure with no entry renders unattributed rather than
   * guessing.
   */
  sapField?: string
}

/**
 * The SAP-side name, without the poller's own scope prefix.
 *
 * `field_map` paths read `parent.InvoicingParty` — `parent` and `child` say which
 * record of an expanded response the value came from, which is this repo's
 * vocabulary and not something an operator can look up in SAP. The name after it is.
 */
function sapFieldName(path: string): string | undefined {
  return path ? path.split(".").pop() : undefined
}

function renderValue(c: WorkItem, field: PackField): string {
  switch (field.type) {
    case "amount": {
      const currency = field.currency_field ? attrString(c, field.currency_field) : null
      return formatAmount(attrNumber(c, field.attribute), currency)
    }
    case "date":
      return attrDate(c, field.attribute) ?? "—"
    case "decimal":
      return attrNumber(c, field.attribute)?.toLocaleString("en-US") ?? "—"
    default:
      return attrString(c, field.attribute) ?? "—"
  }
}

/**
 * Every attribute the poller wrote, for a case whose pack this deployment does not
 * have — a record left behind by a pack that has since been unpublished, or one from
 * a registry this build could not fetch.
 *
 * No `sapField`: the source path is a pack declaration, so with no pack there is
 * nothing to attribute to and claiming one would be a guess.
 */
function genericFields(c: WorkItem): DisplayField[] {
  return Object.entries(caseAttributes(c))
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([attribute, value]) => ({ label: humanise(attribute), value: String(value) }))
}

/**
 * The case's detail fields, in its pack's declared order, each with its origin.
 *
 * Fields the pack marked `panel: false` are left out: they are declared so the poller
 * writes them and the agent can read them, not so they compete for space in a panel.
 */
export function caseFields(c: WorkItem, packs: PackIndex): DisplayField[] {
  // `Created` and `Updated` are our own bookkeeping, so they carry no `sapField`.
  // Claiming an SAP origin for them is the same defect as leaving a real SAP
  // figure unattributed.
  const common: DisplayField[] = [
    { label: "Created", value: timeAgo(c.created_at), exact: c.created_at },
    { label: "Updated", value: timeAgo(c.updated_at), exact: c.updated_at },
  ]

  const pack = packs[c.pack_id]
  if (!pack) return [...genericFields(c), ...common]

  const declared = pack.fields
    .filter(f => f.panel !== false)
    .map(field => ({
      label: field.label,
      sapField: sapFieldName(field.sap_field),
      value: renderValue(c, field),
    }))

  return [...declared, ...common]
}
