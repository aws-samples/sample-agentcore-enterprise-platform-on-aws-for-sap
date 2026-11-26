// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useMemo } from "react"
import { Link, useSearchParams } from "react-router"
import { useAuth } from "react-oidc-context"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { ArrowRight, X } from "lucide-react"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Badge } from "@/components/ui/badge"
import { Button, buttonVariants } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "@/components/ui/empty"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { Separator } from "@/components/ui/separator"
import { TruncatedText } from "@/components/TruncatedText"
import { DecisionPanel, type Decided } from "@/components/case/DecisionPanel"
import { useApi } from "@/hooks/useApi"
import { fetchTickets } from "@/services/ticketsService"
import { fetchCase } from "@/services/casesService"
import { tryNormalizeCaseId } from "@/lib/caseKey"
import { TONE_DOT, type Tone } from "@/lib/caseViews"
import { When } from "@/components/When"
import { TicketStatus, type Ticket } from "@/types/tickets"
import { cn } from "@/lib/utils"

/** The status tabs, in the order a ticket moves through them. */
const TABS: { id: TicketStatus | "all"; label: string }[] = [
  { id: TicketStatus.Open, label: "Open" },
  { id: TicketStatus.Assigned, label: "Assigned" },
  { id: TicketStatus.Approved, label: "Approved" },
  { id: TicketStatus.Denied, label: "Denied" },
  { id: TicketStatus.Replied, label: "Replied" },
  { id: TicketStatus.Closed, label: "Closed" },
  { id: "all", label: "All" },
]

const TONE: Record<string, Tone> = {
  [TicketStatus.Open]: "attention",
  [TicketStatus.Assigned]: "attention",
  [TicketStatus.Approved]: "success",
  [TicketStatus.Denied]: "danger",
  [TicketStatus.Replied]: "progress",
  [TicketStatus.Closed]: "neutral",
}

const label = (status: string) => TABS.find(t => t.id === status)?.label ?? status

const isTab = (v: string | null): v is TicketStatus | "all" => TABS.some(t => t.id === v)

/**
 * Tickets (sample ticketing only): the table pattern (decision 18) with status tabs and their
 * counts, a case filter as a removable chip, and a ticket in a right sheet with the same
 * decision panel the case page has. The sample stands in for a deployment's own ticketing
 * tool, so a ticket decides its case through the ticket's action route.
 */
export default function TicketsPage() {
  const [params, setParams] = useSearchParams()
  const auth = useAuth()
  const getToken = useApi()
  const queryClient = useQueryClient()
  const statusParam = params.get("status")
  // An open ticket is the one that needs a person, so the page opens there.
  const status: TicketStatus | "all" = isTab(statusParam) ? statusParam : TicketStatus.Open
  const caseFilter = tryNormalizeCaseId(params.get("case_id"))
  const openId = params.get("ticket")

  const tickets = useQuery({
    queryKey: ["tickets", "all"],
    queryFn: async () => fetchTickets({}, await getToken()),
    enabled: auth.isAuthenticated,
  })

  const set = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(params)
    for (const [k, v] of Object.entries(patch)) {
      if (v) next.set(k, v)
      else next.delete(k)
    }
    setParams(next, { replace: true })
  }

  const forCase = useMemo(
    () =>
      (tickets.data ?? []).filter(t => !caseFilter || tryNormalizeCaseId(t.case_id) === caseFilter),
    [tickets.data, caseFilter]
  )
  const counts = useMemo(() => {
    const out: Record<string, number> = { all: forCase.length }
    for (const t of forCase) out[t.status] = (out[t.status] ?? 0) + 1
    return out
  }, [forCase])
  const rows = status === "all" ? forCase : forCase.filter(t => t.status === status)
  const opened = (tickets.data ?? []).find(t => t.ticket_id === openId) ?? null

  const refresh = async () => {
    await queryClient.invalidateQueries({ queryKey: ["tickets"] })
    if (opened?.case_id) await queryClient.invalidateQueries({ queryKey: ["case", opened.case_id] })
  }

  return (
    <div className="flex h-full flex-col">
      <h1 className="sr-only">Tickets</h1>
      <div className="flex flex-none items-center gap-2 border-b px-6">
        <Tabs value={status} onValueChange={v => set({ status: String(v) })}>
          <TabsList variant="line" className="h-(--toolbar-height)">
            {TABS.map(t => (
              <TabsTrigger key={t.id} value={t.id}>
                {t.label}
                <span className="text-xs tabular-nums">{counts[t.id] ?? 0}</span>
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>
      </div>

      <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-6 pb-ask">
        {caseFilter && (
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant="secondary" className="gap-1">
              Case: <span className="font-mono">{caseFilter}</span>
              <Button
                variant="ghost"
                size="icon-xs"
                aria-label="Remove the case filter"
                onClick={() => set({ case_id: null })}
              >
                <X />
              </Button>
            </Badge>
          </div>
        )}

        {tickets.isError ? (
          <Alert variant="destructive">
            <AlertTitle>Couldn&apos;t load the tickets</AlertTitle>
            <AlertDescription>{(tickets.error as Error).message}</AlertDescription>
          </Alert>
        ) : tickets.isLoading ? (
          <div className="flex flex-col gap-2" aria-busy="true">
            {Array.from({ length: 5 }, (_, i) => (
              <Skeleton key={i} className="h-9 w-full" />
            ))}
          </div>
        ) : rows.length === 0 ? (
          <Empty>
            <EmptyHeader>
              <EmptyTitle>
                {status === "all" ? "No tickets yet" : `No ${label(status).toLowerCase()} tickets`}
              </EmptyTitle>
              <EmptyDescription>
                The agent raises a ticket here when it asks a person to approve or answer.
              </EmptyDescription>
            </EmptyHeader>
          </Empty>
        ) : (
          <div className="rounded-lg border bg-card">
            <Table className="table-fixed">
              <TableHeader>
                <TableRow>
                  <TableHead className="w-28">Ticket</TableHead>
                  <TableHead>Title</TableHead>
                  <TableHead className="w-32">Status</TableHead>
                  <TableHead className="w-56">Assigned to</TableHead>
                  <TableHead className="w-16 text-right">Age</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map(t => (
                  <TableRow
                    key={t.ticket_id}
                    className="h-9 cursor-pointer"
                    data-state={t.ticket_id === openId ? "selected" : undefined}
                    onClick={() => set({ ticket: t.ticket_id })}
                  >
                    <TableCell className="font-mono">
                      <Button
                        variant="link"
                        size="sm"
                        className="h-auto p-0 font-mono text-foreground"
                        onClick={e => {
                          e.stopPropagation()
                          set({ ticket: t.ticket_id })
                        }}
                      >
                        {t.ticket_id}
                      </Button>
                    </TableCell>
                    <TableCell>
                      <TruncatedText text={t.title} />
                    </TableCell>
                    <TableCell>
                      <StatusDot status={t.status} />
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      <TruncatedText text={t.assigned_to ?? ""} />
                    </TableCell>
                    <TableCell className="text-right tabular-nums text-muted-foreground">
                      <When at={t.updated_at} age focusable={false} />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </div>

      <Sheet open={!!opened} onOpenChange={o => !o && set({ ticket: null })}>
        <SheetContent side="right" className="flex w-full flex-col gap-0 sm:max-w-lg">
          {opened && <TicketSheet ticket={opened} onChanged={refresh} />}
        </SheetContent>
      </Sheet>
    </div>
  )
}

function StatusDot({ status }: { status: string }) {
  return (
    <span className="inline-flex items-center gap-2">
      <span
        aria-hidden="true"
        className={cn("size-2 rounded-full", TONE_DOT[TONE[status] ?? "neutral"])}
      />
      {label(status)}
    </span>
  )
}

/** One ticket: what it asks, its decision panel when it waits for one, and its case. */
function TicketSheet({ ticket, onChanged }: { ticket: Ticket; onChanged: () => Promise<void> }) {
  const auth = useAuth()
  const getToken = useApi()
  const caseId = tryNormalizeCaseId(ticket.case_id)
  const c = useQuery({
    queryKey: ["case", caseId],
    queryFn: async () => fetchCase(caseId!, await getToken()),
    enabled: auth.isAuthenticated && !!caseId,
  })
  const waiting = ticket.status === TicketStatus.Open || ticket.status === TicketStatus.Assigned

  const decided = ({ outcome, awaitingSecond }: Decided) => {
    toast.success(awaitingSecond ? "Approval recorded: 1 of 2" : `${outcome.label}: sent`, {
      description: awaitingSecond
        ? "A different approver must approve this change too."
        : "The agent resumes with your decision.",
    })
    void onChanged()
  }

  return (
    <>
      <SheetHeader className="border-b">
        <SheetTitle className="flex items-center gap-2">
          <span className="font-mono">{ticket.ticket_id}</span>
          <StatusDot status={ticket.status} />
        </SheetTitle>
        <SheetDescription>{ticket.title}</SheetDescription>
      </SheetHeader>
      <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto p-4">
        {ticket.description && (
          <p className="text-sm leading-relaxed whitespace-pre-wrap">{ticket.description}</p>
        )}
        <dl className="flex flex-col gap-1 text-sm">
          {ticket.assigned_to && (
            <div className="flex gap-4">
              <dt className="w-28 flex-none text-muted-foreground">Assigned to</dt>
              <dd className="min-w-0 truncate">{ticket.assigned_to}</dd>
            </div>
          )}
          <div className="flex gap-4">
            <dt className="w-28 flex-none text-muted-foreground">Raised</dt>
            <dd>
              <When at={ticket.created_at} />
              {ticket.created_by ? ` by ${ticket.created_by}` : ""}
            </dd>
          </div>
          {ticket.resolution && (
            <div className="flex gap-4">
              <dt className="w-28 flex-none text-muted-foreground">Resolution</dt>
              <dd className="min-w-0">{ticket.resolution}</dd>
            </div>
          )}
        </dl>

        {caseId && (
          <Link
            to={`/cases/${encodeURIComponent(caseId)}?view=all`}
            className={cn(buttonVariants({ variant: "outline", size: "sm" }), "self-start")}
          >
            Open the case
            <ArrowRight />
          </Link>
        )}

        {waiting && caseId && c.data && (
          <DecisionPanel c={c.data} onDecided={decided} onRefresh={onChanged} />
        )}
        {waiting && caseId && c.isLoading && <Skeleton className="h-48 w-full" />}
        {c.isError && (
          <Alert variant="destructive">
            <AlertTitle>Couldn&apos;t open the case</AlertTitle>
            <AlertDescription>{(c.error as Error).message}</AlertDescription>
          </Alert>
        )}

        {(ticket.comments?.length ?? 0) > 0 && (
          <>
            <Separator />
            <section aria-label="Comments" className="flex flex-col gap-3">
              {ticket.comments!.map((m, i) => (
                <div key={i} className="flex flex-col gap-0.5 text-sm">
                  <p className="text-xs text-muted-foreground">
                    {m.author} · <When at={m.timestamp} />
                  </p>
                  <p>{m.text}</p>
                </div>
              ))}
            </section>
          </>
        )}
      </div>
    </>
  )
}
