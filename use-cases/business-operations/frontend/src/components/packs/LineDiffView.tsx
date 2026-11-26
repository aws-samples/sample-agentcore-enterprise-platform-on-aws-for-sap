// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import type { DiffLine } from "@/lib/lineDiff"
import { cn } from "@/lib/utils"

/**
 * A line diff's lines: added on the success wash, removed on the danger wash and struck
 * through, folded runs as a count. Screen readers hear "Added:" or "Removed:" before a line.
 * The caller gives it a border and, where it can grow long, a scroll region.
 */
export function LineDiffView({ diff }: { diff: DiffLine[] }) {
  return (
    <>
      {diff.map((l, i) =>
        l.kind === "fold" ? (
          <div key={i} className="px-3 py-1 text-xs text-muted-foreground">
            {l.count} unchanged line{l.count === 1 ? "" : "s"}
          </div>
        ) : (
          <div
            key={i}
            className={cn(
              "px-3 whitespace-pre-wrap",
              l.kind === "added" && "bg-success-wash text-success-ink",
              l.kind === "removed" && "bg-danger-wash text-danger-ink line-through"
            )}
          >
            <span aria-hidden="true" className="mr-2 inline-block w-3 text-muted-foreground">
              {l.kind === "added" ? "+" : l.kind === "removed" ? "−" : " "}
            </span>
            <span className="sr-only">
              {l.kind === "added" ? "Added: " : l.kind === "removed" ? "Removed: " : ""}
            </span>
            {l.text || " "}
          </div>
        )
      )}
    </>
  )
}
