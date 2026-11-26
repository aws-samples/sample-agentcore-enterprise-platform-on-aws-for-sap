// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useQuery } from "@tanstack/react-query"
import { useAuth } from "react-oidc-context"
import { useApi } from "@/hooks/useApi"
import { fetchPack, fetchPackStatus } from "@/services/packsService"
import { fetchPackSops } from "@/services/sopsService"
import { fetchRules } from "@/services/rulesService"
import { fetchControls } from "@/services/controlsService"
import { fetchDeploymentTriggerMode, fetchPackTriggerMode } from "@/services/triggerModeService"
import { fetchEvaluations } from "@/services/evaluationsService"
import { fetchDeploymentSettings } from "@/services/deploymentService"
import { fetchPackVersion } from "@/services/versionsService"

/**
 * One query per part of a pack, keyed so a save in one part refetches only that part.
 * Every key starts with `["pack", packId]`, so invalidating that prefix refreshes the page.
 */
function useAuthed<T>(key: readonly unknown[], fn: (token: string) => Promise<T>, enabled = true) {
  const auth = useAuth()
  const getToken = useApi()
  return useQuery({
    queryKey: key,
    queryFn: async () => fn(await getToken()),
    enabled: auth.isAuthenticated && enabled,
  })
}

export const packKey = (packId: string, part: string) => ["pack", packId, part] as const

export const usePackDocument = (packId: string) =>
  useAuthed(packKey(packId, "document"), t => fetchPack(packId, t))
export const usePackSops = (packId: string) =>
  useAuthed(packKey(packId, "sops"), t => fetchPackSops(packId, t))
export const usePackRules = (packId: string) =>
  useAuthed(packKey(packId, "rules"), t => fetchRules(packId, t))
export const usePackControls = (packId: string) =>
  useAuthed(packKey(packId, "controls"), t => fetchControls(packId, t))
export const usePackTriggerMode = (packId: string) =>
  useAuthed(["trigger-mode", "pack", packId], t => fetchPackTriggerMode(packId, t))
export const usePackEvaluations = (packId: string) =>
  useAuthed(packKey(packId, "evaluations"), t => fetchEvaluations(packId, t))

export const useDeploymentTriggerMode = () =>
  useAuthed(["trigger-mode", "deployment"], fetchDeploymentTriggerMode)
export const useDeploymentSettings = () =>
  useAuthed(["deployment-settings"], fetchDeploymentSettings)
export const usePackStatuses = () => useAuthed(["packs", "status"], fetchPackStatus)

/** One version's document, fetched when a person opens it on the Versions tab. */
export const usePackVersion = (packId: string, version: number | null) =>
  useAuthed(
    packKey(packId, `version-${version}`),
    t => fetchPackVersion(packId, version!, t),
    version != null
  )

/**
 * Whether this stack takes packs by promotion only (`deployment.promotion_only`, after
 * sign-in). `undefined` until the settings are read, so nothing flickers as refused.
 */
export function usePromotionOnly(): boolean | undefined {
  return useDeploymentSettings().data?.deployment.promotion_only
}

/** The reason an edit is refused in a promotion-only stack, for its tooltip. */
export const PROMOTION_ONLY_REASON =
  "This stack takes packs by promotion only. Make the change in a development stack and promote it."
