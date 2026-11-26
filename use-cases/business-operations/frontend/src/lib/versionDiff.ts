// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { diffLines, type DiffLine } from "@/lib/lineDiff"
import { PACK_PARTS, type PackPart } from "@/services/packsService"
import type { PackVersionDocument } from "@/services/versionsService"

/**
 * What one pack version changed against the version before it, from the two versions'
 * documents: by part, then by key. `pack.json` keys go to their part through the pack's
 * `parts` map, a changed `constants` is split by symbol (a limit is Controls, a rule value
 * Procedures), the general instructions and each SOP are Procedures with a line diff, and the
 * example cases are Evaluations, by test id.
 */

export type VersionChange =
  | { kind: "value"; key: string; symbol?: string; before: unknown; after: unknown }
  | { kind: "text"; key: string; file?: string; diff: DiffLine[] }
  | { kind: "cases"; key: string; added: string[]; removed: string[]; changed: string[] }

export interface PartChanges {
  /** Null for a key no part claims. */
  part: PackPart | null
  changes: VersionChange[]
}

type Content = Pick<PackVersionDocument, "pack" | "base_prompt" | "sops" | "evals">

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b)

function caseId(c: unknown, i: number): string {
  const id = (c as { test_id?: unknown } | null)?.test_id
  return typeof id === "string" ? id : `#${i + 1}`
}

export function versionDiff(
  before: Content,
  after: Content,
  parts: Record<string, PackPart>,
  constantParts: Record<string, PackPart>
): PartChanges[] {
  const out = new Map<PackPart | null, VersionChange[]>()
  const add = (part: PackPart | null, change: VersionChange) =>
    out.set(part, [...(out.get(part) ?? []), change])

  const keys = [...new Set([...Object.keys(before.pack), ...Object.keys(after.pack)])].sort()
  for (const key of keys) {
    const a = before.pack[key]
    const b = after.pack[key]
    if (same(a, b)) continue
    if (key === "constants" && isRecord(a) && isRecord(b)) {
      for (const symbol of [...new Set([...Object.keys(a), ...Object.keys(b)])].sort())
        if (!same(a[symbol], b[symbol]))
          add(constantParts[symbol] ?? parts.constants ?? "procedures", {
            kind: "value",
            key,
            symbol,
            before: a[symbol],
            after: b[symbol],
          })
      continue
    }
    add(parts[key] ?? null, { kind: "value", key, before: a, after: b })
  }

  if (before.base_prompt !== after.base_prompt)
    add("procedures", {
      kind: "text",
      key: "base_prompt",
      diff: diffLines(before.base_prompt ?? "", after.base_prompt ?? ""),
    })
  const files = [...new Set([...Object.keys(before.sops), ...Object.keys(after.sops)])].sort()
  for (const file of files)
    if (before.sops[file] !== after.sops[file])
      add("procedures", {
        kind: "text",
        key: "sop",
        file,
        diff: diffLines(before.sops[file] ?? "", after.sops[file] ?? ""),
      })

  const casesBefore = new Map(before.evals.map((c, i) => [caseId(c, i), c]))
  const casesAfter = new Map(after.evals.map((c, i) => [caseId(c, i), c]))
  const added = [...casesAfter.keys()].filter(id => !casesBefore.has(id))
  const removed = [...casesBefore.keys()].filter(id => !casesAfter.has(id))
  const changed = [...casesAfter.keys()].filter(
    id => casesBefore.has(id) && !same(casesBefore.get(id), casesAfter.get(id))
  )
  if (added.length || removed.length || changed.length)
    add("evaluations", { kind: "cases", key: "evals", added, removed, changed })

  const order = (p: PackPart | null) => (p == null ? PACK_PARTS.length : PACK_PARTS.indexOf(p))
  return [...out.entries()]
    .sort(([a], [b]) => order(a) - order(b))
    .map(([part, changes]) => ({ part, changes }))
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return !!v && typeof v === "object" && !Array.isArray(v)
}
