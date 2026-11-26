// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useMemo, useState } from "react"
import { Trash2 } from "lucide-react"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Checkbox } from "@/components/ui/checkbox"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select"
import {
  Field,
  FieldDescription,
  FieldGroup,
  FieldLabel,
  FieldLegend,
  FieldSet,
} from "@/components/ui/field"
import type { PackAttributeSpec, PackSpec } from "@/services/packsService"

/**
 * New pack: mint a use case pack from the browser, on stock components.
 *
 * **What this form is, and what it deliberately is not.** It collects a *description of
 * the use case* — what the exception is, what each case carries, what the thresholds are
 * — and the server composes every privileged value from it: which Gateway tools the pack
 * holds (the read set, and only the read set), which model runs it, which SAP service it
 * reads. There is no field here that widens what the agent can reach, which is what makes
 * a create as small an act as a content patch.
 *
 * **The pack is born incomplete.** Every SAP value is written as an `<AWAITING_SURVEY>`
 * marker, so the pack is inert: it is not polled and no `process_type` routes to it. That
 * is the whole point — the analyst's decisions become a real pack immediately, and binding
 * it to SAP is the survey step that comes next, from a checkout.
 *
 * **Two things do not come from here.** SOPs are uploaded separately (one per process
 * type, on this same page) — a pack maps to SOPs nobody has written yet until they are.
 * And `fields`/`field_map` are privileged, so an attribute left out now is a checkout
 * change later rather than another visit to this dialog. The copy says both.
 *
 * Validation mirrors `pack_compose.check_spec` rather than guessing stricter: a value this
 * accepts and the API refuses comes back as the same sentence, after the analyst filled in
 * the whole form.
 *
 * **One create path.** A scaffold drafted elsewhere — a use-case scoping server interviews the
 * analyst and emits one — arrives through the same route this form
 * posts to (`POST /packs`), and Create is still a human pressing Create after reading every value. A pack
 * is the instructions a later agent run reads, so drafting one and publishing one are
 * different acts.
 */

/** `packs/pack.schema.json`'s own patterns, which the API checks again. */
const PACK_ID = /^[a-z][a-z0-9_]*$/
const SHORT_ID = /^[a-z][a-z0-9]{1,7}$/
const NAME = /^[a-z][a-z0-9_]*$/
const MAX_TEXT = 2000

const KINDS: { value: PackSpec["kind"]; label: string; hint: string }[] = [
  { value: "filter", label: "Filter", hint: "A record whose own values make it an exception." },
  { value: "absence", label: "Absence", hint: "A record missing a document that should exist." },
  { value: "aging", label: "Aging", hint: "A record older than a window and still open." },
]

const TYPES: PackAttributeSpec["type"][] = ["string", "date", "amount", "decimal"]

/** A row while it is being typed: every field is a string, and blanks are normal. */
interface AttrRow {
  name: string
  label: string
  type: PackAttributeSpec["type"]
  currency_field: string
  panel: boolean
}

interface ConstantRow {
  symbol: string
  value: string
}

const BLANK_ATTR: AttrRow = { name: "", label: "", type: "string", currency_field: "", panel: true }

export interface NewPackDialogProps {
  open: boolean
  /** Every published pack id, so a collision is refused before the round trip. */
  taken: string[]
  onClose: () => void
  /** Throws on failure: the dialog stays open and shows the reason. */
  onCreate: (packId: string, spec: PackSpec) => Promise<unknown>
}

export function NewPackDialog({ open, taken, onClose, onCreate }: NewPackDialogProps) {
  const [packId, setPackId] = useState("")
  const [displayName, setDisplayName] = useState("")
  const [description, setDescription] = useState("")
  const [shortId, setShortId] = useState("")
  const [kind, setKind] = useState<PackSpec["kind"]>("filter")
  const [definition, setDefinition] = useState("")
  const [processTypes, setProcessTypes] = useState("")
  const [olderThanDays, setOlderThanDays] = useState("30")
  const [attrs, setAttrs] = useState<AttrRow[]>([{ ...BLANK_ATTR }])
  const [constants, setConstants] = useState<ConstantRow[]>([])
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const names = useMemo(
    () =>
      processTypes
        .split(/[\s,]+/)
        .map(s => s.trim())
        .filter(Boolean),
    [processTypes]
  )

  const problems = useMemo(() => {
    const list: string[] = []
    if (!PACK_ID.test(packId))
      list.push("Pack id is lowercase letters, digits and underscores, starting with a letter.")
    else if (taken.includes(packId)) list.push(`${packId} is already published here.`)
    if (!displayName.trim()) list.push("A display name is required.")
    if (!description.trim()) list.push("A description is required.")
    if (!SHORT_ID.test(shortId))
      list.push("Case id prefix is 2–8 lowercase letters or digits, starting with a letter.")
    if (!definition.trim()) list.push("Say what makes a record an exception.")
    if (names.length === 0) list.push("At least one process type is required.")
    if (names.some(n => !NAME.test(n)))
      list.push("A process type is lowercase letters, digits and underscores.")
    if (new Set(names).size !== names.length) list.push("Process types must be distinct.")
    if (kind === "aging" && !(Number(olderThanDays) >= 1))
      list.push("An aging pack needs a window of at least one day.")
    for (const [text, what] of [
      [displayName, "display name"],
      [description, "description"],
      [definition, "definition"],
    ] as const) {
      if (text.length > MAX_TEXT) list.push(`The ${what} must be ${MAX_TEXT} characters or fewer.`)
    }

    const declared = attrs.filter(a => a.name.trim() || a.label.trim())
    const attrNames = declared.map(a => a.name.trim())
    if (attrNames.some(n => !NAME.test(n)))
      list.push("An attribute name is lowercase letters, digits and underscores.")
    if (new Set(attrNames).size !== attrNames.length) list.push("Attribute names must be distinct.")
    if (declared.some(a => !a.label.trim())) list.push("Every attribute needs a label.")
    for (const a of declared) {
      if (a.type !== "amount") continue
      const currency = a.currency_field.trim()
      if (!currency || !attrNames.includes(currency))
        list.push(`${a.name || "An amount"} needs a currency field naming another attribute.`)
    }

    for (const c of constants) {
      if (!c.symbol.trim() && !c.value.trim()) continue
      if (!/^[A-Z][A-Z0-9_]*$/.test(c.symbol.trim()))
        list.push("A rule name is upper case letters, digits and underscores.")
      if (!Number.isFinite(Number(c.value))) list.push(`${c.symbol || "A rule"} is not a number.`)
    }
    return list
  }, [
    packId,
    taken,
    displayName,
    description,
    shortId,
    definition,
    names,
    kind,
    olderThanDays,
    attrs,
    constants,
  ])

  function buildSpec(): PackSpec {
    const declared = attrs.filter(a => a.name.trim())
    const spec: PackSpec = {
      display_name: displayName.trim(),
      description: description.trim(),
      short_id: shortId.trim(),
      kind,
      definition: definition.trim(),
      process_types: names,
    }
    if (declared.length > 0) {
      spec.attributes = declared.map(a => ({
        name: a.name.trim(),
        label: a.label.trim(),
        type: a.type,
        ...(a.type === "amount" ? { currency_field: a.currency_field.trim() } : {}),
        // Sent only when false: the schema's default is true, and a pack document
        // carrying `panel: true` on every field says nothing the default did not.
        ...(a.panel ? {} : { panel: false }),
      }))
    }
    const numbers = Object.fromEntries(
      constants.filter(c => c.symbol.trim()).map(c => [c.symbol.trim(), Number(c.value)])
    )
    if (Object.keys(numbers).length > 0) spec.constants = numbers
    if (kind === "aging") spec.older_than_days = Number(olderThanDays)
    return spec
  }

  async function handleCreate() {
    setSaving(true)
    setError(null)
    try {
      await onCreate(packId.trim(), buildSpec())
      onClose()
    } catch (e) {
      setError(e instanceof Error ? e.message : "Create failed")
    } finally {
      setSaving(false)
    }
  }

  const setAttr = (i: number, patch: Partial<AttrRow>) =>
    setAttrs(prev => prev.map((row, j) => (i === j ? { ...row, ...patch } : row)))

  return (
    <Dialog
      open={open}
      onOpenChange={next => {
        if (!next && !saving) onClose()
      }}
    >
      <DialogContent className="flex max-h-svh flex-col sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>New pack</DialogTitle>
          <DialogDescription>
            The pack is created waiting for a SAP survey: its rules are live and its SOPs and
            instructions can be edited, but nothing polls it and no process type routes to it until
            someone completes it with a SAP service. Write one SOP per process type before you rehearse
            it.
          </DialogDescription>
        </DialogHeader>

        <div className="flex min-h-0 grow flex-col gap-6 overflow-y-auto pr-1">
          <FieldSet>
            <FieldLegend>Identity</FieldLegend>
            <FieldGroup>
              <Field>
                <FieldLabel htmlFor="pack-id">Pack id</FieldLabel>
                <Input
                  id="pack-id"
                  className="font-mono"
                  placeholder="field_service_parts"
                  value={packId}
                  onChange={e => setPackId(e.target.value)}
                />
                <FieldDescription>
                  The folder name under packs/. It cannot change later.
                </FieldDescription>
              </Field>
              <Field>
                <FieldLabel htmlFor="pack-short-id">Case id prefix</FieldLabel>
                <Input
                  id="pack-short-id"
                  className="w-32 font-mono"
                  placeholder="fsp"
                  value={shortId}
                  onChange={e => setShortId(e.target.value)}
                />
                <FieldDescription>
                  Every case this pack opens starts with it. No two packs share one.
                </FieldDescription>
              </Field>
              <Field>
                <FieldLabel htmlFor="pack-name">Display name</FieldLabel>
                <Input
                  id="pack-name"
                  placeholder="Field Service Parts"
                  value={displayName}
                  onChange={e => setDisplayName(e.target.value)}
                />
              </Field>
              <Field>
                <FieldLabel htmlFor="pack-description">Description</FieldLabel>
                <Input
                  id="pack-description"
                  placeholder="Parts shortages that block a scheduled service call."
                  value={description}
                  onChange={e => setDescription(e.target.value)}
                />
                <FieldDescription>
                  One sentence, shown wherever the pack is listed.
                </FieldDescription>
              </Field>
            </FieldGroup>
          </FieldSet>

          <FieldSet>
            <FieldLegend>What makes a case</FieldLegend>
            <FieldGroup>
              <Field>
                <FieldLabel htmlFor="pack-kind">Kind</FieldLabel>
                <NativeSelect
                  id="pack-kind"
                  value={kind}
                  onChange={e => setKind(e.target.value as PackSpec["kind"])}
                >
                  {KINDS.map(k => (
                    <NativeSelectOption key={k.value} value={k.value}>
                      {k.label}
                    </NativeSelectOption>
                  ))}
                </NativeSelect>
                <FieldDescription>{KINDS.find(k => k.value === kind)?.hint}</FieldDescription>
              </Field>
              {kind === "aging" && (
                <Field>
                  <FieldLabel htmlFor="pack-older-than">Older than (days)</FieldLabel>
                  <Input
                    id="pack-older-than"
                    type="number"
                    min={1}
                    className="w-28"
                    value={olderThanDays}
                    onChange={e => setOlderThanDays(e.target.value)}
                  />
                </Field>
              )}
              <Field>
                <FieldLabel htmlFor="pack-definition">Definition</FieldLabel>
                <Textarea
                  id="pack-definition"
                  rows={3}
                  placeholder="A confirmed service order whose parts are not in stock."
                  value={definition}
                  onChange={e => setDefinition(e.target.value)}
                />
                <FieldDescription>
                  In your own words. It frames the pack&apos;s instructions and becomes the filter
                  the survey resolves.
                </FieldDescription>
              </Field>
              <Field>
                <FieldLabel htmlFor="pack-process-types">Process types</FieldLabel>
                <Input
                  id="pack-process-types"
                  className="font-mono"
                  placeholder="parts_shortage, backorder_escalation"
                  value={processTypes}
                  onChange={e => setProcessTypes(e.target.value)}
                />
                <FieldDescription>
                  One per situation the agent handles, separated by commas. Each needs a SOP.
                </FieldDescription>
              </Field>
            </FieldGroup>
          </FieldSet>

          <FieldSet>
            <FieldLegend>Attributes</FieldLegend>
            <FieldDescription>
              What each case carries. Worth filling in now: the SAP path behind a field is
              structure, so an attribute added later is a change a pack administrator makes. A name
              that collides with a case column (status, pack_id, case_id) is refused.
            </FieldDescription>
            {attrs.map((row, i) => (
              <div key={i} className="flex items-center gap-2">
                <Input
                  aria-label={`Attribute ${i + 1} name`}
                  className="flex-1 font-mono"
                  placeholder="order_number"
                  value={row.name}
                  onChange={e => setAttr(i, { name: e.target.value })}
                />
                <Input
                  aria-label={`Attribute ${i + 1} label`}
                  className="flex-1"
                  placeholder="Service order"
                  value={row.label}
                  onChange={e => setAttr(i, { label: e.target.value })}
                />
                <NativeSelect
                  aria-label={`Attribute ${i + 1} type`}
                  value={row.type}
                  onChange={e => setAttr(i, { type: e.target.value as PackAttributeSpec["type"] })}
                >
                  {TYPES.map(t => (
                    <NativeSelectOption key={t} value={t}>
                      {t}
                    </NativeSelectOption>
                  ))}
                </NativeSelect>
                {/* Only an amount reads it, and the API refuses it anywhere else. */}
                {row.type === "amount" ? (
                  <Input
                    aria-label={`Attribute ${i + 1} currency field`}
                    className="w-32 font-mono"
                    placeholder="currency"
                    value={row.currency_field}
                    onChange={e => setAttr(i, { currency_field: e.target.value })}
                  />
                ) : (
                  <Field orientation="horizontal" className="w-32">
                    <Checkbox
                      id={`attr-panel-${i}`}
                      checked={row.panel}
                      onCheckedChange={on => setAttr(i, { panel: on === true })}
                    />
                    <FieldLabel htmlFor={`attr-panel-${i}`} className="font-normal">
                      On the case
                    </FieldLabel>
                  </Field>
                )}
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label={`Remove attribute ${i + 1}`}
                  onClick={() => setAttrs(prev => prev.filter((_, j) => j !== i))}
                >
                  <Trash2 />
                </Button>
              </div>
            ))}
            <div>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setAttrs(prev => [...prev, { ...BLANK_ATTR }])}
              >
                Add attribute
              </Button>
            </div>
          </FieldSet>

          <FieldSet>
            <FieldLegend>Rules</FieldLegend>
            <FieldDescription>
              The numbers this pack&apos;s SOPs compare against. They belong to the pack: an author
              changes one as a pack edit, and the pack carries it to every stack.
            </FieldDescription>
            {constants.map((row, i) => (
              <div key={i} className="flex items-center gap-2">
                <Input
                  aria-label={`Rule ${i + 1} name`}
                  className="max-w-xs font-mono"
                  placeholder="MIN_STOCK_PCT"
                  value={row.symbol}
                  onChange={e =>
                    setConstants(prev =>
                      prev.map((c, j) => (i === j ? { ...c, symbol: e.target.value } : c))
                    )
                  }
                />
                <Input
                  aria-label={`Rule ${i + 1} value`}
                  type="number"
                  step="any"
                  className="w-32"
                  value={row.value}
                  onChange={e =>
                    setConstants(prev =>
                      prev.map((c, j) => (i === j ? { ...c, value: e.target.value } : c))
                    )
                  }
                />
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label={`Remove rule ${i + 1}`}
                  onClick={() => setConstants(prev => prev.filter((_, j) => j !== i))}
                >
                  <Trash2 />
                </Button>
              </div>
            ))}
            <div>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setConstants(prev => [...prev, { symbol: "", value: "" }])}
              >
                Add rule
              </Button>
            </div>
          </FieldSet>
        </div>

        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        {/* One problem at a time: a long form listing eight refusals at once is a wall
            nobody reads, and fixing the first often clears several. */}
        {!error && problems.length > 0 && (
          <p className="text-xs text-muted-foreground">
            {problems[0]}
            {problems.length > 1 && ` (${problems.length - 1} more)`}
          </p>
        )}

        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={saving}>
            Cancel
          </Button>
          <Button onClick={handleCreate} disabled={saving || problems.length > 0}>
            {saving ? "Creating…" : "Create pack"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
