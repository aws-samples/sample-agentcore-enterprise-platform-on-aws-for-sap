// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useMemo, useState } from "react"
import { useNavigate } from "react-router"
import { useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { Plus, Star } from "lucide-react"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "@/components/ui/empty"
import { ReasonButton } from "@/components/ReasonButton"
import { SandboxMarker } from "@/components/SandboxMarker"
import { NewPackDialog } from "@/components/packs/NewPackDialog"
import { useRegistry, REGISTRY_KEY } from "@/hooks/useRegistry"
import { useStarredPacks } from "@/hooks/useStarredPacks"
import { useAgentPulse } from "@/hooks/useCaseCounts"
import { PROMOTION_ONLY_REASON, useDeploymentSettings, usePackStatuses } from "@/hooks/usePack"
import { PromotionOnlyBanner } from "@/components/packs/PromotionOnlyBanner"
import { useApi } from "@/hooks/useApi"
import { createPack, type PackSpec } from "@/services/packsService"
import { canEditPack, type UseCasePack } from "@/lib/registry"
import { CaseStatus } from "@/types/cases"
import { cn } from "@/lib/utils"

/** The statuses that wait for a person: an approval, a reply, or a manual review. */
const NEEDS_PERSON = [CaseStatus.AwaitingHumanInput, CaseStatus.ManualReviewRequired]

/**
 * Packs (decision 18): one table of every pack you can see, starred ones first. A star puts
 * the pack in the sidebar. Each row shows the live version, how many SOPs it has, the cases
 * waiting for a person and what you may do in it.
 */
export default function PacksIndexPage() {
  const navigate = useNavigate()
  const getToken = useApi()
  const queryClient = useQueryClient()
  const registry = useRegistry()
  const [starred, toggleStar] = useStarredPacks()
  const pulse = useAgentPulse()
  const statuses = usePackStatuses()
  const [creating, setCreating] = useState(false)

  const rows = useMemo(
    () =>
      [...registry.packs].sort((a, b) => {
        const s = Number(starred.includes(b.id)) - Number(starred.includes(a.id))
        return s || a.display_name.localeCompare(b.display_name)
      }),
    [registry.packs, starred]
  )
  const version = (id: string) => statuses.data?.find(s => s.pack_id === id)?.version
  const waiting = (id: string) =>
    NEEDS_PERSON.reduce((n, s) => n + (pulse.counts?.[id]?.[s] ?? 0), 0)
  const settings = useDeploymentSettings()
  // The API's own answer (`access.may_create_pack`): the authors group, in development.
  const mayCreate = !!settings.data?.access?.may_create_pack
  const createReason = mayCreate
    ? null
    : settings.data?.deployment.promotion_only
      ? PROMOTION_ONLY_REASON
      : settings.data
        ? "Creating a pack needs the authors group."
        : "Reading what you may do here."

  const create = async (packId: string, spec: PackSpec) => {
    await createPack(packId, spec, await getToken())
    await queryClient.invalidateQueries({ queryKey: REGISTRY_KEY })
    toast.success(`Created ${spec.display_name}`, {
      description: "It waits for a SAP survey before anything polls it.",
    })
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="flex flex-col gap-4 p-6 pb-ask">
        <div className="flex items-start gap-4">
          <div className="flex-1">
            <h1 className="text-xl font-semibold tracking-tight">Packs</h1>
            <p className="mt-1 text-sm text-muted-foreground">
              One pack per SAP process: what becomes a case, the SOPs the agent follows, and the
              limits on what it may change. Star a pack to keep it in the sidebar.
            </p>
          </div>
          <ReasonButton reason={createReason} onClick={() => setCreating(true)}>
            <Plus />
            New pack
          </ReasonButton>
        </div>

        <PromotionOnlyBanner />

        {registry.error ? (
          <Alert variant="destructive">
            <AlertTitle>Couldn&apos;t load the packs</AlertTitle>
            <AlertDescription>{registry.error}</AlertDescription>
          </Alert>
        ) : !registry.resolved ? (
          <div className="flex flex-col gap-2">
            {Array.from({ length: 3 }, (_, i) => (
              <Skeleton key={i} className="h-9 w-full" />
            ))}
          </div>
        ) : rows.length === 0 ? (
          <Empty>
            <EmptyHeader>
              <EmptyTitle>No packs you can see</EmptyTitle>
              <EmptyDescription>
                A pack is visible to the groups it names. Ask an identity administrator to add you
                to one.
              </EmptyDescription>
            </EmptyHeader>
          </Empty>
        ) : (
          <div className="rounded-lg border bg-card">
            <Table className="table-fixed">
              <TableHeader>
                <TableRow>
                  <TableHead className="w-12">
                    <span className="sr-only">Starred</span>
                  </TableHead>
                  <TableHead>Pack</TableHead>
                  <TableHead className="w-24 text-right">SOPs</TableHead>
                  <TableHead className="w-28">Version</TableHead>
                  <TableHead className="w-36 text-right">Needs a person</TableHead>
                  <TableHead className="w-28">You</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map(p => (
                  <PackRow
                    key={p.id}
                    pack={p}
                    starred={starred.includes(p.id)}
                    onStar={() => toggleStar(p.id)}
                    version={version(p.id)}
                    waiting={waiting(p.id)}
                    onOpen={() => navigate(`/packs/${encodeURIComponent(p.id)}`)}
                  />
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </div>
      <NewPackDialog
        open={creating}
        taken={registry.packs.map(p => p.id)}
        onClose={() => setCreating(false)}
        onCreate={create}
      />
    </div>
  )
}

function PackRow({
  pack,
  starred,
  onStar,
  version,
  waiting,
  onOpen,
}: {
  pack: UseCasePack
  starred: boolean
  onStar: () => void
  version: number | null | undefined
  waiting: number
  onOpen: () => void
}) {
  const sops = (pack.sops ?? []).filter(s => s.kind !== "alias").length
  return (
    <TableRow className="h-9 cursor-pointer" onClick={onOpen}>
      <TableCell onClick={e => e.stopPropagation()}>
        <Button
          variant="ghost"
          size="icon-sm"
          aria-pressed={starred}
          aria-label={`${starred ? "Unstar" : "Star"} ${pack.display_name}`}
          onClick={onStar}
        >
          <Star className={cn(starred && "fill-current")} />
        </Button>
      </TableCell>
      <TableCell className="truncate">
        <span className="inline-flex items-center gap-2">
          <span className="truncate font-medium">{pack.display_name}</span>
          <SandboxMarker pack={pack} />
        </span>
      </TableCell>
      <TableCell className="text-right tabular-nums">{sops || "–"}</TableCell>
      <TableCell className="tabular-nums">
        {pack.incomplete ? (
          <span className="text-muted-foreground">Incomplete</span>
        ) : version != null ? (
          `Live v${version}`
        ) : (
          "–"
        )}
      </TableCell>
      <TableCell className="text-right tabular-nums">
        {waiting || <span className="text-muted-foreground">0</span>}
      </TableCell>
      <TableCell className="text-muted-foreground">{canEditPack(pack) ? "Edit" : "Read"}</TableCell>
    </TableRow>
  )
}
