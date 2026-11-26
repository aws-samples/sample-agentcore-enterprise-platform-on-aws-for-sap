// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { getConfig } from "@/lib/config"
import { apiFetch, readApiError } from "@/lib/apiFetch"

/**
 * `GET /packs/{pack_id}/rules`: the values the pack's SOPs cite by name, as this stack
 * resolves them. A rule is pack content: it changes with `PATCH /packs/{pack_id}` (`pack.constants`)
 * and reaches every stack the version does. The two write limits are Controls, not rules.
 */

export interface RuleValue {
  value: number | string | null
  kind: "rule"
  /** `pack v7`. */
  source: string
  /** The write checks that read this rule, sorted. Empty when only the SOP text does. */
  guarded_by: string[]
  /**
   * The process types whose SOP cites the rule as `{{SYMBOL}}`, sorted, aliases beside the
   * type they alias. `[]` when no SOP names it; `null` before the stack stores a version.
   */
  cited_by?: string[] | null
}

export interface PackRules {
  pack_id: string
  version: number | null
  rules: Record<string, RuleValue>
  /** SYMBOL → [low, high], so a form offers no value the API would refuse. */
  bounds: Record<string, [number, number]>
  /** `access.edit.rule_values`. */
  may_edit: boolean
}

export async function fetchRules(packId: string, token: string): Promise<PackRules> {
  const { apiUrl } = await getConfig()
  return apiFetch(
    `${apiUrl}/packs/${encodeURIComponent(packId)}/rules`,
    { token },
    "Could not read the rules",
    readApiError
  )
}
