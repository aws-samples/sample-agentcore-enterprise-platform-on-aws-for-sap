// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import type { ReactNode } from "react"
import { Badge } from "@/components/ui/badge"
import { editScope, mayEditPart, PART_OWNER, type PartTab } from "@/lib/packParts"
import type { UseCasePack } from "@/lib/registry"

/** "You can edit" or "Read only" for one part, with who changes it (decision 17). */
export function AccessBadge({ canEdit }: { canEdit: boolean }) {
  return (
    <Badge variant={canEdit ? "default" : "secondary"}>
      {canEdit ? "You can edit" : "Read only"}
    </Badge>
  )
}

/** "Set here": a value this deployment sets for the pack, not the pack's version. */
export function SetHereBadge() {
  return <Badge variant="outline">Set here</Badge>
}

/** The line at the top of a part: what you may do in it, and who changes it. */
export function PartHeader({ pack, part }: { pack: UseCasePack; part: PartTab }) {
  const canEdit = mayEditPart(pack.access, part)
  return (
    <div className="flex items-center gap-3 text-sm">
      <AccessBadge canEdit={canEdit} />
      <span className="text-muted-foreground">
        {editScope(pack.access, part) ?? PART_OWNER[part]}
      </span>
    </div>
  )
}

/** A label and value list, one row per fact. */
export function Facts({ children }: { children: ReactNode }) {
  return <dl className="flex flex-col gap-2 text-sm">{children}</dl>
}

export function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex gap-6">
      <dt className="w-40 flex-none text-muted-foreground">{label}</dt>
      <dd className="min-w-0 flex-1">{children}</dd>
    </div>
  )
}
