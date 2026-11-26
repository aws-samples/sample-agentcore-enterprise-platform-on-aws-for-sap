// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * A line diff for the save dialog: what a person changed in a SOP, with a few lines of
 * context and the unchanged runs between folded away. A longest-common-subsequence over
 * lines, which is plenty for a document of a few hundred lines.
 */

export type DiffLine =
  { kind: "same" | "added" | "removed"; text: string } | { kind: "fold"; count: number }

export function diffLines(before: string, after: string, context = 2): DiffLine[] {
  const a = before.split("\n")
  const b = after.split("\n")
  const n = a.length
  const m = b.length
  // lcs[i][j]: the longest common run of a[i..] and b[j..], as one flat array.
  const lcs = new Uint32Array((n + 1) * (m + 1))
  const at = (i: number, j: number) => i * (m + 1) + j
  for (let i = n - 1; i >= 0; i--)
    for (let j = m - 1; j >= 0; j--)
      lcs[at(i, j)] =
        a[i] === b[j] ? lcs[at(i + 1, j + 1)] + 1 : Math.max(lcs[at(i + 1, j)], lcs[at(i, j + 1)])

  const raw: { kind: "same" | "added" | "removed"; text: string }[] = []
  let i = 0
  let j = 0
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      raw.push({ kind: "same", text: a[i] })
      i++
      j++
    } else if (lcs[at(i + 1, j)] >= lcs[at(i, j + 1)]) raw.push({ kind: "removed", text: a[i++] })
    else raw.push({ kind: "added", text: b[j++] })
  }
  while (i < n) raw.push({ kind: "removed", text: a[i++] })
  while (j < m) raw.push({ kind: "added", text: b[j++] })

  // Keep `context` unchanged lines either side of a change; fold the rest.
  const keep = raw.map(() => false)
  raw.forEach((l, k) => {
    if (l.kind === "same") return
    for (let d = -context; d <= context; d++) if (raw[k + d]) keep[k + d] = true
  })
  const out: DiffLine[] = []
  let folded = 0
  raw.forEach((l, k) => {
    if (keep[k]) {
      if (folded) out.push({ kind: "fold", count: folded })
      folded = 0
      out.push(l)
    } else folded++
  })
  if (folded && out.length) out.push({ kind: "fold", count: folded })
  return out
}

/** How many lines a diff adds and removes. */
export function diffCounts(diff: DiffLine[]): { added: number; removed: number } {
  return {
    added: diff.filter(l => l.kind === "added").length,
    removed: diff.filter(l => l.kind === "removed").length,
  }
}
