// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { Link, useLocation } from "react-router"
import {
  ArrowUpRight,
  ChartLine,
  Inbox,
  LayoutGrid,
  Package,
  Server,
  Ticket,
  Waypoints,
} from "lucide-react"
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuBadge,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarRail,
} from "@/components/ui/sidebar"
import { useBrand } from "@/hooks/useBrand"
import { useRegistry } from "@/hooks/useRegistry"
import { useIsAdmin } from "@/hooks/useIsAdmin"
import { useDemoFeatures } from "@/hooks/useDemoEnabled"
import { useMonitoringLinks } from "@/hooks/useMonitoringLinks"
import { useStarredPacks } from "@/hooks/useStarredPacks"
import { useAgentPulse } from "@/hooks/useCaseCounts"
import { countOf } from "@/services/casesService"
import { CaseStatus } from "@/types/cases"
import { canEditPack } from "@/lib/registry"
import { NavUser } from "./NavUser"
import { SandboxMarker } from "@/components/SandboxMarker"
import { useOwnApprovals } from "@/hooks/useOwnApprovals"

/**
 * The console's navigation (plan, Sidebar; decisions 17 and 19). Four groups: Cases and
 * Tickets, Packs (all packs and the starred ones), Deployment, and Monitoring for admins.
 * Collapses to icons with ⌘B, the header's panel button or the rail on its edge; each
 * icon then has a tooltip with its name.
 */
export function AppSidebar() {
  const brand = useBrand()
  const { pathname } = useLocation()
  const { ticketing } = useDemoFeatures()
  const admin = useIsAdmin()
  const registry = useRegistry()
  const [starred] = useStarredPacks()
  const pulse = useAgentPulse()
  const monitoring = useMonitoringLinks(admin)

  // The Needs you view's count: cases waiting on a person in the packs the caller may
  // decide in. `counts` is per pack, so this is the same sum `views.needs_you` makes.
  const deciding = Object.fromEntries(
    Object.entries(pulse.counts ?? {}).filter(([id]) => registry.byId[id]?.access?.approve)
  )
  const waiting =
    countOf(deciding, CaseStatus.AwaitingHumanInput) +
    countOf(deciding, CaseStatus.ManualReviewRequired)
  // Less the cases waiting on a second approver after the caller's own approval, as the
  // Needs you view leaves them out.
  const own = useOwnApprovals({}, waiting > 0)
  const needsYou = Math.max(0, waiting - own.items.length)
  const sidebarPacks = starred.length
    ? registry.packs.filter(p => starred.includes(p.id))
    : registry.packs.filter(canEditPack)
  const processing = countOf(pulse.counts, CaseStatus.Processing)
  // Only what the agent is doing: the Needs you count is already on Cases.
  const agentLine = pulse.live
    ? processing
      ? `Agent working on ${processing} case${processing === 1 ? "" : "s"}`
      : pulse.label
    : pulse.tone === "attention" && !pulse.counts
      ? "Agent status unknown"
      : "Agent idle"
  const at = (prefix: string) => pathname === prefix || pathname.startsWith(`${prefix}/`)

  return (
    <Sidebar collapsible="icon" role="navigation" aria-label="Main">
      <SidebarHeader>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton size="lg" render={<Link to="/cases" />} tooltip={brand.name}>
              <span className="flex aspect-square size-8 items-center justify-center rounded-lg bg-primary text-xs font-semibold text-primary-foreground">
                {brand.monogram}
              </span>
              <span className="grid flex-1 text-left leading-tight">
                <span className="truncate text-sm font-semibold">{brand.name}</span>
                <span className="truncate text-xs text-muted-foreground">{brand.subtitle}</span>
              </span>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarHeader>

      <SidebarContent>
        <SidebarGroup>
          <SidebarMenu>
            <SidebarMenuItem>
              <SidebarMenuButton
                render={<Link to="/cases" />}
                isActive={at("/cases")}
                tooltip={needsYou ? `Cases · ${needsYou} need you` : "Cases"}
              >
                <Inbox />
                <span>Cases</span>
              </SidebarMenuButton>
              {needsYou > 0 && (
                <SidebarMenuBadge aria-label={`${needsYou} need you`}>
                  {needsYou > 99 ? "99+" : needsYou}
                </SidebarMenuBadge>
              )}
            </SidebarMenuItem>
            {ticketing && (
              <SidebarMenuItem>
                <SidebarMenuButton
                  render={<Link to="/tickets" />}
                  isActive={at("/tickets")}
                  tooltip="Tickets"
                >
                  <Ticket />
                  <span>Tickets</span>
                </SidebarMenuButton>
              </SidebarMenuItem>
            )}
          </SidebarMenu>
        </SidebarGroup>

        <SidebarGroup>
          <SidebarGroupLabel>Packs</SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              <SidebarMenuItem>
                <SidebarMenuButton
                  render={<Link to="/packs" />}
                  isActive={pathname === "/packs"}
                  tooltip="All packs"
                >
                  <LayoutGrid />
                  <span>All packs</span>
                </SidebarMenuButton>
              </SidebarMenuItem>
              {sidebarPacks.map(p => (
                <SidebarMenuItem key={p.id}>
                  <SidebarMenuButton
                    render={<Link to={`/packs/${encodeURIComponent(p.id)}`} />}
                    isActive={at(`/packs/${p.id}`)}
                    tooltip={p.display_name}
                  >
                    <Package />
                    <span className="truncate">{p.display_name}</span>
                    <SandboxMarker pack={p} />
                  </SidebarMenuButton>
                </SidebarMenuItem>
              ))}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>

        <SidebarGroup>
          <SidebarGroupLabel>Deployment</SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              <SidebarMenuItem>
                <SidebarMenuButton
                  render={<Link to="/deployment" />}
                  isActive={at("/deployment")}
                  tooltip="Deployment settings"
                >
                  <Server />
                  <span>Deployment settings</span>
                </SidebarMenuButton>
              </SidebarMenuItem>
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>

        {monitoring.length > 0 && (
          <SidebarGroup>
            <SidebarGroupLabel>Monitoring</SidebarGroupLabel>
            <SidebarGroupContent>
              <SidebarMenu>
                {monitoring.map(link => (
                  <SidebarMenuItem key={link.label}>
                    <SidebarMenuButton
                      // The button's children become the anchor's content through `render`,
                      // which the lint rule cannot see.
                      // eslint-disable-next-line jsx-a11y/anchor-has-content
                      render={<a href={link.href} target="_blank" rel="noopener noreferrer" />}
                      tooltip={`${link.label} (opens in a new tab)`}
                    >
                      {link.kind === "dashboard" ? <ChartLine /> : <Waypoints />}
                      <span>{link.label}</span>
                      <ArrowUpRight className="ml-auto text-muted-foreground" />
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                ))}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>
        )}
      </SidebarContent>

      <SidebarFooter>
        <SidebarMenu>
          <SidebarMenuItem>
            <div
              role="status"
              aria-label="Agent status"
              className="flex h-8 items-center gap-2 px-2 text-xs text-muted-foreground group-data-[collapsible=icon]:justify-center group-data-[collapsible=icon]:px-0"
            >
              <span
                aria-hidden="true"
                className={
                  pulse.live
                    ? "size-2 flex-none rounded-full bg-progress motion-safe:animate-pulse"
                    : "size-2 flex-none rounded-full bg-neutral"
                }
              />
              <span className="truncate group-data-[collapsible=icon]:sr-only">{agentLine}</span>
            </div>
          </SidebarMenuItem>
        </SidebarMenu>
        <NavUser />
      </SidebarFooter>
      <SidebarRail />
    </Sidebar>
  )
}
