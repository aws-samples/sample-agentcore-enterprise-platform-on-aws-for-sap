// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useId, useMemo, useState, type ReactNode } from "react"
import { Link } from "react-router"
import { useAuth } from "react-oidc-context"
import { useQuery } from "@tanstack/react-query"
import { CircleAlert, CircleCheck, RefreshCw } from "lucide-react"
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Alert, AlertAction, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { Spinner } from "@/components/ui/spinner"
import { Skeleton } from "@/components/ui/skeleton"
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group"
import {
  Field,
  FieldContent,
  FieldDescription,
  FieldLabel,
  FieldTitle,
} from "@/components/ui/field"
import { HoverCard, HoverCardContent, HoverCardTrigger } from "@/components/ui/hover-card"
import { ReasonButton } from "@/components/ReasonButton"
import { When } from "@/components/When"
import { systemRoleWords } from "@/components/SandboxMarker"
import { WriteDiff } from "@/components/case/WriteDiff"
import { useRegistry } from "@/hooks/useRegistry"
import { useApi } from "@/hooks/useApi"
import { usePackControls } from "@/hooks/usePack"
import { useDemoFeatures } from "@/hooks/useDemoEnabled"
import { replyToCase } from "@/services/casesService"
import { fetchTicket, submitTicketAction } from "@/services/ticketsService"
import { errorBody, errorStatus } from "@/lib/apiFetch"
import { isNonProduction, type UseCasePack } from "@/lib/registry"
import { groupLabel } from "@/lib/packParts"
import { proposedRows, pendingProposal } from "@/lib/writeDiff"
import { segmentsOf } from "@/lib/caseTimeline"
import { timeAgo } from "@/lib/timeAgo"
import { entityWords, sapWords } from "@/lib/evidence"
import {
  checksPhrase,
  decisionMode,
  decisionReasons,
  needsTwoApprovers,
  outcomesFor,
  pendingApprovals,
  submitLabel,
  writeVerb,
  type Outcome,
} from "@/lib/decision"
import { EvidenceKind, WriteOp, type ProposedWrite, type WorkItem } from "@/types/cases"
import type { Ticket } from "@/types/tickets"

export interface Decided {
  outcome: Outcome
  /** The first of two approvals: recorded, and the run waits for a different approver. */
  awaitingSecond: boolean
  /** The reason or answer the person gave, as sent. */
  reason: string
  /** When the API took it, as the browser saw it. */
  at: string
}

/**
 * The panel once you have decided: what you chose, your reason, where it went and when. It
 * offers nothing to submit, so a case cannot be decided twice from the same view; `children`
 * is what comes next, such as the Next case button.
 */
export function DecisionDone({ decided, children }: { decided: Decided; children?: ReactNode }) {
  const { outcome, awaitingSecond, reason, at } = decided
  const replied = outcome.decision === "replied"
  return (
    <Card role="status" aria-label="Your decision, sent">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <CircleCheck className="size-4 text-success-ink" aria-hidden="true" />
          {awaitingSecond ? "Approval recorded" : replied ? "Answer sent" : "Decision sent"}
        </CardTitle>
        <CardDescription>
          {awaitingSecond
            ? "1 of 2 approvals. A different approver must approve this change too."
            : "Sent to the agent, which resumes the case with it."}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <dl className="flex flex-col gap-3 text-sm">
          {!replied && (
            <div className="flex flex-col gap-0.5">
              <dt className="text-xs text-muted-foreground">You chose</dt>
              <dd className="font-medium">{outcome.label}</dd>
            </div>
          )}
          <div className="flex flex-col gap-0.5">
            <dt className="text-xs text-muted-foreground">{replied ? "Your answer" : "Reason"}</dt>
            <dd className="whitespace-pre-wrap">{reason}</dd>
          </div>
          <div className="flex flex-col gap-0.5">
            <dt className="text-xs text-muted-foreground">Sent</dt>
            <dd>
              <When at={at} />
            </dd>
          </div>
        </dl>
      </CardContent>
      {children && <CardFooter className="flex-col items-stretch gap-2">{children}</CardFooter>}
    </Card>
  )
}

/**
 * Your decision on a case (preview 1.4, frame 1), the same on the case page and in the ticket
 * sheet: why a person decides, the exact change, the outcomes the SOP allows, a required
 * reason, and a button that names the outcome. A ticket-backed case decides through the
 * ticket's action route, any other case through the case's reply route.
 *
 * The API decides who may decide and whether a second approval counts; the panel only shows
 * its answer. Approval is disabled for a person the pack does not let approve, with the reason
 * in a tooltip.
 */
export function DecisionPanel({
  c,
  onDecided,
  onRefresh,
  after,
  working = false,
}: {
  c: WorkItem
  onDecided?: (decided: Decided) => void
  onRefresh: () => void | Promise<void>
  /** What happens after you decide, such as "the next case in Needs you opens". */
  after?: string
  /** The agent is running on the case, so there is nothing to decide until it stops. */
  working?: boolean
}) {
  const registry = useRegistry()
  const pack = registry.byId[c.pack_id]
  const controls = usePackControls(c.pack_id)
  const { ticketing, known } = useDemoFeatures()
  const getToken = useApi()
  const auth = useAuth()
  const ticketQuery = useQuery({
    queryKey: ["ticket", c.ticket_id],
    queryFn: async () => fetchTicket(c.ticket_id!, await getToken()),
    enabled: auth.isAuthenticated && ticketing && !!c.ticket_id,
  })
  const ticket = ticketing && c.ticket_id ? ticketQuery.data : null
  const mode = working ? "idle" : decisionMode(c, ticket)

  if (mode === "idle")
    return (
      <Card>
        <CardHeader>
          <CardTitle>Your decision</CardTitle>
          <CardDescription>
            Nothing to decide yet. If the agent needs a person, it stops here and this panel shows
            what it proposes.
          </CardDescription>
        </CardHeader>
      </Card>
    )
  // A ticket-backed case waits for its ticket, which says whether it is a decision or a
  // question, and before that for whether ticketing is deployed at all.
  if (c.ticket_id && (known === false || (ticketing && ticketQuery.isLoading)))
    return (
      <Card aria-busy="true">
        <CardHeader>
          <CardTitle>Your decision</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-2">
          <Skeleton className="h-4 w-full" />
          <Skeleton className="h-20 w-full" />
        </CardContent>
      </Card>
    )
  return (
    <DecisionForm
      key={`${c.case_id}:${c.updated_at}`}
      c={c}
      pack={pack}
      controls={controls.data}
      ticket={ticket ?? null}
      mode={mode}
      onDecided={onDecided}
      onRefresh={onRefresh}
      after={after}
    />
  )
}

function DecisionForm({
  c,
  pack,
  controls,
  ticket,
  mode,
  onDecided,
  onRefresh,
  after,
}: {
  c: WorkItem
  pack: UseCasePack | undefined
  controls: Parameters<typeof decisionReasons>[2]
  ticket: Ticket | null
  mode: "decide" | "reply"
  onDecided?: (decided: Decided) => void
  onRefresh: () => void | Promise<void>
  after?: string
}) {
  const auth = useAuth()
  const getToken = useApi()
  const id = useId()
  const me = String(auth.user?.profile?.sub ?? "")
  const outcomes = useMemo(() => outcomesFor(c), [c])
  // A second approver starts on the option the first approved: only the same one counts.
  const firstApproved = pendingApprovals(c)[0]?.option
  const [choice, setChoice] = useState(
    (firstApproved && outcomes.find(o => o.optionId === firstApproved)?.id) || outcomes[0]?.id || ""
  )
  const [text, setText] = useState("")
  const [sending, setSending] = useState(false)
  const [refusal, setRefusal] = useState<{ moved: boolean; message: string } | null>(null)
  const [answer, setAnswer] = useState<string | null>(null)
  useEffect(() => setRefusal(null), [text, choice])

  const outcome = mode === "reply" ? REPLY : (outcomes.find(o => o.id === choice) ?? outcomes[0])
  const reason = text.trim()
  const production = !isNonProduction(pack)
  const system = pack?.sap_system
  const systemWords = system
    ? `${system.name}, ${production ? "the production system" : systemRoleNoun(system.system_role)}`
    : "SAP"

  const reasons = decisionReasons(c, pack, controls)
  const two = needsTwoApprovers(c, pack, controls)
  const approvals = pendingApprovals(c)
  const approvedByMe = approvals.some(a => a.actor && a.actor === me)
  if (two.needed && two.limit != null && mode === "decide")
    reasons.push({
      kind: "limit",
      text: `Above the two-approver limit of ${two.limit.toLocaleString("en-US")} USD, two different approvers must approve.`,
    })
  const checks = checksPhrase(controls)
  const approveGroups = controls?.entitlement?.approve_groups ?? []

  const notEntitled =
    pack?.access && !pack.access.approve
      ? `Only the pack's approvers decide${approveGroups.length ? `: ${approveGroups.map(groupLabel).join(", ")}` : ""}.`
      : null
  const blocked =
    notEntitled ??
    (!reason
      ? mode === "reply"
        ? "Write your answer first."
        : "Write the reason for your decision first."
      : null)

  const submit = async () => {
    if (blocked || sending) return
    setSending(true)
    setRefusal(null)
    setAnswer(null)
    try {
      const token = await getToken()
      if (!token) throw new Error("Your session has expired. Sign in again to send this decision.")
      // An option travels as its id; the route records approved or chose from its write.
      const decision = outcome.decision === "chose" ? "approved" : outcome.decision
      const result = ticket
        ? await submitTicketAction(
            ticket.ticket_id,
            decision,
            reason,
            token,
            outcome.decision === "replied" ? reason : undefined,
            outcome.optionId
          )
        : await replyToCase(c.case_id, reason, decision, token, outcome.optionId)
      if (!result.enqueued && result.awaiting_second_approver) {
        if (approvedByMe) {
          // A route from before round five records a repeat and answers "awaiting" again.
          setAnswer("You already approved this change. A different approver must approve it too.")
          return
        }
        onDecided?.({ outcome, awaitingSecond: true, reason, at: new Date().toISOString() })
        return
      }
      if (!result.enqueued)
        throw new Error("Your decision was recorded, but the agent was not resumed.")
      onDecided?.({ outcome, awaitingSecond: false, reason, at: new Date().toISOString() })
    } catch (e) {
      // The same person approving twice: refused, nothing recorded, the case still waits.
      if (errorStatus(e) === 409 && errorBody(e)?.awaiting_second_approver) {
        setAnswer(
          e instanceof Error
            ? e.message
            : "You already approved this change. A different approver must approve it too."
        )
        return
      }
      setRefusal({
        // A standing emergency stop is a 409 too, but the case has not moved on: Refresh
        // would change nothing, so the panel shows the stop's own words only.
        moved: errorStatus(e) === 409 && !errorBody(e)?.emergency_stop,
        message: e instanceof Error ? e.message : "The decision was not sent.",
      })
    } finally {
      setSending(false)
    }
  }

  const write = outcome.write
  const steps = pendingProposal(c.agent_traces)?.steps ?? latestSteps(c)

  // Beside the case, the card is as tall as the page column at most: its title and the
  // reason with the button stay in view, and what the agent asks scrolls between them.
  return (
    <Card className="min-h-0">
      <CardHeader>
        <CardTitle>{mode === "reply" ? "The agent asks" : "Your decision"}</CardTitle>
        {mode === "decide" && reasons.length > 0 && (
          <CardDescription>
            {reasons.length === 1
              ? "Why a person decides"
              : `${reasons.length} reasons a person decides`}
          </CardDescription>
        )}
      </CardHeader>
      <CardContent className="flex min-h-0 flex-col gap-4 overflow-y-auto">
        {mode === "reply" ? (
          <Question c={c} ticket={ticket} />
        ) : (
          reasons.length > 0 && (
            <ul className="flex flex-col gap-2 text-sm">
              {reasons.map((r, i) => (
                <li key={i} className="flex gap-2">
                  <span
                    aria-hidden="true"
                    className="mt-2 size-1.5 flex-none rounded-full bg-attention"
                  />
                  {r.kind === "clause" ? <Clause reason={r} /> : <span>{r.text}</span>}
                </li>
              ))}
            </ul>
          )
        )}

        {mode === "decide" && (
          <section aria-labelledby={`${id}-change`} className="flex flex-col gap-2">
            <p id={`${id}-change`} className="text-sm font-medium">
              {write ? (
                <>
                  If you{" "}
                  {outcome.decision === "approved" && !outcome.optionId
                    ? "approve"
                    : `choose ${outcome.label}`}
                  , the agent makes this change in {systemWords}
                </>
              ) : outcome.decision === "chose" ? (
                `${outcome.label} makes no change in SAP.`
              ) : outcome.decision === "denied" ? (
                "Denying changes nothing in SAP."
              ) : outcome.decision === "replied" ? (
                "A reply approves no change. The agent resumes with your message."
              ) : (
                `The agent recorded no structured change. Approving lets it go on in ${systemWords}.`
              )}
            </p>
            {write && <ChangeDiff write={write} steps={steps} pack={pack} />}
          </section>
        )}

        {mode === "decide" && outcomes.length > 1 && (
          <RadioGroup
            aria-label="Outcome"
            value={choice}
            onValueChange={v => setChoice(String(v))}
            disabled={sending}
          >
            {outcomes.map(o => (
              <FieldLabel key={o.id} htmlFor={`${id}-${o.id}`}>
                <Field orientation="horizontal">
                  <RadioGroupItem value={o.id} id={`${id}-${o.id}`} />
                  <FieldContent>
                    <FieldTitle>{o.label}</FieldTitle>
                    {o.summary && <FieldDescription>{o.summary}</FieldDescription>}
                  </FieldContent>
                </Field>
              </FieldLabel>
            ))}
          </RadioGroup>
        )}

        {two.needed && mode === "decide" && <Approvals approvals={approvals} me={me} />}
        <div className="flex flex-col gap-1 text-xs text-muted-foreground">
          {mode === "decide" && outcome.decision === "approved" && write && (
            <p>
              Your approval covers exactly this change.
              {checks && ` Before it runs, the checks re-read SAP: ${checks}.`}
            </p>
          )}
          {after && <p>After you decide, {after}.</p>}
        </div>
      </CardContent>

      <CardFooter className="flex-col items-stretch gap-3">
        <Field>
          <FieldLabel htmlFor={`${id}-reason`}>
            {mode === "reply" ? "Your answer" : "Reason"}
            <span className="font-normal text-muted-foreground">required</span>
          </FieldLabel>
          <Textarea
            id={`${id}-reason`}
            value={text}
            disabled={sending || !!notEntitled}
            onChange={e => setText(e.target.value)}
            rows={2}
            placeholder={mode === "reply" ? "Answer the agent's question" : "Why this outcome"}
          />
          <FieldDescription>
            {mode === "reply"
              ? "The agent resumes with your answer, whatever the trigger mode."
              : "Recorded with your name and the change."}
          </FieldDescription>
        </Field>

        {refusal && (
          <Alert variant="destructive">
            <CircleAlert />
            <AlertTitle>{refusal.moved ? "This case moved on" : "Not sent"}</AlertTitle>
            <AlertDescription>
              {refusal.message}
              {refusal.moved && ". Refresh to see where it is now."}
            </AlertDescription>
            {refusal.moved && (
              <AlertAction>
                <Button size="sm" variant="outline" onClick={() => void onRefresh()}>
                  <RefreshCw />
                  Refresh
                </Button>
              </AlertAction>
            )}
          </Alert>
        )}
        {answer && (
          <Alert role="status">
            <AlertDescription>{answer}</AlertDescription>
          </Alert>
        )}

        <ReasonButton
          reason={blocked}
          onClick={() => void submit()}
          aria-busy={sending || undefined}
        >
          {sending && <Spinner />}
          {sending
            ? "Sending…"
            : mode === "reply"
              ? "Send and resume"
              : submitLabel(outcome, production)}
        </ReasonButton>
      </CardFooter>
    </Card>
  )
}

/** "a sandbox system", "a quality system": the system role as a noun for a system. */
function systemRoleNoun(role: string): string {
  const words = systemRoleWords(role)
  return words.endsWith("system") ? words : `${words} system`
}

/** Reply only, for a question with no decision. */
const REPLY: Outcome = {
  id: "replied",
  label: "Reply",
  summary: "",
  decision: "replied",
  write: null,
  action: "Send and resume",
}

function latestSteps(c: WorkItem) {
  const traces = c.agent_traces ?? []
  return traces.length ? segmentsOf(traces[traces.length - 1]) : []
}

function ChangeDiff({
  write,
  steps,
  pack,
}: {
  write: ProposedWrite
  steps: ReturnType<typeof latestSteps>
  pack: UseCasePack | undefined
}) {
  const entity = write.entity && write.op !== WriteOp.FunctionImport ? write.entity : null
  const labels = new Map(
    (pack?.fields ?? []).map(f => [f.sap_field.split(".").pop() ?? "", f.label] as const)
  )
  return (
    <WriteDiff
      rows={proposedRows(write, steps)}
      label="Proposed"
      hideLabel
      labelOf={name => labels.get(name) ?? sapWords(name)}
      target={{
        verb: writeVerb(write) ?? undefined,
        record: entity ? entityWords(entity) : undefined,
        key: write.key || undefined,
      }}
    />
  )
}

/** A SOP clause behind the decision: the SOP's name, with the quote on hover and focus. */
function Clause({ reason }: { reason: ReturnType<typeof decisionReasons>[number] }) {
  const sop = reason.sop
    ? `${reason.sop}${reason.section ? ` ${reason.section}` : ""}`
    : reason.section
      ? `Clause ${reason.section}`
      : "The SOP"
  return (
    <span>
      The SOP says a person decides here:{" "}
      <HoverCard>
        <HoverCardTrigger
          render={
            reason.href ? (
              <Link to={reason.href} className="font-medium underline underline-offset-4" />
            ) : (
              <span className="font-medium underline decoration-dotted underline-offset-4" />
            )
          }
        >
          {sop}
        </HoverCardTrigger>
        <HoverCardContent className="flex flex-col gap-2 text-sm">
          <p>&ldquo;{reason.text}&rdquo;</p>
          <p className="text-xs text-muted-foreground">
            {reason.citation?.verified
              ? "Quoted from the SOP this run was given."
              : "Not found word for word in the SOP this run was given; it may be a paraphrase."}
          </p>
        </HoverCardContent>
      </HoverCard>
    </span>
  )
}

/** "1 of 2 approvals", with who approved and when. */
function Approvals({
  approvals,
  me,
}: {
  approvals: { actor: string; name: string; at: string; optionLabel?: string }[]
  me: string
}) {
  const n = Math.min(approvals.length, 2)
  return (
    <div className="flex flex-col gap-1 text-sm" role="group" aria-label="Approvals">
      <p className="font-medium">{n} of 2 approvals</p>
      {approvals.length === 0 ? (
        <p className="text-muted-foreground">Two different approvers must approve this change.</p>
      ) : (
        approvals.map(a => (
          <p key={`${a.actor}${a.at}`} className="text-muted-foreground">
            {a.actor === me ? "You" : a.name} approved
            {a.optionLabel ? ` ${a.optionLabel}` : ""} {timeAgo(a.at)}.
            {a.actor === me && " A different approver must approve too."}
          </p>
        ))
      )}
    </div>
  )
}

/** The agent's question: the ticket's when one backs the case, else its last notification. */
function Question({ c, ticket }: { c: WorkItem; ticket: Ticket | null }) {
  const asked = lastNotification(c)
  const text = ticket?.description || asked?.body || asked?.subject || c.status_reason
  const to = ticket?.assigned_to || asked?.to
  return (
    <div className="flex flex-col gap-2 text-sm">
      <p className="leading-relaxed">
        {text || "The agent asked for information before it goes on."}
      </p>
      {(to || c.inquiry_sent_at) && (
        <p className="text-xs text-muted-foreground">
          Asked{to ? ` ${to}` : ""}
          {c.inquiry_sent_at ? ` ${timeAgo(c.inquiry_sent_at)}` : ""}.
          {ticket ? " The ticket gets the same answer." : ""}
        </p>
      )}
    </div>
  )
}

function lastNotification(c: WorkItem): { to?: string; subject?: string; body?: string } | null {
  const traces = c.agent_traces ?? []
  for (let t = traces.length - 1; t >= 0; t--) {
    const steps = segmentsOf(traces[t])
    for (let i = steps.length - 1; i >= 0; i--) {
      if (steps[i]?.evidence?.kind !== EvidenceKind.Notification) continue
      try {
        const input = JSON.parse(steps[i].tool_input ?? "{}")
        return { to: input.to, subject: input.subject, body: input.body ?? input.message }
      } catch {
        return null
      }
    }
  }
  return null
}
