// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { ChevronRight } from "lucide-react"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import {
  Item,
  ItemActions,
  ItemContent,
  ItemDescription,
  ItemGroup,
  ItemTitle,
} from "@/components/ui/item"
import { DetailLayout } from "@/components/layout/DetailLayout"
import { TriggerModeSwitch } from "@/components/TriggerModeSwitch"
import { stopStands } from "@/components/EmergencyStop"
import { systemRoleWords } from "@/components/SandboxMarker"
import { AccessBadge, Fact, Facts, SetHereBadge } from "@/components/packs/PartHeader"
import {
  useDeploymentTriggerMode,
  useDeploymentSettings,
  usePackTriggerMode,
  usePackControls,
  usePackDocument,
  usePackEvaluations,
  usePackRules,
  usePackStatuses,
} from "@/hooks/usePack"
import { savePackTriggerMode } from "@/services/triggerModeService"
import {
  formatRule,
  mayEditPart,
  ruleLabel,
  PART_OWNER,
  PACK_TABS,
  type PartTab,
} from "@/lib/packParts"
import type { UseCasePack } from "@/lib/registry"

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`

/**
 * A pack's home: its five parts, each with what it holds, who owns it and what you may do;
 * and beside them what this deployment sets for the pack (decision 17), its trigger mode
 * under the deployment's (decision 16) among them.
 */
export function OverviewPart({
  pack,
  onOpen,
}: {
  pack: UseCasePack
  onOpen: (tab: PartTab) => void
}) {
  const summary = usePartSummaries(pack)
  return (
    <DetailLayout
      main={
        <Card>
          <CardHeader>
            <CardTitle>Parts</CardTitle>
            <CardDescription>
              Versioned together and moved together, so a deployment always runs a set that was
              tested as one.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <ItemGroup>
              {PACK_TABS.filter(t => t.id !== "overview" && t.id !== "versions").map(t => {
                const part = t.id as PartTab
                return (
                  <Item key={part} variant="outline" role="listitem">
                    <ItemContent>
                      <ItemTitle>
                        {t.label} <AccessBadge canEdit={mayEditPart(pack.access, part)} />
                      </ItemTitle>
                      <ItemDescription>{summary[part]}</ItemDescription>
                      <ItemDescription>{PART_OWNER[part]}</ItemDescription>
                    </ItemContent>
                    <ItemActions>
                      <Button
                        variant="ghost"
                        size="icon-sm"
                        aria-label={`Open ${t.label}`}
                        onClick={() => onOpen(part)}
                      >
                        <ChevronRight />
                      </Button>
                    </ItemActions>
                  </Item>
                )
              })}
            </ItemGroup>
          </CardContent>
        </Card>
      }
      side={<InThisDeployment pack={pack} />}
    />
  )
}

function usePartSummaries(pack: UseCasePack): Record<PartTab, string> {
  const doc = usePackDocument(pack.id).data
  const rules = usePackRules(pack.id).data
  const controls = usePackControls(pack.id).data
  const evals = usePackEvaluations(pack.id).data
  const p = doc?.pack ?? {}
  const source = (p.source ?? {}) as { entity?: string; label?: string }
  const sops = pack.sops ?? []
  const count = (k: string) => sops.filter(s => s.kind === k).length
  const outcomes = Array.isArray(p.dispositions) ? p.dispositions.length : 0
  const tools = Array.isArray(p.gateway_tools) ? p.gateway_tools.length : 0
  const limits = Object.entries(controls?.limits ?? {})
  const run = evals?.last_run

  return {
    intake: [source.label ?? pack.label, source.entity && `from ${source.entity}`]
      .filter(Boolean)
      .join(" "),
    procedures: [
      `General instructions, ${plural(count("triage"), "triage SOP")}, ${plural(count("exception"), "exception SOP")} and ${plural(count("shared"), "shared SOP")}`,
      rules && plural(Object.keys(rules.rules).length, "rule"),
      outcomes && plural(outcomes, "outcome"),
    ]
      .filter(Boolean)
      .join(". "),
    controls: controls
      ? [
          limits.map(([k, l]) => `${ruleLabel(k)} ${formatRule(k, l.value)}`).join(", "),
          plural(controls.write_guards?.length ?? 0, "check") + " before every write",
          `Verifier ${controls.verifier?.enabled ? "on" : "off"}`,
        ]
          .filter(Boolean)
          .join(". ")
      : "",
    agent: doc
      ? [
          p.model_tier && `Model tier ${String(p.model_tier)}`,
          p.max_turns && `up to ${String(p.max_turns)} turns`,
          plural(tools, "tool"),
        ]
          .filter(Boolean)
          .join(", ")
      : "",
    evaluations: evals
      ? [
          plural(evals.example_cases.length, "example case"),
          run && `last run ${run.passed} of ${run.total} passed`,
        ]
          .filter(Boolean)
          .join("; ")
      : "",
  }
}

function InThisDeployment({ pack }: { pack: UseCasePack }) {
  const mine = usePackTriggerMode(pack.id)
  const main = useDeploymentTriggerMode()
  const controls = usePackControls(pack.id).data
  const status = usePackStatuses().data?.find(s => s.pack_id === pack.id)
  const mode = mine.data?.["trigger-mode"]
  const mainOn = main.data?.["trigger-mode"] === "auto"
  // While an emergency stop stands the API refuses turning a pack on; turning it off still works.
  const stopped = stopStands(useDeploymentSettings().data?.last_stop)
  const lowered = Object.entries(controls?.limits ?? {}).filter(([, l]) => l.value < l.ceiling)

  return (
    <>
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            In this deployment <SetHereBadge />
          </CardTitle>
          <CardDescription>Set per deployment, not versioned with the pack.</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <div className="flex items-start justify-between gap-3">
            <div className="flex flex-col gap-0.5 text-sm">
              <span className="font-medium">Trigger mode</span>
              <span className="text-muted-foreground">
                {mode === "auto"
                  ? mainOn
                    ? "auto: new cases start without a person."
                    : "auto, but the deployment's trigger mode is manual, so nothing starts on its own."
                  : "manual: each new case waits for a person."}
              </span>
            </div>
            <TriggerModeSwitch
              mode={mode}
              save={(next, token) => savePackTriggerMode(pack.id, next, token)}
              invalidate={[["trigger-mode"]]}
              scope="The agent then starts on every new case of this pack, while the deployment's trigger mode is auto."
              disabledReason={
                !pack.access?.set_trigger_mode
                  ? "Only an administrator changes the trigger mode."
                  : stopped && mode !== "auto"
                    ? "An emergency stop stands. Clear the stop first, then set the trigger mode to auto."
                    : null
              }
            />
          </div>
          <Facts>
            <Fact label="Active version">
              {status?.version != null ? `v${status.version}` : "–"}
            </Fact>
            {pack.sap_system && (
              <Fact label="SAP system">
                {pack.sap_system.name}, {systemRoleWords(pack.sap_system.system_role)}
              </Fact>
            )}
            {lowered.map(([k, l]) => (
              <Fact key={k} label={ruleLabel(k)}>
                {formatRule(k, l.value)}, lowered from {l.ceiling.toLocaleString("en-US")}
              </Fact>
            ))}
          </Facts>
        </CardContent>
      </Card>
      {status?.last_refusal && (
        <Alert variant="destructive">
          <AlertTitle>The last refused write</AlertTitle>
          <AlertDescription>
            {status.last_refusal.error}
            {status.last_refusal.detail ? `: ${status.last_refusal.detail}` : ""}
          </AlertDescription>
        </Alert>
      )}
    </>
  )
}
