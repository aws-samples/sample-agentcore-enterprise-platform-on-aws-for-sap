// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { Ident } from "@/components/Ident"
import { useState } from "react"
import { useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
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
import { ReasonButton } from "@/components/ReasonButton"
import { Fact, Facts, PartHeader, SetHereBadge } from "@/components/packs/PartHeader"
import { useApi } from "@/hooks/useApi"
import {
  PROMOTION_ONLY_REASON,
  usePackControls,
  usePackRules,
  usePromotionOnly,
} from "@/hooks/usePack"
import {
  CASE_WORKERS_GROUP,
  limitOwners,
  lowerLimits,
  type LimitValue,
} from "@/services/controlsService"
import { formatRule, groupLabel, guardLabel, ruleLabel } from "@/lib/packParts"
import type { UseCasePack } from "@/lib/registry"

/**
 * Controls: what the agent may change on its own, who works and approves the cases, and the
 * checks before every SAP write. The limits show the pack's ceiling beside this deployment's
 * value, which a limit owner may lower and never raise ("Set here"). The rest is pack
 * structure: readable by everyone, changed by a pack administrator in development.
 */
export function ControlsPart({ pack }: { pack: UseCasePack }) {
  const controls = usePackControls(pack.id)
  const rules = usePackRules(pack.id)
  const promotionOnly = usePromotionOnly()

  if (controls.isError)
    return (
      <Alert variant="destructive">
        <AlertTitle>Couldn&apos;t read the controls</AlertTitle>
        <AlertDescription>{(controls.error as Error).message}</AlertDescription>
      </Alert>
    )
  const c = controls.data
  const readers = (guard: string) =>
    Object.entries(rules.data?.rules ?? {})
      .filter(([, r]) => r.guarded_by.includes(guard))
      .map(([k, r]) => `${ruleLabel(k)} (${formatRule(k, r.value)})`)

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center gap-3">
        <PartHeader pack={pack} part="controls" />
        {!pack.access?.edit.controls && (
          <ReasonButton
            className="ml-auto"
            variant="outline"
            size="sm"
            reason={
              promotionOnly
                ? PROMOTION_ONLY_REASON
                : "Only a pack administrator changes controls. Ask your control owner."
            }
          >
            Edit controls
          </ReasonButton>
        )}
      </div>
      {!c ? (
        <Skeleton className="h-48 w-full" />
      ) : (
        <>
          <LimitsCard
            pack={pack}
            limits={c.limits}
            bounds={c.bounds}
            mayLower={pack.access?.lower_limits ?? c.may_lower}
            owners={limitOwners(c)}
          />
          <Card>
            <CardHeader>
              <CardTitle>People</CardTitle>
              <CardDescription>
                Groups from your identity provider. An approver never approves a ticket they raised.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <Facts>
                <Fact label="Work the cases">
                  {c.entitlement?.groups.map(groupLabel).join(", ") || "Nobody"}
                </Fact>
                <Fact label="Start the agent on a case">
                  {groupLabel(CASE_WORKERS_GROUP)}
                  {c.entitlement?.process_groups?.length
                    ? `, if also in ${c.entitlement.process_groups.map(groupLabel).join(" or ")}`
                    : ""}
                </Fact>
                <Fact label="Approve changes to SAP">
                  {c.entitlement?.approve_groups?.map(groupLabel).join(", ") || "Nobody"}
                </Fact>
              </Facts>
              <p className="mt-3 text-sm text-muted-foreground">
                Whoever starts the agent on a case lets it post changes under the auto-write limit
                without an approver.
              </p>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Checks before every write</CardTitle>
              <CardDescription>
                Each re-reads SAP before the agent&apos;s change goes through. A failed check
                refuses the write and sends the case to a person.
              </CardDescription>
            </CardHeader>
            <CardContent>
              {!c.write_guards?.length ? (
                <p className="text-sm text-muted-foreground">This pack turns on no write checks.</p>
              ) : (
                <ul className="flex flex-col gap-3 text-sm">
                  {(c.write_guards ?? []).map(g => {
                    const reads = readers(g)
                    return (
                      <li key={g} className="flex flex-col gap-0.5">
                        <span className="font-medium">{guardLabel(g)}</span>
                        <span className="text-muted-foreground">
                          {reads.length ? `Reads ${reads.join(", ")}` : "Reads SAP only"}
                        </span>
                      </li>
                    )
                  })}
                </ul>
              )}
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Verifier</CardTitle>
              <CardDescription>
                When on, a second model must agree with every payment-moving write after the checks
                pass.
              </CardDescription>
              <CardAction>
                <Badge variant="secondary">{c.verifier?.enabled ? "On" : "Off"}</Badge>
              </CardAction>
            </CardHeader>
          </Card>
        </>
      )}
    </div>
  )
}

function money(n: number): string {
  return n.toLocaleString("en-US")
}

function LimitsCard({
  pack,
  limits,
  bounds,
  mayLower,
  owners,
}: {
  pack: UseCasePack
  limits: Record<string, LimitValue>
  bounds: Record<string, [number, number]>
  /** `access.lower_limits`, the rule the route applies. */
  mayLower: boolean
  /** Who may, in words. */
  owners: string
}) {
  const getToken = useApi()
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState<Record<string, string> | null>(null)
  const [saving, setSaving] = useState(false)
  const may = mayLower
  const entries = Object.entries(limits)
  const changed = draft ? entries.filter(([k, l]) => draft[k] !== String(l.value)) : []
  const raised = changed.filter(([k, l]) => Number(draft![k]) > l.ceiling)

  const save = async () => {
    setSaving(true)
    try {
      const body = Object.fromEntries(
        changed.map(([k, l]) => [k, Number(draft![k]) === l.ceiling ? null : Number(draft![k])])
      )
      await lowerLimits(pack.id, body, await getToken())
      toast.success("Saved the limits", { description: "In this deployment only, at once." })
      setDraft(null)
      await queryClient.invalidateQueries({ queryKey: ["pack", pack.id] })
    } catch (e) {
      toast.error("Couldn't save the limits", { description: (e as Error).message })
    } finally {
      setSaving(false)
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Approval limits</CardTitle>
        <CardDescription>
          A limit owner may set either lower for this deployment, never higher. Raising one changes
          the pack.
        </CardDescription>
        <CardAction>
          {draft ? (
            <div className="flex gap-2">
              <Button variant="ghost" size="sm" onClick={() => setDraft(null)} disabled={saving}>
                Cancel
              </Button>
              <Button
                size="sm"
                onClick={() => void save()}
                disabled={!changed.length || raised.length > 0 || saving}
              >
                {saving ? "Saving…" : "Save"}
              </Button>
            </div>
          ) : (
            <ReasonButton
              variant="outline"
              size="sm"
              reason={may ? null : `Lowering a limit here takes ${owners}.`}
              onClick={() =>
                setDraft(Object.fromEntries(entries.map(([k, l]) => [k, String(l.value)])))
              }
            >
              Lower a limit
            </ReasonButton>
          )}
        </CardAction>
      </CardHeader>
      <CardContent className="flex flex-col gap-3 px-0">
        <Table className="table-fixed">
          <TableHeader>
            <TableRow>
              <TableHead className="pl-6">Limit</TableHead>
              <TableHead className="w-40 text-right">Pack</TableHead>
              <TableHead className="w-96 pr-6">
                <span className="inline-flex items-center gap-2">
                  This deployment <SetHereBadge />
                </span>
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {entries.map(([symbol, l]) => (
              <TableRow key={symbol} className="h-9">
                <TableCell className="truncate pl-6">
                  <span className="font-medium">{ruleLabel(symbol)}</span>
                  <Ident className="ml-2">{symbol}</Ident>
                </TableCell>
                <TableCell className="text-right tabular-nums">
                  {formatRule(symbol, l.ceiling)}
                </TableCell>
                <TableCell className="truncate pr-6 tabular-nums">
                  {draft ? (
                    <Input
                      aria-label={`${ruleLabel(symbol)} in this deployment`}
                      type="number"
                      min={bounds[symbol]?.[0] ?? 0}
                      max={l.ceiling}
                      className="h-8 w-40"
                      aria-invalid={Number(draft[symbol]) > l.ceiling || undefined}
                      value={draft[symbol] ?? ""}
                      onChange={e => setDraft({ ...draft, [symbol]: e.target.value })}
                    />
                  ) : l.value === l.ceiling ? (
                    <span className="text-muted-foreground">Same</span>
                  ) : (
                    <span>
                      {formatRule(symbol, l.value)}
                      <span className="ml-2 text-xs text-muted-foreground">
                        lowered from {money(l.ceiling)}
                      </span>
                    </span>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
        {!may && (
          <p className="px-6 text-sm text-muted-foreground">
            Read only. In this deployment, {owners} may lower a limit.
          </p>
        )}
        {raised.length > 0 && (
          <p className="px-6 text-sm text-destructive">
            A deployment may only lower a limit: {raised.map(([k]) => ruleLabel(k)).join(", ")} is
            above the pack&apos;s.
          </p>
        )}
      </CardContent>
    </Card>
  )
}
