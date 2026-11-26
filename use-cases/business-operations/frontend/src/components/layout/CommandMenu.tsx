// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState } from "react"
import { useNavigate } from "react-router"
import { FileText, Inbox, LayoutGrid, Package, Server, Sparkles, Ticket } from "lucide-react"
import {
  Command,
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command"
import { useRegistry } from "@/hooks/useRegistry"
import { useQuery } from "@tanstack/react-query"
import { useAuth } from "react-oidc-context"
import { useApi } from "@/hooks/useApi"
import { queryCases } from "@/services/casesService"
import { useDemoFeatures } from "@/hooks/useDemoEnabled"

/**
 * ⌘K: go to a page, a pack or a case by its document number, or ask the assistant about
 * what you typed. Cases come from the server's search (`GET /cases?q=`, a prefix of the
 * document number, party or case id) once three characters are typed, eight at most.
 */
export function CommandMenu({
  open,
  onOpenChange,
  onAsk,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  onAsk: (question: string) => void
}) {
  const navigate = useNavigate()
  const registry = useRegistry()
  const { ticketing } = useDemoFeatures()
  const auth = useAuth()
  const getToken = useApi()
  const [query, setQuery] = useState("")
  const needle = query.trim()
  const cases = useQuery({
    queryKey: ["cases", "lookup", needle],
    queryFn: async () => (await queryCases({ q: needle, limit: 8 }, await getToken())).items,
    enabled: open && auth.isAuthenticated && needle.length >= 3,
  })

  const go = (to: string) => {
    onOpenChange(false)
    setQuery("")
    navigate(to)
  }

  const pages = [
    { label: "Cases", to: "/cases", icon: Inbox },
    ...(ticketing ? [{ label: "Tickets", to: "/tickets", icon: Ticket }] : []),
    { label: "All packs", to: "/packs", icon: LayoutGrid },
    { label: "Deployment settings", to: "/deployment", icon: Server },
  ]

  return (
    <CommandDialog
      open={open}
      onOpenChange={onOpenChange}
      title="Search"
      description="Search cases, packs and pages"
    >
      <Command>
        <CommandInput
          placeholder="Search cases, packs, SOPs"
          value={query}
          onValueChange={setQuery}
        />
        <CommandList>
          <CommandEmpty>Nothing matches.</CommandEmpty>
          {query.trim() && (
            <CommandGroup heading="Assistant">
              <CommandItem
                value={`ask ${query}`}
                onSelect={() => {
                  onOpenChange(false)
                  onAsk(query.trim())
                  setQuery("")
                }}
              >
                <Sparkles />
                Ask the assistant about “{query.trim()}”
              </CommandItem>
            </CommandGroup>
          )}
          {needle.length >= 3 && (cases.data?.length ?? 0) > 0 && (
            <CommandGroup heading="Cases">
              {cases.data!.map(c => (
                <CommandItem
                  key={c.case_id}
                  // The server already matched; the query keeps cmdk from filtering it out.
                  value={`${needle} ${c.document_number ?? ""} ${c.case_id}`}
                  onSelect={() => go(`/cases/${encodeURIComponent(c.case_id)}?view=all`)}
                >
                  <FileText />
                  <span className="font-mono">{c.document_number ?? c.case_id}</span>
                  <span className="truncate text-muted-foreground">{c.title}</span>
                </CommandItem>
              ))}
            </CommandGroup>
          )}
          <CommandGroup heading="Packs">
            {registry.packs.map(p => (
              <CommandItem
                key={p.id}
                value={`${p.display_name} ${p.id}`}
                onSelect={() => go(`/packs/${encodeURIComponent(p.id)}`)}
              >
                <Package />
                {p.display_name}
              </CommandItem>
            ))}
          </CommandGroup>
          <CommandGroup heading="Pages">
            {pages.map(p => (
              <CommandItem key={p.to} value={p.label} onSelect={() => go(p.to)}>
                <p.icon />
                {p.label}
              </CommandItem>
            ))}
          </CommandGroup>
        </CommandList>
      </Command>
    </CommandDialog>
  )
}
