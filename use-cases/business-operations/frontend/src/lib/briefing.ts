// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The briefing's own derivations: ordering, labelling, and the two sentences.
 *
 * Every count in here comes off a `Digest` and never off model output. This module holds
 * the arrangement; `lib/digest.ts` holds the arithmetic. That split is what makes the
 * narrated opening safe to ship — prose is the model's, numbers are the code's.
 */

import { CaseStatus } from "@/types/cases"
import {
  byOldestFirst,
  windowProse,
  UNKNOWN_CURRENCY,
  type Digest,
  type DigestRow,
} from "@/lib/digest"
import { formatAmount } from "@/lib/registry"

/**
 * The briefing's window, fixed rather than selectable: a shift briefing means "since
 * yesterday", and Analytics is where you go to ask about a different period. Only the
 * *windowed* half of the page uses it; the queue is unwindowed.
 */
export const BRIEFING_HOURS = "24"

/** Time-of-day greeting. `hour` is a local 0–23, passed in so this stays pure. */
export function greeting(hour: number): string {
  if (hour < 12) return "Good morning"
  if (hour < 18) return "Good afternoon"
  return "Good evening"
}

export interface QueueEntry {
  kind: "waiting" | "blocked"
  row: DigestRow
  /** The group label when it names a person, else null. Never guessed. */
  owner: string | null
  /** True only when a *known* owner matches the signed-in operator. */
  mine: boolean
}

/**
 * The queue, in the order the operator should walk it.
 *
 * Waiting groups flattened, then blocked — blocked cases need a human too, and leaving
 * them out would make the band's own count disagree with its own list. Yours first within
 * each, then oldest first, inherited from `digest()`'s comparator rather than re-derived.
 *
 * `d` must be the **unwindowed** digest. A case that has waited five days is exactly what
 * this list exists to surface, and any finite window drops it.
 */
export function queueOrder(d: Digest, identities: ReadonlySet<string>): QueueEntry[] {
  const waiting = d.waiting.flatMap(g =>
    g.rows.map(row => ({
      kind: "waiting" as const,
      row,
      // `ownerKnown` is digest()'s own honesty flag: false covers both "no ticket recorded
      // the recipient" and "no ticket data at all". Neither is someone to chase, and
      // neither can be you.
      owner: g.ownerKnown ? g.label : null,
      mine: g.ownerKnown && identities.has(g.label.toLowerCase()),
    }))
  )
  const blocked = d.blocked.map(row => ({
    kind: "blocked" as const,
    row,
    owner: null,
    mine: false,
  }))
  const yoursFirst = (a: QueueEntry, b: QueueEntry) =>
    a.mine === b.mine ? byOldestFirst(a.row, b.row) : a.mine ? -1 : 1
  return [...waiting.sort(yoursFirst), ...blocked.sort(yoursFirst)]
}

/**
 * Why an entry is in the queue, in words that cannot be read as the case's own status.
 *
 * A blocked row's `reason` is the last *run's* outcome — `digest()` documents it as such —
 * and `complete` is a value in that vocabulary as well as in `CaseStatus`. Rendered bare it
 * reads as a finished case: a narration advised pulling a case because it was "marked
 * complete" while it sat in `manual_review_required`. `digest()`'s vocabulary is shared with
 * the analytics surfaces, so the disambiguation lives with the two consumers that need it
 * rather than in the derivation.
 */
export function reasonLabel(entry: QueueEntry): string {
  return entry.kind === "blocked" ? `last run: ${entry.row.reason}` : entry.row.reason
}

export interface Chip {
  count: number
  label: string
  /** The workspace filter this chip drills into, when one can express it. */
  status: CaseStatus | null
}

/**
 * The band's counts.
 *
 * Two digests, and the labels say which is which: the queue counts are the standing
 * backlog, the cleared count carries the window's own prose. They are not peers and must
 * never read as though they were.
 */
export function chips(backlog: Digest, recent: Digest, mineCount: number, hours: string): Chip[] {
  const out: Chip[] = []
  if (mineCount > 0) {
    out.push({ count: mineCount, label: "ready for you", status: CaseStatus.AwaitingHumanInput })
    const others = backlog.waitingCount - mineCount
    if (others > 0) {
      out.push({
        count: others,
        label: "waiting on someone else",
        status: CaseStatus.AwaitingHumanInput,
      })
    }
  } else if (backlog.waitingCount > 0) {
    // Not "waiting on others". With no assignment signal the record cannot say whose these
    // are, and splitting them would be a claim rather than a count.
    out.push({
      count: backlog.waitingCount,
      label: "waiting on someone",
      status: CaseStatus.AwaitingHumanInput,
    })
  }
  if (backlog.blocked.length > 0) {
    out.push({
      count: backlog.blocked.length,
      label: "blocked",
      status: CaseStatus.ManualReviewRequired,
    })
  }
  out.push({
    count: recent.postedCount,
    label: `cleared without you in the ${windowProse(hours)}`,
    status: null,
  })
  return out
}

/**
 * Posted value, one string per currency.
 *
 * Never one scalar: `digest()` groups by currency because a pack's amount field names its
 * own currency field per case, so a single total over two currencies is a wrong number
 * rather than an imprecise one.
 *
 * Shared with `PeriodBriefing`, which joins the same parts differently. The join is each
 * surface's own; the decision about a case whose currency was never recorded is one
 * decision and belongs in one place.
 */
export function valueParts(d: Digest): string[] {
  return [...d.postedValue.entries()].map(([currency, total]) =>
    currency === UNKNOWN_CURRENCY
      ? `${formatAmount(total)} (currency not recorded)`
      : formatAmount(total, currency)
  )
}

/**
 * The deterministic opening line, posted before any run starts.
 *
 * Plain text rather than JSX: it goes into the transcript through `postNotice`, so the
 * operator's queue is on screen in the first paint with no model in the path. This is the
 * bar a narration has to beat, and the thing that stands if the run fails.
 *
 * `PeriodBriefing` states the same figures as a retrospective; this states them as a queue,
 * which is why it names the backlog as the backlog. Value and spend come off `recent` —
 * both are claims about a period, and the queue half is the only unwindowed half.
 */
export function briefingSentence(backlog: Digest, recent: Digest, hours: string): string {
  const cleared =
    `In the ${windowProse(hours)} the agent cleared ${recent.postedCount} of ` +
    `${recent.total} ${recent.total === 1 ? "case" : "cases"} without you.`
  const parts: string[] = []
  if (backlog.waitingCount > 0) {
    parts.push(
      `${backlog.waitingCount} ${backlog.waitingCount === 1 ? "case is" : "cases are"} ` +
        "waiting on a person"
    )
  }
  if (backlog.blocked.length > 0) {
    parts.push(`${backlog.blocked.length} could not be cleared`)
  }
  const queue = parts.length
    ? `${parts.join(" and ")} — that is the whole backlog, not just this window.`
    : "Nothing is waiting on a person and nothing is blocked."

  // Nothing in the window is not a value of zero, it is no claim to make. The queue half
  // above is unwindowed and still stands.
  if (recent.total === 0) return `${cleared} ${queue}`

  // Lowercase and clause-shaped: both endings below finish their sentence with it. A
  // partial figure stated as a total is the one thing this sentence must not do, so where
  // an in-window case has no recorded cost the figure is a floor and says so.
  const spend =
    `${recent.spendPartial ? "at least " : ""}${formatAmount(recent.spend, "USD")} ` +
    "in agent spend"

  // Spend is a claim about every case touched in the window; value is a claim about the
  // posted ones only. With nothing posted that set is empty, and even "no posted case
  // carried an amount" would be a statement about no cases at all — the same defect the
  // guard above is for, one set narrower.
  if (recent.postedCount === 0) return `${cleared} ${queue} That still cost ${spend}.`

  const amounts = valueParts(recent)
  // "unsigned" because `abs_decimal` discards the sign on `InvoiceGrossAmount`, so a credit
  // memo adds rather than subtracts. `PeriodBriefing` discloses it and so must this.
  const value = amounts.length
    ? `${recent.valuePartial ? "At least " : ""}${amounts.join(" and ")} of ` +
      `${recent.signUnknown ? "unsigned " : ""}value posted`
    : "No posted case carried an amount"

  return `${cleared} ${queue} ${value}, for ${spend}.`
}

/**
 * The narration prompt.
 *
 * States the counts rather than asking for them: the band has already rendered them from
 * `digest()`, so the model's job is to say what to do first, not to redo arithmetic the
 * code has done. That is the whole reason a model is safe here at all.
 */
export function narrationPrompt(
  name: string | null,
  backlog: Digest,
  recent: Digest,
  hours: string,
  oldest: QueueEntry | undefined
): string {
  const lines = [
    `You are greeting ${name ?? "the operator"} at the start of their shift. ` +
      "Speak directly to them in two or three sentences. The band above has already " +
      "greeted them for the time of day, and you are not told the hour — do not open " +
      "with a greeting of your own.",
    "",
    "These figures are already on their screen, rendered from the stored case records. " +
      "Do not restate them as a list and do not compute new ones:",
    `- cleared by the agent with no human, ${windowProse(hours)}: ` +
      `${recent.postedCount} of ${recent.total}`,
    `- waiting on a person, whole backlog: ${backlog.waitingCount}`,
    `- blocked, whole backlog: ${backlog.blocked.length}`,
  ]
  if (oldest) {
    lines.push(
      // `headline`, not `caseId`, because this sentence gets read: the queue beside it prints
      // the headline, and naming the case by its digest here would name something not on
      // screen — in a paragraph whose whole job is to point at one row.
      `- the case that has waited longest: ${oldest.row.headline}, ${oldest.kind} ` +
        `(${reasonLabel(oldest)}), ` +
        `${oldest.row.ageSource === "inquiry" ? "asked" : "last touched"} ${oldest.row.since}`
    )
  }
  lines.push(
    "",
    "Say what is worth doing first and why. Do not invent case ids, amounts, or names. " +
      // The figures above carry no owner, so a possessive is the one hallucination this
      // prompt makes easy: "your oldest case" needs no invented name to be a false claim.
      "Never call a case theirs — you have not been told who owns any of them. " +
      "If nothing is waiting, say so plainly and stop."
  )
  return lines.join("\n")
}
