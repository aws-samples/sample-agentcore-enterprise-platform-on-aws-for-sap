// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { getConfig } from "@/lib/config"
import { apiFetch, readApiError } from "@/lib/apiFetch"
import type { PackAccess } from "@/lib/registry"

/**
 * `/packs` and `/packs/{pack_id}`: the pack document itself, read whole and changed by part.
 *
 * Every write goes to the pack composer, the only writer, so each is a new version of the
 * pack in a development stack, and a quality or production stack refuses it. What a caller
 * may change is the server's decision (`pack_tiers.check_patch`); `GET /registry` `access`
 * says it in advance for badges and disabled controls.
 */

/** The five parts of a pack (docs/packs.md, Pack parts). */
export type PackPart = "intake" | "procedures" | "controls" | "agent_configuration" | "evaluations"

export const PACK_PARTS: readonly PackPart[] = [
  "intake",
  "procedures",
  "controls",
  "agent_configuration",
  "evaluations",
]

/** `GET /packs/{pack_id}`: the published document, and the part each top-level key is in. */
export interface PackDocument {
  pack_id: string
  /** The active version; null before the first verb stores version 0. */
  version: number | null
  /** How the active version arrived, as `GET /packs/status` says it. */
  origin: PackStatus["origin"]
  pack: Record<string, unknown>
  /** Top-level key → part, for the keys the document carries (`pack_tiers.PARTS`). */
  parts: Record<string, PackPart>
  /** Constants that belong to a part other than Procedures: the two limits, Controls. */
  constant_parts: Record<string, PackPart>
  access: PackAccess
}

export async function fetchPack(packId: string, token: string): Promise<PackDocument> {
  const { apiUrl } = await getConfig()
  return apiFetch(
    `${apiUrl}/packs/${encodeURIComponent(packId)}`,
    { token },
    "Could not read the pack",
    readApiError
  )
}

/** The keys of a pack document that belong to one part, in document order. */
export function keysOfPart(doc: Pick<PackDocument, "pack" | "parts">, part: PackPart): string[] {
  return Object.keys(doc.pack).filter(k => doc.parts[k] === part)
}

/** One attribute a created pack carries. Its SAP path is written as a survey marker. */
export interface PackAttributeSpec {
  name: string
  label: string
  type: "string" | "date" | "amount" | "decimal"
  /** Required on an `amount`, and must name another attribute in the same spec. */
  currency_field?: string
  /** false keeps the field out of the case page. */
  panel?: boolean
}

/**
 * What the create form collects. Mirrors `SPEC_KEYS` in `api/layer/python/pack_compose.py`:
 * a key this does not declare is refused by the API rather than dropped.
 */
export interface PackSpec {
  display_name: string
  description: string
  /** The prefix of every case id this pack mints. No two packs may claim the same one. */
  short_id: string
  kind: "filter" | "absence" | "aging"
  /** What makes a record an exception, in the analyst's own words. */
  definition: string
  process_types: string[]
  attributes?: PackAttributeSpec[]
  constants?: Record<string, number>
  /** Required for `kind: "aging"`, refused for anything else. */
  older_than_days?: number
}

export interface WriteResult {
  pack_id?: string
  /** The pack version the write made, in a development stack. */
  version?: number
}

/**
 * `POST /packs`: mint a pack. It is born incomplete, every SAP value a survey marker, so
 * nothing polls it and no process type routes to it until someone completes it with SAP values. Its
 * rules are live and its content is editable at once. `pack_id` cannot change afterwards;
 * an id already published is a 409.
 */
export async function createPack(
  packId: string,
  spec: PackSpec,
  token: string
): Promise<WriteResult> {
  const { apiUrl } = await getConfig()
  return apiFetch(
    `${apiUrl}/packs`,
    { token, method: "POST", body: { pack_id: packId, create: spec } },
    "Could not create the pack",
    readApiError
  )
}

/** A patch: top-level keys of the document (rule values as `constants`), or the base prompt. */
export interface PackPatch {
  pack?: Record<string, unknown>
  base_prompt?: string
}

/**
 * `PATCH /packs/{pack_id}`. `expectedVersion` is the version the content was read at: the API
 * answers 409, and writes nothing, when someone else saved the pack since.
 */
export async function patchPack(
  packId: string,
  patch: PackPatch,
  token: string,
  expectedVersion?: number | null
): Promise<WriteResult> {
  const { apiUrl } = await getConfig()
  const expected = expectedVersion == null ? {} : { expected_version: expectedVersion }
  return apiFetch(
    `${apiUrl}/packs/${encodeURIComponent(packId)}`,
    { token, method: "PATCH", body: { ...patch, ...expected } },
    "Could not save the pack",
    readApiError
  )
}

/**
 * `GET /packs/{pack_id}/base_prompt`: the general instructions, with the version read. Not in
 * `GET /registry`, since a prompt is agent instructions rather than UI data.
 */
export async function fetchBasePrompt(
  packId: string,
  token: string
): Promise<{ text: string; version: number | null }> {
  const { apiUrl } = await getConfig()
  const body = await apiFetch<{ base_prompt?: string; version?: number | null }>(
    `${apiUrl}/packs/${encodeURIComponent(packId)}/base_prompt`,
    { token },
    "Could not read the general instructions",
    readApiError
  )
  return { text: body.base_prompt ?? "", version: body.version ?? null }
}

/** One pack's live state, `GET /packs/status`: what `ops.py pack status` shows, in short. */
export interface PackStatus {
  pack_id: string
  /** The active version; null before the first verb stores version 0. */
  version: number | null
  /** `via` is `seed`, `adopted`, `import`, `edit`, `create`, `export`, `rollback` or `retired`. */
  origin: { via: string; at?: string | null; by?: string | null; change_ref?: string | null } | null
  marked: boolean
  /** The process types that route to this pack now. */
  routes: string[]
  /** Why nothing routes to it, when the pack is the reason. Empty means it may route. */
  unroutable: string[]
  /** The last write the pack composer refused, with the rule that refused it. */
  last_refusal: { at: string; verb: string; error: string; detail?: string | null } | null
}

export async function fetchPackStatus(token: string): Promise<PackStatus[]> {
  const { apiUrl } = await getConfig()
  const body = await apiFetch<{ packs: PackStatus[] }>(
    `${apiUrl}/packs/status`,
    { token },
    "Could not read pack status",
    readApiError
  )
  return body.packs
}
