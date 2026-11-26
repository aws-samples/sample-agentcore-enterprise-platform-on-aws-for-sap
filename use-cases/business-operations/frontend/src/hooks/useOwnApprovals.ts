// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useMemo } from "react"
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query"
import { useAuth } from "react-oidc-context"
import { useApi } from "@/hooks/useApi"
import { useCaseCounts } from "@/hooks/useCaseCounts"
import { useRegistry } from "@/hooks/useRegistry"
import { packKey } from "@/hooks/usePack"
import { fetchCase, queryCasePages } from "@/services/casesService"
import { fetchControls } from "@/services/controlsService"
import { needsTwoApprovers, pendingApprovals } from "@/lib/decision"
import { CaseStatus, type WorkItem } from "@/types/cases"

/**
 * The cases in the caller's Needs you that wait on a second approver after their own first
 * approval (round seven, Q10). The index entry behind `GET /cases` carries no action log, so
 * the console reads it: the Needs you list, then the full record of each case waiting for a
 * decision whose change needs two approvers, which is the only kind a first approval leaves
 * waiting. Such a case leaves the caller's Needs you, and stays in Waiting on a person.
 *
 * Under the list's search and packs, not its exception types, so a facet's counts can follow
 * too. Refetches when the queue moves, and when a decision invalidates `["cases"]`.
 */
export function useOwnApprovals(filter: { q?: string; packIds?: string[] }, enabled = true) {
  const auth = useAuth()
  const getToken = useApi()
  const queryClient = useQueryClient()
  const registry = useRegistry()
  const me = String(auth.user?.profile?.sub ?? "")
  const counts = useCaseCounts()
  const stamp = counts.isSuccess ? JSON.stringify(counts.data) : null
  const query = useQuery({
    queryKey: ["cases", "own-approvals", me, filter, stamp],
    enabled: auth.isAuthenticated && !!me && enabled,
    placeholderData: keepPreviousData,
    queryFn: async (): Promise<WorkItem[]> => {
      const token = await getToken()
      const { items } = await queryCasePages(
        { view: "needs_you", ...filter, limit: 200 },
        token,
        () => false
      )
      const waiting = items.filter(c => c.status === CaseStatus.AwaitingHumanInput)
      const controls = Object.fromEntries(
        await Promise.all(
          [...new Set(waiting.map(c => c.pack_id))].map(async id => [
            id,
            await queryClient
              .fetchQuery({
                queryKey: packKey(id, "controls"),
                queryFn: () => fetchControls(id, token),
                staleTime: 60_000,
              })
              .catch(() => undefined),
          ])
        )
      )
      const candidates = waiting.filter(
        c => needsTwoApprovers(c, registry.byId[c.pack_id], controls[c.pack_id]).needed
      )
      const mine = await Promise.all(
        candidates.map(async c => {
          const full = await queryClient
            .fetchQuery({
              queryKey: ["case", c.case_id],
              queryFn: () => fetchCase(c.case_id, token),
              staleTime: 10_000,
            })
            .catch(() => undefined)
          return full && pendingApprovals(full).some(a => a.actor === me) ? c : null
        })
      )
      return mine.filter((c): c is WorkItem => c != null)
    },
  })
  const items = useMemo(() => query.data ?? [], [query.data])
  const ids = useMemo(() => new Set(items.map(c => c.case_id)), [items])
  return { items, ids }
}

/** A case's type as the exception filter and its facet read it. */
export const typeOf = (c: WorkItem) => String(c.exception_type ?? c.process_type)
