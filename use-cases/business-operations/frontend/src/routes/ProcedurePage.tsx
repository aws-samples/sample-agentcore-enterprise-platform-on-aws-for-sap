// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { Suspense, lazy, useEffect, useMemo, useRef, useState } from "react"
import { Link, Navigate, useBlocker, useNavigate, useParams } from "react-router"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useAuth } from "react-oidc-context"
import { toast } from "sonner"
import { ArrowLeft, CircleCheck, CircleX, FlaskConical, Pencil } from "lucide-react"
import { Button, buttonVariants } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "@/components/ui/empty"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { EditorLayout } from "@/components/layout/EditorLayout"
import { ReasonButton } from "@/components/ReasonButton"
import { SetHereBadge } from "@/components/packs/PartHeader"
import { SopProse, parseSop, type Resolve } from "@/components/packs/SopProse"
import { useApi } from "@/hooks/useApi"
import { useRegistry } from "@/hooks/useRegistry"
import {
  PROMOTION_ONLY_REASON,
  useDeploymentSettings,
  usePackEvaluations,
  usePackRules,
} from "@/hooks/usePack"
import { PromotionOnlyBanner } from "@/components/packs/PromotionOnlyBanner"
import { fetchBasePrompt, patchPack } from "@/services/packsService"
import { checkSop, fetchSop, saveSop } from "@/services/sopsService"
import { citedSymbols, formatRule, ruleLabel, sopKindSentence } from "@/lib/packParts"
import { diffCounts, diffLines, type DiffLine } from "@/lib/lineDiff"
import { LineDiffView } from "@/components/packs/LineDiffView"
import { humanise, type PackSop, type UseCasePack } from "@/lib/registry"
import { timeAgo } from "@/lib/timeAgo"

const SopCodeEditor = lazy(() => import("@/components/packs/SopCodeEditor"))

/** One document on this page: a SOP by its process type, or the pack's general instructions. */
interface Doc {
  text: string
  version: number | null
  savedBy?: string | null
  savedAt?: string | null
  canAuthor: boolean
}

/**
 * A procedure (preview 1.3): the SOP read as a document, with every rule and contact shown
 * as its value; Edit in CodeMirror, checked as you type by the same check a save runs; and a
 * diff before Save. Unsaved changes hold navigation until the person chooses.
 *
 * `/packs/:packId/procedures/:processType` opens a SOP; `/packs/:packId/instructions` the
 * general instructions, which the agent reads around every SOP and which have no SOP check.
 */
export default function ProcedurePage({ instructions = false }: { instructions?: boolean }) {
  const { packId = "", processType = "" } = useParams()
  const registry = useRegistry()
  const pack = registry.byId[packId]
  const sop = pack?.sops?.find(s => s.process_type === processType)

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
  if (!instructions && sop?.kind === "alias" && sop.alias_of)
    return (
      <Navigate to={`/packs/${encodeURIComponent(packId)}/procedures/${sop.alias_of}`} replace />
    )
  if (!instructions && !sop)
    return (
      <Empty>
        <EmptyHeader>
          <EmptyTitle>No SOP for {humanise(processType)}</EmptyTitle>
          <EmptyDescription>This pack maps no SOP to that process type.</EmptyDescription>
        </EmptyHeader>
      </Empty>
    )
  return (
    <Procedure
      key={instructions ? "instructions" : processType}
      pack={pack}
      sop={instructions ? null : sop!}
    />
  )
}

function Procedure({ pack, sop }: { pack: UseCasePack; sop: PackSop | null }) {
  const auth = useAuth()
  const getToken = useApi()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const rules = usePackRules(pack.id)
  const settings = useDeploymentSettings()
  const evals = usePackEvaluations(pack.id)
  const key = ["pack", pack.id, "sop", sop?.process_type ?? "_instructions"]
  const query = useQuery({
    queryKey: key,
    enabled: auth.isAuthenticated,
    queryFn: async (): Promise<Doc> => {
      const token = await getToken()
      if (!sop) {
        const b = await fetchBasePrompt(pack.id, token)
        return { text: b.text, version: b.version, canAuthor: !!pack.access?.edit.procedures_text }
      }
      const d = await fetchSop(pack.id, sop.process_type, token)
      return {
        text: d.content,
        version: d.pack_version ?? null,
        savedBy: d.uploaded_by,
        savedAt: d.uploaded_at ?? d.last_modified,
        canAuthor: d.can_author && !!pack.access?.edit.procedures_text,
      }
    },
  })
  const doc = query.data

  const [draft, setDraft] = useState<string | null>(null)
  const [reviewing, setReviewing] = useState(false)
  const [saving, setSaving] = useState(false)
  const dirty = draft != null && doc != null && draft !== doc.text
  const check = useLiveCheck(pack.id, sop, draft)

  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) =>
      dirty && !saving && currentLocation.pathname !== nextLocation.pathname
  )
  useEffect(() => {
    if (!dirty) return
    const warn = (e: BeforeUnloadEvent) => e.preventDefault()
    window.addEventListener("beforeunload", warn)
    return () => window.removeEventListener("beforeunload", warn)
  }, [dirty])

  // What a `{{SYMBOL}}` reads as in this deployment.
  const resolve: Resolve = useMemo(() => {
    const ruleValues = rules.data?.rules ?? {}
    const contacts = settings.data?.contacts.effective ?? {}
    return symbol => {
      const r = ruleValues[symbol]
      if (r)
        return {
          value: formatRule(symbol, r.value),
          note: r.guarded_by.length ? "a rule, also checked before every write" : "a rule",
        }
      const c = contacts[symbol]?.value
      if (c) return { value: c, note: "a contact, set per deployment" }
      return null
    }
  }, [rules.data, settings.data])

  const symbols = useMemo(
    () => [
      ...Object.entries(rules.data?.rules ?? {}).map(([k, r]) => ({
        name: k,
        detail: formatRule(k, r.value),
      })),
      ...Object.entries(settings.data?.contacts.effective ?? {}).map(([k, c]) => ({
        name: k,
        detail: c.value ?? "contact",
      })),
    ],
    [rules.data, settings.data]
  )

  const title = sop ? humanise(sop.process_type) : "General instructions"
  const packPath = `/packs/${encodeURIComponent(pack.id)}`
  const rehearsePath = `${packPath}/rehearse${sop ? `?sop=${encodeURIComponent(sop.process_type)}` : ""}`

  const save = async (andRehearse: boolean) => {
    if (!doc || draft == null) return
    setSaving(true)
    try {
      const token = await getToken()
      if (sop) await saveSop(pack.id, sop.process_type, draft, token, doc.version)
      else await patchPack(pack.id, { base_prompt: draft }, token, doc.version)
      toast.success(`Saved ${title}`, {
        description: "The next run here uses it. SOP search re-indexes it within minutes.",
      })
      setReviewing(false)
      setDraft(null)
      await queryClient.invalidateQueries({ queryKey: ["pack", pack.id] })
      if (andRehearse) navigate(rehearsePath)
    } catch (e) {
      toast.error(`Couldn't save ${title}`, { description: (e as Error).message })
    } finally {
      setSaving(false)
    }
  }

  if (query.isError)
    return (
      <div className="p-6">
        <Alert variant="destructive">
          <AlertTitle>Couldn&apos;t open {title}</AlertTitle>
          <AlertDescription>{(query.error as Error).message}</AlertDescription>
        </Alert>
      </div>
    )
  if (!doc)
    return (
      <div className="flex flex-col gap-3 p-6">
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-96 w-full" />
      </div>
    )

  const parsed = parseSop(draft ?? doc.text)
  const cited = citedSymbols(draft ?? doc.text)
  const examples = sop
    ? (evals.data?.example_cases.filter(c => c.process_type === sop.process_type) ?? [])
    : []
  const handsOver = sop ? handOvers(doc.text, pack.sops ?? [], sop.process_type) : []
  const editing = draft != null
  const blocked =
    editing && (check.pending || (check.findings?.length ?? 0) > 0 || check.error != null)
  const editReason = settings.data?.deployment.promotion_only
    ? PROMOTION_ONLY_REASON
    : doc.canAuthor
      ? null
      : "Editing a SOP needs the pack's authors."

  return (
    <div className="h-full overflow-y-auto">
      <div className="flex flex-col gap-6 p-6 pb-ask">
        <div>
          <Link
            className={buttonVariants({ variant: "ghost", size: "sm" })}
            to={`${packPath}?tab=procedures`}
          >
            <ArrowLeft />
            All SOPs
          </Link>
        </div>
        <PromotionOnlyBanner />
        <header className="flex items-start gap-4">
          <div className="min-w-0 flex-1">
            <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
            <p className="mt-1 max-w-measure text-sm text-muted-foreground">
              {editing
                ? "Editing. Nothing is saved until you save; the check runs as you type."
                : [
                    sop
                      ? sopKindSentence(sop, pack.sops ?? [])
                      : "Read on every case of this pack, around whichever SOP applies.",
                    parsed.version && `Version ${parsed.version}.`,
                    doc.savedAt &&
                      `Saved ${timeAgo(doc.savedAt)}${doc.savedBy && doc.savedBy !== "deploy" ? ` by ${doc.savedBy}` : " by the deploy"}.`,
                  ]
                    .filter(Boolean)
                    .join(" ")}
            </p>
          </div>
          <div className="flex flex-none gap-2">
            {editing ? (
              <>
                <Button variant="ghost" onClick={() => setDraft(null)} disabled={saving}>
                  Cancel
                </Button>
                <Button onClick={() => setReviewing(true)} disabled={!dirty || blocked || saving}>
                  Save
                </Button>
              </>
            ) : (
              <>
                <ReasonButton
                  variant="outline"
                  reason={pack.access?.rehearse ? null : "Rehearsing needs the pack's authors."}
                  onClick={() => navigate(rehearsePath)}
                >
                  <FlaskConical />
                  Rehearse
                </ReasonButton>
                <ReasonButton reason={editReason} onClick={() => setDraft(doc.text)}>
                  <Pencil />
                  Edit
                </ReasonButton>
              </>
            )}
          </div>
        </header>

        <EditorLayout
          main={
            editing ? (
              <Tabs defaultValue="edit">
                <TabsList>
                  <TabsTrigger value="edit">Edit</TabsTrigger>
                  <TabsTrigger value="preview">Preview</TabsTrigger>
                </TabsList>
                <TabsContent value="edit" className="pt-3">
                  <Suspense fallback={<Skeleton className="h-96 w-full" />}>
                    <SopCodeEditor
                      value={draft}
                      onChange={setDraft}
                      symbols={symbols}
                      label={`${title} SOP text`}
                    />
                  </Suspense>
                </TabsContent>
                <TabsContent value="preview" className="pt-3">
                  <SopProse sop={parsed} resolve={resolve} />
                </TabsContent>
              </Tabs>
            ) : parsed.sections.length ? (
              <SopProse sop={parsed} resolve={resolve} />
            ) : (
              <pre className="max-w-measure font-sans text-base leading-relaxed whitespace-pre-wrap">
                {doc.text}
              </pre>
            )
          }
          side={
            <>
              {editing && sop && <CheckCard check={check} />}
              {!editing && parsed.sections.length > 2 && (
                <Card size="sm">
                  <CardHeader>
                    <CardTitle>On this page</CardTitle>
                  </CardHeader>
                  <CardContent>
                    <nav aria-label="On this page">
                      <ul className="flex flex-col gap-1 text-sm">
                        {parsed.sections.map(s => (
                          <li key={s.id}>
                            <a
                              className="text-muted-foreground hover:text-foreground"
                              href={`#sop-${s.id}`}
                            >
                              {s.title}
                            </a>
                          </li>
                        ))}
                      </ul>
                    </nav>
                  </CardContent>
                </Card>
              )}
              <CitesCard
                cited={cited}
                resolve={resolve}
                packPath={packPath}
                rules={rules.data?.rules}
              />
              {handsOver.length > 0 && (
                <Card size="sm">
                  <CardHeader>
                    <CardTitle>Hands over to</CardTitle>
                  </CardHeader>
                  <CardContent>
                    <ul className="flex flex-col gap-1 text-sm">
                      {handsOver.map(pt => (
                        <li key={pt}>
                          <Link className="hover:underline" to={`${packPath}/procedures/${pt}`}>
                            {humanise(pt)}
                          </Link>
                        </li>
                      ))}
                    </ul>
                  </CardContent>
                </Card>
              )}
              {sop && (
                <Card size="sm">
                  <CardHeader>
                    <CardTitle>Example cases</CardTitle>
                    <CardDescription>
                      {examples.length
                        ? `${examples.length} example case${examples.length === 1 ? "" : "s"} test this SOP.`
                        : "No example case tests this SOP yet."}
                    </CardDescription>
                  </CardHeader>
                  <CardContent>
                    <Link className="text-sm hover:underline" to={`${packPath}?tab=evaluations`}>
                      Evaluations
                    </Link>
                  </CardContent>
                </Card>
              )}
            </>
          }
        />
      </div>

      <SaveDialog
        open={reviewing}
        title={title}
        diff={reviewing && draft != null ? diffLines(doc.text, draft) : []}
        saving={saving}
        onCancel={() => setReviewing(false)}
        onSave={andRehearse => void save(andRehearse)}
        canRehearse={!!pack.access?.rehearse}
      />

      <AlertDialog open={blocker.state === "blocked"}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Leave without saving?</AlertDialogTitle>
            <AlertDialogDescription>
              Your changes to {title} are not saved. Leaving discards them.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel onClick={() => blocker.reset?.()}>Keep editing</AlertDialogCancel>
            <AlertDialogAction variant="destructive" onClick={() => blocker.proceed?.()}>
              Discard changes
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}

/** The process types a SOP hands over to with `load_sop`, as it names them in its text. */
function handOvers(text: string, sops: PackSop[], self: string): string[] {
  const known = new Set(sops.filter(s => s.kind !== "alias").map(s => s.process_type))
  const named = [...text.matchAll(/process_type\s+`?([a-z][a-z0-9_]*)`?/g)].map(m => m[1])
  return [...new Set(named)].filter(pt => pt !== self && known.has(pt))
}

interface LiveCheck {
  pending: boolean
  findings: string[] | null
  error: string | null
}

/** The API's SOP check, run as a person types (debounced), with what a save would refuse. */
function useLiveCheck(packId: string, sop: PackSop | null, draft: string | null): LiveCheck {
  const getToken = useApi()
  const [state, setState] = useState<LiveCheck>({ pending: false, findings: null, error: null })
  const seq = useRef(0)
  useEffect(() => {
    if (draft == null || !sop) {
      setState({ pending: false, findings: null, error: null })
      return
    }
    const mine = ++seq.current
    setState(s => ({ ...s, pending: true }))
    const t = setTimeout(async () => {
      try {
        const r = await checkSop(packId, sop.process_type, draft, await getToken())
        if (mine === seq.current) setState({ pending: false, findings: r.findings, error: null })
      } catch (e) {
        if (mine === seq.current)
          setState({ pending: false, findings: null, error: (e as Error).message })
      }
    }, 400)
    return () => clearTimeout(t)
  }, [packId, sop, draft, getToken])
  return state
}

function CheckCard({ check }: { check: LiveCheck }) {
  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          Check {check.pending && <Spinner />}
        </CardTitle>
        <CardDescription>
          The same check runs again when the SOP is saved. Save is off while it finds a problem.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {check.error ? (
          <p className="text-sm text-destructive">{check.error}</p>
        ) : check.findings == null ? (
          <p className="text-sm text-muted-foreground">Checking…</p>
        ) : check.findings.length === 0 ? (
          <p className="flex items-center gap-2 text-sm">
            <CircleCheck className="size-4 text-success-ink" />
            Found nothing
          </p>
        ) : (
          <ul aria-label="Check findings" className="flex flex-col gap-2 text-sm">
            {check.findings.map(f => (
              <li key={f} className="flex gap-2">
                <CircleX className="mt-0.5 size-4 flex-none text-destructive" />
                {f}
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  )
}

function CitesCard({
  cited,
  resolve,
  packPath,
  rules,
}: {
  cited: string[]
  resolve: Resolve
  packPath: string
  rules: Record<string, { guarded_by: string[] }> | undefined
}) {
  const ruleSyms = cited.filter(s => rules?.[s])
  const contacts = cited.filter(s => s.startsWith("CONTACT_"))
  if (!ruleSyms.length && !contacts.length) return null
  return (
    <>
      {ruleSyms.length > 0 && (
        <Card size="sm">
          <CardHeader>
            <CardTitle>Rules this cites</CardTitle>
            <CardDescription>
              A rule belongs to the pack: changing it changes every SOP that cites it.{" "}
              <Link className="underline" to={`${packPath}?tab=procedures`}>
                Rules
              </Link>
            </CardDescription>
          </CardHeader>
          <CardContent>
            <dl className="flex flex-col gap-2 text-sm">
              {ruleSyms.map(s => (
                <div key={s} className="flex items-baseline justify-between gap-3">
                  <dt className="min-w-0">
                    <span className="block truncate">{ruleLabel(s)}</span>
                    {rules?.[s]?.guarded_by.length ? (
                      <span className="text-xs text-muted-foreground">
                        Checked before every write
                      </span>
                    ) : null}
                  </dt>
                  <dd className="flex-none tabular-nums">{resolve(s)?.value}</dd>
                </div>
              ))}
            </dl>
          </CardContent>
        </Card>
      )}
      {contacts.length > 0 && (
        <Card size="sm">
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              Contacts <SetHereBadge />
            </CardTitle>
          </CardHeader>
          <CardContent>
            <dl className="flex flex-col gap-2 text-sm">
              {contacts.map(s => (
                <div key={s} className="flex flex-col">
                  <dt>{humanise(s.replace(/^CONTACT_/, "").toLowerCase())}</dt>
                  <dd className="truncate text-muted-foreground">
                    {resolve(s)?.value ?? "No address set"}
                  </dd>
                </div>
              ))}
            </dl>
          </CardContent>
        </Card>
      )}
    </>
  )
}

function SaveDialog({
  open,
  title,
  diff,
  saving,
  onCancel,
  onSave,
  canRehearse,
}: {
  open: boolean
  title: string
  diff: DiffLine[]
  saving: boolean
  onCancel: () => void
  onSave: (andRehearse: boolean) => void
  canRehearse: boolean
}) {
  const { added, removed } = diffCounts(diff)
  return (
    <Dialog open={open} onOpenChange={o => !o && !saving && onCancel()}>
      <DialogContent className="flex max-h-svh flex-col sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle>Save {title}</DialogTitle>
          <DialogDescription>
            {added} line{added === 1 ? "" : "s"} added, {removed} removed. In development the next
            run here uses it at once; at the next release the process owner approves it with the
            rest of Procedures.
          </DialogDescription>
        </DialogHeader>
        {/* Focusable so a keyboard user can scroll a long diff (axe: scrollable-region-focusable). */}
        <div
          role="region"
          aria-label="Changes"
          // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
          tabIndex={0}
          className="min-h-0 grow overflow-auto rounded-lg border bg-card py-2 text-sm"
        >
          <LineDiffView diff={diff} />
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={onCancel} disabled={saving}>
            Keep editing
          </Button>
          {canRehearse && (
            <Button variant="outline" onClick={() => onSave(true)} disabled={saving}>
              Save and rehearse
            </Button>
          )}
          <Button onClick={() => onSave(false)} disabled={saving}>
            {saving ? "Saving…" : "Save"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
