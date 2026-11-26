// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useMemo, useState } from "react"
import { Link, useParams, useSearchParams } from "react-router"
import { useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { ArrowLeft, FlaskConical, Save } from "lucide-react"
import { Button, buttonVariants } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { Empty, EmptyHeader, EmptyTitle } from "@/components/ui/empty"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { DetailLayout } from "@/components/layout/DetailLayout"
import { ReasonButton } from "@/components/ReasonButton"
import { MarkdownRenderer } from "@/components/chat/MarkdownRenderer"
import { OUTCOME_LABEL, caseLabel } from "@/components/packs/EvaluationsPart"
import { useAssistant } from "@/hooks/useAgentChat"
import { getTranscript, useTranscript } from "@/hooks/transcript"
import { useApi } from "@/hooks/useApi"
import { useRegistry } from "@/hooks/useRegistry"
import {
  PROMOTION_ONLY_REASON,
  packKey,
  usePackEvaluations,
  usePromotionOnly,
} from "@/hooks/usePack"
import {
  saveExampleCase,
  type ExampleCase,
  type ExpectedOutcome,
} from "@/services/evaluationsService"
import { toolTick } from "@/lib/toolLabel"
import { humanise, type UseCasePack } from "@/lib/registry"
import type { Message } from "@/components/chat/types"

/**
 * Why a case described in words cannot become an example case: the evaluation runner seeds
 * every example case's documents in the sandbox from its `seed.inputs` before it runs it
 * (`evals/run_regression.py`, `seed_case`), and a description carries none.
 */
const NO_SEED =
  "An example case needs seed inputs to post in the sandbox. Start from an example case, or create one on the test data page."

/** What a rehearsal is, said where it runs. Not a marker anyone can hide (decision 22). */
const REHEARSAL =
  "Nothing is read from or written to a real case; the tools that change SAP, a case or a ticket are withheld."

/**
 * Rehearse (preview 1.7): pick the SOP, optionally start from an example case, describe the
 * case, and run. The run's steps show on this page as the agent works, read from the
 * transcript the rehearsal writes; the assistant panel does not open for it (decision 20).
 * A rehearsal worth keeping can be saved as an example case.
 */
export default function RehearsePage() {
  const { packId = "" } = useParams()
  const registry = useRegistry()
  const pack = registry.byId[packId]
  if (!pack)
    return registry.resolved ? (
      <Empty>
        <EmptyHeader>
          <EmptyTitle>No pack {packId} that you can see</EmptyTitle>
        </EmptyHeader>
      </Empty>
    ) : (
      <Skeleton className="m-6 h-32" />
    )
  return <Rehearse pack={pack} />
}

function describe(c: ExampleCase): string {
  const inputs = Object.entries(c.seed?.inputs ?? {})
    .map(([k, v]) => `${humanise(k)}: ${String(v)}`)
    .join(", ")
  return [c.description, inputs && `Seeded with ${inputs}.`].filter(Boolean).join("\n\n")
}

function Rehearse({ pack }: { pack: UseCasePack }) {
  const promotionOnly = usePromotionOnly()
  const [params] = useSearchParams()
  const chat = useAssistant()
  const evals = usePackEvaluations(pack.id)
  const sops = (pack.sops ?? []).filter(s => s.kind !== "alias")
  const example = evals.data?.example_cases.find(c => c.test_id === params.get("example"))
  const [processType, setProcessType] = useState(
    () =>
      params.get("sop") ??
      sops.find(s => s.kind === "triage")?.process_type ??
      sops[0]?.process_type ??
      ""
  )
  const [fromCase, setFromCase] = useState<string>("")
  const [text, setText] = useState("")
  const [started, setStarted] = useState<{
    slot: string
    at: number
    text: string
    from: string
  } | null>(null)
  const [saving, setSaving] = useState(false)

  // An example case named in the link fills the form once its list has loaded.
  const [seeded, setSeeded] = useState(false)
  if (example && !seeded) {
    setSeeded(true)
    setProcessType(example.process_type)
    setFromCase(example.test_id)
    setText(describe(example))
  }

  const examples = useMemo(
    () => (evals.data?.example_cases ?? []).filter(c => c.process_type === processType),
    [evals.data, processType]
  )
  const chosenSop = sops.find(s => s.process_type === processType)
  const perRun = evals.data?.example_cases.length
    ? evals.data.estimate_usd / evals.data.example_cases.length
    : 0.2
  const reason = !pack.access?.rehearse
    ? "Rehearsing needs the pack's authors."
    : !text.trim()
      ? "Describe the case first."
      : !chat.ready
        ? "Runs cannot start until the console has its config."
        : null

  const [refused, setRefused] = useState<string | null>(null)
  const run = async () => {
    const slot = chat.slotKey
    setRefused(null)
    setStarted({ slot, at: getTranscript(slot).length, text, from: fromCase })
    // Refused (a standing emergency stop, say): back to the form, with the API's words.
    const refusal = await chat.rehearse(pack.id, processType, text)
    if (refusal) {
      setStarted(null)
      setRefused(refusal)
    }
  }

  const startedFrom = evals.data?.example_cases.find(c => c.test_id === started?.from)
  const packPath = `/packs/${encodeURIComponent(pack.id)}`
  return (
    <div className="h-full overflow-y-auto">
      <div className="flex flex-col gap-6 p-6 pb-ask">
        <div>
          <Link className={buttonVariants({ variant: "ghost", size: "sm" })} to={packPath}>
            <ArrowLeft />
            {pack.display_name}
          </Link>
        </div>
        <header>
          <h1 className="flex items-center gap-2 text-xl font-semibold tracking-tight">
            <FlaskConical className="text-muted-foreground" />
            Rehearse
          </h1>
          <p className="mt-1 max-w-measure text-sm text-muted-foreground">
            Runs the saved SOP against a case you describe, with this deployment&apos;s values.
          </p>
        </header>
        <Alert>
          <FlaskConical />
          <AlertTitle>Rehearsal</AlertTitle>
          <AlertDescription>{REHEARSAL}</AlertDescription>
        </Alert>
        <DetailLayout
          main={
            started ? (
              <RunResult
                slot={started.slot}
                from={started.at}
                running={chat.isRunning(started.slot)}
                onSave={() => setSaving(true)}
                saveReason={
                  promotionOnly
                    ? PROMOTION_ONLY_REASON
                    : !pack.access?.edit.example_cases
                      ? "Changing example cases needs the pack's authors."
                      : !started.from
                        ? NO_SEED
                        : null
                }
                onAgain={() => setStarted(null)}
              />
            ) : (
              <Card>
                <CardHeader>
                  <CardTitle>The case</CardTitle>
                  <CardDescription>
                    Describe it the way the exception would arrive: documents, amounts, what SAP
                    shows.
                  </CardDescription>
                </CardHeader>
                <CardContent>
                  <form
                    onSubmit={e => {
                      e.preventDefault()
                      if (!reason) void run()
                    }}
                  >
                    <FieldGroup>
                      <Field>
                        <FieldLabel htmlFor="rehearse-sop">SOP</FieldLabel>
                        <NativeSelect
                          id="rehearse-sop"
                          value={processType}
                          onChange={e => {
                            setProcessType(e.target.value)
                            setFromCase("")
                          }}
                        >
                          {sops.map(s => (
                            <NativeSelectOption key={s.process_type} value={s.process_type}>
                              {humanise(s.process_type)}
                            </NativeSelectOption>
                          ))}
                        </NativeSelect>
                        {chosenSop?.kind === "shared" && (
                          <FieldDescription>
                            A shared SOP reads what the SOP that loads it recorded. Rehearsing it
                            from that SOP is closer to a real case.
                          </FieldDescription>
                        )}
                      </Field>
                      <Field>
                        <FieldLabel htmlFor="rehearse-example">
                          Start from an example case{" "}
                          <span className="font-normal text-muted-foreground">optional</span>
                        </FieldLabel>
                        <NativeSelect
                          id="rehearse-example"
                          value={fromCase}
                          disabled={!examples.length}
                          onChange={e => {
                            setFromCase(e.target.value)
                            const c = examples.find(x => x.test_id === e.target.value)
                            if (c) setText(describe(c))
                          }}
                        >
                          <NativeSelectOption value="">
                            {examples.length ? "None" : "No example case for this SOP"}
                          </NativeSelectOption>
                          {examples.map(c => (
                            <NativeSelectOption key={c.test_id} value={c.test_id}>
                              {caseLabel(c.test_id)}
                            </NativeSelectOption>
                          ))}
                        </NativeSelect>
                      </Field>
                      <Field>
                        <FieldLabel htmlFor="rehearse-text">The case</FieldLabel>
                        <Textarea
                          id="rehearse-text"
                          rows={6}
                          value={text}
                          placeholder="Supplier invoice for 10 units at 5,010.00 against PO 4500000123 at 5,000.00 per unit. Goods receipt for 10 units posted. Payment block R."
                          onChange={e => setText(e.target.value)}
                        />
                      </Field>
                      <div className="flex items-center gap-3">
                        <ReasonButton type="submit" reason={reason}>
                          <FlaskConical />
                          Rehearse
                        </ReasonButton>
                        <span className="text-sm text-muted-foreground">
                          Costs one run, about {perRun.toFixed(2)} USD. The transcript is not
                          stored.
                        </span>
                      </div>
                      {refused && (
                        <p role="alert" className="text-sm text-destructive">
                          {refused}
                        </p>
                      )}
                    </FieldGroup>
                  </form>
                </CardContent>
              </Card>
            )
          }
          side={
            <Card size="sm">
              <CardHeader>
                <CardTitle>{chosenSop ? humanise(chosenSop.process_type) : "SOP"}</CardTitle>
                <CardDescription>
                  The saved version runs. A change you have not saved is not rehearsed.
                </CardDescription>
              </CardHeader>
              {chosenSop && (
                <CardContent>
                  <Link
                    className="text-sm hover:underline"
                    to={`${packPath}/procedures/${encodeURIComponent(chosenSop.process_type)}`}
                  >
                    Open the SOP
                  </Link>
                </CardContent>
              )}
            </Card>
          }
        />
      </div>
      {started && startedFrom && (
        <SaveExampleDialog
          open={saving}
          pack={pack}
          processType={processType}
          text={started.text}
          from={startedFrom}
          version={evals.data?.version ?? null}
          onClose={() => setSaving(false)}
        />
      )}
    </div>
  )
}

/** The rehearsal's own turn, from the transcript it writes, as plain steps. */
function RunResult({
  slot,
  from,
  running,
  onSave,
  saveReason,
  onAgain,
}: {
  slot: string
  from: number
  running: boolean
  onSave: () => void
  saveReason: string | null
  onAgain: () => void
}) {
  const messages = useTranscript(slot).slice(from + 1)
  const replies = messages.filter((m): m is Message => m.role === "assistant")
  const steps = replies.flatMap(m => m.segments ?? [])
  const busy =
    running ||
    replies.length === 0 ||
    steps.some(
      s =>
        s.type === "tool" &&
        (s.toolCall.status === "streaming" || s.toolCall.status === "executing")
    )
  const text = replies
    .map(m =>
      m.segments?.length
        ? m.segments.map(s => (s.type === "text" ? s.content : "")).join("")
        : m.content
    )
    .join("\n\n")
    .trim()

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          {busy ? (
            <>
              <span
                aria-hidden="true"
                className="size-2 rounded-full bg-progress motion-safe:animate-pulse"
              />
              The agent is working through the SOP
            </>
          ) : (
            "Result"
          )}
        </CardTitle>
        <CardDescription>
          The same steps as a live run; nothing it would write was sent.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div role="log" aria-live="polite" aria-label="Steps">
          <ol className="flex flex-col gap-1.5 text-sm">
            {steps
              .filter(s => s.type === "tool")
              .map((s, i) => {
                if (s.type !== "tool") return null
                const tick = toolTick(s.toolCall.name, s.toolCall.input, s.toolCall.status)
                const words = [tick.verb, tick.subject].filter(Boolean).join(" ")
                return (
                  <li key={s.toolCall.toolUseId || i} className="flex items-center gap-2">
                    <span aria-hidden="true" className="size-1.5 rounded-full bg-foreground" />
                    {words.charAt(0).toUpperCase() + words.slice(1)}
                    {tick.writes && (
                      <span className="text-muted-foreground">· withheld in a rehearsal</span>
                    )}
                  </li>
                )
              })}
            {busy && (
              <li className="flex items-center gap-2 text-muted-foreground">
                <Spinner />
                Working
              </li>
            )}
          </ol>
        </div>
        {text && (
          <div className="max-w-measure text-sm">
            <MarkdownRenderer content={text} />
          </div>
        )}
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={onAgain} disabled={busy}>
            Rehearse again
          </Button>
          <ReasonButton
            variant="outline"
            size="sm"
            reason={busy ? "Wait for the rehearsal to finish." : saveReason}
            onClick={onSave}
          >
            <Save />
            Save as example case
          </ReasonButton>
        </div>
      </CardContent>
    </Card>
  )
}

const OUTCOMES = Object.keys(OUTCOME_LABEL) as ExpectedOutcome[]

function slugOf(s: string): string {
  const slug = s
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 60)
  return /^[a-z]/.test(slug) ? slug : `case_${slug}`
}

/**
 * Save as example case (decision recorded in task 2.18): a rehearsal started from an example
 * case, with the outcome the agent must reach and why, written through the evaluations edit
 * route as the pack's next version. It keeps that case's seed, which the evaluation runner
 * posts in the sandbox; a rehearsal described freely has none, so it cannot be saved here.
 */
function SaveExampleDialog({
  open,
  pack,
  processType,
  text,
  from,
  version,
  onClose,
}: {
  open: boolean
  pack: UseCasePack
  processType: string
  text: string
  from: ExampleCase
  version: number | null
  onClose: () => void
}) {
  const getToken = useApi()
  const queryClient = useQueryClient()
  const first = text.split("\n")[0].slice(0, 200)
  const [testId, setTestId] = useState(() =>
    slugOf(`${processType}_${first.split(" ").slice(0, 6).join(" ")}`)
  )
  const [description, setDescription] = useState(first)
  const [outcome, setOutcome] = useState<ExpectedOutcome>(from.expected.outcome)
  const [why, setWhy] = useState("")
  const [busy, setBusy] = useState(false)
  const valid = /^[a-z][a-z0-9_]{2,79}$/.test(testId) && description.trim() && why.trim()

  const save = async () => {
    setBusy(true)
    try {
      await saveExampleCase(
        pack.id,
        testId,
        {
          description: description.trim(),
          process_type: processType,
          seed: from.seed,
          expected: { outcome, reason: why.trim() },
        },
        await getToken(),
        version
      )
      toast.success("Saved as an example case", {
        description: "A new version of the pack. The next evaluation run includes it.",
      })
      await queryClient.invalidateQueries({ queryKey: packKey(pack.id, "evaluations") })
      onClose()
    } catch (e) {
      toast.error("Couldn't save the example case", { description: (e as Error).message })
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={o => !o && !busy && onClose()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Save as example case</DialogTitle>
          <DialogDescription>
            An example case names the outcome the agent must reach. Every evaluation run checks it.
          </DialogDescription>
        </DialogHeader>
        <FieldGroup>
          <Field>
            <FieldLabel htmlFor="example-id">Name</FieldLabel>
            <Input
              id="example-id"
              className="font-mono"
              value={testId}
              onChange={e => setTestId(e.target.value)}
            />
            <FieldDescription>
              Lowercase letters, digits and underscores. The same name replaces a case.
            </FieldDescription>
          </Field>
          <Field>
            <FieldLabel htmlFor="example-description">Description</FieldLabel>
            <Textarea
              id="example-description"
              rows={3}
              value={description}
              onChange={e => setDescription(e.target.value)}
            />
          </Field>
          <Field>
            <FieldLabel htmlFor="example-outcome">The agent must reach</FieldLabel>
            <NativeSelect
              id="example-outcome"
              value={outcome}
              onChange={e => setOutcome(e.target.value as ExpectedOutcome)}
            >
              {OUTCOMES.map(o => (
                <NativeSelectOption key={o} value={o}>
                  {OUTCOME_LABEL[o]}
                </NativeSelectOption>
              ))}
            </NativeSelect>
          </Field>
          <Field>
            <FieldLabel htmlFor="example-why">Why</FieldLabel>
            <Textarea
              id="example-why"
              rows={2}
              value={why}
              onChange={e => setWhy(e.target.value)}
              placeholder="The clause that decides it, and the numbers."
            />
          </Field>
        </FieldGroup>
        <DialogFooter>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={() => void save()} disabled={!valid || busy}>
            {busy ? "Saving…" : "Save"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
