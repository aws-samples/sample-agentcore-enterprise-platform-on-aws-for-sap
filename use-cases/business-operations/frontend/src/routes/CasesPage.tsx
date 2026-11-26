// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useMemo, useState } from "react"
import { useNavigate, useSearchParams } from "react-router"
import { useAuth } from "react-oidc-context"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import {
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  ListFilter,
  Play,
  Search,
  Sparkles,
  X,
} from "lucide-react"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Checkbox } from "@/components/ui/checkbox"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "@/components/ui/empty"
import { InputGroup, InputGroupAddon, InputGroupInput } from "@/components/ui/input-group"
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select"
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { CaseMarker } from "@/components/SandboxMarker"
import { FirstRunBanner } from "@/components/FirstRunBanner"
import { TruncatedText } from "@/components/TruncatedText"
import { notifyWorkEnqueued } from "@/hooks/useCaseCounts"
import {
  caseListState,
  useCaseList,
  useDoneSummary,
  useExceptionTypeCounts,
  useViewCounts,
  type CaseSort,
} from "@/hooks/useCaseQuery"
import { useRegistry } from "@/hooks/useRegistry"
import { useShell } from "@/hooks/useAgentChat"
import { useApi } from "@/hooks/useApi"
import { useDemoFeatures } from "@/hooks/useDemoEnabled"
import { enqueueCases, queryCasePages, type CaseSummaryTotals } from "@/services/casesService"
import { fetchTickets } from "@/services/ticketsService"
import { digest } from "@/lib/digest"
import { BRIEFING_HOURS, narrationPrompt, queueOrder } from "@/lib/briefing"
import { operatorFirstName, operatorIdentities } from "@/lib/identity"
import {
  VIEWS,
  caseState,
  documentLabel,
  exceptionLabel,
  formatAmount,
  API_VIEW,
  tableFields,
  TONE_DOT,
  waitingFor,
  type CaseView,
} from "@/lib/caseViews"
import { cn } from "@/lib/utils"
import { mayProcess } from "@/lib/registry"
import { processReason } from "@/services/controlsService"
import { ReasonButton } from "@/components/ReasonButton"
import { When } from "@/components/When"

/** The Done view's summary windows; the route reaches back 30 days at most. */
const WINDOWS = [
  { hours: 24, label: "Last 24 hours" },
  { hours: 24 * 7, label: "Last 7 days" },
  { hours: 24 * 30, label: "Last 30 days" },
]

/** "12 done · 182,400.50 USD released · 3.42 USD agent cost", from the summary's totals. */
function summaryLine(t: CaseSummaryTotals): string {
  const released = Object.entries(t.by_outcome.released?.value ?? {})
    .map(([currency, n]) => `${formatAmount(n)} ${currency}`)
    .join(" and ")
  return [
    `${t.closed.toLocaleString("en-US")} done`,
    released && `${released} released`,
    `${t.agent_cost_usd.toFixed(2)} USD agent cost`,
  ]
    .filter(Boolean)
    .join(" · ")
}

/** An exception type's id in words: `price_variance` → "Price variance". */
function typeLabel(id: string): string {
  const words = id.replace(/_/g, " ")
  return words.charAt(0).toUpperCase() + words.slice(1)
}

/** What the Briefing button asks, as the person sees it in the assistant (decision 20). */
export const BRIEFING_QUESTION = "Give me a briefing"

/**
 * Cases (preview 1.5, decision 18): a full-width table in views, one line per row.
 * The view, search, filters and sort live in the URL, so a link opens the same list.
 * J and K move through the rows, Enter opens one. Starting runs is the only bulk action,
 * and only in Not started.
 */
export default function CasesPage() {
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()
  const auth = useAuth()
  const getToken = useApi()
  const queryClient = useQueryClient()
  const { chat, openAssistant } = useShell()
  const { ticketing } = useDemoFeatures()
  const registry = useRegistry()
  const state = caseListState(params)
  const { view, q, exceptions, sort } = state
  const packFilter = state.packs
  const list = useCaseList(state)
  const views = useViewCounts(state)
  const [facetOpen, setFacetOpen] = useState(false)
  const typeCounts = useExceptionTypeCounts(state, facetOpen)
  const tickets = useQuery({
    queryKey: ["tickets", "all"],
    queryFn: async () => fetchTickets({}, await getToken()),
    enabled: auth.isAuthenticated && ticketing,
  })

  // Old links: /cases?case=x opens the case.
  const legacy = params.get("case")
  useEffect(() => {
    if (legacy) navigate(`/cases/${encodeURIComponent(legacy)}`, { replace: true })
  }, [legacy, navigate])

  const set = (patch: Record<string, string | string[] | null>) => {
    const next = new URLSearchParams(params)
    for (const [k, v] of Object.entries(patch)) {
      next.delete(k)
      if (Array.isArray(v)) v.forEach(x => next.append(k, x))
      else if (v) next.set(k, v)
    }
    setParams(next, { replace: true })
  }

  const me = operatorIdentities(auth.user?.profile)
  const multi = registry.packs.length > 1
  // The pack's own columns, from the one pack the list shows; none when it shows several.
  const shown = packFilter.length === 1 ? registry.byId[packFilter[0]] : undefined
  const single = registry.packs.length <= 1 ? registry.packs[0] : shown
  const fields = tableFields(single)

  const counts = Object.fromEntries(
    VIEWS.map(v => [v.id, views.data?.views[API_VIEW[v.id]] ?? 0])
  ) as Record<CaseView, number>
  // The exception types the packs in view declare, by id: the server matches a case's
  // identified exception type, or its process type while it has none.
  const facetValues = useMemo(() => {
    const packs = packFilter.length
      ? packFilter.map(id => registry.byId[id]).filter(Boolean)
      : registry.packs
    const ids = packs.flatMap(p =>
      (p.sops ?? []).filter(s => s.kind !== "alias").map(s => s.process_type)
    )
    return [...new Set(ids.length ? ids : packs.flatMap(p => p.process_types))].sort()
  }, [packFilter, registry.byId, registry.packs])
  const rows = list.rows
  const amountReason = single ? null : "Sorting by amount needs one pack: filter by a pack first."
  const effectiveSort: CaseSort = sort ?? (view === "done" || view === "all" ? "newest" : "oldest")

  // Keyboard: J and K move, Enter opens, X selects (Not started only).
  const [cursor, setCursor] = useState(0)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const bulk = view === "not-started"
  useEffect(() => {
    setCursor(0)
    setSelected(new Set())
  }, [view])
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement
      if (
        e.metaKey ||
        e.ctrlKey ||
        e.altKey ||
        /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName) ||
        target.isContentEditable
      )
        return
      // Not inside a menu or a dialog, which have keys of their own; and Enter or X only when
      // focus is on no control, so Enter on a focused link or button still activates it.
      if (target.closest("[role='dialog'], [role='menu'], [role='listbox']")) return
      const onControl = !!target.closest(
        "a, button, [role='button'], [role='tab'], [role='checkbox'], [role='menuitem']"
      )
      if (e.key === "j") setCursor(i => Math.min(i + 1, rows.length - 1))
      else if (e.key === "k") setCursor(i => Math.max(i - 1, 0))
      else if (e.key === "Enter" && !onControl && rows[cursor])
        navigate(caseHref(rows[cursor].case_id))
      else if (e.key === "x" && !onControl && bulk && rows[cursor]) toggle(rows[cursor].case_id)
      else return
      e.preventDefault()
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  })

  // The case page steps through the same list, so it carries the list's state.
  const caseHref = (id: string) => {
    const carried = new URLSearchParams(params)
    carried.delete("case")
    if (!carried.get("view")) carried.set("view", view)
    return `/cases/${encodeURIComponent(id)}?${carried}`
  }
  const toggle = (id: string) =>
    setSelected(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  const [starting, setStarting] = useState(false)
  const startRuns = async () => {
    const ids = [...selected]
    setStarting(true)
    try {
      await enqueueCases(ids, await getToken())
      notifyWorkEnqueued(queryClient)
      toast.success(`Started ${ids.length} run${ids.length === 1 ? "" : "s"}`, {
        description: "They show in Agent working.",
      })
      setSelected(new Set())
    } catch (e) {
      toast.error("Couldn't start the runs", { description: (e as Error).message })
    } finally {
      setStarting(false)
    }
  }

  // The briefing reads what it narrates when asked, not at page load: the open views and
  // the done cases in its window.
  const briefing = async () => {
    const token = await getToken()
    const since = Date.now() - Number(BRIEFING_HOURS) * 3_600_000
    const open = await Promise.all(
      (["needs_you", "new", "running", "failed"] as const).map(v =>
        queryCasePages({ view: v, limit: 200 }, token, () => true)
      )
    )
    const done = await queryCasePages(
      { view: "done", sort: "updated_at", order: "desc", limit: 200 },
      token,
      page => page.some(c => new Date(c.updated_at).getTime() < since)
    )
    const all = [...open.flatMap(r => r.items), ...done.items]
    const packs = registry.byId
    const backlog = digest(all, Number.POSITIVE_INFINITY, tickets.data, packs)
    const recent = digest(all, Number(BRIEFING_HOURS), tickets.data, packs)
    const queue = queueOrder(backlog, me)
    openAssistant()
    void chat.narrate(
      narrationPrompt(
        operatorFirstName(auth.user?.profile),
        backlog,
        recent,
        BRIEFING_HOURS,
        queue[0]
      ),
      BRIEFING_QUESTION
    )
  }

  const windowHours = WINDOWS.some(w => String(w.hours) === params.get("window"))
    ? Number(params.get("window"))
    : 24
  const summary = useDoneSummary(view === "done", windowHours, packFilter)

  return (
    <div className="flex h-full flex-col">
      {/* The view tabs say where you are, so the title is for screen readers only. */}
      <h1 className="sr-only">Cases</h1>
      {views.data && (
        <div className="flex-none px-6 pt-3">
          <FirstRunBanner
            total={counts.all}
            started={counts.all - counts["not-started"]}
            done={counts.done}
          />
        </div>
      )}
      {/* Too narrow for both, Briefing moves above the tabs, which stay on the rule; too
          narrow for the tabs alone, they scroll. Either way the page cuts off no label.
          pb-px -mb-px keeps the active tab's line, which overlaps the rule, in the scroll box. */}
      <div className="flex flex-none flex-wrap-reverse items-center gap-x-2 border-b px-6">
        <Tabs
          className="-mb-px max-w-full min-w-0 overflow-x-auto overflow-y-hidden pb-px"
          value={view}
          onValueChange={v => set({ view: String(v), exception: null, sort: null })}
        >
          <TabsList variant="line" className="h-(--toolbar-height)">
            {VIEWS.map(v => (
              <TabsTrigger key={v.id} value={v.id}>
                {v.label}
                <span className="text-xs tabular-nums">{counts[v.id]}</span>
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>
        <Button
          variant="ghost"
          size="sm"
          className="ml-auto shrink-0"
          onClick={() => void briefing()}
          disabled={!chat.ready}
        >
          <Sparkles className="text-progress" />
          Briefing
        </Button>
      </div>

      <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-6 pb-ask">
        {view === "done" && (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <NativeSelect
              size="sm"
              aria-label="Summary window"
              value={String(windowHours)}
              onChange={e => set({ window: e.target.value === "24" ? null : e.target.value })}
            >
              {WINDOWS.map(w => (
                <NativeSelectOption key={w.hours} value={String(w.hours)}>
                  {w.label}
                </NativeSelectOption>
              ))}
            </NativeSelect>
            <span>{summary.data ? summaryLine(summary.data.totals) : "…"}</span>
          </div>
        )}

        <div className="flex flex-wrap items-center gap-2">
          <InputGroup className="w-64">
            <InputGroupAddon>
              <Search />
            </InputGroupAddon>
            <InputGroupInput
              aria-label="Search cases"
              placeholder="Search"
              value={q}
              onChange={e => set({ q: e.target.value || null })}
            />
          </InputGroup>
          {exceptions.map(x => (
            <Badge key={x} variant="secondary" className="gap-1">
              Exception: {typeLabel(x)}
              <Button
                variant="ghost"
                size="icon-xs"
                aria-label={`Remove filter ${typeLabel(x)}`}
                onClick={() => set({ exception: exceptions.filter(e => e !== x) })}
              >
                <X />
              </Button>
            </Badge>
          ))}
          {packFilter.map(id => (
            <Badge key={id} variant="secondary" className="gap-1">
              Pack: {registry.byId[id]?.display_name ?? id}
              <Button
                variant="ghost"
                size="icon-xs"
                aria-label={`Remove filter ${registry.byId[id]?.display_name ?? id}`}
                onClick={() => set({ pack: packFilter.filter(p => p !== id) })}
              >
                <X />
              </Button>
            </Badge>
          ))}
          <DropdownMenu open={facetOpen} onOpenChange={setFacetOpen}>
            <DropdownMenuTrigger render={<Button variant="outline" size="sm" />}>
              <ListFilter />
              Filter
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start">
              <DropdownMenuGroup>
                <DropdownMenuLabel>Exception</DropdownMenuLabel>
                {facetValues.map(x => (
                  <DropdownMenuCheckboxItem
                    key={x}
                    checked={exceptions.includes(x)}
                    onCheckedChange={on =>
                      set({ exception: on ? [...exceptions, x] : exceptions.filter(e => e !== x) })
                    }
                  >
                    {typeLabel(x)}
                    <span className="ml-auto pl-4 text-xs tabular-nums text-muted-foreground">
                      {typeCounts.data
                        ? (typeCounts.data[API_VIEW[view]]?.[x] ?? 0).toLocaleString()
                        : ""}
                    </span>
                  </DropdownMenuCheckboxItem>
                ))}
              </DropdownMenuGroup>
              <DropdownMenuSeparator />
              <DropdownMenuGroup>
                <DropdownMenuLabel>Pack</DropdownMenuLabel>
                {registry.packs.map(p => (
                  <DropdownMenuCheckboxItem
                    key={p.id}
                    checked={packFilter.includes(p.id)}
                    onCheckedChange={on =>
                      set({
                        pack: on ? [...packFilter, p.id] : packFilter.filter(x => x !== p.id),
                      })
                    }
                  >
                    {p.display_name}
                  </DropdownMenuCheckboxItem>
                ))}
              </DropdownMenuGroup>
            </DropdownMenuContent>
          </DropdownMenu>
          {bulk && selected.size > 0 && (
            <ReasonButton
              size="sm"
              onClick={() => void startRuns()}
              disabled={starting}
              reason={
                rows.some(r => selected.has(r.case_id) && !mayProcess(registry.byId[r.pack_id]))
                  ? processReason(undefined)
                  : null
              }
            >
              <Play />
              {starting
                ? "Starting…"
                : `Start ${selected.size} run${selected.size === 1 ? "" : "s"}`}
            </ReasonButton>
          )}
          <NativeSelect
            size="sm"
            className="ml-auto"
            aria-label="Sort cases"
            value={effectiveSort}
            onChange={e => set({ sort: e.target.value })}
          >
            <NativeSelectOption value="oldest">Oldest first</NativeSelectOption>
            <NativeSelectOption value="newest">Newest first</NativeSelectOption>
            {/* The server sorts by amount on its own index, within one pack (6.5). */}
            <NativeSelectOption value="amount" disabled={!!amountReason}>
              {amountReason ? "Largest amount (one pack only)" : "Largest amount"}
            </NativeSelectOption>
            <NativeSelectOption value="amount-asc" disabled={!!amountReason}>
              {amountReason ? "Smallest amount (one pack only)" : "Smallest amount"}
            </NativeSelectOption>
          </NativeSelect>
        </div>

        {list.isError ? (
          <Alert variant="destructive">
            <AlertTitle>Couldn&apos;t load the cases</AlertTitle>
            <AlertDescription>{(list.error as Error).message}</AlertDescription>
          </Alert>
        ) : list.isLoading && !list.data ? (
          <div className="@container overflow-x-auto rounded-lg border bg-card" aria-busy="true">
            <Table className="min-w-2xl table-fixed">
              <TableHeader>
                <TableRow>
                  <TableHead className="w-44">Document</TableHead>
                  <TableHead>Exception</TableHead>
                  <TableHead className="w-40">Status</TableHead>
                  <TableHead className="w-40 text-right">Amount</TableHead>
                  <TableHead className="w-16 text-right">Age</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {Array.from({ length: 6 }, (_, i) => (
                  <TableRow key={i} className="h-9">
                    {Array.from({ length: 5 }, (_, j) => (
                      <TableCell key={j}>
                        <Skeleton className="h-4 w-full" />
                      </TableCell>
                    ))}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        ) : rows.length === 0 ? (
          <EmptyView
            view={view}
            counts={counts}
            filtered={!!q || exceptions.length > 0 || packFilter.length > 0}
            onView={v => set({ view: v })}
          />
        ) : (
          // Beside the open assistant the column narrows: the secondary columns go first,
          // and below the table's floor it scrolls sideways rather than squeezing a column.
          <div className="@container overflow-x-auto rounded-lg border bg-card">
            <Table className="min-w-2xl table-fixed">
              <TableHeader>
                <TableRow>
                  {bulk && (
                    <TableHead className="w-10">
                      <Checkbox
                        aria-label="Select all"
                        checked={selected.size > 0 && selected.size === rows.length}
                        onCheckedChange={on =>
                          setSelected(on ? new Set(rows.map(r => r.case_id)) : new Set())
                        }
                      />
                    </TableHead>
                  )}
                  <TableHead className="w-44">Document</TableHead>
                  {multi && <TableHead className="w-40">Pack</TableHead>}
                  <TableHead>Exception</TableHead>
                  <TableHead className="w-40">Status</TableHead>
                  {fields.amount && (
                    <TableHead
                      className="w-40 text-right"
                      aria-sort={
                        effectiveSort === "amount"
                          ? "descending"
                          : effectiveSort === "amount-asc"
                            ? "ascending"
                            : undefined
                      }
                    >
                      <Button
                        variant="ghost"
                        size="xs"
                        className="-mr-2"
                        onClick={() =>
                          set({ sort: effectiveSort === "amount" ? "amount-asc" : "amount" })
                        }
                      >
                        Amount
                        {effectiveSort === "amount" ? (
                          <ArrowDown aria-hidden="true" />
                        ) : effectiveSort === "amount-asc" ? (
                          <ArrowUp aria-hidden="true" />
                        ) : (
                          <ArrowUpDown aria-hidden="true" className="text-muted-foreground" />
                        )}
                      </Button>
                    </TableHead>
                  )}
                  {fields.party && (
                    <TableHead className="hidden @5xl:table-cell">{fields.party.label}</TableHead>
                  )}
                  {(view === "needs-you" || view === "team") && (
                    <TableHead className="hidden w-36 @4xl:table-cell">Waiting for</TableHead>
                  )}
                  <TableHead className="w-16 text-right">Age</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((c, i) => {
                  const doc = documentLabel(c)
                  const state = caseState(c)
                  const amount = fields.amount
                    ? formatAmount(c.attributes?.[fields.amount.attribute])
                    : null
                  const currency = fields.amount?.currency
                    ? c.attributes?.[fields.amount.currency]
                    : null
                  const party = fields.party
                    ? String(c.attributes?.[fields.party.attribute] ?? "")
                    : ""
                  return (
                    <TableRow
                      key={c.case_id}
                      data-state={selected.has(c.case_id) ? "selected" : undefined}
                      aria-current={i === cursor ? "true" : undefined}
                      className={cn("h-9 cursor-pointer", i === cursor && "bg-muted")}
                      onClick={() => navigate(caseHref(c.case_id))}
                    >
                      {bulk && (
                        <TableCell onClick={e => e.stopPropagation()}>
                          <Checkbox
                            aria-label={`Select ${doc.number}`}
                            checked={selected.has(c.case_id)}
                            onCheckedChange={() => toggle(c.case_id)}
                          />
                        </TableCell>
                      )}
                      <TableCell className="truncate font-mono">
                        <span className="inline-flex items-center gap-2">
                          <span className="truncate">
                            {doc.number}
                            {doc.year && (
                              <span className="text-muted-foreground"> · {doc.year}</span>
                            )}
                          </span>
                          <CaseMarker c={c} pack={registry.byId[c.pack_id]} />
                        </span>
                      </TableCell>
                      {multi && (
                        <TableCell className="text-muted-foreground">
                          <TruncatedText
                            text={registry.byId[c.pack_id]?.display_name ?? c.pack_id}
                          />
                        </TableCell>
                      )}
                      <TableCell>
                        <TruncatedText text={exceptionLabel(c)} />
                      </TableCell>
                      <TableCell>
                        <span className="inline-flex items-center gap-2">
                          <span
                            aria-hidden="true"
                            className={cn("size-2 rounded-full", TONE_DOT[state.tone])}
                          />
                          {state.label}
                        </span>
                      </TableCell>
                      {fields.amount && (
                        <TableCell className="text-right tabular-nums">
                          {amount}
                          {currency && (
                            <span className="ml-1 text-xs text-muted-foreground">
                              {String(currency)}
                            </span>
                          )}
                        </TableCell>
                      )}
                      {fields.party && (
                        <TableCell className="hidden text-muted-foreground @5xl:table-cell">
                          <TruncatedText text={party} />
                        </TableCell>
                      )}
                      {(view === "needs-you" || view === "team") && (
                        <TableCell className="hidden text-muted-foreground @4xl:table-cell">
                          <TruncatedText text={waitingFor(c, me) ?? ""} />
                        </TableCell>
                      )}
                      <TableCell className="text-right tabular-nums text-muted-foreground">
                        <When at={c.updated_at} age focusable={false} />
                      </TableCell>
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
          </div>
        )}
        {list.hasNextPage && (
          <Button
            variant="outline"
            size="sm"
            className="self-center"
            disabled={list.isFetchingNextPage}
            onClick={() => void list.fetchNextPage()}
          >
            {list.isFetchingNextPage && <Spinner />}
            {list.isFetchingNextPage ? "Loading…" : "Load more"}
          </Button>
        )}
      </div>
    </div>
  )
}

function EmptyView({
  view,
  counts,
  filtered,
  onView,
}: {
  view: CaseView
  counts: Record<CaseView, number>
  filtered: boolean
  onView: (v: CaseView) => void
}) {
  if (filtered)
    return (
      <Empty>
        <EmptyHeader>
          <EmptyTitle>Nothing matches</EmptyTitle>
          <EmptyDescription>Clear the search or a filter to see more.</EmptyDescription>
        </EmptyHeader>
      </Empty>
    )
  const working = counts.working
  const titles: Record<CaseView, string> = {
    "needs-you": "Nothing needs you",
    team: "Nothing is waiting on a person",
    "not-started": "Nothing waiting to start",
    working: "The agent is not working on anything",
    done: "Nothing done yet",
    failed: "Nothing failed",
    all: "No cases yet",
  }
  return (
    <Empty>
      <EmptyHeader>
        <EmptyTitle>{titles[view]}</EmptyTitle>
        <EmptyDescription>
          {view !== "working" && working > 0 ? (
            <>
              The agent is working on {working} case{working === 1 ? "" : "s"}.{" "}
              <Button variant="link" className="h-auto p-0" onClick={() => onView("working")}>
                Agent working
              </Button>
            </>
          ) : view === "all" ? (
            "New SAP exceptions appear here once the poller or SAP's events open a case."
          ) : (
            <Button variant="link" className="h-auto p-0" onClick={() => onView("all")}>
              See all cases
            </Button>
          )}
        </EmptyDescription>
      </EmptyHeader>
    </Empty>
  )
}
