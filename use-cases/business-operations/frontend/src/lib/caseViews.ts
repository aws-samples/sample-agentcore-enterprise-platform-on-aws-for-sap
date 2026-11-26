// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { CaseStatus, EvidenceKind, type WorkItem } from "@/types/cases"
import { humanizeToken } from "@/lib/utils"
import type { ApiView } from "@/services/casesService"
import { pendingProposal } from "@/lib/writeDiff"

/**
 * The Cases views and the words a case is shown with (plan, Information architecture;
 * decisions 9 and 18). One place, so the table, the case page and the sidebar count agree.
 */

export type CaseView = "needs-you" | "team" | "not-started" | "working" | "done" | "failed" | "all"

const PARKED = [CaseStatus.AwaitingHumanInput, CaseStatus.ManualReviewRequired]

export const VIEWS: { id: CaseView; label: string; statuses: CaseStatus[] | null }[] = [
  // The cases waiting on a person that you may decide, then every one in your packs,
  // whoever may decide it: the same statuses, told apart by the server from your groups.
  { id: "needs-you", label: "Needs you", statuses: PARKED },
  { id: "team", label: "Waiting on a person", statuses: PARKED },
  { id: "not-started", label: "Not started", statuses: [CaseStatus.Detected] },
  { id: "working", label: "Agent working", statuses: [CaseStatus.Processing] },
  { id: "done", label: "Done", statuses: [CaseStatus.Complete, CaseStatus.SapUpdated] },
  { id: "failed", label: "Failed", statuses: [CaseStatus.Error] },
  { id: "all", label: "All", statuses: null },
]

/** Each view's id on `GET /cases?view=` (2.22). */
export const API_VIEW: Record<CaseView, ApiView> = {
  "needs-you": "needs_you",
  team: "team",
  "not-started": "new",
  working: "running",
  done: "done",
  failed: "failed",
  all: "all",
}

export function isView(value: string | null): value is CaseView {
  return VIEWS.some(v => v.id === value)
}

export function inView(c: WorkItem, view: CaseView): boolean {
  const statuses = VIEWS.find(v => v.id === view)?.statuses
  return !statuses || statuses.includes(c.status as CaseStatus)
}

export type Tone = "attention" | "progress" | "success" | "danger" | "neutral"

/**
 * Whether a case parked on a person waits on an answer rather than a decision. Not
 * `inquiry_sent_at`, which every park stamps. The case's ticket kind says it; a case without
 * one (no ticketing, or a ticket raised before cases carried it) is a decision when it offers
 * outcomes or a write, and otherwise a question.
 */
export function awaitsReply(c: WorkItem): boolean {
  if (c.ticket_response_type) return c.ticket_response_type === "free_text"
  return !c.decision_options?.options?.length && !pendingProposal(c.agent_traces)
}

/** What a case is waiting on, in words, with the tone its dot takes. */
export function caseState(c: WorkItem): { label: string; tone: Tone } {
  switch (c.status) {
    case CaseStatus.AwaitingHumanInput:
      return awaitsReply(c)
        ? { label: "Needs a reply", tone: "attention" }
        : { label: "Needs approval", tone: "attention" }
    case CaseStatus.ManualReviewRequired:
      return { label: "Manual review", tone: "attention" }
    case CaseStatus.Detected:
      return { label: "Not started", tone: "neutral" }
    case CaseStatus.Processing:
      return { label: "Agent working", tone: "progress" }
    case CaseStatus.Complete:
    case CaseStatus.SapUpdated:
      return { label: "Done", tone: "success" }
    case CaseStatus.Error:
      return { label: "Failed", tone: "danger" }
    default:
      return { label: humanizeToken(String(c.status)), tone: "neutral" }
  }
}

/** The background a status dot takes, from its tone. */
export const TONE_DOT: Record<Tone, string> = {
  attention: "bg-attention",
  progress: "bg-progress",
  success: "bg-success",
  danger: "bg-danger",
  neutral: "bg-neutral",
}

/**
 * A case by its document number, then the rest of its key (a fiscal year, a plant),
 * leaving out a part that is just this year (decision 18).
 */
export function documentLabel(
  c: WorkItem,
  now = new Date()
): { number: string; year: string | null } {
  const number = c.document_number ?? c.case_id
  const rest = Object.values(c.key ?? {}).filter(
    v => v && v !== number && v !== String(now.getFullYear())
  )
  return { number, year: rest.length ? rest.join(" · ") : null }
}

/** The exception the agent identified, else the type the case was opened with. */
export function exceptionLabel(c: WorkItem): string {
  const label = humanizeToken(c.exception_type ?? c.process_type)
  return label.charAt(0).toUpperCase() + label.slice(1)
}

/**
 * Who the case waits for: "You" when the person it was sent to is the one signed in,
 * otherwise the recipient's name part, taken from the last notification the agent sent.
 */
export function waitingFor(c: WorkItem, me: ReadonlySet<string>): string | null {
  if (c.status !== CaseStatus.AwaitingHumanInput) return null
  const traces = c.agent_traces ?? []
  for (let t = traces.length - 1; t >= 0; t--) {
    const segments = traces[t].segments ?? []
    for (let i = segments.length - 1; i >= 0; i--) {
      const s = segments[i]
      if (s.evidence?.kind !== EvidenceKind.Notification) continue
      let to: string | undefined
      try {
        to = JSON.parse(s.tool_input ?? "{}").to
      } catch {
        to = undefined
      }
      if (!to) return null
      if (me.has(to.toLowerCase())) return "You"
      const local = to.split("@")[0].replace(/[-_.]+/g, " ")
      return local.charAt(0).toUpperCase() + local.slice(1)
    }
  }
  return null
}

const money = new Map<string, Intl.NumberFormat>()
/** An amount with two decimals; the currency is shown beside it, muted. */
export function formatAmount(value: unknown): string | null {
  const n = typeof value === "number" ? value : typeof value === "string" ? Number(value) : NaN
  if (!Number.isFinite(n)) return null
  if (!money.has("x"))
    money.set(
      "x",
      new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })
    )
  return money.get("x")!.format(n)
}

/**
 * The pack fields a table shows beside the fixed columns: its amount (with the field that
 * holds the currency) and its first plain text field, such as a supplier. From the pack's
 * own `fields`, so no pack's attribute names are written here.
 */
export function tableFields(
  pack:
    | {
        fields: {
          attribute: string
          label: string
          type: string
          role?: string
          currency_field?: string
          panel?: boolean
        }[]
      }
    | undefined
): {
  amount?: { attribute: string; currency?: string }
  party?: { attribute: string; label: string }
} {
  if (!pack) return {}
  const amount = pack.fields.find(f => f.type === "amount")
  const currencies = new Set(pack.fields.map(f => f.currency_field).filter(Boolean))
  const party = pack.fields.find(
    f =>
      f.type === "string" &&
      f.role !== "reason" &&
      f.panel !== false &&
      !currencies.has(f.attribute)
  )
  return {
    ...(amount ? { amount: { attribute: amount.attribute, currency: amount.currency_field } } : {}),
    ...(party ? { party: { attribute: party.attribute, label: party.label } } : {}),
  }
}
