// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import type { PackAccess, PackSop, SopKind, UseCasePack } from "@/lib/registry"
import { humanise } from "@/lib/registry"

/**
 * The pack's five parts as the console names them (docs/packs.md, Pack parts), who owns each
 * (docs/roles.md), and what the signed-in person may do with each, from
 * `GET /registry` `access`. Nothing here names a pack, a field or a rule: those come from the
 * API.
 */

export type PackTab =
  "overview" | "intake" | "procedures" | "controls" | "agent" | "evaluations" | "versions"

export const PACK_TABS: { id: PackTab; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "intake", label: "Intake" },
  { id: "procedures", label: "Procedures" },
  { id: "controls", label: "Controls" },
  { id: "agent", label: "Agent configuration" },
  { id: "evaluations", label: "Evaluations" },
  { id: "versions", label: "Versions" },
]

export function isPackTab(v: string | null | undefined): v is PackTab {
  return PACK_TABS.some(t => t.id === v)
}

/** The tabs that are a pack part: every tab but the Overview and the Versions. */
export type PartTab = Exclude<PackTab, "overview" | "versions">

/** The pack parts as a version names them (`parts`), in the reader's words. */
export const VERSION_PART_LABEL: Record<string, string> = {
  intake: "Intake",
  procedures: "Procedures",
  controls: "Controls",
  agent_configuration: "Agent configuration",
  evaluations: "Evaluations",
}

/** Who changes each part, in the roles' own names. */
export const PART_OWNER: Record<PartTab, string> = {
  intake: "The SAP functional consultant and the AI engineer change it, with the process owner.",
  procedures: "The process owner and process analysts change it.",
  controls: "The control owner decides; a pack administrator makes the change in development.",
  agent: "The AI engineer changes it.",
  evaluations: "Process analysts change the example cases, with the AI engineer.",
}

/** May this person change anything in the part? */
export function mayEditPart(access: PackAccess | undefined, part: PartTab): boolean {
  if (!access) return false
  const e = access.edit
  switch (part) {
    case "intake":
      return e.intake
    case "procedures":
      return e.procedures_text || e.rule_values || e.procedures_structure
    case "controls":
      return e.controls
    case "agent":
      return e.agent_configuration
    case "evaluations":
      return !!e.example_cases || e.evaluations
  }
}

/** What "You can edit" covers, when the person may change only some of a part. */
export function editScope(access: PackAccess | undefined, part: PartTab): string | null {
  if (!access || !mayEditPart(access, part)) return null
  const e = access.edit
  if (part === "procedures" && !e.procedures_structure)
    return "SOP text and rule values. Which SOP handles which exception type, and the outcomes, need a pack administrator."
  if (part === "evaluations" && !e.evaluations)
    return "Example cases. The sandbox seed recipe needs a pack administrator."
  return null
}

/**
 * Where a person lands on a pack: a process analyst on Procedures, everyone else on Overview
 * (decision 17), unless they chose a tab before. There is no analyst group, so an analyst is
 * someone who may write SOP text but is neither an operator nor an approver here.
 */
export function landingTab(pack: UseCasePack | undefined, last: string | null): PackTab {
  if (isPackTab(last)) return last
  const a = pack?.access
  if (a?.edit.procedures_text && !a.set_trigger_mode && !a.approve) return "procedures"
  return "overview"
}

export const KIND_LABEL: Record<SopKind, string> = {
  triage: "Triage",
  exception: "Exception types",
  shared: "Shared",
  alias: "Other names",
}

/** When the agent uses a SOP of each kind. */
export function sopUse(sop: PackSop): string {
  switch (sop.kind) {
    case "triage":
      return "Every case starts here"
    case "exception":
      return "When triage finds this exception type"
    case "shared":
      return "Loaded by another SOP"
    case "alias":
      return `Opens the ${humanise(sop.alias_of ?? "")} SOP`
  }
}

/** One sentence for a SOP page's header. */
export function sopKindSentence(sop: PackSop, all: PackSop[]): string {
  const aliases = all.filter(s => s.kind === "alias" && s.alias_of === sop.process_type)
  const also = aliases.length
    ? ` Also answers to ${aliases.map(a => humanise(a.process_type)).join(" and ")}.`
    : ""
  switch (sop.kind) {
    case "triage":
      return `Triage SOP. Every case starts here: it identifies the exception type and hands over to that type's SOP.${also}`
    case "exception":
      return `Exception SOP. Used when triage finds a ${humanise(sop.process_type).toLowerCase()}.${also}`
    case "shared":
      return `Shared SOP. Never chosen by triage: another SOP loads it.${also}`
    case "alias":
      return `Another name for the ${humanise(sop.alias_of ?? "")} SOP.`
  }
}

/** The SOPs in the order the Procedures part lists them. */
export function sopsByKind(pack: UseCasePack | undefined): [SopKind, PackSop[]][] {
  const sops = pack?.sops ?? []
  const order: SopKind[] = ["triage", "exception", "shared", "alias"]
  return order
    .map(k => [k, sops.filter(s => s.kind === k)] as [SopKind, PackSop[]])
    .filter(([, list]) => list.length > 0)
}

/** `PRICE_VARIANCE_ABS_USD` → "Price variance abs", with its unit read off the suffix. */
export function ruleLabel(symbol: string): string {
  if (RULE_NAMES[symbol]) return RULE_NAMES[symbol]
  const words = symbol
    .replace(/_(PCT|USD|DAYS|UNITS)$/, "")
    .toLowerCase()
    .split("_")
    .map(w => RULE_WORDS[w] ?? w)
    .join(" ")
    .replace("auto- ", "auto-")
  return words.charAt(0).toUpperCase() + words.slice(1)
}

/** Symbols whose words name the concept differently from the rest of the console. */
const RULE_NAMES: Record<string, string> = {
  DUAL_APPROVAL_LIMIT_USD: "Two-approver limit",
}

/** Short forms in rule symbols, in the words a reader uses. */
const RULE_WORDS: Record<string, string> = {
  abs: "absolute",
  qty: "quantity",
  gr: "GR",
  po: "PO",
  auto: "auto-",
}

export function formatRule(symbol: string, value: unknown): string {
  if (value == null || value === "") return "Not set"
  const n = Number(value)
  if (!Number.isFinite(n)) return String(value)
  const shown = n.toLocaleString("en-US", { maximumFractionDigits: 2 })
  if (symbol.endsWith("_PCT")) return `${shown} %`
  if (symbol.endsWith("_USD"))
    return `${n.toLocaleString("en-US", { minimumFractionDigits: n % 1 ? 2 : 0 })} USD`
  if (symbol.endsWith("_DAYS")) return `${shown} day${n === 1 ? "" : "s"}`
  if (symbol.endsWith("_UNITS")) return `${shown} unit${n === 1 ? "" : "s"}`
  return shown
}

/** `{{SYMBOL}}` names a SOP cites, in order of first use. */
export function citedSymbols(text: string): string[] {
  return [...new Set([...text.matchAll(/\{\{([A-Z][A-Z0-9_]*)\}\}/g)].map(m => m[1]))]
}

/** Words a check id abbreviates, kept upper case. The API names checks by id only. */
const ACRONYMS = new Set(["po", "gr", "sap", "id", "uom", "usd", "vat", "iban"])

/**
 * A write check's name made readable: `within_tolerance_or_approved` → "Within tolerance or
 * approved", `po_reference` → "PO reference".
 */
export function guardLabel(guard: string): string {
  const words = guard.split("_").filter(Boolean)
  return words
    .map((w, i) => {
      if (ACRONYMS.has(w.toLowerCase())) return w.toUpperCase()
      const lower = w.toLowerCase()
      return i === 0 ? lower.charAt(0).toUpperCase() + lower.slice(1) : lower
    })
    .join(" ")
}

/** Identity-provider groups, without the use case's prefix. */
export function groupLabel(group: string): string {
  return group.replace(/^business-operations-/, "")
}

/**
 * The SOPs a rule's `cited_by` names, once each: an alias is listed beside the type it aliases
 * (both run that SOP), so it folds into the type that owns the file. `null` when the stack
 * stores no version yet, and the API could not say.
 */
export function citingSops(
  citedBy: string[] | null | undefined,
  sops: PackSop[]
): { processType: string; name: string }[] | null {
  if (citedBy == null) return null
  const owner = (pt: string) => sops.find(s => s.process_type === pt)?.alias_of ?? pt
  return [...new Set(citedBy.map(owner))].map(pt => ({ processType: pt, name: humanise(pt) }))
}
