// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * What a trace tick on the spine says: a verb and the thing it acted on.
 *
 * Display-only, and deliberately a second source of labels rather than the one
 * `lib/caseTimeline.ts` uses. That one reads `Evidence.kind`, which exists so the UI
 * never pattern-matches tool names — but evidence is written server-side *after* the
 * turn, and `packages/run-client`'s `mapSegment` drops it on the way to the chat
 * stream. So a tick in the conversation has only the tool name and its arguments, live
 * or replayed, and this is the table that turns those into a sentence.
 *
 * The verbs and argument keys are copied from the backend's own mapping
 * (`agent/utils/evidence.py`) and from each Gateway target's tool schema,
 * so the two ends agree on what a call is called.
 *
 * Display-only table; if it drifts from the tool set, an endpoint that returns
 * labels is the upgrade, not a parser.
 */

import type { ToolCallStatus } from "@/components/chat/types"

/**
 * Gateway targets prefix their tools: `target___odata_read`. Stripped in three other
 * places already — `basic_agent.py`, `evidence.py:_tool_key`, and (via this) the chat
 * tree. Any of them missing the strip shows the operator a Gateway implementation
 * detail as if it were the name of an SAP call.
 */
export function bareToolName(name: string): string {
  return (name || "").split("___").pop() || ""
}

type Verb = { present: string; past: string; writes?: true }

/**
 * `writes` marks the four calls that change SAP. Nothing else in the app treats a
 * DynamoDB case update or a ticket as a write, and neither should the spine: the
 * distinction the operator is scanning for is "did this touch the ERP".
 */
const VERBS: Record<string, Verb> = {
  odata_read: { present: "reading", past: "read" },
  odata_count: { present: "counting", past: "counted" },
  odata_update: { present: "updating", past: "updated", writes: true },
  odata_create: { present: "creating", past: "created", writes: true },
  odata_delete: { present: "deleting", past: "deleted", writes: true },
  odata_function_import: { present: "calling", past: "called", writes: true },
  find_sap_services: { present: "finding the service for", past: "found the service for" },
  get_metadata: { present: "reading the metadata for", past: "read the metadata for" },
  get_service_hints: { present: "reading the hints for", past: "read the hints for" },
  search_sap_sops: { present: "checking the SOP for", past: "checked the SOP for" },
  load_sop: { present: "loading the SOP for", past: "loaded the SOP for" },
  skills: { present: "loading the SOP for", past: "loaded the SOP for" },
  search_sap_api_docs: { present: "searching the API docs for", past: "searched the API docs for" },
  get_case_state: { present: "reading case", past: "read case" },
  update_case_state: { present: "updating case", past: "updated case" },
  send_notification: { present: "notifying", past: "notified" },
  demo_create_ticket: { present: "raising a ticket on", past: "raised a ticket on" },
  demo_get_ticket: { present: "reading ticket", past: "read ticket" },
  get_precedent: { present: "looking up precedent for", past: "looked up precedent for" },
  check_vendor_risk: { present: "checking vendor risk on", past: "checked vendor risk on" },
  calculator: { present: "calculating", past: "calculated" },
  current_time: { present: "reading the clock", past: "read the clock" },
}

/**
 * One ordered list rather than an extractor per tool: the first key present wins, and
 * every tool in the inventory above has exactly one of these as its subject. Order
 * matters where a call carries two — `odata_function_import` has both `function_name`
 * and a `service_name`, and the function is what the operator recognises.
 */
const SUBJECT_KEYS = [
  "entity_set",
  "function_name",
  "process_type",
  "recipient",
  "case_id",
  "ticket_id",
  "supplier_number",
  "query",
  "service_name",
  "title",
] as const

const SUBJECT_RE = new RegExp(`"(${SUBJECT_KEYS.join("|")})"\\s*:\\s*"([^"]*)"`, "g")

/** `identifier_fields`/`parameters` are objects; their scalar values are the key. */
const KEY_OBJECT_RE = /"(?:identifier_fields|parameters)"\s*:\s*\{([^}]*)\}/
const SCALAR_RE = /:\s*(?:"([^"]*)"|(-?\d+(?:\.\d+)?))/g
/** An OData `$filter` string; the document number is whatever it quotes. */
const FILTER_RE = /"filter"\s*:\s*"((?:[^"\\]|\\.)*)"/
const QUOTED_RE = /'([^']*)'/g

/** Long enough for `A_PurchaseOrderItem 4500000123/10`, short enough to stay one line. */
const SUBJECT_MAX = 56

/**
 * Read the arguments with regexes rather than `JSON.parse`, because in both of the
 * cases that reach a tick the blob is not valid JSON: a live call's arguments stream in
 * token by token, and a stored `tool_input` is cut at `TOOL_INPUT_MAX_BYTES = 256`
 * (`evidence.py`) — routinely mid-string. One lenient path covers both; a parse would
 * throw on the common case.
 */
function firstOf(args: string, keys: readonly string[]): string | undefined {
  const found = new Map<string, string>()
  for (const [, key, value] of args.matchAll(SUBJECT_RE)) {
    if (value && !found.has(key)) found.set(key, value)
  }
  for (const key of keys) {
    const value = found.get(key)
    if (value) return value
  }
  return undefined
}

function keyFrom(args: string): string | undefined {
  const fields = KEY_OBJECT_RE.exec(args)
  if (fields) {
    const values = [...fields[1].matchAll(SCALAR_RE)]
      .map(m => m[1] ?? m[2])
      .filter((v): v is string => Boolean(v))
    if (values.length) return values.join("/")
  }
  const filter = FILTER_RE.exec(args)
  if (filter) {
    const quoted = [...filter[1].matchAll(QUOTED_RE)].map(m => m[1]).filter(Boolean)
    if (quoted.length) return quoted.join("/")
  }
  return undefined
}

export type ToolTick = {
  /** "read", "updating" — past tense only for a call that actually completed. */
  verb: string
  /** The object acted on, or undefined when the arguments named nothing. */
  subject?: string
  /** True for the four calls that change SAP, so the tick can carry more weight. */
  writes: boolean
}

/**
 * Past tense is reserved for `complete`. An `error` or `incomplete` call reads as the
 * attempt it was — "updating A_SupplierInvoice 5100001976" beside the mark that says how
 * it ended — because `incomplete` means the run stopped without a result and the write
 * may well have landed. "Updated" there would claim an outcome nobody confirmed.
 */
export function toolTick(name: string, args: string, status: ToolCallStatus): ToolTick {
  const bare = bareToolName(name)
  const verb = VERBS[bare]
  const blob = args || ""

  let subject = firstOf(blob, SUBJECT_KEYS)
  const key = keyFrom(blob)
  if (subject && key && !subject.includes(key)) subject = `${subject} ${key}`
  else if (!subject) subject = key
  if (subject && subject.length > SUBJECT_MAX) subject = `${subject.slice(0, SUBJECT_MAX - 1)}…`

  const done = status === "complete"
  // An unknown tool is named, not dressed up — the same fallthrough the backend takes
  // when a tool has no evidence kind.
  if (!verb)
    return {
      verb: done ? "ran" : "running",
      subject: subject ? `${bare} ${subject}` : bare,
      writes: false,
    }
  return { verb: done ? verb.past : verb.present, subject, writes: Boolean(verb.writes) }
}
