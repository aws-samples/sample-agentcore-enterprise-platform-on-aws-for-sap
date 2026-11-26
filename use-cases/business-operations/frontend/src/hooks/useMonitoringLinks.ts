// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useState } from "react"
import { getConfig } from "@/lib/config"

export interface MonitoringLink {
  kind: "dashboard" | "traces"
  label: string
  href: string
}

/** The CloudWatch links `aws-exports.json` names, for admins only. */
export function useMonitoringLinks(admin: boolean): MonitoringLink[] {
  const [links, setLinks] = useState<MonitoringLink[]>([])
  useEffect(() => {
    let active = true
    getConfig()
      .then(c => {
        if (!active) return
        const out: MonitoringLink[] = []
        if (c.dashboardUrl) out.push({ kind: "dashboard", label: "CloudWatch", href: c.dashboardUrl })
        if (c.agentObservabilityUrl)
          out.push({ kind: "traces", label: "Agent traces", href: c.agentObservabilityUrl })
        setLinks(out)
      })
      .catch(() => active && setLinks([]))
    return () => {
      active = false
    }
  }, [])
  return admin ? links : []
}
