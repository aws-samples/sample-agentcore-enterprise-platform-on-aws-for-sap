// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useRef, useState, type ReactNode } from "react"
import { Link, useNavigate, useParams, useSearchParams } from "react-router"
import { useAuth } from "react-oidc-context"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import {
  ArrowLeft,
  ArrowRight,
  ChevronDown,
  ChevronUp,
  Database,
  Play,
  Square,
  Ticket,
} from "lucide-react"
import { isLiveRunStale } from "@business-operations/run-client"
import { Button, buttonVariants } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Kbd } from "@/components/ui/kbd"
import { JsonBlock } from "@/components/ui/json-block"
import { DetailLayout } from "@/components/layout/DetailLayout"
import { CaseMarker } from "@/components/SandboxMarker"
import { CaseTimeline, HumanDecisions } from "@/components/case/CaseTimeline"
import { CaseRating } from "@/components/case/CaseRating"
import { MarkdownRenderer } from "@/components/chat/MarkdownRenderer"
import { DecisionDone, DecisionPanel, type Decided } from "@/components/case/DecisionPanel"
import { caseListState, useCaseList } from "@/hooks/useCaseQuery"
import { useTranscript } from "@/hooks/transcript"
import { toolTick } from "@/lib/toolLabel"
import type { Message } from "@/components/chat/types"
import { useRegistry } from "@/hooks/useRegistry"
import { useDemoFeatures } from "@/hooks/useDemoEnabled"
import { useApi } from "@/hooks/useApi"
import { useAssistant } from "@/hooks/useAgentChat"
import { fetchCase } from "@/services/casesService"
import { caseFields, caseReason, mayProcess } from "@/lib/registry"
import { usePackControls } from "@/hooks/usePack"
import { processReason } from "@/services/controlsService"
import { ReasonButton } from "@/components/ReasonButton"
import { rowHeadline, splitProse } from "@/lib/caseTimeline"
import {
  VIEWS,
  caseState,
  documentLabel,
  exceptionLabel,
  tableFields,
  TONE_DOT,
  type CaseView,
} from "@/lib/caseViews"
import { CaseStatus, EvidenceKind, type TraceSegment, type WorkItem } from "@/types/cases"
import { cn } from "@/lib/utils"
import { entityWords, evidenceRows, money } from "@/lib/evidence"
import { humanise, type UseCasePack } from "@/lib/registry"
import { describeTransport } from "@/lib/transports"
import { When } from "@/components/When"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"

const TONE_BADGE: Record<string, string> = {
  attention: "bg-attention-wash text-attention-ink",
  progress: "bg-progress-wash text-progress-ink",
  success: "bg-success-wash text-success-ink",
  danger: "bg-danger-wash text-danger-ink",
  neutral: "bg-neutral-wash text-foreground",
}

/**
 * One case (preview 1.4): what the agent found and read, then what it needs from you.
 * Opened from a Cases view, it steps through that view with J and K. The run streams onto
 * this page while the agent works, so the assistant stays closed.
 */
export default function CasePage() {
  const { caseId = "" } = useParams()
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const auth = useAuth()
  const getToken = useApi()
  const queryClient = useQueryClient()
  const chat = useAssistant()
  const registry = useRegistry()
  // The list this case was opened from, as the Cases page queried it: same state, same cache.
  const listState = caseListState(params)
  const list = useCaseList(listState)
  const { ticketing } = useDemoFeatures()

  const view: CaseView = listState.view
  const query = useQuery({
    queryKey: ["case", caseId],
    queryFn: async () => fetchCase(caseId, await getToken()),
    enabled: auth.isAuthenticated && !!caseId,
    refetchInterval: q =>
      q.state.data?.status === CaseStatus.Processing || chat.isLoading ? 4_000 : false,
  })
  const c = query.data

  // The stepper walks the list this case was opened from, in the order it showed.
  const siblings = list.rows
  const index = siblings.findIndex(x => x.case_id === caseId)
  const go = (id: string) => navigate(`/cases/${encodeURIComponent(id)}?${params}`)
  // The decision taken on this page, kept while the person stays on its case: the panel shows
  // it in place of the form, so the same case cannot be decided twice from here.
  const [done, setDone] = useState<(Decided & { caseId: string; next: WorkItem | null }) | null>(
    null
  )
  const decidedHere = done?.caseId === caseId ? done : null
  const step = (d: number) => {
    // A decided case may have left the view; J then goes to the case it named as next.
    if (index < 0 && d > 0 && decidedHere?.next) return go(decidedHere.next.case_id)
    const next = siblings[index + d]
    if (next) go(next.case_id)
  }
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement
      if (
        e.metaKey ||
        e.ctrlKey ||
        /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) ||
        t.isContentEditable ||
        t.closest("[role='dialog'], [role='menu'], [role='listbox']")
      )
        return
      if (e.key === "j") step(1)
      else if (e.key === "k") step(-1)
      else return
      e.preventDefault()
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  })

  const refresh = async () => {
    await queryClient.invalidateQueries({ queryKey: ["case", caseId] })
    await queryClient.invalidateQueries({ queryKey: ["agent-pulse"] })
  }

  // After a decision the case stays on screen and the panel says what was sent: moving on is
  // the person's own step (Next case, or J), never automatic. No undo: once the agent
  // resumes, the write cannot be withdrawn.
  const decided = (d: Decided) => {
    const others = siblings.filter(x => x.case_id !== caseId)
    const at = Math.max(index, 0)
    const next = others[at] ?? others[at - 1] ?? null
    setDone({ ...d, caseId, next })
    void refresh()
    void queryClient.invalidateQueries({ queryKey: ["cases"] })
  }

  // Each case opens at the top, so moving to another one never reads as the same page.
  const scroller = useRef<HTMLDivElement>(null)
  useEffect(() => {
    scroller.current?.scrollTo({ top: 0 })
  }, [caseId])

  if (query.isError)
    return (
      <div className="p-6">
        <Alert variant="destructive">
          <AlertTitle>Couldn&apos;t open this case</AlertTitle>
          <AlertDescription>{(query.error as Error).message}</AlertDescription>
        </Alert>
      </div>
    )
  if (!c)
    return (
      <div className="flex flex-col gap-3 p-6">
        <Skeleton className="h-5 w-48" />
        <Skeleton className="h-9 w-80" />
        <Skeleton className="h-32 w-full" />
      </div>
    )

  const pack = registry.byId[c.pack_id]
  const doc = documentLabel(c)
  const keyParts = Object.values(c.key ?? {}).filter(v => v && v !== c.document_number)
  const state = caseState(c)
  const fields = tableFields(pack)
  const amountRaw = fields.amount ? c.attributes?.[fields.amount.attribute] : null
  const currency = fields.amount?.currency ? c.attributes?.[fields.amount.currency] : null
  const amount =
    amountRaw != null && amountRaw !== ""
      ? money(amountRaw as string | number, currency ? String(currency) : null)
      : null
  const reasonField = pack?.fields.find(f => f.role === "reason")
  const traces = c.agent_traces ?? []
  const last = traces[traces.length - 1]
  const summary = last ? splitProse(last).conclusion : null
  const viewLabel = VIEWS.find(v => v.id === view)?.label ?? "Cases"

  const facts: (readonly [string, ReactNode])[] = [
    ["Exception", exceptionLabel(c)] as const,
    ...(amount ? ([["Amount", <span className="tabular-nums">{amount}</span>]] as const) : []),
    ...(fields.party
      ? ([[fields.party.label, String(c.attributes?.[fields.party.attribute] ?? "")]] as const)
      : []),
    ...(reasonField ? ([[reasonField.label, caseReason(c, registry.byId) ?? ""]] as const) : []),
    ["Opened", <When at={c.created_at} />] as const,
  ]

  return (
    <div ref={scroller} className="h-full overflow-y-auto">
      <div className="flex flex-col gap-6 p-6 pb-ask">
        <div className="flex items-center gap-3 text-sm">
          <Link
            to={`/cases?${params}`}
            className={buttonVariants({ variant: "ghost", size: "sm" })}
          >
            <ArrowLeft />
            {viewLabel}
          </Link>
          {index < 0 && decidedHere && (
            <span className="text-muted-foreground">Decided, no longer in {viewLabel}</span>
          )}
          {index >= 0 && (
            <>
              {/* Read out when it changes, so moving to another case is announced too. */}
              <span
                key={caseId}
                aria-live="polite"
                className="animate-case-change rounded-md px-1 text-muted-foreground"
              >
                {index + 1} of {siblings.length} in {viewLabel}
              </span>
              <span className="ml-auto flex items-center gap-1">
                <Button
                  variant="outline"
                  size="icon-sm"
                  aria-label="Previous case"
                  disabled={index <= 0}
                  onClick={() => step(-1)}
                >
                  <ChevronUp />
                </Button>
                <Button
                  variant="outline"
                  size="icon-sm"
                  aria-label="Next case"
                  disabled={index >= siblings.length - 1}
                  onClick={() => step(1)}
                >
                  <ChevronDown />
                </Button>
                <span className="ml-1 text-xs text-muted-foreground">
                  <Kbd>J</Kbd> <Kbd>K</Kbd>
                </span>
              </span>
            </>
          )}
        </div>

        <header className="flex flex-col gap-2">
          <p className="text-sm text-muted-foreground">
            {[pack?.display_name ?? c.pack_id, ...keyParts].join(" · ")}
          </p>
          <h1 className="flex items-center gap-3 text-xl font-semibold tracking-tight">
            {/* Keyed on the case, so a new case flashes its number: never the same page twice. */}
            <span key={caseId} className="animate-case-change rounded-md px-1 -mx-1 font-mono">
              {doc.number}
            </span>
            <CaseMarker c={c} pack={pack} />
            <Badge className={cn("gap-1.5", TONE_BADGE[state.tone])}>
              <span
                aria-hidden="true"
                className={cn("size-1.5 rounded-full", TONE_DOT[state.tone])}
              />
              {state.label}
            </Badge>
            {ticketing && c.ticket_id && (
              <Link
                to={`/tickets?status=all&case_id=${encodeURIComponent(c.case_id)}`}
                className={cn(buttonVariants({ variant: "ghost", size: "sm" }), "ml-auto")}
              >
                <Ticket />
                Tickets
              </Link>
            )}
          </h1>
          <dl className="flex flex-wrap gap-x-8 gap-y-2">
            {facts.map(([k, v]) => (
              <div key={k} className="flex flex-col">
                <dt className="text-xs text-muted-foreground">{k}</dt>
                <dd className="text-sm font-medium">{v || "—"}</dd>
              </div>
            ))}
          </dl>
        </header>

        <DetailLayout
          wide
          main={
            <>
              <LiveRun c={c} onChanged={refresh} />
              {summary && (
                <Card>
                  <CardHeader>
                    <CardDescription>What the agent found</CardDescription>
                  </CardHeader>
                  {/* The assistant's renderer: the agent writes Markdown, and raw HTML in it
                      is never rendered. */}
                  <CardContent className="text-sm">
                    <MarkdownRenderer content={summary} />
                  </CardContent>
                </Card>
              )}
              <Tabs
                key={c.status === CaseStatus.Processing ? "run" : "case"}
                defaultValue={c.status === CaseStatus.Processing ? "activity" : "evidence"}
              >
                <TabsList>
                  <TabsTrigger value="evidence">Evidence</TabsTrigger>
                  <TabsTrigger value="activity">Activity</TabsTrigger>
                  <TabsTrigger value="record">SAP record</TabsTrigger>
                  <TabsTrigger value="run">Run details</TabsTrigger>
                </TabsList>
                <TabsContent value="evidence" className="flex flex-col gap-3 pt-3">
                  <Evidence c={c} pack={pack} />
                </TabsContent>
                <TabsContent value="activity" className="flex flex-col gap-4 pt-3">
                  <HumanDecisions log={c.action_log} />
                  <CaseTimeline traces={traces} summaryShownFor={summary ? last : undefined} />
                </TabsContent>
                <TabsContent value="record" className="pt-3">
                  <dl className="flex flex-col gap-2 text-sm">
                    {caseFields(c, registry.byId).map(f => (
                      <div key={f.label} className="flex gap-6">
                        <dt className="w-44 flex-none text-muted-foreground">{f.label}</dt>
                        <dd className="min-w-0 truncate">
                          {f.exact ? <When at={f.exact} /> : f.value}
                          {f.sapField && (
                            <span className="ml-2 font-mono text-xs text-muted-foreground">
                              {f.sapField}
                            </span>
                          )}
                        </dd>
                      </div>
                    ))}
                  </dl>
                </TabsContent>
                <TabsContent value="run" className="flex flex-col gap-3 pt-3">
                  {traces.length === 0 ? (
                    <p className="text-sm text-muted-foreground">No run yet.</p>
                  ) : (
                    traces.map(t => (
                      <Card key={t.trace_id}>
                        <CardHeader>
                          <CardTitle className="text-sm">
                            <When at={t.timestamp} /> · {describeTransport(t.trigger).label}
                            {t.outcome ? ` · ${humanise(String(t.outcome))}` : ""}
                          </CardTitle>
                          <CardDescription className="font-mono text-xs">
                            {t.sop_key ?? ""}
                            {t.estimated_cost_usd != null
                              ? ` · ${Number(t.estimated_cost_usd).toFixed(2)} USD`
                              : ""}
                          </CardDescription>
                        </CardHeader>
                        <CardContent>
                          <JsonBlock fit text={JSON.stringify(t.segments, null, 2)} />
                        </CardContent>
                      </Card>
                    ))
                  )}
                </TabsContent>
              </Tabs>
            </>
          }
          side={
            <CaseSide
              c={c}
              onChanged={refresh}
              onDecided={decided}
              after="this case stays open here, and you move on with Next case"
              done={
                decidedHere && (
                  <DecisionDone decided={decidedHere}>
                    {decidedHere.next ? (
                      <Button onClick={() => go(decidedHere.next!.case_id)}>
                        Next case:{" "}
                        <span className="font-mono">{documentLabel(decidedHere.next).number}</span>
                        <ArrowRight />
                        <Kbd className="ml-1">J</Kbd>
                      </Button>
                    ) : (
                      <Link to={`/cases?${params}`} className={buttonVariants()}>
                        <ArrowLeft />
                        Back to {viewLabel}
                      </Link>
                    )}
                    {decidedHere.next && (
                      <p className="text-xs text-muted-foreground">
                        The next case opens only when you choose it.
                      </p>
                    )}
                  </DecisionDone>
                )
              }
            />
          }
        />
      </div>
    </div>
  )
}

/** What the case needs from you now, or what the agent is doing with it. */
function CaseSide({
  c,
  onChanged,
  onDecided,
  after,
  done,
}: {
  c: WorkItem
  onChanged: () => Promise<void>
  onDecided: (d: Decided) => void
  after: string
  /** The decision taken on this page, shown in place of everything else the side offers. */
  done?: ReactNode
}) {
  const chat = useAssistant()
  const auth = useAuth()
  const registry = useRegistry()
  const pack = registry.byId[c.pack_id]
  const controls = usePackControls(c.pack_id)
  const live = c.live_run && !isLiveRunStale(c.live_run) ? c.live_run : null
  const working = c.status === CaseStatus.Processing || !!live || chat.isRunning(c.case_id)
  const token = auth.user?.id_token ?? ""
  // The API's refusal to start, in its words: a standing emergency stop, say.
  const [refused, setRefused] = useState<string | null>(null)

  if (done) return <>{done}</>

  if (working || c.status === CaseStatus.AwaitingHumanInput)
    return (
      <DecisionPanel
        c={c}
        working={working}
        onRefresh={onChanged}
        onDecided={onDecided}
        after={after}
      />
    )

  if (c.status === CaseStatus.Complete || c.status === CaseStatus.SapUpdated)
    return (
      <Card>
        <CardHeader>
          <CardTitle>Was this handled well?</CardTitle>
          <CardDescription>Rated cases become precedent for similar ones.</CardDescription>
        </CardHeader>
        <CardContent>
          <CaseRating
            caseId={c.case_id}
            ratings={c.ratings}
            mySub={auth.user?.profile?.sub}
            token={token}
            onSaved={() => void onChanged()}
          />
        </CardContent>
      </Card>
    )

  const failed = c.status === CaseStatus.Error || c.status === CaseStatus.ManualReviewRequired
  const notCaseWorker = pack && !mayProcess(pack) ? processReason(controls.data) : null
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          {failed
            ? c.status === CaseStatus.Error
              ? "The run failed"
              : "Manual review"
            : "Not started"}
        </CardTitle>
        <CardDescription>
          {c.status_reason ??
            (failed
              ? "The agent stopped and handed this case to a person."
              : "The agent follows the pack's SOP on this case when you start it.")}
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        <ReasonButton
          className="w-full"
          reason={notCaseWorker}
          disabled={!chat.ready}
          onClick={() =>
            void chat.processCase(c.case_id, c.process_type).then(r => {
              setRefused(r)
              return onChanged()
            })
          }
        >
          <Play />
          {failed ? "Run again" : "Process"}
        </ReasonButton>
        {refused && (
          <p role="alert" className="text-sm text-destructive">
            {refused}
          </p>
        )}
      </CardContent>
    </Card>
  )
}

/** The run streaming onto the page while the agent works (preview 1.4, frame 2). */
function LiveRun({ c, onChanged }: { c: WorkItem; onChanged: () => Promise<void> }) {
  const chat = useAssistant()
  const transcript = useTranscript(c.case_id)
  const live = c.live_run && !isLiveRunStale(c.live_run) ? c.live_run : null
  const working = c.status === CaseStatus.Processing || !!live || chat.isRunning(c.case_id)
  if (!working) return null
  // The run as the channel streams it into the case's transcript; else the record's snapshot.
  const streamed = liveSteps(transcript)
  const steps = streamed.length
    ? streamed
    : (live?.segments ?? [])
        .filter(s => s.type === "tool")
        .map((s, i) => ({ id: s.tool_call_id ?? String(i), words: rowHeadline(s), done: true }))
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-sm">
          <span
            aria-hidden="true"
            className="size-2 rounded-full bg-progress motion-safe:animate-pulse"
          />
          The agent is working on this case
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {/* A polite log: each step is read out as it lands, without interrupting. */}
        <div role="log" aria-live="polite" aria-label="Steps so far">
          <ol className="flex flex-col gap-1.5 text-sm">
            {steps.map(s => (
              <li key={s.id} className="flex items-center gap-2">
                {s.done ? (
                  <span aria-hidden="true" className="size-1.5 rounded-full bg-progress" />
                ) : (
                  <Spinner />
                )}
                {s.words}
              </li>
            ))}
            {steps.every(s => s.done) && (
              <li className="flex items-center gap-2 text-muted-foreground">
                <Spinner />
                Working
              </li>
            )}
          </ol>
        </div>
        <Button
          variant="outline"
          size="sm"
          className="self-start"
          onClick={() => void chat.stop().then(onChanged)}
        >
          <Square />
          Stop
        </Button>
      </CardContent>
    </Card>
  )
}

/** The tool steps of the run on screen: the turn after the last thing a person said. */
function liveSteps(messages: Message[]): { id: string; words: string; done: boolean }[] {
  let from = messages.length - 1
  while (from >= 0 && messages[from].role === "assistant") from--
  const turn = messages.slice(from + 1)
  const replies = from < 0 ? turn.slice(-1) : turn
  return replies
    .flatMap(m => m.segments ?? [])
    .flatMap((s, i) => {
      if (s.type !== "tool") return []
      const t = toolTick(s.toolCall.name, s.toolCall.input, s.toolCall.status)
      const words = [t.verb, t.subject].filter(Boolean).join(" ")
      const done = s.toolCall.status !== "streaming" && s.toolCall.status !== "executing"
      return [
        {
          id: s.toolCall.toolUseId || String(i),
          words: words.charAt(0).toUpperCase() + words.slice(1),
          done,
        },
      ]
    })
}

/**
 * What the agent read in SAP (plan, Case page, Details): one card per SAP record, its latest
 * read. The title names the record in words with its key beside it; the fields are a
 * description list with the pack's labels, values at body size, amounts with their currency
 * and only identifiers in mono; each label names its SAP field in a tooltip, and a quiet line
 * says where and when it was read.
 */
function Evidence({ c, pack }: { c: WorkItem; pack: UseCasePack | undefined }) {
  const records = new Map<string, { segment: TraceSegment; at: string }>()
  for (const t of c.agent_traces ?? [])
    for (const s of t.segments ?? []) {
      const source = s.evidence?.kind === EvidenceKind.SapRead ? s.evidence.source : undefined
      if (!source) continue
      // A record read twice is one card, with what the later read returned.
      const key = [source.service, source.entity, source.key].join("|")
      records.delete(key)
      records.set(key, { segment: s, at: t.timestamp })
    }
  if (records.size === 0)
    return <p className="text-sm text-muted-foreground">Nothing read in SAP yet.</p>
  const currencyAttr = tableFields(pack).amount?.currency
  const caseCurrency = currencyAttr ? String(c.attributes?.[currencyAttr] ?? "") || null : null
  return (
    <>
      {[...records.values()].map(({ segment: s, at }, i) => {
        const source = s.evidence!.source!
        const rows = evidenceRows(s.evidence!.fields ?? [], pack, caseCurrency)
        return (
          <Card key={s.tool_call_id ?? i}>
            <CardHeader>
              <CardTitle className="flex flex-wrap items-baseline gap-x-2 gap-y-1 text-base">
                <Database className="size-4 self-center text-muted-foreground" aria-hidden="true" />
                {source.entity ? entityWords(source.entity) : "SAP record"}
                {source.key && (
                  <span className="font-mono text-xs font-normal text-muted-foreground">
                    {source.key}
                  </span>
                )}
              </CardTitle>
              {s.evidence!.sap_result?.accepted === false && (
                <CardDescription className="text-destructive">
                  {s.evidence!.sap_result.message}
                </CardDescription>
              )}
            </CardHeader>
            {rows.length > 0 && (
              <CardContent className="@container">
                <dl className="grid grid-cols-2 gap-x-8 gap-y-4 @[36rem]:grid-cols-3">
                  {rows.map(r => (
                    <div key={r.sapName} className="flex min-w-0 flex-col gap-1">
                      <dt className="text-xs text-muted-foreground">
                        <SapName name={r.sapName}>{r.label}</SapName>
                      </dt>
                      <dd
                        className={cn(
                          "break-words text-sm",
                          r.mono && "font-mono",
                          r.numeric && "tabular-nums"
                        )}
                      >
                        {r.value}
                      </dd>
                    </div>
                  ))}
                </dl>
              </CardContent>
            )}
            <CardFooter className="border-t-0 bg-transparent pt-0 text-xs text-muted-foreground">
              <span>
                Read <When at={at} />
                {source.entity && (
                  <>
                    {" "}
                    from <span className="font-mono">{source.entity}</span>
                  </>
                )}
                {source.service && (
                  <>
                    {" "}
                    in <span className="font-mono">{source.service}</span>
                  </>
                )}
              </span>
            </CardFooter>
          </Card>
        )
      })}
    </>
  )
}

/** A label with the SAP name it stands for in a tooltip, on hover and keyboard focus. */
function SapName({ name, children }: { name: string; children: ReactNode }) {
  if (!name) return <>{children}</>
  return (
    <Tooltip>
      <TooltipTrigger render={<span />} tabIndex={0}>
        {children}
      </TooltipTrigger>
      <TooltipContent>
        In SAP: <span className="font-mono">{name}</span>
      </TooltipContent>
    </Tooltip>
  )
}
