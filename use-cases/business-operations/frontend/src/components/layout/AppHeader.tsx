// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { Fragment } from "react"
import { Link, useLocation } from "react-router"
import { Search } from "lucide-react"
import { SidebarTrigger } from "@/components/ui/sidebar"
import { Separator } from "@/components/ui/separator"
import { Button } from "@/components/ui/button"
import { Kbd, KbdGroup } from "@/components/ui/kbd"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "@/components/ui/breadcrumb"
import { skipToken, useQuery } from "@tanstack/react-query"
import { useRegistry } from "@/hooks/useRegistry"
import { humanise } from "@/lib/registry"
import { documentLabel } from "@/lib/caseViews"
import type { WorkItem } from "@/types/cases"

interface Crumb {
  label: string
  to?: string
}

/** Where you are, from the path: one label at the top level, a trail from level two. */
export function useCrumbs(): Crumb[] {
  const { pathname } = useLocation()
  const registry = useRegistry()
  const [first, second, third, fourth] = pathname.split("/").filter(Boolean)
  // The case page's own query, read from the cache: the crumb names the document, as the
  // page title does, once the case has loaded.
  const caseId = first === "cases" && second ? decodeURIComponent(second) : ""
  const opened = useQuery<WorkItem>({ queryKey: ["case", caseId], queryFn: skipToken })
  switch (first) {
    case "cases":
      if (!second) return [{ label: "Cases" }]
      return [
        { label: "Cases", to: "/cases" },
        { label: opened.data ? documentLabel(opened.data).number : "Case" },
      ]
    case "tickets":
      return [{ label: "Tickets" }]
    case "deployment":
      return [{ label: "Deployment settings" }]
    case "packs": {
      if (!second) return [{ label: "Packs" }]
      const id = decodeURIComponent(second)
      const pack = { label: registry.byId[id]?.display_name ?? id, to: `/packs/${second}` }
      if (!third) return [{ label: "Packs", to: "/packs" }, { label: pack.label }]
      const trail = [{ label: "Packs", to: "/packs" }, pack]
      if (third === "rehearse") return [...trail, { label: "Rehearse" }]
      const procedures = { label: "Procedures", to: `/packs/${second}?tab=procedures` }
      if (third === "instructions") return [...trail, procedures, { label: "General instructions" }]
      if (third === "procedures" && fourth)
        return [...trail, procedures, { label: humanise(decodeURIComponent(fourth)) }]
      return trail
    }
    default:
      return []
  }
}

/**
 * The 48 px band above every page: the sidebar toggle, the breadcrumb, and search.
 * No assistant button: the ask bar at the foot of the page is the way in (decision 20).
 */
export function AppHeader({ onSearch }: { onSearch: () => void }) {
  const crumbs = useCrumbs()
  return (
    <header className="flex h-(--header-height) flex-none items-center gap-2 border-b px-4">
      <Tooltip>
        <TooltipTrigger render={<SidebarTrigger className="-ml-1" />} />
        <TooltipContent>
          Toggle sidebar{" "}
          <KbdGroup>
            <Kbd>⌘</Kbd>
            <Kbd>B</Kbd>
          </KbdGroup>
        </TooltipContent>
      </Tooltip>
      <Separator
        orientation="vertical"
        className="mr-1 data-vertical:h-4 data-vertical:self-auto"
      />
      <Breadcrumb>
        <BreadcrumbList>
          {crumbs.map((c, i) => (
            <Fragment key={`${c.label}-${i}`}>
              {i > 0 && <BreadcrumbSeparator />}
              <BreadcrumbItem>
                {c.to ? (
                  <BreadcrumbLink render={<Link to={c.to} />}>{c.label}</BreadcrumbLink>
                ) : (
                  <BreadcrumbPage>{c.label}</BreadcrumbPage>
                )}
              </BreadcrumbItem>
            </Fragment>
          ))}
        </BreadcrumbList>
      </Breadcrumb>
      <Button
        variant="outline"
        size="sm"
        onClick={onSearch}
        className="ml-auto w-64 justify-start text-muted-foreground"
      >
        <Search />
        Search cases, packs, SOPs
        <KbdGroup className="ml-auto">
          <Kbd>⌘</Kbd>
          <Kbd>K</Kbd>
        </KbdGroup>
      </Button>
    </header>
  )
}
