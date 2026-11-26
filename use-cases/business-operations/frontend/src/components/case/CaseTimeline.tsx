// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState, type ReactElement } from "react"
import { When } from "@/components/When"
import {
  AlertTriangle,
  Bell,
  Bot,
  Calculator,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Clock,
  Copy,
  Database,
  FileText,
  PenLine,
  Unplug,
  XCircle,
} from "lucide-react"
import { AuthzMode, AuthzOutcome, EvidenceKind, TraceSegmentType, WriteOp } from "@/types/cases"
import type {
  ActionLogEntry,
  AgentTrace,
  EvidenceAuthz,
  EvidenceField,
  TraceSegment,
} from "@/types/cases"
import {
  citationOf,
  rowHeadline,
  segmentStatus,
  segmentsOf,
  sopLink,
  splitProse,
} from "@/lib/caseTimeline"
import { appliedRows, baselineFor, functionImportCard, proposedRows } from "@/lib/writeDiff"
import { TONE_BADGE, TONE_TEXT } from "@/lib/statusTone"
import { TRANSPORT_ICON, describeTransport } from "@/lib/transports"
import { humanise } from "@/lib/registry"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "@/components/ui/empty"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { Citation } from "@/components/chat/Citation"
import { MarkdownRenderer } from "@/components/chat/MarkdownRenderer"
import { ToolCallDisplay } from "@/components/chat/ToolCallDisplay"
import { WriteDiff } from "@/components/case/WriteDiff"
import { cn } from "@/lib/utils"

/**
 * A processed case renders as its decision chain, not as a conversation.
 *
 * The run that produced the case's current state is never behind a click; earlier
 * runs collapse beneath it, because re-processing a case is an event a controller
 * needs to see as distinct. Rows are driven by `evidence.kind`, so nothing here
 * pattern-matches a tool name.
 */

// Outcome is state, so colour comes from the tone vocabulary.
const OUTCOME_STYLE: Record<string, { icon: typeof CheckCircle2; color: string; label: string }> = {
  complete: { icon: CheckCircle2, color: TONE_TEXT.success, label: "Completed" },
  stopped: { icon: AlertTriangle, color: TONE_TEXT.progress, label: "Stopped early" },
  cancelled: { icon: AlertTriangle, color: TONE_TEXT.progress, label: "Stopped early" },
  error: { icon: XCircle, color: TONE_TEXT.danger, label: "Error" },
  disconnected: { icon: Unplug, color: "text-muted-foreground", label: "Disconnected" },
}

const KIND_ICON: Record<string, typeof Database> = {
  [EvidenceKind.SapRead]: Database,
  [EvidenceKind.SapWrite]: PenLine,
  [EvidenceKind.SopLookup]: FileText,
  [EvidenceKind.CaseUpdate]: Check,
  [EvidenceKind.Notification]: Bell,
  [EvidenceKind.Computation]: Calculator,
}

/** A label with its explanation in a tooltip, which hover and keyboard focus open. */
function Explained({ tip, children }: { tip: string; children: ReactElement }) {
  return (
    <Tooltip>
      <TooltipTrigger render={children} />
      <TooltipContent>{tip}</TooltipContent>
    </Tooltip>
  )
}

function TriggerBadge({ trigger }: { trigger?: string }) {
  const transport = describeTransport(trigger)
  const Icon = TRANSPORT_ICON[transport.kind]
  // The id, not the label, for an unrecognised transport: its label is derived from it and
  // the operator may need the raw value to go looking.
  return (
    <Explained
      tip={
        transport.known
          ? `Started by ${transport.label}`
          : `Unrecognised transport: ${transport.id}`
      }
    >
      <Badge variant="outline" tabIndex={0}>
        <Icon />
        {transport.label}
      </Badge>
    </Explained>
  )
}

function OutcomeBadge({ outcome }: { outcome?: string }) {
  const style = OUTCOME_STYLE[outcome ?? ""]
  if (!style) return null
  const Icon = style.icon
  return (
    <span
      className={cn("inline-flex flex-none items-center gap-1 text-xs font-medium", style.color)}
    >
      <Icon className="size-3.5" />
      {style.label}
    </span>
  )
}

/** All text content of a run, for clipboard copy. */
function traceText(trace: AgentTrace): string {
  return segmentsOf(trace)
    .filter(s => s?.type === TraceSegmentType.Text && s.content)
    .map(s => s.content!)
    .join("\n\n")
}

function CopyButton({ trace }: { trace: AgentTrace }) {
  const [copied, setCopied] = useState(false)
  return (
    <Explained tip="Copy this run's text">
      <Button
        variant="ghost"
        size="icon-sm"
        aria-label="Copy this run's text"
        onClick={async () => {
          try {
            await navigator.clipboard.writeText(traceText(trace))
            setCopied(true)
            setTimeout(() => setCopied(false), 2000)
          } catch {
            /* clipboard unavailable — nothing to report, the text is on screen */
          }
        }}
      >
        {copied ? <Check className={TONE_TEXT.success} /> : <Copy />}
      </Button>
    </Explained>
  )
}

/**
 * Whether the write was authorized or merely logged. Writes only: a chip on every
 * read is noise that trains an operator to stop reading it, and it is the write that
 * Cedar is there to gate. Reads carry `authz` in the expanded detail.
 *
 * The decision leads, because "was this allowed" is the question — mode only
 * qualifies how much the answer was worth. A rejected write under LOG_ONLY still
 * reached SAP, so that pairing is the one an operator most needs to see, and it
 * takes `danger` rather than the mode's neutral.
 *
 * The `" (log only)"` suffix is the difference between "Cedar permitted this" and "Cedar was
 * not asked to stop it", so it is never smaller than the row.
 */
function AuthzChip({ authz }: { authz?: EvidenceAuthz }) {
  if (!authz) return null
  const rejected = authz.outcome === AuthzOutcome.Rejected
  const permitted = authz.outcome === AuthzOutcome.Permitted
  // Cedar off is not "logged only": Cedar was not asked at all. The run checks still decide,
  // so a refusal is the Gateway's and is said so.
  if (authz.mode === AuthzMode.Off) {
    return (
      <Explained
        tip={
          rejected
            ? "The Gateway refused this write; Cedar is off on this deployment."
            : "Cedar is off on this deployment, so no policy evaluated this write."
        }
      >
        <Badge
          variant="secondary"
          tabIndex={0}
          className={rejected ? TONE_BADGE.danger : TONE_BADGE.attention}
        >
          {rejected ? "Gateway: refused" : "Cedar: off"}
        </Badge>
      </Explained>
    )
  }

  const enforced = authz.mode === AuthzMode.Enforce
  const modeNote = enforced
    ? "A denial would have blocked it."
    : "Cedar is in log-only mode, so a denial would not have blocked it."

  return (
    <Explained
      tip={
        rejected
          ? `Cedar rejected this write. ${modeNote}`
          : permitted
            ? `Cedar permitted this write. ${modeNote}`
            : `Cedar evaluated this write in ${enforced ? "enforce" : "log-only"} mode; the call failed for another reason, so no decision was recorded.`
      }
    >
      <Badge
        variant="secondary"
        tabIndex={0}
        className={
          // Absent outcome is not a denial: the call failed for some other reason and the
          // row's own error state already reports that.
          rejected
            ? TONE_BADGE.danger
            : permitted && enforced
              ? TONE_BADGE.success
              : TONE_BADGE.neutral
        }
      >
        {rejected ? "Cedar: rejected" : permitted ? "Cedar: permitted" : "Cedar: no decision"}
        {!enforced && (rejected || permitted) && " (log only)"}
      </Badge>
    </Explained>
  )
}

// `text-sm`, matching `WriteDiff` — these are the same class of thing (a SAP value an
// operator checks against another screen) and they render side by side in one expanded
// row, so two registers here would say one of them mattered less.
function FieldList({ fields }: { fields: EvidenceField[] }) {
  return (
    <dl className="flex flex-col gap-1 text-sm">
      {fields.map((f, i) => (
        // Names are not unique: `_scalar_fields` appends its own `result` field, so a
        // tool whose args already carry one produces two rows named `result`.
        <div key={`${f.name}-${i}`} className="flex flex-wrap gap-x-3">
          <dt className="text-muted-foreground">{f.name}</dt>
          {/* Mono because an operator compares this against another screen. */}
          <dd className="font-mono break-all text-foreground">{f.value || "—"}</dd>
        </div>
      ))}
    </dl>
  )
}

/**
 * `steps` is the whole run, because a write's before values come from an earlier read.
 *
 * `sopHref` comes from the run too — the SOP is a property of the trace, not of any one
 * step in it.
 */
function EvidenceRow({
  segment,
  steps,
  sopHref,
}: {
  segment: TraceSegment
  steps: TraceSegment[]
  sopHref?: string
}) {
  const [open, setOpen] = useState(false)
  const evidence = segment.evidence!
  const failed = segmentStatus(segment) === "error"
  const Icon = KIND_ICON[evidence.kind] ?? Bot
  const isWrite = evidence.kind === EvidenceKind.SapWrite
  const proposal = evidence.proposed_write
  // Both arrive as JSON from DynamoDB and `Evidence`'s index signature admits any
  // shape, so a stored scalar would throw in `.map` and blank the route.
  const clauses = Array.isArray(evidence.clauses_retrieved) ? evidence.clauses_retrieved : []
  const fields = Array.isArray(evidence.fields) ? evidence.fields : []
  // Shared with the approval card, which asks the same question of the same untrusted
  // JSON — see `citationOf`.
  const citation = citationOf(evidence)
  const hasDetail =
    fields.length > 0 ||
    clauses.length > 0 ||
    Boolean(citation) ||
    Boolean(evidence.source) ||
    Boolean(evidence.at) ||
    Boolean(proposal) ||
    (!isWrite && Boolean(evidence.authz))

  const headline = (
    <>
      {!hasDetail ? (
        <span className="w-4 flex-none" />
      ) : open ? (
        <ChevronDown className="text-muted-foreground" />
      ) : (
        <ChevronRight className="text-muted-foreground" />
      )}
      <Icon className={failed ? TONE_TEXT.danger : "text-muted-foreground"} />
      <span className="min-w-0 flex-1 truncate">{rowHeadline(segment)}</span>
      {failed && <XCircle className={TONE_TEXT.danger} aria-label="Step failed" />}
      {evidence.truncated && (
        <span className="text-xs text-muted-foreground">preview cut short</span>
      )}
    </>
  )

  return (
    <Collapsible
      render={<li className="rounded-md border" />}
      open={open}
      onOpenChange={o => hasDetail && setOpen(o)}
    >
      <div className="flex items-center gap-2 pr-2">
        {/* A row with nothing behind it is still a button: it carries the headline and has
            to stay reachable, but it must not claim a disclosure it cannot open. */}
        <CollapsibleTrigger
          render={
            <Button
              variant="ghost"
              className="h-auto min-w-0 flex-1 justify-start px-2 py-1.5 text-left font-normal"
            />
          }
          nativeButton
          aria-expanded={hasDetail ? open : undefined}
        >
          {headline}
        </CollapsibleTrigger>
        {isWrite && <AuthzChip authz={evidence.authz} />}
      </div>

      {hasDetail && (
        <CollapsibleContent className="flex flex-col gap-1.5 border-t px-2 py-1.5">
          {clauses.length > 0 && (
            <div className="flex flex-wrap gap-1">
              {clauses.map(c => (
                <Badge key={c} variant="secondary" className="font-mono">
                  §{c}
                </Badge>
              ))}
            </div>
          )}
          {/* The clause numbers above are the locator; this is the sentence itself, marked the
              way an auditor marks a page, and linked to the SOP the run was given, because a
              quoted sentence with no way to read its paragraph is a claim an operator has to
              take on trust. Unlinked on a run that recorded no SOP. */}
          {citation && (
            <p className="text-sm leading-relaxed">
              <Citation citation={citation} href={sopHref} />
            </p>
          )}
          {isWrite ? (
            // A write's payload is only half the fact. Paired with the read that
            // preceded it, the same fields say what actually changed.
            <WriteDiff
              rows={
                evidence.op === WriteOp.FunctionImport
                  ? []
                  : appliedRows(segment, baselineFor(segment, steps))
              }
              label="Applied"
              card={
                evidence.op === WriteOp.FunctionImport ? functionImportCard(segment) : undefined
              }
            />
          ) : (
            fields.length > 0 && <FieldList fields={fields} />
          )}
          {proposal && <WriteDiff rows={proposedRows(proposal, steps)} label="Proposed" />}
          {evidence.source && (
            <p className="font-mono text-xs text-muted-foreground">
              {[evidence.source.service, evidence.source.entity, evidence.source.key]
                .filter(Boolean)
                .join(" · ")}
            </p>
          )}
          {!isWrite && evidence.authz && (
            <p className="text-xs text-muted-foreground">
              {`Cedar ${
                evidence.authz.mode === AuthzMode.Enforce
                  ? "enforced"
                  : evidence.authz.mode === AuthzMode.Off
                    ? "off"
                    : "log-only"
              }${evidence.authz.outcome ? ` · ${evidence.authz.outcome}` : " · no decision"}`}
            </p>
          )}
          {evidence.at && (
            <p className="text-xs text-muted-foreground">
              <When at={evidence.at} />
            </p>
          )}
        </CollapsibleContent>
      )}
    </Collapsible>
  )
}

function RunSteps({
  trace,
  summaryShownAbove = false,
}: {
  trace: AgentTrace
  /** The page already shows this run's conclusion ("What the agent found"); don't repeat it. */
  summaryShownAbove?: boolean
}) {
  const [showReasoning, setShowReasoning] = useState(false)
  const prose = splitProse(trace)
  const conclusion = summaryShownAbove ? null : prose.conclusion
  const { reasoning } = prose
  const steps = segmentsOf(trace).filter(s => s?.type === TraceSegmentType.Tool)

  return (
    <div className="flex flex-col gap-2">
      {steps.length > 0 && (
        <ol className="flex flex-col gap-1">
          {steps.map((segment, i) =>
            segment.evidence ? (
              <EvidenceRow
                key={segment.tool_call_id ?? i}
                segment={segment}
                steps={steps}
                sopHref={sopLink(trace.sop_key)}
              />
            ) : (
              // Pre-evidence trace. `merge_evidence` writes `status` only alongside
              // `evidence`, so this branch always reads complete — kept for the day a
              // failure is recorded without evidence, not because one is today.
              <li key={segment.tool_call_id ?? i}>
                <ToolCallDisplay
                  name={segment.tool_name ?? "unknown"}
                  args={segment.tool_input ?? ""}
                  status={segmentStatus(segment)}
                  result={segment.tool_result}
                />
              </li>
            )
          )}
        </ol>
      )}

      {conclusion && (
        <div className="border-l-2 border-border pl-3 text-sm">
          <MarkdownRenderer content={conclusion} />
        </div>
      )}

      {reasoning.length > 0 && (
        <Collapsible open={showReasoning} onOpenChange={setShowReasoning}>
          <CollapsibleTrigger
            render={<Button variant="ghost" size="sm" className="text-muted-foreground" />}
          >
            {showReasoning ? <ChevronDown /> : <ChevronRight />}
            Reasoning ({reasoning.length} step{reasoning.length === 1 ? "" : "s"})
          </CollapsibleTrigger>
          <CollapsibleContent className="mt-1 flex flex-col gap-1.5 border-l pl-3 text-sm text-muted-foreground">
            {reasoning.map((content, i) => (
              <MarkdownRenderer key={i} content={content} />
            ))}
          </CollapsibleContent>
        </Collapsible>
      )}

      {steps.length === 0 && !prose.conclusion && reasoning.length === 0 && (
        <p className="text-sm text-muted-foreground">This run recorded no steps.</p>
      )}
    </div>
  )
}

/** The SOP this run was governed by, at the version it read: what a cited clause means. */
function SopVersion({ trace }: { trace: AgentTrace }) {
  if (!trace.sop_key) return null
  const name = trace.sop_key
    .split("/")
    .pop()
    ?.replace(/\.(txt|md)$/, "")
  const link = (
    <a
      href={sopLink(trace.sop_key)}
      className="flex-none text-xs text-muted-foreground underline underline-offset-4"
    >
      {humanise(name ?? "")} SOP
      {trace.sop_version ? `, version ${trace.sop_version}` : ""}
    </a>
  )
  return trace.sop_version_id ? (
    <Explained tip={`Stored version ${trace.sop_version_id}`}>{link}</Explained>
  ) : (
    link
  )
}

/**
 * The humans who decided on this case, from the record the decision routes wrote before
 * the run they started — not the agent's account of it. An approval names the write it
 * authorized by digest, so it can be matched to the proposal the approver was shown.
 */
export function HumanDecisions({ log }: { log?: ActionLogEntry[] }) {
  const decisions = (log ?? []).filter(e => e.action.startsWith("human_") && e.actor)
  if (decisions.length === 0) return null
  return (
    <ul aria-label="Human decisions" className="flex flex-col gap-1 text-sm">
      {decisions.map(e => (
        <li key={`${e.timestamp}:${e.actor}`}>
          {/* A chosen outcome by its label ("Approve HOLD by"), not "chose by". */}
          <span className="font-medium">
            {e.option_label || e.action.replace("human_", "")}
          </span> by {e.actor_display || e.actor}
          <span className="text-muted-foreground">
            {e.source ? ` via ${e.source}` : ""} · <When at={e.timestamp} />
            {e.approved_write ? ` · write ${e.approved_write.slice(0, 12)}` : ""}
          </span>
        </li>
      ))}
    </ul>
  )
}

function RunHeader({ trace }: { trace: AgentTrace }) {
  return (
    <div className="flex items-center gap-2 text-sm">
      <Clock className="size-3.5 flex-none text-muted-foreground" />
      {/* Dated like a collapsed run: a case opened days after it was processed would
          otherwise read a bare "14:05" as this afternoon. */}
      <span className="flex-none text-muted-foreground">
        <When at={trace.timestamp} />
      </span>
      <TriggerBadge trigger={trace.trigger} />
      <OutcomeBadge outcome={trace.outcome} />
      <SopVersion trace={trace} />
      <span className="flex-1" />
      <CopyButton trace={trace} />
    </div>
  )
}

function CollapsedRun({ trace }: { trace: AgentTrace }) {
  const [open, setOpen] = useState(false)
  const toolCount = segmentsOf(trace).filter(s => s?.type === TraceSegmentType.Tool).length

  return (
    <Collapsible className="rounded-md border" open={open} onOpenChange={setOpen}>
      <CollapsibleTrigger
        render={
          <Button
            variant="ghost"
            className="h-auto w-full justify-start px-2 py-1.5 text-left font-normal"
          />
        }
      >
        {open ? (
          <ChevronDown className="text-muted-foreground" />
        ) : (
          <ChevronRight className="text-muted-foreground" />
        )}
        <span className="flex-none text-muted-foreground">
          <When at={trace.timestamp} focusable={false} />
        </span>
        <span className="flex-none">{describeTransport(trace.trigger).label}</span>
        <OutcomeBadge outcome={trace.outcome} />
        <span className="flex-1" />
        <span className="flex-none text-xs text-muted-foreground">
          {toolCount} step{toolCount === 1 ? "" : "s"}
        </span>
      </CollapsibleTrigger>
      <CollapsibleContent className="border-t px-2 py-2">
        <RunSteps trace={trace} />
      </CollapsibleContent>
    </Collapsible>
  )
}

export function CaseTimeline({
  traces,
  summaryShownFor,
}: {
  traces: AgentTrace[]
  /** The run whose conclusion the page shows above the tabs, so its steps leave it out. */
  summaryShownFor?: AgentTrace
}) {
  if (!traces || traces.length === 0) {
    return (
      <Empty>
        <EmptyHeader>
          <EmptyTitle>No processing history yet.</EmptyTitle>
          <EmptyDescription>
            Process this case to record what the agent did: every SAP read, every clause it
            consulted and every write.
          </EmptyDescription>
        </EmptyHeader>
      </Empty>
    )
  }

  const sorted = [...traces].sort((a, b) => (b.timestamp ?? "").localeCompare(a.timestamp ?? ""))
  const [latest, ...earlier] = sorted

  return (
    <div className="flex flex-col gap-3">
      <section aria-label="Latest run" className="flex flex-col gap-2">
        <RunHeader trace={latest} />
        <RunSteps trace={latest} summaryShownAbove={summaryShownFor === latest} />
      </section>

      {earlier.length > 0 && (
        <section aria-labelledby="earlier-runs" className="flex flex-col gap-1">
          <h3 id="earlier-runs" className="text-sm font-medium">
            Earlier runs
          </h3>
          {earlier.map(t => (
            <CollapsedRun key={t.trace_id} trace={t} />
          ))}
        </section>
      )}
    </div>
  )
}
