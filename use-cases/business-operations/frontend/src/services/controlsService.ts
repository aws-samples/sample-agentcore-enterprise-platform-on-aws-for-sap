// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { getConfig } from "@/lib/config"
import { apiFetch, readApiError } from "@/lib/apiFetch"

/**
 * `/packs/{pack_id}/controls`: what the agent may change on its own, who approves, and the
 * checks before every write.
 *
 * Two homes in one part. A limit's ceiling, the approvers and the write checks are pack
 * structure, changed in development by a pack administrator. Lowering a limit is this
 * stack's own setting: `PUT` takes the lowered limits only, so a stack setting and a pack
 * version never share a request.
 */

export interface LimitValue {
  /** What this stack applies: the ceiling, or lower. */
  value: number
  kind: "limit"
  /** `pack v7` or `stack override (lowered from 100000)`. */
  source: string
  guarded_by: string[]
  /** The pack's value, which no stack may exceed. */
  ceiling: number
}

export interface PackControls {
  pack_id: string
  limits: Record<string, LimitValue>
  /** SYMBOL → [low, high]: the form offers no value outside it. */
  bounds: Record<string, [number, number]>
  entitlement: {
    groups: string[]
    approve_groups?: string[]
    /** Narrows who may lower a limit in this pack, within the limit owners group. */
    limit_groups?: string[]
    /** Narrows who may start Process in this pack, within the case workers group. */
    process_groups?: string[]
  } | null
  write_guards: string[] | null
  verifier: { enabled?: boolean; model_tier?: string } | null
  write_amount_field: string | null
  write_limit_waiver: unknown
  /** `access.lower_limits`. */
  may_lower: boolean
}

/** The stack group whose members may lower a pack's write limits (round six, 6.2). */
export const LIMIT_OWNERS_GROUP = "business-operations-limit-owners"

/** Who may lower this pack's limits, in a sentence. */
export function limitOwners(controls: Pick<PackControls, "entitlement">): string {
  const narrowed = controls.entitlement?.limit_groups ?? []
  return narrowed.length
    ? `members of ${LIMIT_OWNERS_GROUP} who are also in ${narrowed.join(" or ")}`
    : `members of ${LIMIT_OWNERS_GROUP}`
}

/** The stack group whose members may start Process and Re-run (round seven, 7.1). */
export const CASE_WORKERS_GROUP = "business-operations-case-workers"

/** Who may start Process on this pack's cases, in a sentence. */
export function caseWorkers(controls: Pick<PackControls, "entitlement"> | undefined): string {
  const narrowed = controls?.entitlement?.process_groups ?? []
  return narrowed.length
    ? `members of ${CASE_WORKERS_GROUP} who are also in ${narrowed.join(" or ")}`
    : `members of ${CASE_WORKERS_GROUP}`
}

/** Why Process is disabled for a person outside the case workers. */
export function processReason(controls: Pick<PackControls, "entitlement"> | undefined): string {
  return `Only ${caseWorkers(controls)} start the agent on this pack's cases.`
}

export async function fetchControls(packId: string, token: string): Promise<PackControls> {
  const { apiUrl } = await getConfig()
  return apiFetch(
    `${apiUrl}/packs/${encodeURIComponent(packId)}/controls`,
    { token },
    "Could not read the controls",
    readApiError
  )
}

/** `null` returns a limit to the pack's ceiling. */
export async function lowerLimits(
  packId: string,
  limits: Record<string, number | null>,
  token: string
): Promise<{ updated: number; updated_by: string }> {
  const { apiUrl } = await getConfig()
  return apiFetch(
    `${apiUrl}/packs/${encodeURIComponent(packId)}/controls`,
    { token, method: "PUT", body: { limits } },
    "Could not save the limits",
    readApiError
  )
}
