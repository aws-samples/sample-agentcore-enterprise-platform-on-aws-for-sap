// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { FlaskConical } from "lucide-react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { usePreference } from "@/hooks/usePreference"
import { useDeploymentSettings } from "@/hooks/usePack"
import { isNonProduction, type UseCasePack } from "@/lib/registry"
import type { WorkItem } from "@/types/cases"

/**
 * Sandbox markers (decision 22): marked means not production, unmarked means production,
 * from each SAP system's role. A person may hide them in this browser for a demo, which
 * only ever makes a sandbox look like production.
 */
export const MARKERS_KEY = "ui.system_role.markers"

export function useMarkers(): [boolean, (on: boolean) => void] {
  return usePreference<boolean>(MARKERS_KEY, true)
}

const SYSTEM_ROLE: Record<string, string> = {
  sandbox: "a sandbox",
  development: "a development system",
  quality: "a quality system",
}

/** The system role of a SAP system in words, for a sentence. */
export function systemRoleWords(role: string): string {
  return SYSTEM_ROLE[role] ?? "a production system"
}

/** The flask beside a pack on a non-production SAP system, with a tooltip saying which. */
export function SandboxMarker({ pack }: { pack: UseCasePack | undefined }) {
  const [on] = useMarkers()
  if (!on || !pack?.sap_system || !isNonProduction(pack)) return null
  const text = `SAP system ${pack.sap_system.name} is ${systemRoleWords(pack.sap_system.system_role)}. Not production.`
  return <Flask text={text} />
}

/**
 * The flask beside a case: always for test data a person posted, and for a case on a
 * non-production system when the band is not already saying so for the whole window.
 */
export function CaseMarker({ c, pack }: { c: WorkItem; pack: UseCasePack | undefined }) {
  const [on] = useMarkers()
  const settings = useDeploymentSettings()
  if (!on) return null
  if (c.test_data)
    return <Flask text="Test data: posted to the sandbox from the test data page." />
  if (settings.data?.deployment.non_production || !pack?.sap_system || !isNonProduction(pack))
    return null
  return (
    <Flask
      text={`On SAP system ${pack.sap_system.name}, ${systemRoleWords(pack.sap_system.system_role)}. Not production.`}
    />
  )
}

function Flask({ text }: { text: string }) {
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <span
            role="img"
            aria-label={text}
            className="inline-flex flex-none text-muted-foreground"
          />
        }
      >
        <FlaskConical className="size-3.5" />
      </TooltipTrigger>
      <TooltipContent>{text}</TooltipContent>
    </Tooltip>
  )
}

/**
 * The band across the page when every SAP system this deployment reaches is non-production.
 * Hide markers turns off the band and every flask in this browser, and says where to turn
 * them back on.
 */
export function SandboxBand() {
  const [on, setOn] = useMarkers()
  const settings = useDeploymentSettings()
  const facts = settings.data?.deployment
  if (!on || !facts?.non_production) return null
  const systems = facts.sap_systems
  const named =
    systems.length === 1
      ? `SAP system ${systems[0].name} is ${systemRoleWords(systems[0].system_role)}.`
      : `SAP systems ${systems.map(s => s.name).join(", ")} are not production systems.`
  return (
    <div
      role="region"
      aria-label="Sandbox"
      className="flex h-8 flex-none items-center gap-2 border-b bg-hatch px-4 text-sm"
    >
      <FlaskConical className="size-3.5 text-muted-foreground" />
      <span className="font-medium">Sandbox</span>
      <span className="truncate text-muted-foreground">
        {named} Nothing here reaches a production system.
      </span>
      <Button
        variant="ghost"
        size="xs"
        className="ml-auto"
        onClick={() => {
          setOn(false)
          toast("Sandbox markers hidden", {
            description: "Turn them back on in the account menu, under Sandbox markers.",
          })
        }}
      >
        Hide markers
      </Button>
    </div>
  )
}
