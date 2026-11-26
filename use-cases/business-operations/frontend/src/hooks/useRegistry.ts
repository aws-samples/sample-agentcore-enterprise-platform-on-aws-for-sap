// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useMemo } from "react"
import { useQuery } from "@tanstack/react-query"
import { useAuth } from "react-oidc-context"
import { useApi } from "@/hooks/useApi"
import { fetchRegistry } from "@/services/registryService"
import { indexPacks, type PackIndex, type UseCasePack } from "@/lib/registry"

export const REGISTRY_KEY = ["registry"] as const

/** A publish is a rare event, so this is cached hard — but not forever, or a publish would need a reload. */
const REGISTRY_STALE_MS = 5 * 60_000

const NO_PACKS: UseCasePack[] = []
const NO_INDEX: PackIndex = {}

export interface Registry {
  /** Every pack this caller may see, id-ordered as the API returns them. */
  packs: UseCasePack[]
  byId: PackIndex
  /**
   * The scopes a case list can filter by: packs that actually have a source, and so
   * can have cases at all. A pack with none is a tab that filters to nothing.
   */
  scopes: UseCasePack[]
  /**
   * The fetch has settled, either way. Callers gate on this rather than on success:
   * with no registry reachable, a case still has to render, and it renders its own
   * attributes. Waiting forever for packs that are not coming would show an empty
   * workspace instead.
   */
  resolved: boolean
  error: string | null
}

/**
 * Which use cases this deployment runs.
 *
 * Every consumer shares one cache entry, so the case table, the detail panel and the
 * handover cannot disagree about a pack's columns.
 */
export function useRegistry(): Registry {
  const auth = useAuth()
  const getToken = useApi()

  const query = useQuery({
    queryKey: REGISTRY_KEY,
    queryFn: async () => {
      const token = await getToken()
      return fetchRegistry(token)
    },
    enabled: auth.isAuthenticated,
    staleTime: REGISTRY_STALE_MS,
  })

  // Memoised on the response, not rebuilt per render: `byId` and `scopes` reach
  // effect and query dependency lists, where a fresh object every render is a loop.
  return useMemo(() => {
    const packs = query.data ?? NO_PACKS
    return {
      packs,
      byId: query.data ? indexPacks(query.data) : NO_INDEX,
      scopes: packs.filter(p => p.source.kind),
      resolved: query.isSuccess || query.isError,
      error: query.error instanceof Error ? query.error.message : null,
    }
  }, [query.data, query.isSuccess, query.isError, query.error])
}
