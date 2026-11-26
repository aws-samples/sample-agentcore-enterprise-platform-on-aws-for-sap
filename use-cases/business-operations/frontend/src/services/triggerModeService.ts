// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { getConfig } from "@/lib/config"
import { apiFetch, readApiError } from "@/lib/apiFetch"

/**
 * Trigger mode: whether a new case is queued for the agent without a person.
 *
 * Two trigger modes. `/deployment/trigger-mode` is the deployment's, which containment and break glass
 * turn to manual; `/packs/{pack_id}/trigger-mode` is one per pack. An unattended producer queues
 * a case only when both read `auto`, and an unset pack is `manual`. Admins only, and every
 * change alerts the operations topic.
 */

/** The two the API accepts. `null` is "never set", which reads as manual. */
export type TriggerMode = "auto" | "manual"

export interface DeploymentTriggerMode {
  "trigger-mode": TriggerMode | null
  "trigger-mode-last-changed-by"?: string | null
  /**
   * Whether this deployment has an unattended caller at all. Without one, `auto` can be
   * stored and stays inert. `null` is unknown, never "not capable".
   */
  "autonomous-capable"?: boolean | null
  /** Each pack the caller may see; null when the registry cannot be read. */
  packs?: Record<string, Omit<PackTriggerMode, "pack_id" | "deployment">> | null
}

export interface PackTriggerMode {
  pack_id: string
  "trigger-mode": TriggerMode | null
  "trigger-mode-last-changed-by"?: string | null
  /** `auto` only when this pack and the deployment's trigger mode are both `auto`. */
  effective: TriggerMode
  /** The deployment's trigger mode, as read with it. */
  deployment: TriggerMode | null
}

export async function fetchDeploymentTriggerMode(token: string): Promise<DeploymentTriggerMode> {
  const { apiUrl } = await getConfig()
  return apiFetch(
    `${apiUrl}/deployment/trigger-mode`,
    { token },
    "Could not read the trigger mode",
    readApiError
  )
}

export async function saveDeploymentTriggerMode(
  mode: TriggerMode,
  token: string
): Promise<{ "trigger-mode": TriggerMode }> {
  const { apiUrl } = await getConfig()
  return apiFetch(
    `${apiUrl}/deployment/trigger-mode`,
    { token, method: "PUT", body: { "trigger-mode": mode } },
    "Could not change the trigger mode",
    readApiError
  )
}

export async function fetchPackTriggerMode(packId: string, token: string): Promise<PackTriggerMode> {
  const { apiUrl } = await getConfig()
  return apiFetch(
    `${apiUrl}/packs/${encodeURIComponent(packId)}/trigger-mode`,
    { token },
    "Could not read the pack's trigger mode",
    readApiError
  )
}

export async function savePackTriggerMode(
  packId: string,
  mode: TriggerMode,
  token: string
): Promise<{ pack_id: string; "trigger-mode": TriggerMode }> {
  const { apiUrl } = await getConfig()
  return apiFetch(
    `${apiUrl}/packs/${encodeURIComponent(packId)}/trigger-mode`,
    { token, method: "PUT", body: { "trigger-mode": mode } },
    "Could not change the pack's trigger mode",
    readApiError
  )
}
