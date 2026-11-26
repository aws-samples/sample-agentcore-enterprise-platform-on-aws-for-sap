// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { getConfig } from "@/lib/config"
import { apiFetch, readApiError } from "@/lib/apiFetch"
import type { PackPart } from "@/services/packsService"

/**
 * `GET /packs/{pack_id}/versions` and `/versions/{version}`: the versions this stack stores of
 * a pack, newest first, and one version's document. Read only, in every stack.
 */

export interface VersionOrigin {
  /** How the version became active here: `edit`, `create`, `seed`, `import`, `adopted`. */
  via: string
  by?: string | null
  at?: string | null
  change_ref?: string | null
}

export interface PackVersion {
  version: number
  sha256: string
  made_by: string | null
  made_at: string | null
  /** The stack that minted it. */
  made_in: string | null
  /** The verb that minted it: `edit`, `create`, `export`, `seed`, `import`, `adopt`. */
  via: string
  /** The note or change reference it was minted with. */
  change_note: string | null
  /** The parts it changed against the version before it, in the order of the five parts. */
  parts: PackPart[]
  changes: {
    keys: { added: string[]; removed: string[]; changed: string[] }
    files: { added: string[]; removed: string[]; changed: string[] }
  }
  origin: VersionOrigin | null
}

export interface PackVersions {
  pack_id: string
  active: number | null
  versions: PackVersion[]
  next_token: string | null
}

export interface PackVersionDocument extends PackVersion {
  pack_id: string
  previous: { version: number; sha256: string } | null
  pack: Record<string, unknown>
  base_prompt: string
  sops: Record<string, string>
  evals: unknown[]
}

export async function fetchPackVersions(
  packId: string,
  token: string,
  nextToken?: string | null
): Promise<PackVersions> {
  const { apiUrl } = await getConfig()
  const query = nextToken ? `?next_token=${encodeURIComponent(nextToken)}` : ""
  return apiFetch(
    `${apiUrl}/packs/${encodeURIComponent(packId)}/versions` + query,
    { token },
    "Could not read the pack's versions",
    readApiError
  )
}

export async function fetchPackVersion(
  packId: string,
  version: number,
  token: string
): Promise<PackVersionDocument> {
  const { apiUrl } = await getConfig()
  return apiFetch(
    `${apiUrl}/packs/${encodeURIComponent(packId)}/versions/${version}`,
    { token },
    `Could not read version ${version}`,
    readApiError
  )
}
