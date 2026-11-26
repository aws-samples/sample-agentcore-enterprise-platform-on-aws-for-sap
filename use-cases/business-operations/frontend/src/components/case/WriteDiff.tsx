// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { ArrowRight, CircleAlert, Play } from "lucide-react"
import type { DiffRow, FunctionImportCard } from "@/lib/writeDiff"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"

/**
 * A SAP write as a before and after, field by field: the record of a write that happened
 * (`Applied`) or the change a person is asked to approve (`Proposed`).
 *
 * Before is muted, after is ink; neither is a state colour. Only a mismatch takes one,
 * because only a mismatch is a fact the reviewer must act on: the SAP read in this run
 * contradicts the value the agent stated. Values are mono, since a reviewer compares them
 * against SAP character by character, and every value is set at the body size. A field reads
 * as its pack label when the caller passes `labelOf`, with the SAP name in a tooltip.
 */

const NOTE: Partial<Record<DiffRow["state"], string>> = {
  "no-baseline": "no prior value on record",
  unverified: "as the agent stated it; it did not read this field in SAP in this run",
}

/** An empty SAP value is a value; a missing one is not, and they must not look alike. */
function Value({ value, className }: { value: string; className: string }) {
  return value ? (
    <span className={cn("font-mono break-all", className)}>{value}</span>
  ) : (
    <span className="font-mono text-muted-foreground">(empty)</span>
  )
}

function FieldName({ name, labelOf }: { name: string; labelOf?: (name: string) => string }) {
  const label = labelOf?.(name)
  if (!label || label === name)
    return <dt className="font-mono break-all text-muted-foreground">{name}</dt>
  return (
    <dt className="text-muted-foreground">
      <Tooltip>
        <TooltipTrigger render={<span />} tabIndex={0}>
          {label}
        </TooltipTrigger>
        <TooltipContent>
          In SAP: <span className="font-mono">{name}</span>
        </TooltipContent>
      </Tooltip>
    </dt>
  )
}

function Row({
  row,
  quiet,
  labelOf,
}: {
  row: DiffRow
  quiet?: boolean
  labelOf?: (name: string) => string
}) {
  // `quiet` when the whole table already states the absence.
  const note = quiet ? undefined : NOTE[row.state]
  return (
    <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
      <FieldName name={row.name} labelOf={labelOf} />
      <dd className="flex min-w-0 flex-col gap-1">
        <span className="flex flex-wrap items-baseline gap-1.5">
          {row.before !== undefined && (
            <>
              <Value
                value={row.before}
                className={row.state === "mismatch" ? "text-danger-ink" : "text-muted-foreground"}
              />
              <ArrowRight className="size-3 flex-none text-muted-foreground" aria-label="to" />
            </>
          )}
          <Value value={row.after} className="font-medium text-foreground" />
          {row.state === "unchanged" && (
            <span className="text-xs text-muted-foreground">unchanged</span>
          )}
          {note && <span className="text-xs text-muted-foreground">{note}</span>}
        </span>
        {row.state === "mismatch" && (
          <Alert variant="destructive">
            <CircleAlert />
            <AlertDescription>
              SAP read in this run returned{" "}
              <span className="font-mono">{row.observed || "(empty)"}</span>, not the stated value
            </AlertDescription>
          </Alert>
        )}
      </dd>
    </div>
  )
}

/**
 * Stated once for the whole table when no row has a before value, rather than repeated
 * per row. Derived here, not passed in, so a mount point cannot forget to say it.
 */
function absenceNote(rows: DiffRow[], label: string): string | undefined {
  if (rows.length === 0 || rows.some(r => r.before !== undefined)) return undefined
  return label === "Proposed"
    ? "The agent stated no current values, so there is nothing to compare against."
    : "No read of this record in this run, so the previous values are not on record."
}

export function WriteDiff({
  rows,
  label,
  card,
  note,
  target,
  hideLabel,
  labelOf,
}: {
  rows: DiffRow[]
  label: "Applied" | "Proposed"
  card?: FunctionImportCard
  note?: string
  /** The change's verb and record ("Release", "Supplier invoice", "5100001976/2026"), as a header. */
  target?: { verb?: string; record?: string; key?: string }
  /** A field's label for a reader, such as the pack's; the SAP name goes in a tooltip. */
  labelOf?: (name: string) => string
  /** When the caller's own heading already says what the table is. */
  hideLabel?: boolean
}) {
  const absence = card ? undefined : absenceNote(rows, label)
  return (
    <div className="flex flex-col gap-1.5 text-sm">
      {!hideLabel && <p className="text-xs font-medium text-muted-foreground">{label}</p>}
      <div className="flex flex-col gap-2 rounded-lg border bg-card p-3">
        {target && (target.verb || target.record || target.key) && (
          <p className="flex flex-wrap items-baseline gap-2">
            {target.verb && <span className="font-medium">{target.verb}</span>}
            {target.record && <span className="text-muted-foreground">{target.record}</span>}
            {target.key && (
              <span className="font-mono text-xs text-muted-foreground">{target.key}</span>
            )}
          </p>
        )}
        {card ? (
          <>
            <p className="flex items-center gap-1.5">
              <Play className="size-3 flex-none text-muted-foreground" />
              <span className="font-mono text-foreground">{card.fn}</span>
              {card.target && (
                <span className="font-mono text-muted-foreground">{card.target}</span>
              )}
            </p>
            {card.params.length > 0 ? (
              <dl className="flex flex-col gap-1">
                {card.params.map((p, i) => (
                  <div key={`${p.name}-${i}`} className="flex flex-wrap gap-x-3">
                    <dt className="font-mono text-muted-foreground">{p.name}</dt>
                    <dd className="font-mono break-all text-foreground">{p.value || "—"}</dd>
                  </div>
                ))}
              </dl>
            ) : (
              <p className="text-xs text-muted-foreground">This action took no parameters.</p>
            )}
            {/* A lifecycle action changes no field, so there is nothing to diff. */}
            <p className="text-xs text-muted-foreground">
              A lifecycle action changes no field values, so there is nothing to compare.
            </p>
          </>
        ) : rows.length === 0 ? (
          <p className="text-muted-foreground">
            {label === "Proposed"
              ? "The agent recorded no fields for this proposal."
              : "This write recorded no field values."}
          </p>
        ) : (
          <dl className="flex flex-col gap-1.5">
            {rows.map((row, i) => (
              <Row key={`${row.name}-${i}`} row={row} quiet={Boolean(absence)} labelOf={labelOf} />
            ))}
          </dl>
        )}
      </div>
      {absence && <p className="text-xs text-muted-foreground">{absence}</p>}
      {note && <p className="text-xs text-muted-foreground">{note}</p>}
    </div>
  )
}
