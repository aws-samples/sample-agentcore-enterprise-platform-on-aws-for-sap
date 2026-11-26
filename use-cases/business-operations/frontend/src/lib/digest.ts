// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { CaseStatus } from "@/types/cases"
import type { AgentTrace, Citation, WorkItem } from "@/types/cases"
import type { Ticket } from "@/types/tickets"
import { caseHeadline } from "@/lib/caseKey"
import { caseMoney, caseReason, type PackIndex } from "@/lib/registry"
import { citationOf, segmentsOf } from "@/lib/caseTimeline"
import { isUnattended } from "@/lib/transports"

/**
 * What happened in a window, derived from stored cases alone.
 *
 * One derivation, two consumers: the shift handover renders it as rows, the briefing
 * narrates it as a period. Neither can disagree with the other about the same numbers,
 * and neither calls a model to produce them.
 *
 * Every field here is either grounded in a stored value or carries a flag saying what
 * is missing. A figure the data cannot support does not get computed and softened —
 * it gets a partial flag, and the panel says so.
 */

/** Currency bucket for a posted case whose `DocumentCurrency` was not polled. */
export const UNKNOWN_CURRENCY = "unknown"

/** Group label for awaiting cases with no ticket, and so no recorded recipient. */
export const UNRECORDED_OWNER = "recipient not recorded"

/** Statuses that mean the case left the queue having been written to SAP. */
const TERMINAL: readonly CaseStatus[] = [CaseStatus.Complete, CaseStatus.SapUpdated]

export interface DigestRow {
  /** Canonical case identity — this is also the workspace's `?case=` key. */
  caseId: string
  /**
   * The same identity as an operator recognises it — `caseHeadline`, carried on the row
   * because a row is all its consumers get.
   *
   * A minted `caseId` is a digest, so it is the wrong thing to *print* and still the right
   * thing to click with. Derived once here, where the whole `WorkItem` is in hand, rather
   * than by threading the record through two panels that need nothing else from it.
   */
  headline: string
  amount?: number | null
  currency?: string | null
  /** Why it is here: the exception for a waiting case, the run outcome for a blocked one. */
  reason: string
  /** Timestamp the age is measured from. */
  since: string
  /**
   * Which timestamp `since` is. `inquiry` means someone was actually asked at that
   * moment; `activity` means only that we touched the case then. The row's label has
   * to track this — "waiting 6d" and "last activity 6d" are different claims.
   */
  ageSource: "inquiry" | "activity"
  /**
   * The SOP sentence the run quoted, on `posted` rows only.
   *
   * A field one row kind carries rather than a second row type, the way `DiffRow.observed`
   * is: every other consumer of a `DigestRow` renders a case somebody still has to act on,
   * and the clause is only interesting once nobody does. Absent means the agent quoted
   * nothing gradeable — which the render states rather than hides.
   */
  clause?: Citation
  /**
   * The SOP that run was given, paired with `clause` so the sentence can be opened where
   * it lives — see `sopLink`. On the trace rather than the evidence, so only the walk that
   * found the clause can carry it; absent on a run whose SOP did not load.
   */
  sopKey?: string
}

export interface DigestGroup {
  /** Who holds it, or what kind of work it is when nobody is recorded. */
  label: string
  /**
   * False when `label` is not a person: either no ticket recorded the recipient, or
   * ticketing is off and the grouping fell back to `process_type`. The panel must not
   * present these as someone to chase.
   */
  ownerKnown: boolean
  rows: DigestRow[]
}

export interface Digest {
  /** Terminal cases the agent closed with no human in the loop. */
  postedCount: number
  /**
   * Those same cases as rows, newest first, each carrying the clause it cleared under.
   *
   * Newest first rather than `byOldestFirst`: for a case still open the age is what it has
   * cost, so the oldest is the one to look at; for a posted one the age is when it happened,
   * and the manager reading this wants what the agent did overnight in the order it did it.
   */
  posted: DigestRow[]
  /**
   * Value posted, per currency, in whichever field each pack declared as its money.
   * Never one scalar: a pack that declares a `currency_field` records a currency per
   * case, so a single sum across currencies is wrong rather than imprecise.
   */
  postedValue: Map<string, number>
  /**
   * True whenever any posted case carried an amount. A pack's amount is normally cast
   * with `abs_decimal`, so a reversal sums in the same direction as the document it
   * reverses and the total is a magnitude, not a net.
   */
  signUnknown: boolean
  /** A posted case had no amount at all, so the value is under-counted. */
  valuePartial: boolean
  /** Awaiting cases, grouped by who holds them. */
  waiting: DigestGroup[]
  /** Total across `waiting`, so a caller need not re-sum the groups. */
  waitingCount: number
  /** Cases the agent could not clear. */
  blocked: DigestRow[]
  /** Agent spend over the window, in USD. */
  spend: number
  /** An in-window case had no `cost_summary`, so `spend` is a floor, not a total. */
  spendPartial: boolean
  /** Grouping fell back to `process_type` because no tickets were available. */
  groupedByProcess: boolean
  /** Cases in the window at all — zero means "nothing happened", not "nothing to show". */
  total: number
}

/**
 * The window vocabulary, shared by every digest consumer.
 *
 * `label` is the selector option; `prose` is the same window inside a sentence. Both
 * live here because two lists of the same four windows is how the handover and the
 * briefing end up describing different periods with the same numbers.
 */
export const WINDOWS = [
  { value: "1", label: "Last 1h", prose: "last hour" },
  { value: "6", label: "Last 6h", prose: "last 6 hours" },
  { value: "24", label: "Last 24h", prose: "last 24 hours" },
  { value: "72", label: "Last 3d", prose: "last 3 days" },
] as const

/** The window as it reads inside a sentence, for an hours value off the selector. */
export function windowProse(hours: number | string): string {
  return WINDOWS.find(w => w.value === String(hours))?.prose ?? `last ${hours} hours`
}

/** The most recent trace, or undefined for a case that has never been run. */
function lastTrace(c: WorkItem) {
  const traces = Array.isArray(c.agent_traces) ? c.agent_traces : []
  if (traces.length === 0) return undefined
  return traces.reduce((latest, t) =>
    (t.timestamp ?? "").localeCompare(latest.timestamp ?? "") > 0 ? t : latest
  )
}

/**
 * The last SOP sentence a run quoted, scanned from the end.
 *
 * From the end because `merge_evidence` attaches `citation` to `case_update` and
 * `notification` evidence and to nothing else — never to the `sap_write` itself — so for a
 * case the agent closed unattended the clause lives on the `case_update` that closed it,
 * which is the last of those in the run. Taking the first hit instead would report whatever
 * rule the agent quoted while it was still deciding.
 */
function clauseOf(trace: AgentTrace | undefined): Citation | undefined {
  if (!trace) return undefined
  const segments = segmentsOf(trace)
  for (let i = segments.length - 1; i >= 0; i--) {
    const found = citationOf(segments[i]?.evidence)
    if (found) return found
  }
  return undefined
}

function toRow(c: WorkItem, reason: string, packs: PackIndex): DigestRow {
  // `inquiry_sent_at` is when a human was asked; `updated_at` is when we last wrote the
  // record. Preferring the first is the whole point, and recording which one we used is
  // what stops the panel labelling the second as a wait.
  const inquiry = c.inquiry_sent_at ?? null
  // Read through the case's own pack, so absent for a pack that declares no money —
  // which the row renders as no figure rather than as a zero.
  const money = caseMoney(c, packs)
  return {
    caseId: c.case_id,
    headline: caseHeadline(c, packs[c.pack_id]),
    amount: money.amount,
    currency: money.currency,
    reason,
    since: inquiry ?? c.updated_at,
    ageSource: inquiry ? "inquiry" : "activity",
  }
}

/**
 * Oldest first — the case that has waited longest is the one costing money.
 *
 * Exported because the briefing's queue inherits this ordering rather than re-deriving it.
 * Two comparators for one rule is how two surfaces come to disagree about what "next" means.
 */
export function byOldestFirst(a: DigestRow, b: DigestRow): number {
  return (a.since ?? "").localeCompare(b.since ?? "")
}

/**
 * Derive the window's digest.
 *
 * @param cases - Whole case records, as `GET /cases` returns them.
 * @param sinceHours - Window width. Matches the Analytics hours selector's values.
 * @param tickets - Optional, and last: the briefing consumer does not need owner
 * grouping, and with `demo.ticketing` off there are none to pass. Absent, awaiting
 * cases group by `process_type` and `groupedByProcess` says so.
 * @param packs - The registry, which is what says which attribute holds a case's money
 * and which holds its reason. Absent — or unreachable — every case reads as carrying
 * neither, so amounts render as no figure and `valuePartial` says the total is a floor.
 */
export function digest(
  cases: WorkItem[],
  sinceHours: number,
  tickets?: Ticket[],
  packs: PackIndex = {}
): Digest {
  const cutoff = Date.now() - sinceHours * 3_600_000
  const inWindow = cases.filter(c => {
    const t = Date.parse(c.updated_at ?? "")
    return Number.isFinite(t) && t >= cutoff
  })

  const ownerByTicket = new Map<string, string>()
  for (const t of tickets ?? []) {
    if (t.assigned_to) ownerByTicket.set(t.ticket_id, t.assigned_to)
  }
  // Tickets exist but none is assigned is still "we have ticket data" — the fallback is
  // for having no ticket data at all, which is what `demo.ticketing` off looks like.
  const groupedByProcess = tickets === undefined

  let postedCount = 0
  const posted: DigestRow[] = []
  const postedValue = new Map<string, number>()
  let signUnknown = false
  let valuePartial = false
  let spend = 0
  let spendPartial = false
  const waitingGroups = new Map<string, DigestGroup>()
  const blocked: DigestRow[] = []

  for (const c of inWindow) {
    const cost = c.cost_summary?.total_cost_usd
    if (typeof cost === "number") spend += cost
    // Every case in the window was run by something, so an absent cost_summary means
    // the record predates cost accounting — not that the run was free.
    else spendPartial = true

    if (TERMINAL.includes(c.status)) {
      // Terminal alone is not "the agent did it": a case a human approved through a
      // ticket also ends complete. The last run's trigger is what distinguishes them.
      // `error` never reaches here — the schema calls it terminal-until-retried, which
      // is not posted.
      const last = lastTrace(c)
      if (isUnattended(last?.trigger)) {
        postedCount += 1
        // A pack whose cases carry no money counts toward `postedCount` and flags the
        // value as partial. That is the honest reading: the case was posted, and its
        // worth is not something this platform was told.
        const { amount, currency } = caseMoney(c, packs)
        if (amount !== null) {
          const key = currency ?? UNKNOWN_CURRENCY
          postedValue.set(key, (postedValue.get(key) ?? 0) + amount)
          signUnknown = true
        } else {
          valuePartial = true
        }
        posted.push({
          caseId: c.case_id,
          headline: caseHeadline(c, packs[c.pack_id]),
          amount,
          currency,
          reason: caseReason(c, packs) ?? c.process_type,
          // Not `toRow`, which prefers `inquiry_sent_at`: that is the right timestamp for a
          // case somebody is still holding and the wrong one for a closed case, where the
          // age being asked about is how long ago it posted. `updated_at` is that moment.
          since: c.updated_at,
          ageSource: "activity",
          clause: clauseOf(last),
          sopKey: last?.sop_key,
        })
      }
      continue
    }

    if (c.status === CaseStatus.AwaitingHumanInput) {
      const owner = c.ticket_id ? ownerByTicket.get(c.ticket_id) : undefined
      // Three honesty cases, and only the first names a person: an assigned ticket; a
      // case with no recorded recipient; and no ticket data at all, where the process
      // type is the most we can truthfully say.
      const [label, ownerKnown] = owner
        ? [owner, true]
        : groupedByProcess
          ? [c.process_type, false]
          : [UNRECORDED_OWNER, false]
      const group = waitingGroups.get(label) ?? { label, ownerKnown, rows: [] }
      // The pack's own reason field, falling back to the process type — a
      // classification rather than a cause, but always true.
      group.rows.push(toRow(c, caseReason(c, packs) ?? c.process_type, packs))
      waitingGroups.set(label, group)
      continue
    }

    if (c.status === CaseStatus.ManualReviewRequired) {
      blocked.push(toRow(c, lastTrace(c)?.outcome || "no reason recorded", packs))
    }
  }

  const waiting = [...waitingGroups.values()]
  for (const g of waiting) g.rows.sort(byOldestFirst)
  waiting.sort((a, b) => {
    // A group nobody owns sinks: it is a data gap, not the biggest queue.
    if (a.ownerKnown !== b.ownerKnown) return a.ownerKnown ? -1 : 1
    return b.rows.length - a.rows.length || a.label.localeCompare(b.label)
  })
  blocked.sort(byOldestFirst)
  // The one list read newest-first — see `Digest.posted`. Inverted through the shared
  // comparator rather than a second one, so "which timestamp orders a row" stays one answer.
  posted.sort((a, b) => byOldestFirst(b, a))

  return {
    postedCount,
    posted,
    postedValue,
    signUnknown,
    valuePartial,
    waiting,
    waitingCount: waiting.reduce((n, g) => n + g.rows.length, 0),
    blocked,
    spend,
    spendPartial,
    groupedByProcess,
    total: inWindow.length,
  }
}
