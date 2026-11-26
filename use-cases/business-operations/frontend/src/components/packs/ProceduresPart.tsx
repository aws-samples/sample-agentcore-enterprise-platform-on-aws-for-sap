// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { Ident } from "@/components/Ident"
import { Fragment, useMemo, useState, type ReactNode } from "react"
import { Link } from "react-router"
import { useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { ShieldCheck } from "lucide-react"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { ReasonButton } from "@/components/ReasonButton"
import { PartHeader } from "@/components/packs/PartHeader"
import { useApi } from "@/hooks/useApi"
import {
  PROMOTION_ONLY_REASON,
  usePackDocument,
  usePackEvaluations,
  usePackRules,
  usePackSops,
  usePromotionOnly,
} from "@/hooks/usePack"
import { patchPack } from "@/services/packsService"
import {
  KIND_LABEL,
  citingSops,
  formatRule,
  guardLabel,
  ruleLabel,
  sopUse,
  sopsByKind,
} from "@/lib/packParts"
import { TruncatedText } from "@/components/TruncatedText"
import { humanise, type UseCasePack } from "@/lib/registry"
import { timeAgo } from "@/lib/timeAgo"

/**
 * Procedures: the SOPs grouped by the part each plays, the rules they cite with the write
 * checks that also read them, and the outcomes a case may close with. A rule's value is pack
 * content, edited as the pack (`PATCH /packs/{pack_id}`), so a change is the pack's next
 * version and reaches every stack it is promoted to.
 */
export function ProceduresPart({ pack }: { pack: UseCasePack }) {
  return (
    <div className="flex flex-col gap-6">
      <PartHeader pack={pack} part="procedures" />
      <SopsCard pack={pack} />
      <RulesCard pack={pack} />
      <OutcomesCard pack={pack} />
    </div>
  )
}

function SopsCard({ pack }: { pack: UseCasePack }) {
  const sops = usePackSops(pack.id)
  const evals = usePackEvaluations(pack.id)
  const files = new Map((sops.data?.sops ?? []).map(s => [s.key, s]))
  const examples = (pt: string) =>
    evals.data?.example_cases.filter(c => c.process_type === pt).length ?? null
  const base = `/packs/${encodeURIComponent(pack.id)}`

  return (
    <Card>
      <CardHeader>
        <CardTitle>SOPs</CardTitle>
        <CardDescription>
          The written steps the agent follows. Triage identifies the exception type and hands over
          to that type&apos;s SOP.
        </CardDescription>
      </CardHeader>
      <CardContent className="px-0">
        <Table className="table-fixed">
          <TableHeader>
            <TableRow>
              <TableHead className="pl-6">SOP</TableHead>
              <TableHead>When it is used</TableHead>
              <TableHead className="w-36 text-right">Example cases</TableHead>
              <TableHead className="w-32 pr-6 text-right">Last saved</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            <GroupHeading>Every case</GroupHeading>
            <TableRow className="h-9">
              <TableCell className="truncate pl-6">
                <Link className="font-medium hover:underline" to={`${base}/instructions`}>
                  General instructions
                </Link>
              </TableCell>
              <TableCell className="truncate text-muted-foreground">
                Every case, around the SOP
              </TableCell>
              <TableCell className="text-right text-muted-foreground">–</TableCell>
              <TableCell className="pr-6 text-right text-muted-foreground">–</TableCell>
            </TableRow>
            {sopsByKind(pack).map(([kind, list]) => (
              <Fragment key={kind}>
                <GroupHeading>{KIND_LABEL[kind]}</GroupHeading>
                {list.map(s => {
                  const file = files.get(s.sop)
                  const n = s.kind === "alias" ? null : examples(s.process_type)
                  return (
                    <TableRow key={s.process_type} className="h-9">
                      <TableCell className="truncate pl-6">
                        <Link
                          className="font-medium hover:underline"
                          to={`${base}/procedures/${encodeURIComponent(s.alias_of ?? s.process_type)}`}
                        >
                          {humanise(s.process_type)}
                        </Link>
                      </TableCell>
                      <TableCell className="truncate text-muted-foreground">{sopUse(s)}</TableCell>
                      <TableCell className="text-right tabular-nums text-muted-foreground">
                        {n == null ? "–" : n === 0 ? "None" : n}
                      </TableCell>
                      <TableCell className="pr-6 text-right text-muted-foreground">
                        {file && s.kind !== "alias" ? timeAgo(file.last_modified) : "–"}
                      </TableCell>
                    </TableRow>
                  )
                })}
              </Fragment>
            ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  )
}

/**
 * A group's heading among the SOPs: a header cell for the rows under it, at section heading
 * size and weight with room above, so the group reads above its SOP names, not below them.
 */
function GroupHeading({ children }: { children: ReactNode }) {
  return (
    <TableRow className="border-b-0 hover:bg-transparent">
      <TableHead
        colSpan={4}
        scope="colgroup"
        className="h-auto pt-6 pb-1 pl-6 text-sm font-semibold text-foreground"
      >
        {children}
      </TableHead>
    </TableRow>
  )
}

function RulesCard({ pack }: { pack: UseCasePack }) {
  const rules = usePackRules(pack.id)
  const doc = usePackDocument(pack.id)
  const getToken = useApi()
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState<Record<string, string> | null>(null)
  const [saving, setSaving] = useState(false)
  const may = !!pack.access?.edit.rule_values
  const promotionOnly = usePromotionOnly()
  const entries = useMemo(() => Object.entries(rules.data?.rules ?? {}), [rules.data])
  const changed = draft
    ? entries.filter(([k, r]) => draft[k] !== undefined && draft[k] !== String(r.value ?? ""))
    : []
  const guardedChange = changed.some(([, r]) => r.guarded_by.length > 0)

  const save = async () => {
    setSaving(true)
    try {
      const constants = Object.fromEntries(changed.map(([k]) => [k, Number(draft![k])]))
      await patchPack(pack.id, { pack: { constants } }, await getToken(), doc.data?.version)
      toast.success(`Saved ${changed.length} rule${changed.length === 1 ? "" : "s"}`, {
        description: "A new version of the pack. The next run here uses it.",
      })
      setDraft(null)
      await queryClient.invalidateQueries({ queryKey: ["pack", pack.id] })
    } catch (e) {
      toast.error("Couldn't save the rules", { description: (e as Error).message })
    } finally {
      setSaving(false)
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Rules</CardTitle>
        <CardDescription>
          Named values the SOPs cite. They belong to the pack, so they are the same in every
          deployment this version reaches.
        </CardDescription>
        <CardAction>
          {draft ? (
            <div className="flex gap-2">
              <Button variant="ghost" size="sm" onClick={() => setDraft(null)} disabled={saving}>
                Cancel
              </Button>
              <Button size="sm" onClick={() => void save()} disabled={!changed.length || saving}>
                {saving ? "Saving…" : "Save"}
              </Button>
            </div>
          ) : (
            <ReasonButton
              variant="outline"
              size="sm"
              reason={
                promotionOnly
                  ? PROMOTION_ONLY_REASON
                  : may
                    ? null
                    : "Changing a rule needs the pack's authors."
              }
              onClick={() =>
                setDraft(Object.fromEntries(entries.map(([k, r]) => [k, String(r.value ?? "")])))
              }
            >
              Edit values
            </ReasonButton>
          )}
        </CardAction>
      </CardHeader>
      <CardContent className="flex flex-col gap-3 px-0">
        {rules.isError ? (
          <div className="px-6">
            <Alert variant="destructive">
              <AlertTitle>Couldn&apos;t read the rules</AlertTitle>
              <AlertDescription>{(rules.error as Error).message}</AlertDescription>
            </Alert>
          </div>
        ) : !rules.data ? (
          <div className="px-6">
            <Skeleton className="h-24 w-full" />
          </div>
        ) : entries.length === 0 ? (
          <p className="px-6 text-sm text-muted-foreground">This pack declares no rules.</p>
        ) : (
          <Table className="table-fixed">
            <TableHeader>
              <TableRow>
                <TableHead className="pl-6">Rule</TableHead>
                <TableHead className="w-48">Value</TableHead>
                <TableHead className="w-56">Cited by</TableHead>
                <TableHead className="w-44 pr-6">Also checked</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {entries.map(([symbol, r]) => (
                <TableRow key={symbol} className="h-9">
                  <TableCell className="truncate pl-6">
                    <span className="font-medium">{ruleLabel(symbol)}</span>
                    <Ident className="ml-2">{symbol}</Ident>
                  </TableCell>
                  <TableCell className="tabular-nums">
                    {draft ? (
                      <Input
                        aria-label={ruleLabel(symbol)}
                        type="number"
                        step="any"
                        className="h-8 w-32"
                        value={draft[symbol] ?? ""}
                        onChange={e => setDraft({ ...draft, [symbol]: e.target.value })}
                      />
                    ) : (
                      formatRule(symbol, r.value)
                    )}
                  </TableCell>
                  <TableCell>
                    <CitedBy pack={pack} citedBy={r.cited_by} />
                  </TableCell>
                  <TableCell className="pr-6">
                    {r.guarded_by.length ? (
                      <Tooltip>
                        <TooltipTrigger
                          render={<span className="inline-flex items-center gap-1.5" />}
                        >
                          <ShieldCheck className="size-3.5 text-muted-foreground" />
                          Before writes
                        </TooltipTrigger>
                        <TooltipContent>
                          Read by {r.guarded_by.map(guardLabel).join(", ")}
                        </TooltipContent>
                      </Tooltip>
                    ) : (
                      <span className="text-muted-foreground">SOP text only</span>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
        {guardedChange && (
          <div className="px-6">
            <Alert>
              <AlertTitle>A check before every write reads this rule</AlertTitle>
              <AlertDescription>
                Changing it changes what the agent may post without an approval. Agree it with the
                control owner.
              </AlertDescription>
            </Alert>
          </div>
        )}
      </CardContent>
    </Card>
  )
}

/** The SOPs that cite a rule, by name, once each. */
function CitedBy({ pack, citedBy }: { pack: UseCasePack; citedBy?: string[] | null }) {
  const sops = citingSops(citedBy, pack.sops ?? [])
  if (sops == null) return <span className="text-muted-foreground">Not known yet</span>
  if (sops.length === 0) return <span className="text-muted-foreground">No SOP</span>
  return <TruncatedText text={sops.map(s => s.name).join(", ")} />
}

function OutcomesCard({ pack }: { pack: UseCasePack }) {
  const doc = usePackDocument(pack.id)
  const outcomes = doc.data?.pack.dispositions
  if (!Array.isArray(outcomes) || outcomes.length === 0) return null
  return (
    <Card>
      <CardHeader>
        <CardTitle>Outcomes</CardTitle>
        <CardDescription>A closed case carries one of these.</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-wrap gap-2">
        {outcomes.map(o => (
          <Badge key={String(o)} variant="secondary">
            {humanise(String(o))}
          </Badge>
        ))}
      </CardContent>
    </Card>
  )
}
