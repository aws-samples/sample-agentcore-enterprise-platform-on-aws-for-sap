// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { getConfig } from "@/lib/config"
import { apiFetch, readApiError } from "@/lib/apiFetch"

/**
 * `/packs/{pack_id}/evaluations`: the pack's example cases (`evals.json`) and the last run
 * published for it. An example case is content: a save is the pack's next version, in
 * development, for the pack's authors. Starting a run is not served yet; `run_refusal` names
 * the command that does.
 */

export type ExpectedOutcome =
  "auto_release" | "approval_required" | "manual_review_required" | "refused"

export interface ExampleCase {
  test_id: string
  group: "behaviour" | "boundaries" | "security"
  description: string
  process_type: string
  tags?: string[]
  seed?: { inputs?: Record<string, unknown>; case_attributes?: Record<string, unknown> }
  payload?: unknown
  expected: { outcome: ExpectedOutcome; reason: string; required_tool_calls?: string[] }
}

export interface CaseResult {
  passed: boolean
  failures: string[]
  warnings: string[]
  status?: string
  cost_usd?: number
  judges?: Record<string, number>
}

export interface EvaluationRun {
  run_id: string
  recorded_at: string
  recorded_by: string
  pack_version: number
  total: number
  passed: number
  failed: number
  cost_usd: number
  judges?: Record<string, number>
  results: Record<string, CaseResult>
}

export interface PackEvaluations {
  pack_id: string
  version: number | null
  example_cases: ExampleCase[]
  last_run: EvaluationRun | null
  estimate_usd: number
  may_edit: boolean
  may_run: boolean
  run_refusal?: string | null
}

export async function fetchEvaluations(packId: string, token: string): Promise<PackEvaluations> {
  const { apiUrl } = await getConfig()
  return apiFetch(
    `${apiUrl}/packs/${encodeURIComponent(packId)}/evaluations`,
    { token },
    "Could not read the evaluations",
    readApiError
  )
}

export type ExampleCaseBody = Omit<ExampleCase, "test_id" | "group">

/** Add or replace one example case. `409` when the pack moved past `expectedVersion`. */
export async function saveExampleCase(
  packId: string,
  testId: string,
  body: ExampleCaseBody,
  token: string,
  expectedVersion?: number | null
): Promise<{ version: number }> {
  const { apiUrl } = await getConfig()
  const expected = expectedVersion == null ? {} : { expected_version: expectedVersion }
  return apiFetch(
    `${apiUrl}/packs/${encodeURIComponent(packId)}/evaluations/${encodeURIComponent(testId)}`,
    { token, method: "PUT", body: { case: body, ...expected } },
    "Could not save the example case",
    readApiError
  )
}
