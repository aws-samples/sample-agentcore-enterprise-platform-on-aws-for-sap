// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import type { WorkItem, CaseStatus } from "@/types/cases"
import { getConfig } from "@/lib/config"
import { apiFetch, readApiError } from "@/lib/apiFetch"

/** The views `GET /cases?view=` takes (`case_status.VIEWS`). */
export type ApiView = "needs_you" | "team" | "new" | "running" | "failed" | "done" | "all"

/** What the Cases page asks the server for (2.22): the view, the search, the facets, the sort. */
export interface CaseQuery {
  view?: ApiView
  /** A prefix of the document number, the party value or the case id, ignoring case. */
  q?: string
  exceptionTypes?: string[]
  packIds?: string[]
  sort?: "updated_at" | "amount"
  /** Omitted for the view's own default order. */
  order?: "asc" | "desc"
  /** 1 to 200; 50 when omitted. */
  limit?: number
}

/** One page of `GET /cases`. A page can be short, or empty, and still carry a token. */
export interface CaseQueryPage {
  items: WorkItem[]
  next_token: string | null
  view?: ApiView
  sort?: string
  order?: string
}

/** The query's parameters, in the order the route documents them. */
export function caseQueryParams(query: CaseQuery, nextToken?: string | null): URLSearchParams {
  const params = new URLSearchParams()
  if (query.view) params.set("view", query.view)
  const q = query.q?.trim()
  if (q) params.set("q", q.slice(0, 64))
  for (const e of query.exceptionTypes ?? []) params.append("exception_type", e)
  for (const p of query.packIds ?? []) params.append("pack_id", p)
  if (query.sort) params.set("sort", query.sort)
  if (query.order) params.set("order", query.order)
  if (query.limit) params.set("limit", String(query.limit))
  if (nextToken) params.set("next_token", nextToken)
  return params
}

/** One page of cases, as the server filters, sorts and pages them. */
export async function queryCases(
  query: CaseQuery,
  token: string,
  nextToken?: string | null
): Promise<CaseQueryPage> {
  const { apiUrl } = await getConfig()
  return apiFetch(
    `${apiUrl}/cases?${caseQueryParams(query, nextToken)}`,
    { token },
    "Failed to fetch cases",
    readApiError
  )
}

// ponytail: a hard stop at 5 pages for a caller that follows the token itself, so a server
// that keeps handing one back cannot spin the tab forever.
export const MAX_CASE_PAGES = 5

/**
 * Follow `next_token` for a caller that needs more than one page (the briefing, the last 24
 * hours), until `stop` says the rest is not wanted or the page cap. Answers whether it
 * stopped at the cap with more left.
 */
export async function queryCasePages(
  query: CaseQuery,
  token: string,
  stop: (page: WorkItem[]) => boolean = () => false
): Promise<{ items: WorkItem[]; more: boolean }> {
  const items: WorkItem[] = []
  let next: string | null = null
  for (let page = 0; page < MAX_CASE_PAGES; page++) {
    const result = await queryCases(query, token, next)
    items.push(...result.items)
    next = result.next_token
    if (!next || stop(result.items)) return { items, more: false }
  }
  return { items, more: !!next }
}

/** Cases closed in a window, by outcome, with value per currency and agent spend (2.24). */
export interface CaseSummaryTotals {
  closed: number
  by_outcome: Record<string, { cases: number; value: Record<string, number> }>
  value: Record<string, number>
  agent_cost_usd: number
}

export interface CaseSummary {
  since: string
  until: string
  packs: string[]
  totals: CaseSummaryTotals
  days: ({ date: string } & CaseSummaryTotals)[]
}

/** `GET /cases/summary`: what closed since `since` (at most 30 days ago), for the caller's packs. */
export async function fetchCaseSummary(
  since: Date,
  packIds: string[],
  token: string
): Promise<CaseSummary> {
  const { apiUrl } = await getConfig()
  const params = new URLSearchParams({ since: since.toISOString() })
  for (const p of packIds) params.append("pack_id", p)
  return apiFetch(
    `${apiUrl}/cases/summary?${params}`,
    { token },
    "Failed to fetch the summary",
    readApiError
  )
}

/** `GET /cases/counts`: per pack, per status, how many cases the caller may see. */
export type CaseCounts = Record<string, Partial<Record<string, number>>>

/** `GET /cases/counts` with the query's search and facets: every view's count, and per pack. */
export interface ViewCounts {
  views: Partial<Record<ApiView, number>>
  counts: CaseCounts
  /** With `by=exception_type`: per view, per exception type, under `q` and the packs only. */
  exception_types?: Partial<Record<ApiView, Record<string, number>>>
}

export async function fetchViewCounts(
  filter: Pick<CaseQuery, "q" | "exceptionTypes" | "packIds">,
  token: string
): Promise<ViewCounts> {
  const { apiUrl } = await getConfig()
  const params = caseQueryParams({
    q: filter.q,
    exceptionTypes: filter.exceptionTypes,
    packIds: filter.packIds,
  })
  return apiFetch(
    `${apiUrl}/cases/counts?${params}`,
    { token },
    "Failed to fetch case counts",
    readApiError
  )
}

/**
 * `GET /cases/counts?by=exception_type`: how many cases each exception type would show in
 * each view, under the search and the packs but not the exception facet itself, so choosing
 * one type does not zero the others. Opt-in: it costs the function more than a count.
 */
export async function fetchExceptionTypeCounts(
  filter: Pick<CaseQuery, "q" | "packIds">,
  token: string
): Promise<NonNullable<ViewCounts["exception_types"]>> {
  const { apiUrl } = await getConfig()
  const params = caseQueryParams({ q: filter.q, packIds: filter.packIds })
  params.set("by", "exception_type")
  const res: ViewCounts = await apiFetch(
    `${apiUrl}/cases/counts?${params}`,
    { token },
    "Failed to count cases by exception type",
    readApiError
  )
  return res.exception_types ?? {}
}

/** Every pack and status, for the sidebar's heartbeat. */
export async function fetchCaseCounts(token: string): Promise<CaseCounts> {
  return (await fetchViewCounts({}, token)).counts
}

/** One status, summed across packs. */
export function countOf(counts: CaseCounts | undefined, status: CaseStatus): number {
  return Object.values(counts ?? {}).reduce((n, per) => n + (per[status] ?? 0), 0)
}

/**
 * `list` with `record`'s status fields on its row, when the record is newer than the row.
 *
 * The list reads a GSI, which can trail the table by seconds after a run's last write;
 * the open case polls its own record, so its row follows that record instead of waiting
 * for a list fetch that lands after the write. Returns `list` itself when nothing changed,
 * so a cache write made from an effect does not re-render for nothing.
 */
export function withCaseRecord(
  list: WorkItem[] | undefined,
  record: WorkItem
): WorkItem[] | undefined {
  if (!list) return list
  let changed = false
  const next = list.map(c => {
    if (c.case_id !== record.case_id || record.updated_at <= c.updated_at) return c
    changed = true
    return {
      ...c,
      status: record.status,
      status_reason: record.status_reason,
      disposition: record.disposition,
      updated_at: record.updated_at,
    }
  })
  return changed ? next : list
}

export async function fetchCase(caseId: string, token: string): Promise<WorkItem> {
  const { apiUrl } = await getConfig()
  // case_id is the table key and is URL-safe by construction, so no encoding here.
  return apiFetch(`${apiUrl}/cases/${caseId}`, { token }, "Failed to fetch case")
}

export async function enqueueCases(caseIds: string[], token: string): Promise<void> {
  const { apiUrl } = await getConfig()
  // Backend derives the acting user from the auth token, not a field in this payload.
  await Promise.all(
    caseIds.map(caseId =>
      apiFetch(
        `${apiUrl}/cases/enqueue`,
        { token, method: "POST", body: { case_id: caseId } },
        `Failed to enqueue ${caseId}`,
        res => res.text()
      )
    )
  )
}

export async function submitCaseRating(
  caseId: string,
  rating: "positive" | "negative",
  comment: string | undefined,
  token: string
): Promise<void> {
  const { apiUrl } = await getConfig()
  await apiFetch(
    `${apiUrl}/cases/${caseId}/rating`,
    { token, method: "PUT", body: { rating, comment } },
    "Failed to submit rating"
  )
}

/** What the operator is recording alongside their words. */
export type CaseDecision = "approved" | "denied" | "replied"

/**
 * Send a reply on a case, resuming the agent.
 *
 * The path for a case no ticket backs. Ticket-backed cases go through
 * `submitTicketAction`, which records a resolution as well as resuming — this one only
 * resumes, because with no ticket there is no resolution record to write.
 *
 * The sender is never sent: the backend takes it from the caller's token, so a body
 * field here could only ever be ignored or believed, and neither is worth the ambiguity.
 *
 * Every refusal this route can answer with has a cause the operator can act on — an
 * unrecognised decision, no entitlement to approve *this* domain, a queue the deployment
 * never configured — so the body's `error` is surfaced rather than the bare status. "403"
 * on its own reads as "you cannot touch this case", which is false: approval is gated on
 * the domain's approve_groups while a denial or a plain reply needs only read entitlement.
 */
export async function replyToCase(
  caseId: string,
  message: string,
  decision: CaseDecision,
  token: string,
  /** The chosen option's id, on a case with decision options (2.23); `decision` is then unused. */
  optionId?: string
): Promise<{ enqueued: boolean; case_id: string; awaiting_second_approver?: boolean }> {
  const { apiUrl } = await getConfig()
  // case_id is the table key and is URL-safe by construction, so no encoding here.
  return apiFetch(
    `${apiUrl}/cases/${caseId}/reply`,
    {
      token,
      method: "POST",
      // An option is chosen instead of a decision (2.23): the route refuses both together.
      body: optionId ? { option: optionId, message } : { message, decision },
    },
    "Failed to send reply",
    async res => (await res.json().catch(() => ({}))).error
  )
}
