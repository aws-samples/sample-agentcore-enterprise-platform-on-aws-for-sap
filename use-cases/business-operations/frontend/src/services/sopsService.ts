// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { getConfig } from "@/lib/config"
import { apiFetch, readApiError } from "@/lib/apiFetch"
import type { SopKind } from "@/lib/registry"

/**
 * `/packs/{pack_id}/sops`: a pack's SOPs, each addressed by the process type that maps to it.
 *
 * A save has the pack composer write the SOP, which the next run reads at once, and starts
 * the ingestion job that re-indexes SOP search. In a development stack each save is a new
 * version of the pack. An alias reads the file it aliases and refuses a write with a 409
 * naming that type, so one file never has two write paths.
 */

const id = encodeURIComponent

/** One `process_type_to_sop` entry, with its file's size and date when the file exists. */
export interface SopRow {
  process_type: string
  kind: SopKind
  /** Bucket key the entry maps to, `<pack_id>/<file>`. */
  sop: string
  alias_of?: string
  key: string
  size?: number
  last_modified?: string
}

export type SearchState = "current" | "indexing" | "stale"

export interface IngestionStatus {
  job_id: string
  /** Amazon Bedrock's own: STARTING | IN_PROGRESS | COMPLETE | FAILED. */
  status: string
  started_at: string
  documents?: number | null
}

export interface PackSops {
  pack_id: string
  version: number | null
  sops: SopRow[]
  ingestion: IngestionStatus | null
  /** Per pack, whether SOP search serves its SOPs as they stand. Null with no index. */
  search: Record<string, SearchState> | null
  can_author: boolean
}

export async function fetchPackSops(packId: string, token: string): Promise<PackSops> {
  const { apiUrl } = await getConfig()
  return apiFetch(
    `${apiUrl}/packs/${id(packId)}/sops`,
    { token },
    "Could not list the SOPs",
    readApiError
  )
}

/** One SOP with its text, which is what the procedure page opens. An alias reads the file it
 *  aliases. */
export interface SopDocument {
  key: string
  size: number
  last_modified: string
  version_id?: string | null
  process_type: string
  kind: SopKind
  alias_of?: string
  content: string
  /** `deploy` for a SOP the deploy imported rather than written through this API. */
  uploaded_by?: string | null
  uploaded_at?: string | null
  /** The pack's active version as read; a save sends it back to catch a lost update. */
  pack_version?: number | null
  can_author: boolean
}

export async function fetchSop(
  packId: string,
  processType: string,
  token: string
): Promise<SopDocument> {
  const { apiUrl } = await getConfig()
  return apiFetch(
    `${apiUrl}/packs/${id(packId)}/sops/${id(processType)}`,
    { token },
    "Could not open the SOP",
    readApiError
  )
}

export interface SaveResult {
  key: string
  bytes: number
  uploaded_by: string
  /** The pack version the write made, in a development stack. */
  version?: number
}

/**
 * `PUT /packs/{pack_id}/sops/{process_type}`. `expectedVersion` is the `pack_version` the
 * text was read at: the API answers 409, and writes nothing, when someone saved the pack since.
 */
export async function saveSop(
  packId: string,
  processType: string,
  content: string,
  token: string,
  expectedVersion?: number | null
): Promise<SaveResult> {
  const { apiUrl } = await getConfig()
  const expected = expectedVersion == null ? {} : { expected_version: expectedVersion }
  return apiFetch(
    `${apiUrl}/packs/${id(packId)}/sops/${id(processType)}`,
    { token, method: "PUT", body: { content, ...expected } },
    "Could not save the SOP",
    readApiError
  )
}

export interface CheckResult {
  key: string
  /** No findings. */
  ok: boolean
  /** What the SOP check says: sections, the Version line, RFC 2119, symbols, mapping. */
  findings: string[]
}

/**
 * `POST /packs/{pack_id}/sops/{process_type}/check`: every refusal a save would give comes
 * back the same way (thrown), then the check's findings. Nothing is written.
 */
export async function checkSop(
  packId: string,
  processType: string,
  content: string,
  token: string
): Promise<CheckResult> {
  const { apiUrl } = await getConfig()
  return apiFetch(
    `${apiUrl}/packs/${id(packId)}/sops/${id(processType)}/check`,
    { token, method: "POST", body: { content } },
    "Could not check the SOP",
    readApiError
  )
}
