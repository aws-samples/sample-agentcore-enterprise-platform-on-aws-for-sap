// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useNavigate, useParams, useSearchParams } from "react-router"
import { FlaskConical } from "lucide-react"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Skeleton } from "@/components/ui/skeleton"
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "@/components/ui/empty"
import { ReasonButton } from "@/components/ReasonButton"
import { PromotionOnlyBanner } from "@/components/packs/PromotionOnlyBanner"
import { SandboxMarker } from "@/components/SandboxMarker"
import { OverviewPart } from "@/components/packs/OverviewPart"
import { IntakePart } from "@/components/packs/IntakePart"
import { ProceduresPart } from "@/components/packs/ProceduresPart"
import { ControlsPart } from "@/components/packs/ControlsPart"
import { AgentPart } from "@/components/packs/AgentPart"
import { EvaluationsPart } from "@/components/packs/EvaluationsPart"
import { VersionsPart } from "@/components/packs/VersionsPart"
import { useRegistry } from "@/hooks/useRegistry"
import { useStoredState } from "@/hooks/useStoredState"
import { isPackTab, landingTab, PACK_TABS, type PackTab } from "@/lib/packParts"

/** The last tab a person opened on a pack page, in this browser. */
export const LAST_TAB_KEY = "ui.packs.tab"

/**
 * A pack (preview 1.1): its name and description, then an Overview and one tab per part.
 * A process analyst lands on Procedures and everyone else on Overview, then on the last tab
 * used. Rehearse sits in the header on Overview and Procedures only.
 */
export default function PackPage() {
  const { packId = "" } = useParams()
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()
  const registry = useRegistry()
  const [lastTab, setLastTab] = useStoredState<string | null>(LAST_TAB_KEY, null)
  const pack = registry.byId[packId]

  if (!pack)
    return registry.resolved ? (
      <Empty>
        <EmptyHeader>
          <EmptyTitle>No pack {packId} that you can see</EmptyTitle>
          <EmptyDescription>
            It may not exist in this deployment, or its groups may not include you.
          </EmptyDescription>
        </EmptyHeader>
      </Empty>
    ) : (
      <div className="flex flex-col gap-3 p-6">
        <Skeleton className="h-8 w-80" />
        <Skeleton className="h-32 w-full" />
      </div>
    )

  const asked = params.get("tab")
  const tab: PackTab = isPackTab(asked) ? asked : landingTab(pack, lastTab)
  const choose = (next: PackTab) => {
    setLastTab(next)
    setParams({ tab: next }, { replace: true })
  }
  const headerActions = tab === "overview" || tab === "procedures"

  return (
    <Tabs
      value={tab}
      onValueChange={v => choose(v as PackTab)}
      className="flex h-full flex-col gap-0"
    >
      <div className="flex flex-none flex-col gap-4 border-b px-6 pt-6">
        <div className="flex items-start gap-4">
          <div className="min-w-0 flex-1">
            <h1 className="flex items-center gap-2 text-xl font-semibold tracking-tight">
              <span className="truncate">{pack.display_name}</span>
              <SandboxMarker pack={pack} />
            </h1>
            {pack.description && (
              <p className="mt-1 max-w-measure text-sm text-muted-foreground">{pack.description}</p>
            )}
          </div>
          {headerActions && (
            <div className="flex flex-none gap-2">
              <ReasonButton
                variant="outline"
                reason={pack.access?.rehearse ? null : "Rehearsing needs the pack's authors."}
                onClick={() => navigate(`/packs/${encodeURIComponent(pack.id)}/rehearse`)}
              >
                <FlaskConical />
                Rehearse
              </ReasonButton>
            </div>
          )}
        </div>
        <PromotionOnlyBanner />
        <TabsList variant="line" className="h-(--toolbar-height)">
          {PACK_TABS.map(t => (
            <TabsTrigger key={t.id} value={t.id}>
              {t.label}
            </TabsTrigger>
          ))}
        </TabsList>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="p-6 pb-ask">
          <TabsContent value="overview">
            <OverviewPart pack={pack} onOpen={choose} />
          </TabsContent>
          <TabsContent value="intake">
            <IntakePart pack={pack} />
          </TabsContent>
          <TabsContent value="procedures">
            <ProceduresPart pack={pack} />
          </TabsContent>
          <TabsContent value="controls">
            <ControlsPart pack={pack} />
          </TabsContent>
          <TabsContent value="agent">
            <AgentPart pack={pack} />
          </TabsContent>
          <TabsContent value="evaluations">
            <EvaluationsPart pack={pack} caseId={params.get("case")} />
          </TabsContent>
          <TabsContent value="versions">
            <VersionsPart pack={pack} />
          </TabsContent>
        </div>
      </div>
    </Tabs>
  )
}
