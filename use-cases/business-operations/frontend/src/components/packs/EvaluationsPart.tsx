// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { Ident } from "@/components/Ident"
import { Fragment } from "react"
import { Link, useNavigate } from "react-router"
import { ArrowLeft, FlaskConical } from "lucide-react"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { buttonVariants } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { ReasonButton } from "@/components/ReasonButton"
import { Fact, Facts, PartHeader } from "@/components/packs/PartHeader"
import { usePackEvaluations } from "@/hooks/usePack"
import type { CaseResult, ExampleCase, ExpectedOutcome } from "@/services/evaluationsService"
import { humanise, type UseCasePack } from "@/lib/registry"
import { TONE_DOT } from "@/lib/caseViews"
import { timeAgo } from "@/lib/timeAgo"
import { cn } from "@/lib/utils"

export const OUTCOME_LABEL: Record<ExpectedOutcome, string> = {
  auto_release: "Released, no person",
  approval_required: "Asks for approval",
  manual_review_required: "Manual review",
  refused: "Refused",
}

const GROUPS: { id: ExampleCase["group"]; label: string }[] = [
  { id: "behaviour", label: "Behaviour" },
  { id: "boundaries", label: "Boundaries" },
  { id: "security", label: "Security" },
]

/** `boundary_line_amount_tolerance_usd_at` → "Boundary line amount tolerance usd at". */
export function caseLabel(testId: string): string {
  const words = testId.replace(/_/g, " ")
  return words.charAt(0).toUpperCase() + words.slice(1)
}

/**
 * Evaluations: the pack's example cases, each with the outcome it must reach and the last
 * published run's result; one case in detail. Starting a run is shown with the command that
 * does it, because the API does not start one yet.
 */
export function EvaluationsPart({ pack, caseId }: { pack: UseCasePack; caseId: string | null }) {
  const evals = usePackEvaluations(pack.id)
  const navigate = useNavigate()
  const base = `/packs/${encodeURIComponent(pack.id)}?tab=evaluations`

  if (evals.isError)
    return (
      <Alert variant="destructive">
        <AlertTitle>Couldn&apos;t read the evaluations</AlertTitle>
        <AlertDescription>{(evals.error as Error).message}</AlertDescription>
      </Alert>
    )
  const e = evals.data
  if (!e) return <Skeleton className="h-48 w-full" />
  const run = e.last_run
  const chosen = caseId ? e.example_cases.find(c => c.test_id === caseId) : undefined
  if (chosen)
    return (
      <ExampleCaseDetail pack={pack} c={chosen} result={run?.results[chosen.test_id]} back={base} />
    )

  const covered = new Set(e.example_cases.map(c => c.process_type))
  const uncovered = (pack.sops ?? []).filter(
    s => s.kind === "exception" && !covered.has(s.process_type)
  )

  return (
    <div className="flex flex-col gap-6">
      <PartHeader pack={pack} part="evaluations" />
      <Card>
        <CardHeader>
          <CardTitle>Last run</CardTitle>
          <CardDescription>
            {run
              ? `${run.passed} of ${run.total} passed · Pack v${run.pack_version} · ${timeAgo(run.recorded_at)} · ${run.cost_usd.toFixed(2)} USD`
              : "No run has been published for this pack yet."}
          </CardDescription>
        </CardHeader>
        <CardContent className="flex items-center gap-3">
          <ReasonButton
            variant="outline"
            size="sm"
            reason={
              e.may_run ? null : (e.run_refusal ?? "Evaluations run from the command line today.")
            }
          >
            Run evaluations
          </ReasonButton>
          <span className="text-sm text-muted-foreground">
            About {e.estimate_usd.toFixed(2)} USD for {e.example_cases.length} cases
          </span>
        </CardContent>
      </Card>

      {uncovered.length > 0 && (
        <Alert>
          <AlertTitle>Exception types with no example case</AlertTitle>
          <AlertDescription>
            {uncovered.map(s => humanise(s.process_type)).join(", ")}
          </AlertDescription>
        </Alert>
      )}

      <div className="rounded-lg border bg-card">
        <Table className="table-fixed">
          <TableHeader>
            <TableRow>
              <TableHead>Example case</TableHead>
              <TableHead className="w-48">SOP</TableHead>
              <TableHead className="w-44">Expects</TableHead>
              <TableHead className="w-28">Last run</TableHead>
              <TableHead className="w-20 text-right">USD</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {GROUPS.map(g => {
              const list = e.example_cases.filter(c => c.group === g.id)
              if (!list.length) return null
              return (
                <Fragment key={g.id}>
                  <TableRow className="hover:bg-transparent">
                    <TableCell colSpan={5} className="text-xs font-medium text-muted-foreground">
                      {g.label} <span className="tabular-nums">{list.length}</span>
                    </TableCell>
                  </TableRow>
                  {list.map(c => {
                    const r = run?.results[c.test_id]
                    return (
                      <TableRow
                        key={c.test_id}
                        className="h-9 cursor-pointer"
                        onClick={() => navigate(`${base}&case=${encodeURIComponent(c.test_id)}`)}
                      >
                        <TableCell className="truncate">
                          <Link
                            className="hover:underline"
                            to={`${base}&case=${encodeURIComponent(c.test_id)}`}
                            onClick={ev => ev.stopPropagation()}
                          >
                            {caseLabel(c.test_id)}
                          </Link>
                        </TableCell>
                        <TableCell className="truncate text-muted-foreground">
                          {humanise(c.process_type)}
                        </TableCell>
                        <TableCell className="truncate">
                          {OUTCOME_LABEL[c.expected.outcome]}
                        </TableCell>
                        <TableCell>
                          <ResultWord r={r} />
                        </TableCell>
                        <TableCell className="text-right tabular-nums text-muted-foreground">
                          {r?.cost_usd != null ? r.cost_usd.toFixed(2) : "–"}
                        </TableCell>
                      </TableRow>
                    )
                  })}
                </Fragment>
              )
            })}
          </TableBody>
        </Table>
      </div>
    </div>
  )
}

function ResultWord({ r }: { r: CaseResult | undefined }) {
  if (!r) return <span className="text-muted-foreground">Not run</span>
  return (
    <span className="inline-flex items-center gap-2">
      <span
        aria-hidden="true"
        className={cn("size-2 rounded-full", TONE_DOT[r.passed ? "success" : "danger"])}
      />
      {r.passed ? "Passed" : "Failed"}
    </span>
  )
}

function ExampleCaseDetail({
  pack,
  c,
  result,
  back,
}: {
  pack: UseCasePack
  c: ExampleCase
  result: CaseResult | undefined
  back: string
}) {
  const navigate = useNavigate()
  const inputs = Object.entries(c.seed?.inputs ?? {})
  const judges = Object.entries(result?.judges ?? {})
  const packPath = `/packs/${encodeURIComponent(pack.id)}`
  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center gap-3">
        <Link className={buttonVariants({ variant: "ghost", size: "sm" })} to={back}>
          <ArrowLeft />
          All example cases
        </Link>
        <ReasonButton
          className="ml-auto"
          variant="outline"
          size="sm"
          reason={pack.access?.rehearse ? null : "Rehearsing needs the pack's authors."}
          onClick={() => navigate(`${packPath}/rehearse?example=${encodeURIComponent(c.test_id)}`)}
        >
          <FlaskConical />
          Rehearse this case
        </ReasonButton>
      </div>
      <div className="flex flex-col gap-2">
        <h2 className="flex items-center gap-3 text-base font-semibold">
          {caseLabel(c.test_id)}
          {result && (
            <Badge variant={result.passed ? "secondary" : "destructive"}>
              {result.passed ? "Passed" : "Failed"}
            </Badge>
          )}
        </h2>
        <p className="max-w-measure text-sm text-muted-foreground">{c.description}</p>
      </div>
      <Card>
        <CardHeader>
          <CardTitle>Expected</CardTitle>
        </CardHeader>
        <CardContent>
          <Facts>
            <Fact label="Outcome">{OUTCOME_LABEL[c.expected.outcome]}</Fact>
            <Fact label="Why">{c.expected.reason}</Fact>
            <Fact label="SOP">
              <Link
                className="hover:underline"
                to={`${packPath}/procedures/${encodeURIComponent(c.process_type)}`}
              >
                {humanise(c.process_type)}
              </Link>
            </Fact>
            {c.expected.required_tool_calls?.length ? (
              <Fact label="Must call">
                <Ident>{c.expected.required_tool_calls.join(", ")}</Ident>
              </Fact>
            ) : null}
          </Facts>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>What the last run did</CardTitle>
        </CardHeader>
        <CardContent>
          {!result ? (
            <p className="text-sm text-muted-foreground">Not in the last published run.</p>
          ) : (
            <Facts>
              <Fact label="Case ended">{result.status ? humanise(result.status) : "–"}</Fact>
              {result.failures.map((f, i) => (
                <Fact key={i} label="Failed">
                  {f}
                </Fact>
              ))}
              {result.warnings.map((w, i) => (
                <Fact key={i} label="Warning">
                  {w}
                </Fact>
              ))}
              {result.failures.length === 0 && <Fact label="Checks">All passed</Fact>}
            </Facts>
          )}
        </CardContent>
      </Card>
      {inputs.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Seeded in the sandbox</CardTitle>
          </CardHeader>
          <CardContent>
            <Facts>
              {inputs.map(([k, v]) => (
                <Fact key={k} label={humanise(k)}>
                  <span className="tabular-nums">{String(v)}</span>
                </Fact>
              ))}
            </Facts>
          </CardContent>
        </Card>
      )}
      {judges.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Judges</CardTitle>
          </CardHeader>
          <CardContent>
            <Facts>
              {judges.map(([k, v]) => (
                <Fact
                  key={k}
                  label={k.replace(/^Builtin\./, "").replace(/([a-z])([A-Z])/g, "$1 $2")}
                >
                  <span className="tabular-nums">{v.toFixed(2)}</span>
                </Fact>
              ))}
            </Facts>
          </CardContent>
        </Card>
      )}
    </div>
  )
}
