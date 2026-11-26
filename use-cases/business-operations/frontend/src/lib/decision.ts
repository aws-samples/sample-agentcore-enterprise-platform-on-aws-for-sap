// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import {
  WriteOp,
  type AgentTrace,
  type Citation,
  type ProposedWrite,
  type WorkItem,
} from "@/types/cases"
import type { Ticket } from "@/types/tickets"
import type { PackControls } from "@/services/controlsService"
import type { UseCasePack } from "@/lib/registry"
import { pendingProposal } from "@/lib/writeDiff"
import { awaitsReply, formatAmount, tableFields } from "@/lib/caseViews"
import { guardLabel } from "@/lib/packParts"

/**
 * What a person decides on a case, worked out from the case, its pack and the pack's
 * controls, kept pure so the panel and its tests read one statement of it. Nothing here names
 * a pack: outcomes, fields, limits and checks come from the API.
 */

/** The limits a deployment applies to a write, and the one currency they are in. */
export const AUTO_LIMIT = "AUTO_WRITE_LIMIT_USD"
export const DUAL_LIMIT = "DUAL_APPROVAL_LIMIT_USD"
const LIMIT_CURRENCY = "USD"

/**
 * One outcome the SOP allows for this decision (2.23): the agent records them on the case as
 * `decision_options` when it parks the case, each with the exact write it would make, or none.
 */
export interface DecisionOption {
  id: string
  label: string
  /** One line on what happens. */
  effect: string
  /** The exact change this outcome makes in SAP; absent for an outcome that makes none. */
  write?: ProposedWrite
}

/** The case's current set of options, `[]` for a case that offers none. */
export function optionsOf(c: WorkItem): DecisionOption[] {
  const set = (c as { decision_options?: { options?: unknown } }).decision_options
  const raw = set?.options
  if (!Array.isArray(raw)) return []
  return raw.filter(
    (o): o is DecisionOption =>
      !!o && typeof o === "object" && typeof o.id === "string" && typeof o.label === "string"
  )
}

/** One choice in the panel: an option from the case, or Approve, Deny or Reply only. */
export interface Outcome {
  id: string
  label: string
  summary: string
  /** What the routes record: an option is `approved` with a write, `chose` without one. */
  decision: "approved" | "denied" | "replied" | "chose"
  write: ProposedWrite | null
  /** The submit button's words, before "in production". */
  action: string
  /** Set on an option from the case (2.23); the routes take its id. */
  optionId?: string
}

/** What the panel shows. */
export type DecisionMode = "idle" | "decide" | "reply"

export function decisionMode(c: WorkItem, ticket?: Ticket | null): DecisionMode {
  if (c.status !== "awaiting_human_input") return "idle"
  if (ticket) return ticket.response_type === "free_text" ? "reply" : "decide"
  return awaitsReply(c) ? "reply" : "decide"
}

/** "Release", "Update", "Create": the change's own verb, from the write. */
export function writeVerb(write: ProposedWrite | null | undefined): string | null {
  if (!write) return null
  if (write.op === WriteOp.FunctionImport) return write.entity || null
  if (write.op === WriteOp.Create) return "Create"
  return "Update"
}

/** The outcomes on offer, in the order the panel lists them. */
export function outcomesFor(c: WorkItem, traces?: AgentTrace[]): Outcome[] {
  const options = optionsOf(c)
  if (options.length)
    return [
      ...options.map(o => ({
        id: `option:${o.id}`,
        optionId: o.id,
        label: o.label,
        summary: o.effect ?? "",
        decision: o.write ? ("approved" as const) : ("chose" as const),
        write: o.write ?? null,
        action: o.label,
      })),
      // A reply authorises nothing, so it stays beside the options (2.23).
      REPLY_ONLY,
    ]
  const pending = pendingProposal(traces ?? c.agent_traces)
  const write = pending?.proposal ?? null
  const verb = writeVerb(write)
  return [
    {
      id: "approved",
      label: "Approve",
      summary: write
        ? "The agent makes exactly this change in SAP."
        : "The agent goes on with the SOP from here.",
      decision: "approved",
      write,
      action: verb ? `Approve and ${verb.toLowerCase()}` : "Approve",
    },
    {
      id: "denied",
      label: "Deny",
      summary: "No change in SAP. The case goes to manual review.",
      decision: "denied",
      write: null,
      action: "Deny",
    },
    REPLY_ONLY,
  ]
}

const REPLY_ONLY: Outcome = {
  id: "replied",
  label: "Reply only",
  summary: "Send the agent your message without deciding.",
  decision: "replied",
  write: null,
  action: "Send reply",
}

/** The submit button's words: "in production" when the write would land there (decision 22). */
export function submitLabel(outcome: Outcome, production: boolean): string {
  return production && outcome.decision === "approved"
    ? `${outcome.action} in production`
    : outcome.action
}

/** The case's amount in the field the pack declares, with its currency. */
export function caseAmount(
  c: WorkItem,
  pack: UseCasePack | undefined
): { value: number; currency: string } | null {
  const f = tableFields(pack).amount
  if (!f) return null
  const value = Number(c.attributes?.[f.attribute])
  if (!Number.isFinite(value)) return null
  const currency = f.currency ? String(c.attributes?.[f.currency] ?? "") : ""
  return { value, currency }
}

function money(n: number, currency: string): string {
  return `${formatAmount(n)?.replace(/\.00$/, "")} ${currency}`.trim()
}

/** A reason a person decides: a SOP clause, or a limit the write crossed. */
export interface DecisionReason {
  kind: "clause" | "limit"
  text: string
  /** On a clause: the SOP's name, the quote, and where to read it. */
  sop?: string
  /** The SOP clause the quote starts in ("3.3"), when the trace recorded one. */
  section?: string | null
  citation?: Citation
  href?: string
}

/** The limit's value this stack applies, if the pack declares it. */
function limitOf(controls: PackControls | undefined, symbol: string): number | null {
  const l = controls?.limits?.[symbol]
  return l && Number.isFinite(Number(l.value)) ? Number(l.value) : null
}

/** Why the agent stopped for a person, each reason in words. */
export function decisionReasons(
  c: WorkItem,
  pack: UseCasePack | undefined,
  controls: PackControls | undefined
): DecisionReason[] {
  const out: DecisionReason[] = []
  const pending = pendingProposal(c.agent_traces)
  if (pending?.citation) {
    const sop = sopProcessType(pack, pending.sopKey)
    out.push({
      kind: "clause",
      text: pending.citation.quote,
      sop: sop ? sentence(sop) : undefined,
      section: typeof pending.citation.section === "string" ? pending.citation.section : null,
      citation: pending.citation,
      href: sop
        ? `/packs/${encodeURIComponent(c.pack_id)}/procedures/${encodeURIComponent(sop)}`
        : undefined,
    })
  }
  const amount = caseAmount(c, pack)
  const auto = limitOf(controls, AUTO_LIMIT)
  if (amount && auto != null) {
    if (amount.currency && amount.currency !== LIMIT_CURRENCY)
      out.push({
        kind: "limit",
        text: `The amount is in ${amount.currency} and this deployment's limits are in ${LIMIT_CURRENCY}, so the agent may not write it on its own.`,
      })
    else if (amount.value > auto)
      out.push({
        kind: "limit",
        text: `${money(amount.value, amount.currency || LIMIT_CURRENCY)} is above this deployment's auto-write limit of ${money(auto, LIMIT_CURRENCY)}.`,
      })
  }
  return out
}

/** `price_variance` → "Price variance". */
function sentence(id: string): string {
  const words = id.replace(/_/g, " ")
  return words.charAt(0).toUpperCase() + words.slice(1)
}

/** The process type whose SOP is `sopKey`, preferring the SOP's own name over an alias. */
function sopProcessType(pack: UseCasePack | undefined, sopKey: string | undefined): string | null {
  if (!sopKey) return null
  const rows = (pack?.sops ?? []).filter(s => s.sop === sopKey)
  const own = rows.find(s => s.kind !== "alias") ?? rows[0]
  if (own) return own.process_type
  const file = sopKey
    .split("/")
    .pop()
    ?.replace(/\.txt$/, "")
  return file || null
}

/** Whether this write needs two approvers, by the rule `decisions.needs_second_approver` uses. */
export function needsTwoApprovers(
  c: WorkItem,
  pack: UseCasePack | undefined,
  controls: PackControls | undefined
): { needed: boolean; limit: number | null } {
  const limit = limitOf(controls, DUAL_LIMIT)
  if (limit == null) return { needed: false, limit }
  if ((c as { origin?: string }).origin === "sap-push") return { needed: true, limit }
  const amount = caseAmount(c, pack)
  if (!amount) return { needed: true, limit }
  return {
    needed: amount.currency !== LIMIT_CURRENCY || amount.value > limit,
    limit,
  }
}

/** One approval recorded on the case and not yet used by a run. */
export interface Approval {
  actor: string
  name: string
  at: string
  /** The option approved, on a case with options; a second approver must pick the same one. */
  option?: string
  optionLabel?: string
}

/**
 * The approvals at the end of the case's decisions, newest first: the ones a second approval
 * would join. A run since the latest proposal, or any other decision, ends them, as
 * `decisions.approvals_of` reads the log.
 */
export function pendingApprovals(c: WorkItem): Approval[] {
  const log = c.action_log ?? []
  const since = latestTraceAt(c)
  const out: Approval[] = []
  for (let i = log.length - 1; i >= 0; i--) {
    const e = log[i] as {
      action?: string
      actor?: string
      actor_display?: string
      timestamp?: string
      consumed_by_run?: unknown
      option?: string
      option_label?: string
    }
    if (!String(e.action ?? "").startsWith("human_")) continue
    if (e.action !== "human_approved" || e.consumed_by_run) break
    if (since && (e.timestamp ?? "") < since) break
    // One per person: the API counts approvers, not approvals.
    if (out.some(a => a.actor === (e.actor ?? ""))) continue
    // Approvals of another option are not this one's: the API counts one write's digest.
    if (out.length && e.option !== out[0].option) break
    out.push({
      actor: e.actor ?? "",
      name: e.actor_display || e.actor || "Someone",
      at: e.timestamp ?? "",
      ...(e.option ? { option: e.option, optionLabel: e.option_label ?? e.option } : {}),
    })
  }
  return out
}

function latestTraceAt(c: WorkItem): string | null {
  const times = (c.agent_traces ?? [])
    .map(t => t.timestamp ?? "")
    .filter(Boolean)
    .sort()
  return times.length ? times[times.length - 1] : null
}

/** The checks that re-read SAP before the write, as a phrase: "PO reference, no duplicate". */
export function checksPhrase(controls: PackControls | undefined): string | null {
  const guards = controls?.write_guards ?? []
  if (!guards.length) return null
  return guards
    .map(g => {
      const label = guardLabel(g)
      return /^[A-Z]{2,}\b/.test(label) ? label : label.charAt(0).toLowerCase() + label.slice(1)
    })
    .join(", ")
}
