// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useMemo, useState } from "react"
import { keepPreviousData, useInfiniteQuery, useQuery } from "@tanstack/react-query"
import { useAuth } from "react-oidc-context"
import { useApi } from "@/hooks/useApi"
import { useCaseCounts } from "@/hooks/useCaseCounts"
import {
  fetchCaseSummary,
  fetchExceptionTypeCounts,
  fetchViewCounts,
  queryCases,
  type CaseQuery,
} from "@/services/casesService"
import { API_VIEW, isView, type CaseView } from "@/lib/caseViews"
import { typeOf, useOwnApprovals } from "@/hooks/useOwnApprovals"

/**
 * The Cases list as the server serves it (2.22): the view, search, facets and sort in the URL
 * become one `GET /cases` query, paged with `next_token`, and the view counts come from
 * `GET /cases/counts` under the same search and facets. Both refetch when the sidebar's
 * heartbeat sees the counts change, so a list that is open follows the queue without polling
 * on its own.
 */

/** `amount` is largest first, `amount-asc` smallest first; the server sorts both (6.5). */
export type CaseSort = "oldest" | "newest" | "amount" | "amount-asc"
const SORTS = new Set<string>(["oldest", "newest", "amount", "amount-asc"])

/** The page's state as the URL holds it. */
export interface CaseListState {
  view: CaseView
  q: string
  exceptions: string[]
  packs: string[]
  sort: CaseSort | null
}

export function caseListState(params: URLSearchParams): CaseListState {
  const view = params.get("view")
  const sort = params.get("sort")
  return {
    view: isView(view) ? view : "needs-you",
    q: params.get("q") ?? "",
    exceptions: params.getAll("exception"),
    packs: params.getAll("pack"),
    sort: sort && SORTS.has(sort) ? (sort as CaseSort) : null,
  }
}

/** The server query for a state. With no sort chosen, the view's own order. */
export function toCaseQuery(state: CaseListState, q = state.q): CaseQuery {
  const sort =
    state.sort === "amount"
      ? { sort: "amount" as const, order: "desc" as const }
      : state.sort === "amount-asc"
        ? { sort: "amount" as const, order: "asc" as const }
        : state.sort === "oldest"
          ? { sort: "updated_at" as const, order: "asc" as const }
          : state.sort === "newest"
            ? { sort: "updated_at" as const, order: "desc" as const }
            : {}
  return {
    view: API_VIEW[state.view],
    ...(q.trim() ? { q: q.trim() } : {}),
    ...(state.exceptions.length ? { exceptionTypes: state.exceptions } : {}),
    ...(state.packs.length ? { packIds: state.packs } : {}),
    ...sort,
    limit: 50,
  }
}

/** A value that follows `value` once it has stopped changing for `ms`. */
function useSettled<T>(value: T, ms: number): T {
  const [settled, setSettled] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setSettled(value), ms)
    return () => clearTimeout(t)
  }, [value, ms])
  return settled
}

/** The heartbeat's counts as one value, which changes when the queue does. */
function useQueueStamp(): string | null {
  const counts = useCaseCounts()
  return counts.isSuccess ? JSON.stringify(counts.data) : null
}

/** The list for a state, a page at a time. The search settles for 250 ms before it asks. */
export function useCaseList(state: CaseListState) {
  const auth = useAuth()
  const getToken = useApi()
  const q = useSettled(state.q, 250)
  const query = useMemo(() => toCaseQuery(state, q), [state, q])
  const list = useInfiniteQuery({
    queryKey: ["cases", "query", query],
    queryFn: async ({ pageParam }) => queryCases(query, await getToken(), pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: last => last.next_token ?? undefined,
    enabled: auth.isAuthenticated,
    placeholderData: keepPreviousData,
  })
  const stamp = useQueueStamp()
  const { refetch } = list
  useEffect(() => {
    if (stamp) void refetch()
  }, [stamp, refetch])
  // Needs you leaves out a case waiting on a second approver after the caller's own approval.
  const own = useOwnApprovals(ownFilter(q, state.packs), state.view === "needs-you")
  const rows = useMemo(() => {
    const all = list.data?.pages.flatMap(p => p.items) ?? []
    return state.view === "needs-you" ? all.filter(c => !own.ids.has(c.case_id)) : all
  }, [list.data, state.view, own.ids])
  return { ...list, rows, query }
}

/** The filter the own-approvals read takes: the search and packs, as the facet's. */
function ownFilter(q: string, packs: string[]) {
  return {
    ...(q.trim() ? { q: q.trim() } : {}),
    ...(packs.length ? { packIds: packs } : {}),
  }
}

/** Every view's count under the state's search and facets. */
export function useViewCounts(state: Pick<CaseListState, "q" | "exceptions" | "packs">) {
  const auth = useAuth()
  const getToken = useApi()
  const q = useSettled(state.q, 250)
  const filter = useMemo(
    () => ({
      q: q.trim() || undefined,
      exceptionTypes: state.exceptions.length ? state.exceptions : undefined,
      packIds: state.packs.length ? state.packs : undefined,
    }),
    [q, state.exceptions, state.packs]
  )
  const stamp = useQueueStamp()
  const counts = useQuery({
    queryKey: ["cases", "counts", filter, stamp],
    queryFn: async () => fetchViewCounts(filter, await getToken()),
    enabled: auth.isAuthenticated,
    placeholderData: keepPreviousData,
  })
  // Needs you counts what it lists: less the cases waiting on a second approver after the
  // caller's own approval, of the types the filter shows.
  const own = useOwnApprovals(ownFilter(q, state.packs), (counts.data?.views.needs_you ?? 0) > 0)
  const data = useMemo(() => {
    if (!counts.data || !own.items.length) return counts.data
    const hidden = own.items.filter(
      c => !state.exceptions.length || state.exceptions.includes(typeOf(c))
    ).length
    const views = counts.data.views
    return {
      ...counts.data,
      views: { ...views, needs_you: Math.max(0, (views.needs_you ?? 0) - hidden) },
    }
  }, [counts.data, own.items, state.exceptions])
  return { ...counts, data }
}

/**
 * Each exception type's count per view, for the facet, under the state's search and packs.
 * Fetched only while the facet is open (`enabled`), since it reads more than a count does.
 */
export function useExceptionTypeCounts(
  state: Pick<CaseListState, "q" | "packs">,
  enabled: boolean
) {
  const auth = useAuth()
  const getToken = useApi()
  const q = useSettled(state.q, 250)
  const filter = useMemo(
    () => ({ q: q.trim() || undefined, packIds: state.packs.length ? state.packs : undefined }),
    [q, state.packs]
  )
  const stamp = useQueueStamp()
  const counts = useQuery({
    queryKey: ["cases", "counts", "by-exception-type", filter, stamp],
    queryFn: async () => fetchExceptionTypeCounts(filter, await getToken()),
    enabled: auth.isAuthenticated && enabled,
    placeholderData: keepPreviousData,
  })
  const own = useOwnApprovals(ownFilter(q, state.packs), enabled)
  const data = useMemo(() => {
    if (!counts.data?.needs_you || !own.items.length) return counts.data
    const needsYou = { ...counts.data.needs_you }
    for (const c of own.items) {
      const t = typeOf(c)
      if (needsYou[t] != null) needsYou[t] = Math.max(0, needsYou[t] - 1)
    }
    return { ...counts.data, needs_you: needsYou }
  }, [counts.data, own.items])
  return { ...counts, data }
}

/** The Done view's window: what closed in the last 24 hours, 7 days or 30 days (2.24). */
export function useDoneSummary(enabled: boolean, hours: number, packIds: string[]) {
  const auth = useAuth()
  const getToken = useApi()
  const stamp = useQueueStamp()
  return useQuery({
    queryKey: ["cases", "summary", hours, packIds, stamp],
    queryFn: async () =>
      fetchCaseSummary(new Date(Date.now() - hours * 3_600_000), packIds, await getToken()),
    enabled: auth.isAuthenticated && enabled,
    placeholderData: keepPreviousData,
  })
}
