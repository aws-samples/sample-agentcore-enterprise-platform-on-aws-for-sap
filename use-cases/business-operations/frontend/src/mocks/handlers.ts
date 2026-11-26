// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The console API, served from memory, on the resource tree the API is moving to: a pack's
 * parts under `/packs/{pack_id}/…`, what applies to every pack under `/deployment/…`. Every
 * path a service calls has a handler; `test/drift/mock-handlers.test.ts` keeps the two in
 * step. What a role may do follows `GET /registry`'s `access`, decided here as the API's own
 * gates decide it in a development stack.
 *
 * State lives in this module, so a decision or a save sticks until the page reloads.
 */
import { http, HttpResponse, type HttpHandler, type WebSocketHandler } from "msw"
import { CaseStatus, type WorkItem } from "@/types/cases"
import { TicketStatus } from "@/types/generated-tickets"
import { Source } from "@/types/generated-cases"
import {
  CASES,
  CONTACTS,
  ENTITLEMENT_GROUPS,
  EVALS,
  PACK,
  PACK_DOC,
  PACK_DOCUMENTS,
  SOPS,
  TICKETS,
} from "./fixtures"
import {
  ADMINS_GROUP,
  CASE_WORKERS_GROUP,
  LIMIT_OWNERS_GROUP,
  currentRole,
  seesPack,
  type MockRole,
} from "./roles"
import {
  EVENTS_HTTP_DOMAIN,
  EVENTS_REALTIME_DOMAIN,
  replay,
  runChannel,
  type Replay,
} from "./runChannel"
import { answer, processRun, rehearsal, resume, type RecordedRun } from "./runs"

export const API = "/__mock_api"

export const EXPORTS = {
  authority: "https://auth.mock.invalid",
  client_id: "mock-client",
  redirect_uri: "http://localhost:3000/",
  post_logout_redirect_uri: "http://localhost:3000/",
  response_type: "code",
  scope: "openid email profile",
  automaticSilentRenew: false,
  awsRegion: "us-east-1",
  apiUrl: API,
  runEventsHttpDomain: EVENTS_HTTP_DOMAIN,
  runEventsRealtimeDomain: EVENTS_REALTIME_DOMAIN,
  ticketingEnabled: true,
  adminsGroup: ADMINS_GROUP,
  features: { test_data: false },
  brand: { name: "Business Operations", subtitle: "SAP exception handling" },
}

/** The two constants a stack may lower and never raise (`pack_tiers.LIMIT_SYMBOLS`). */
const LIMITS = new Set(["AUTO_WRITE_LIMIT_USD", "DUAL_APPROVAL_LIMIT_USD"])

/** The rules each write check reads (`agent/utils/guard_rules.py`). */
const GUARDS: Record<string, string[]> = {
  po_reference: [],
  no_duplicate: ["DUPLICATE_MATCH_WINDOW_USD"],
  gr_coverage: [],
  within_tolerance_or_approved: [
    "PRICE_VARIANCE_PCT",
    "PRICE_VARIANCE_ABS_USD",
    "QTY_VARIANCE_PCT",
    "QTY_VARIANCE_UNITS",
    "LINE_AMOUNT_TOLERANCE_USD",
  ],
}

/** Each top-level key's part, as `pack_tiers.PARTS` maps it (docs/packs.md, Pack parts). */
const PART_OF: Record<string, string> = {
  pack_id: "procedures",
  sap_system: "intake",
  write_limit_waiver: "controls",
  run_budget: "agent_configuration",
  model_effort: "agent_configuration",
  context_editing: "agent_configuration",
  source: "intake",
  fields: "intake",
  case_document_check: "intake",
  sap_service: "intake",
  display_name: "procedures",
  description: "procedures",
  process_type_to_sop: "procedures",
  shared_sops: "procedures",
  constants: "procedures",
  dispositions: "procedures",
  entitlement: "controls",
  write_guards: "controls",
  write_amount_field: "controls",
  verifier: "controls",
  model_tier: "agent_configuration",
  max_turns: "agent_configuration",
  gateway_tools: "agent_configuration",
  seed: "evaluations",
}

/** Content a pack author may change; every other key is structure (`pack_tiers`). */
const CONTENT = new Set(["display_name", "description", "constants"])

/**
 * The stack the mock bar picked: development, where packs are edited, or quality, which
 * takes packs by promotion only (`deployment.promotion_only`) and refuses every pack and SOP
 * write over HTTP, as `pack_tiers.edit_refusal` does.
 */
export const STACK_KEY = "mock.stack"
const promotionOnly = () =>
  typeof localStorage !== "undefined" && localStorage.getItem(STACK_KEY) === "quality"
const PROMOTION_REFUSAL =
  "This stack takes packs by promotion only: a pack arrives here by an import with a change reference"
/** A pack or SOP write in a promotion-only stack: refused before anything else. */
const promotionRefusal = () => (promotionOnly() ? forbidden(PROMOTION_REFUSAL) : null)

/** The scenario the mock bar picked: the demo cases, or a new deployment with none. */
export const SCENARIO_KEY = "mock.scenario"
const emptyStack = () =>
  typeof localStorage !== "undefined" && localStorage.getItem(SCENARIO_KEY) === "empty"
const cases = new Map<string, WorkItem>(
  emptyStack() ? [] : CASES.map(c => [c.case_id, structuredClone(c)])
)
const tickets = new Map(TICKETS.map(t => [t.ticket_id, structuredClone(t)]))

/** Runs in flight, by slot: a case id, or a case-less run's session id. */
const running = new Map<string, { runId: string; replay: Replay; before?: Partial<WorkItem> }>()

/**
 * Replay a recorded run on `slot`. On a case, the run claims it as a real one does: the case
 * is `processing` with a `live_run` that grows a segment at a time, and at the end the trace
 * is written and the case takes the run's outcome.
 */
function startRun(slot: string, run: RecordedRun, trigger: string, c?: WorkItem): string {
  const runId = `run-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`
  const startedAt = now()
  const claims = !!c && !!run.after
  const before = c ? { status: c.status, live_run: c.live_run } : undefined
  // Moving a case to processing removes its options, so a run never leaves an earlier set.
  if (c && claims) delete (c as { decision_options?: unknown }).decision_options
  if (c && claims)
    Object.assign(c, {
      status: CaseStatus.Processing,
      updated_at: startedAt,
      live_run: { run_id: runId, segments: [], started_at: startedAt, updated_at: startedAt },
    })
  const done: typeof run.segments = []
  const r = replay(
    slot,
    runId,
    run.segments,
    segment => {
      done.push(segment)
      if (c?.live_run?.run_id === runId)
        c.live_run = { ...c.live_run, segments: [...done], updated_at: now() }
    },
    () => {
      running.delete(slot)
      if (!c) return
      const cost = 0.08 + done.length / 100
      c.agent_traces = [
        ...(c.agent_traces ?? []),
        {
          trace_id: `trace-${runId}`,
          timestamp: now(),
          trigger,
          outcome: run.outcome,
          ...(run.sopKey ? { sop_key: run.sopKey } : {}),
          estimated_cost_usd: Number(cost.toFixed(2)),
          segments: run.segments,
        } as never,
      ]
      c.cost_summary = {
        ...(c.cost_summary ?? {}),
        total_cost_usd: Number(((c.cost_summary?.total_cost_usd ?? 0) + cost).toFixed(2)),
        invocation_count: (c.cost_summary?.invocation_count ?? 0) + 1,
      }
      delete c.live_run
      if (run.after) Object.assign(c, run.after)
      c.updated_at = now()
    }
  )
  running.set(slot, { runId, replay: r, before })
  return runId
}

/** Stop the run on a slot: its frames end, and a case goes back to where it was. */
function stopRun(slot: string): boolean {
  const run = running.get(slot)
  if (!run) return false
  run.replay.cancel()
  running.delete(slot)
  const c = cases.get(slot)
  if (c && run.before) {
    delete c.live_run
    Object.assign(c, { status: run.before.status, updated_at: now() })
  }
  return true
}

/** The auto-write limit this stack applies, which picks the run Process replays. */
const autoLimit = () => limitOverrides.AUTO_WRITE_LIMIT_USD ?? doc.constants.AUTO_WRITE_LIMIT_USD

/** Put the cases and tickets back as the fixtures have them, for a test that decides one. */
export function resetMockCases(): void {
  for (const slot of [...running.keys()]) stopRun(slot)
  lastStop = null
  cases.clear()
  for (const c of CASES) cases.set(c.case_id, structuredClone(c))
  tickets.clear()
  for (const t of TICKETS) tickets.set(t.ticket_id, structuredClone(t))
}
const sops = new Map(Object.entries(SOPS))
const doc = structuredClone(PACK_DOC) as Record<string, unknown> & {
  constants: Record<string, number>
  process_type_to_sop: Record<string, string>
  entitlement: {
    groups: string[]
    approve_groups: string[]
    limit_groups?: string[]
    process_groups?: string[]
  }
  write_guards: string[]
  verifier?: { enabled?: boolean }
  write_amount_field?: string
  write_limit_waiver?: unknown
}
const evals = structuredClone(EVALS) as Record<string, unknown>[]
let basePrompt = PACK_DOCUMENTS.base_prompt
let triggerMode: "auto" | "manual" = "manual"
let triggerActor = "set by sam@example.com (mock-operator) at 2026-11-19T09:00:00+00:00"
/** `POST /deployment/stop`'s record, which `GET /deployment/settings` carries. */
let lastStop: {
  stopped_at: string
  stopped_by: string
  reason: string
  trigger_mode_set: boolean
  runs_total: number
  runs: {
    run_id: string
    case_id: string | null
    session_stopped: boolean
    released: boolean
    parked: boolean
  }[]
  complete: boolean
  stands: boolean
  queue_held: boolean
  cleared_at?: string
  cleared_by?: string
  clear_reason?: string
} | null = null
/**
 * The `409` every start and decision gives while a stop stands (7.2), questions included: the
 * stop's reason, who and when in the sentence, and the stop beside it.
 */
function stopBody() {
  const s = lastStop!
  return {
    error: `An emergency stop stands: ${s.reason} (${s.stopped_by}, ${s.stopped_at}). Nothing starts until an admin clears it.`,
    emergency_stop: {
      stands: true,
      stopped_at: s.stopped_at,
      stopped_by: s.stopped_by,
      reason: s.reason,
    },
  }
}
const stopRefused = () => (lastStop?.stands ? HttpResponse.json(stopBody(), { status: 409 }) : null)
const packModes: Record<string, "auto" | "manual" | null> = { [PACK.id]: null }
const contactOverrides: Record<string, string> = {}
// Lowered so the demo cases cross both: one approval over 50,000 USD, two over 150,000.
const limitOverrides: Record<string, number> = {
  AUTO_WRITE_LIMIT_USD: 50000,
  DUAL_APPROVAL_LIMIT_USD: 150000,
}
let packVersion = 14

/** The versions this stack stores, newest first (`_bundles/<pack_id>/<v>.json`), as the
 *  versions routes list them. Every save below adds one. */
interface MockVersion {
  version: number
  made_by: string
  made_at: string
  via: string
  parts: string[]
  keys: string[]
  files: string[]
  origin: { via: string; by: string | null; at: string | null; change_ref: string | null }
}
const versions: MockVersion[] = [
  {
    version: 14,
    made_by: "ana@example.com",
    made_at: "2026-11-19T10:02:11+00:00",
    via: "edit",
    parts: ["procedures"],
    keys: [],
    files: ["sops/price_variance.txt"],
    origin: {
      via: "edit",
      by: "ana@example.com",
      at: "2026-11-19T10:02:11+00:00",
      change_ref: null,
    },
  },
  {
    version: 13,
    made_by: "ana@example.com",
    made_at: "2026-11-18T16:40:00+00:00",
    via: "edit",
    parts: ["procedures"],
    keys: ["constants"],
    files: ["pack.json"],
    origin: {
      via: "edit",
      by: "ana@example.com",
      at: "2026-11-18T16:40:00+00:00",
      change_ref: null,
    },
  },
  ...Array.from({ length: 12 }, (_, i) => {
    const v = 12 - i
    const at = new Date(Date.UTC(2026, 10, 5 + Math.floor(v / 2), 9 + (v % 8))).toISOString()
    return {
      version: v,
      made_by: v === 0 ? "deploy" : "sam@example.com",
      made_at: at,
      via: v === 0 ? "seed" : "edit",
      parts:
        v === 0 ? [] : v % 3 === 0 ? ["controls"] : v % 3 === 1 ? ["procedures"] : ["evaluations"],
      keys: v % 3 === 0 ? ["constants"] : [],
      files:
        v === 0
          ? []
          : v % 3 === 1
            ? ["sops/invoice_matching.txt"]
            : v % 3 === 2
              ? ["evals.json"]
              : ["pack.json"],
      origin: {
        via: v === 0 ? "seed" : "edit",
        by: v === 0 ? null : "sam@example.com",
        at,
        change_ref: null,
      },
    }
  }),
]
/** One stored version's content, as `GET /packs/{pack_id}/versions/{version}` serves it. */
interface Snapshot {
  pack: Record<string, unknown>
  base_prompt: string
  sops: Record<string, string>
  evals: Record<string, unknown>[]
}
const live = (): Snapshot =>
  structuredClone({
    pack: doc,
    base_prompt: basePrompt,
    sops: Object.fromEntries([...sops].map(([k, text]) => [k.split("/").pop()!, text])),
    evals,
  })
/** What each save stored, from the page's load on; older versions are made up below. */
const snapshots = new Map<number, Snapshot>()

/** The last line that says something, dropped: what an older SOP or prompt looked like. */
const withoutLastLine = (text: string) => {
  const lines = text.trimEnd().split("\n")
  return lines.slice(0, -1).join("\n") + "\n"
}

/**
 * A version's content. A save made since the page loaded stored its own; for the fixture's
 * history, each version is the one after it with that one's change undone, so a diff between
 * neighbours shows the change its entry lists.
 */
function snapshotOf(n: number): Snapshot | undefined {
  const stored = snapshots.get(n)
  if (stored) return stored
  const after = versions.find(v => v.version === n + 1)
  const base = after && snapshotOf(n + 1)
  if (!after || !base) return undefined
  const older = structuredClone(base)
  for (const file of after.files) {
    const name = file.replace(/^sops\//, "")
    if (file.startsWith("sops/") && older.sops[name] != null)
      older.sops[name] = withoutLastLine(older.sops[name])
    if (file === "base_prompt.txt") older.base_prompt = withoutLastLine(older.base_prompt)
    if (file === "evals.json") older.evals = older.evals.slice(0, -1)
  }
  if (after.keys.includes("constants")) {
    const constants = older.pack.constants as Record<string, number>
    const symbol = after.parts.includes("controls") ? "AUTO_WRITE_LIMIT_USD" : "PRICE_VARIANCE_PCT"
    if (typeof constants[symbol] === "number")
      constants[symbol] = symbol.endsWith("_USD")
        ? constants[symbol] + 25000
        : constants[symbol] + 1
  }
  snapshots.set(n, older)
  return older
}

/** Record the version a save just made. */
function recordVersion(parts: string[], files: string[], keys: string[] = []) {
  // Called after the save bumped `packVersion`: this is the new version's content.
  snapshots.set(packVersion, live())
  const role = currentRole()
  const at = now()
  versions.unshift({
    version: packVersion,
    made_by: role.email,
    made_at: at,
    via: "edit",
    parts,
    keys,
    files,
    origin: { via: "edit", by: role.email, at, change_ref: null },
  })
}
function versionEntry(v: MockVersion) {
  return {
    version: v.version,
    sha256: `${(v.version * 2654435761).toString(16).padStart(8, "0")}${"0".repeat(56)}`,
    made_by: v.made_by,
    made_at: v.made_at,
    made_in: "bo-dev-business-operations",
    via: v.via,
    change_note: null,
    parts: v.parts,
    changes: {
      keys: { added: [], removed: [], changed: v.keys },
      files: { added: [], removed: [], changed: v.files },
    },
    origin: v.origin,
  }
}
const savedAt: Record<string, { at: string; by: string }> = {}
snapshots.set(packVersion, live())

const visible = () => (seesPack(currentRole(), ENTITLEMENT_GROUPS) ? [...cases.values()] : [])
const now = () => new Date().toISOString()
const isAdmin = (role: MockRole = currentRole()) => role.groups.includes(ADMINS_GROUP)
const refuse = (status: number, error: string) => HttpResponse.json({ error }, { status })
const forbidden = (why = "Forbidden") => refuse(403, why)
const NOT_A_CASE_WORKER = "Only this pack's case workers may start the agent on its cases"
/** The API's order: an unknown pack is a 404, then one the caller may not see a 403. */
function packRefusal(id: unknown) {
  if (id !== PACK.id) return refuse(404, `No published pack ${String(id)}`)
  if (!seesPack(currentRole(), ENTITLEMENT_GROUPS)) return forbidden("Not entitled to this pack")
  return null
}
const ACTOR = "set by sam@example.com (mock-operator) at 2026-11-19T09:00:00+00:00"

/** `GET /registry` `access`, as the API's gates decide it in a development stack. No mock
 *  role holds `business-operations-pack-admins`, so every structure key is false. */
function access(role: MockRole) {
  const author = role.canAuthor && !promotionOnly()
  const approver = role.groups.some(g => doc.entitlement.approve_groups.includes(g))
  return {
    see: true,
    edit: {
      intake: false,
      procedures_text: author,
      rule_values: author,
      procedures_structure: false,
      controls: false,
      agent_configuration: false,
      evaluations: false,
      example_cases: author,
    },
    // A current setting, so any stack: the limit owners group, then the pack's
    // `entitlement.limit_groups` where it sets them.
    lower_limits:
      role.groups.includes(LIMIT_OWNERS_GROUP) &&
      (!doc.entitlement.limit_groups?.length ||
        role.groups.some(g => doc.entitlement.limit_groups!.includes(g))),
    set_trigger_mode: isAdmin(role),
    approve: approver,
    // Process and Start runs (7.1): the case workers group, then the pack's
    // `entitlement.process_groups` where it sets them.
    process:
      role.groups.includes(CASE_WORKERS_GROUP) &&
      (!doc.entitlement.process_groups?.length ||
        role.groups.some(g => doc.entitlement.process_groups!.includes(g))),
    rehearse: author,
  }
}

function packMode() {
  const stored = packModes[PACK.id]
  return {
    "trigger-mode": stored,
    "trigger-mode-last-changed-by": stored ? `${stored} ${ACTOR}` : null,
    effective: stored === "auto" && triggerMode === "auto" ? "auto" : "manual",
  }
}

function rules() {
  return Object.fromEntries(
    Object.entries(doc.constants)
      .filter(([k]) => !LIMITS.has(k))
      .map(([k, v]) => [
        k,
        {
          value: v,
          kind: "rule",
          source: `pack v${packVersion}`,
          guarded_by: Object.entries(GUARDS)
            .filter(([g, reads]) => doc.write_guards.includes(g) && reads.includes(k))
            .map(([g]) => g)
            .sort(),
          cited_by: citedBy(k),
        },
      ])
  )
}

/** The process types whose SOP names `{{SYMBOL}}`, aliases beside the type they alias. */
function citedBy(symbol: string): string[] {
  return (PACK.sops ?? [])
    .filter(row => sops.get(row.sop)?.includes(`{{${symbol}}}`))
    .map(row => row.process_type)
    .sort()
}

function controls(role: MockRole) {
  return {
    pack_id: PACK.id,
    limits: Object.fromEntries(
      [...LIMITS].map(k => {
        const ceiling = doc.constants[k]
        const lowered = limitOverrides[k]
        return [
          k,
          {
            value: lowered ?? ceiling,
            kind: "limit",
            source:
              lowered != null ? `stack override (lowered from ${ceiling})` : `pack v${packVersion}`,
            guarded_by: [],
            ceiling,
          },
        ]
      })
    ),
    bounds: Object.fromEntries([...LIMITS].map(k => [k, [0, 10000000]])),
    entitlement: doc.entitlement,
    write_guards: doc.write_guards,
    verifier: doc.verifier ?? null,
    write_amount_field: doc.write_amount_field ?? null,
    write_limit_waiver: doc.write_limit_waiver ?? null,
    may_lower: access(role).lower_limits,
  }
}

/** The SOP check, in short: the rules `sop_lint.py` applies that a mock can apply. */
function lint(content: string): string[] {
  const findings: string[] = []
  if (!/^Version \d+(\.\d+)*\s*$/m.test(content))
    findings.push("Add a Version line, such as Version 1.0.")
  if (!/\bMUST\b/.test(content)) findings.push("Use RFC 2119 wording: at least one MUST.")
  const known = new Set([
    ...Object.keys(doc.constants),
    ...Object.keys(CONTACTS).map(k => `CONTACT_${k.toUpperCase()}`),
  ])
  for (const [, symbol] of content.matchAll(/\{\{([A-Z0-9_]+)\}\}/g)) {
    if (!known.has(symbol)) findings.push(`{{${symbol}}} is not a rule or contact of this pack.`)
  }
  return [...new Set(findings)]
}

function sopKey(processType: string): string | undefined {
  return doc.process_type_to_sop[processType]
}

function group(c: Record<string, unknown>): string {
  if (Array.isArray(c.tags) && c.tags.includes("red-team")) return "security"
  return /_(under|at|over)(_|$)/.test(String(c.test_id)) ? "boundaries" : "behaviour"
}

/** A made-up last run: every case passed but two, so a failure has something to show. */
function lastRun() {
  const failed: Record<string, string> = {
    ap_quantity_variance_money_floor: "outcome: expected approval_required, got complete",
    boundary_line_amount_tolerance_usd_at:
      "outcome: expected auto_release, got awaiting_human_input",
  }
  const results = Object.fromEntries(
    evals.map((c, i) => {
      const id = String(c.test_id)
      const cost = 0.14 + ((i * 7) % 9) / 100
      return [
        id,
        {
          passed: !failed[id],
          failures: failed[id] ? [failed[id]] : [],
          warnings: [],
          status: failed[id] ? "awaiting_human_input" : "complete",
          cost_usd: Number(cost.toFixed(2)),
          judges: { SapActionAccuracy: failed[id] ? 0.4 : 0.95, "Builtin.Helpfulness": 0.86 },
        },
      ]
    })
  )
  return {
    run_id: "2026-11-13T10:02:11Z",
    recorded_at: "2026-11-13T10:41:52+00:00",
    recorded_by: "arn:aws:sts::111111111111:assumed-role/Admin/sam",
    pack_version: packVersion,
    total: evals.length,
    passed: evals.length - Object.keys(failed).length,
    failed: Object.keys(failed).length,
    cost_usd: 4.85,
    judges: { "Builtin.Helpfulness": 0.86 },
    results,
  }
}

// cases query (2.22) -------------------------------------------------------------------------

/** `case_status.VIEWS`: each view's statuses and its default order. */
const VIEW_STATUSES: Record<string, string[] | null> = {
  needs_you: [CaseStatus.AwaitingHumanInput, CaseStatus.ManualReviewRequired],
  team: [CaseStatus.AwaitingHumanInput, CaseStatus.ManualReviewRequired],
  new: [CaseStatus.Detected],
  running: [CaseStatus.Processing],
  failed: [CaseStatus.Error],
  done: [CaseStatus.Complete, CaseStatus.SapUpdated],
  all: null,
}
const OLDEST_FIRST = new Set(["needs_you", "team", "new", "running", "failed"])
const QUERY_PARAMS = new Set([
  "view",
  "q",
  "exception_type",
  "pack_id",
  "sort",
  "order",
  "limit",
  "next_token",
  "key",
])
/**
 * The most a mock page holds. The route may answer a short page with a token (its search stops
 * after 1,000 index entries per pack and status), so mock mode pages short to show Load more
 * over its few cases.
 */
const MOCK_PAGE = 8

class QueryRefusal extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly field?: string
  ) {
    super(message)
  }
}

const bad = (message: string, field: string) => new QueryRefusal(400, message, field)

/** The party value `GET /cases?q=` matches: the pack's first plain text field. */
const party = (c: WorkItem) => String(c.attributes?.supplier_number ?? "")

/** The filters `GET /cases` and `GET /cases/counts` share, read and checked as the route does. */
function caseFilter(url: URL, allowed: Set<string>) {
  const p = url.searchParams
  for (const k of p.keys())
    if (!allowed.has(k)) throw bad(`${k} is not a parameter of this route`, k)
  const q = p.get("q")
  if (q != null && (q.length < 1 || q.length > 64 || /[\u0000-\u001f]/.test(q)))
    throw bad("q is 1 to 64 characters, with no control characters", "q")
  const types = p.getAll("exception_type")
  if (types.length > 20) throw bad("At most 20 exception_type values", "exception_type")
  for (const t of types)
    if (!/^[a-z][a-z0-9_]{0,63}$/.test(t))
      throw bad(`${t} is not an exception type`, "exception_type")
  const packs = p.getAll("pack_id")
  if (packs.length > 50) throw bad("At most 50 pack_id values", "pack_id")
  for (const id of packs)
    if (id !== PACK.id || !seesPack(currentRole(), ENTITLEMENT_GROUPS))
      throw new QueryRefusal(403, "Not entitled to this pack")
  const needle = q?.toLowerCase()
  return (c: WorkItem) =>
    (!packs.length || packs.includes(c.pack_id)) &&
    (!types.length || types.includes(String(c.exception_type ?? c.process_type))) &&
    (!needle ||
      [c.document_number ?? "", party(c), c.case_id].some(v => v.toLowerCase().startsWith(needle)))
}

/**
 * Whether a case is in a view. `team` is every case waiting on a person in the caller's
 * packs; `needs_you` only the ones the caller may decide (the pack's approve groups).
 */
function inApiView(c: WorkItem, view: string): boolean {
  const statuses = VIEW_STATUSES[view]
  if (view === "needs_you" && !access(currentRole()).approve) return false
  return !statuses || statuses.includes(c.status)
}

/** A list item as the route cuts it: the latest trace only, its count, no action log. */
function listItem(c: WorkItem): WorkItem {
  const traces = c.agent_traces ?? []
  const { action_log: _log, ...rest } = c
  void _log
  return { ...rest, agent_traces: traces.slice(-1), traces_count: traces.length } as WorkItem
}

function caseQuery(url: URL) {
  const p = url.searchParams
  const match = caseFilter(url, QUERY_PARAMS)
  const view = p.get("view") ?? "all"
  if (!(view in VIEW_STATUSES)) throw bad(`${view} is not a view`, "view")
  const sort = p.get("sort") ?? "updated_at"
  if (sort !== "updated_at" && sort !== "amount") throw bad("sort is updated_at or amount", "sort")
  const order = p.get("order") ?? (OLDEST_FIRST.has(view) ? "asc" : "desc")
  if (order !== "asc" && order !== "desc") throw bad("order is asc or desc", "order")
  const limit = Number(p.get("limit") ?? 50)
  if (!Number.isInteger(limit) || limit < 1 || limit > 200) throw bad("limit is 1 to 200", "limit")
  // The token carries where the page ended and a digest of the request; nothing else.
  const params = new URLSearchParams(p)
  params.delete("next_token")
  const digest = params.toString()
  let offset = 0
  const token = p.get("next_token")
  if (token) {
    try {
      const t = JSON.parse(atob(token)) as { o: number; d: string }
      if (t.d !== digest || !Number.isInteger(t.o)) throw new Error()
      offset = t.o
    } catch {
      throw bad("next_token is not this request's", "next_token")
    }
  }
  const sign = order === "asc" ? 1 : -1
  // `amount_sort` (6.5): the amount, or -1E+100 without one, so it sorts below every amount.
  const amountSort = (c: WorkItem) => {
    const n = Number(c.attributes?.amount)
    return c.attributes?.amount == null || !Number.isFinite(n) ? -1e100 : n
  }
  const value = (c: WorkItem) =>
    sort === "amount" ? amountSort(c) : new Date(c.updated_at).getTime()
  const all = visible()
    .filter(c => inApiView(c, view) && match(c))
    .sort((a, b) => sign * (value(a) - value(b)) || a.case_id.localeCompare(b.case_id))
  const size = Math.min(limit, MOCK_PAGE)
  const items = all.slice(offset, offset + size).map(listItem)
  const end = offset + size
  return {
    items,
    next_token: end < all.length ? btoa(JSON.stringify({ o: end, d: digest })) : null,
    view,
    sort,
    order,
  }
}

function viewCounts(url: URL) {
  const by = url.searchParams.get("by")
  if (by != null && by !== "exception_type") throw bad("by is exception_type", "by")
  const plain = new URL(url)
  plain.searchParams.delete("by")
  const match = caseFilter(plain, new Set(["q", "exception_type", "pack_id"]))
  const shown = visible().filter(match)
  const byStatus: Record<string, number> = {}
  for (const c of shown) byStatus[c.status] = (byStatus[c.status] ?? 0) + 1
  // Per view, per type, under q and pack_id but not exception_type (round five): a facet
  // counts what each choice would show.
  const facet = () => {
    const unfaceted = new URL(plain)
    unfaceted.searchParams.delete("exception_type")
    const pool = visible().filter(caseFilter(unfaceted, new Set(["q", "pack_id"])))
    return Object.fromEntries(
      Object.keys(VIEW_STATUSES).map(v => {
        const per: Record<string, number> = {}
        for (const c of pool.filter(x => inApiView(x, v))) {
          const t = String(c.exception_type ?? c.process_type)
          per[t] = (per[t] ?? 0) + 1
        }
        return [v, per]
      })
    )
  }
  return {
    ...(by ? { exception_types: facet() } : {}),
    views: Object.fromEntries(
      Object.keys(VIEW_STATUSES).map(v => [v, shown.filter(c => inApiView(c, v)).length])
    ),
    counts: shown.length ? { [PACK.id]: byStatus } : {},
  }
}

function refusal(e: unknown) {
  if (e instanceof QueryRefusal)
    return HttpResponse.json(
      { error: e.message, ...(e.field ? { field: e.field } : {}) },
      { status: e.status }
    )
  throw e
}

const DECISIONS = new Set(["approved", "denied", "replied"])

/**
 * A person's decision on a case, as `decisions.py` records it for both routes: refused unless
 * the caller may approve in the pack, a 409 once the case has moved on, the entry appended to
 * the action log, and the first of two approvals held for a different approver.
 */
function decide(
  c: WorkItem,
  decision: string | undefined,
  option: string | undefined,
  source: Source
): {
  status: number
  error?: string
  field?: string
  awaiting?: boolean
  options?: string[]
  repeat?: boolean
  stop?: boolean
} {
  const role = currentRole()
  const set = (
    c as { decision_options?: { options: { id: string; label: string; write?: unknown }[] } }
  ).decision_options
  // An option is chosen instead of a decision; the two together are a 400 (2.23).
  if (option != null && decision != null)
    return { status: 400, error: "Choose an option or a decision, not both", field: "option" }
  if (option != null && !/^[a-z][a-z0-9_]{0,39}$/.test(option))
    return { status: 400, error: "option is not an option id", field: "option" }
  if (option == null && !DECISIONS.has(decision ?? ""))
    return { status: 400, error: "decision must be 'approved', 'denied', or 'replied'" }
  if (option == null && set && decision !== "replied")
    return { status: 400, error: "Choose one of the case's options", field: "option" }
  if (!access(role).approve)
    return { status: 403, error: "Deciding on this case needs one of the pack's approve groups" }
  if (c.status !== CaseStatus.AwaitingHumanInput)
    return { status: 409, error: "The case is already decided, or changed since it was read" }
  // A decision resumes the run, which a standing stop refuses: nothing is recorded (7.2).
  if (lastStop?.stands) return { status: 409, error: stopBody().error, stop: true }
  const chosen = option != null ? set?.options.find(o => o.id === option) : undefined
  if (option != null && !set)
    return { status: 409, error: "This case offers no options; approve, deny or reply" }
  if (option != null && !chosen)
    return {
      status: 409,
      error: "That option is not one the case offers now",
      options: set!.options.map(o => o.id),
    }
  const action = chosen ? (chosen.write ? "approved" : "chose") : decision!
  // A person who already approved this write, with nothing decided between, is refused and
  // nothing is recorded (round five).
  if (action === "approved") {
    const log = c.action_log ?? []
    for (let i = log.length - 1; i >= 0; i--) {
      const e = log[i] as { action?: string; actor?: string; option?: string }
      if (!String(e.action).startsWith("human_")) continue
      if (e.action !== "human_approved" || e.option !== (chosen?.id ?? undefined)) break
      if (e.actor === `mock-${role.id}`)
        return {
          status: 409,
          error: "You already approved this change. A different approver must approve it too.",
          repeat: true,
        }
    }
  }
  const entry = {
    action: `human_${action}`,
    timestamp: now(),
    actor: `mock-${role.id}`,
    actor_display: role.name,
    source,
    ...(chosen ? { option: chosen.id, option_label: chosen.label } : {}),
  }
  c.action_log = [...(c.action_log ?? []), entry]
  if (action === "approved") {
    const log = c.action_log ?? []
    const approvers = new Set<string>()
    for (let i = log.length - 1; i >= 0; i--) {
      const e = log[i] as { action?: string; actor?: string; option?: string }
      if (!String(e.action).startsWith("human_")) continue
      if (e.action !== "human_approved" || e.option !== entry.option) break
      approvers.add(String(e.actor))
    }
    const dual = limitOverrides.DUAL_APPROVAL_LIMIT_USD ?? doc.constants.DUAL_APPROVAL_LIMIT_USD
    const amount = Number(c.attributes?.amount)
    const needsTwo = dual != null && (c.attributes?.currency !== "USD" || !(amount <= dual))
    if (needsTwo && approvers.size < 2) {
      c.updated_at = now()
      return { status: 202, awaiting: true }
    }
  }
  // The decision resumes the run, which ends the case as the SOP says: an option without a
  // write, like a denial, hands the case to the AP team.
  Object.assign(c, { inquiry_sent_at: null })
  const resumed = chosen && !chosen.write ? "denied" : action
  startRun(c.case_id, resume(c, resumed, role.name), "console-action", c)
  return { status: 202 }
}

export const handlers: (HttpHandler | WebSocketHandler)[] = [
  runChannel,
  http.get("/aws-exports.json", () => HttpResponse.json(EXPORTS)),

  // cases ---------------------------------------------------------------------------------
  http.get(`${API}/cases/counts`, ({ request }) => {
    try {
      return HttpResponse.json(viewCounts(new URL(request.url)))
    } catch (e) {
      return refusal(e)
    }
  }),
  http.post(`${API}/cases/enqueue`, async ({ request }) => {
    const body = (await request.json()) as { case_ids?: string[]; case_id?: string }
    if (!access(currentRole()).process) return forbidden(NOT_A_CASE_WORKER)
    const stopped = stopRefused()
    if (stopped) return stopped
    for (const id of body.case_ids ?? (body.case_id ? [body.case_id] : [])) {
      const c = cases.get(id)
      if (c && !running.has(id)) startRun(id, processRun(c, autoLimit()), "console", c)
    }
    return HttpResponse.json({ enqueued: true }, { status: 202 })
  }),
  // What closed since `since`, by outcome (the case's disposition), with value per currency
  // and agent spend, one row per UTC day (2.24).
  http.get(`${API}/cases/summary`, ({ request }) => {
    const url = new URL(request.url)
    try {
      const match = caseFilter(url, new Set(["since", "pack_id"]))
      const raw = url.searchParams.get("since")
      const since = raw ? new Date(raw) : new Date(Date.now() - 24 * 3_600_000)
      if (Number.isNaN(since.getTime()) || Date.now() - since.getTime() > 30 * 86_400_000)
        throw bad("since is a time with a zone, at most 30 days ago", "since")
      const closed = visible().filter(
        c => match(c) && inApiView(c, "done") && new Date(c.updated_at) >= since
      )
      const totals = (list: WorkItem[]) => {
        const by: Record<string, { cases: number; value: Record<string, number> }> = {}
        const value: Record<string, number> = {}
        for (const c of list) {
          const o = (by[c.disposition ?? "none"] ??= { cases: 0, value: {} })
          o.cases++
          const amount = Number(c.attributes?.amount)
          if (!Number.isFinite(amount)) continue
          const cur = String(c.attributes?.currency ?? "unknown")
          o.value[cur] = (o.value[cur] ?? 0) + amount
          value[cur] = (value[cur] ?? 0) + amount
        }
        const cost = list.reduce((n, c) => n + (c.cost_summary?.total_cost_usd ?? 0), 0)
        return {
          closed: list.length,
          by_outcome: by,
          value,
          agent_cost_usd: Number(cost.toFixed(2)),
        }
      }
      const days: ({ date: string } & ReturnType<typeof totals>)[] = []
      for (
        let d = new Date(since.toISOString().slice(0, 10));
        d <= new Date();
        d.setUTCDate(d.getUTCDate() + 1)
      ) {
        const date = d.toISOString().slice(0, 10)
        days.push({ date, ...totals(closed.filter(c => c.updated_at.slice(0, 10) === date)) })
      }
      return HttpResponse.json({
        since: since.toISOString(),
        until: now(),
        packs: seesPack(currentRole(), ENTITLEMENT_GROUPS) ? [PACK.id] : [],
        totals: totals(closed),
        days,
      })
    } catch (e) {
      return refusal(e)
    }
  }),
  http.get(`${API}/cases/:caseId`, ({ params }) => {
    const c = visible().find(x => x.case_id === params.caseId)
    return c
      ? HttpResponse.json(c)
      : HttpResponse.json({ error: "Case not found" }, { status: 404 })
  }),
  http.get(`${API}/cases`, ({ request }) => {
    const url = new URL(request.url)
    const key = url.searchParams.get("key")
    try {
      if (key != null) {
        if ([...url.searchParams.keys()].some(k => k !== "key"))
          throw bad("key takes no other parameter", "key")
        const at = key.indexOf(":")
        const packId = key.slice(0, at)
        let natural: Record<string, string>
        try {
          natural = JSON.parse(key.slice(at + 1))
        } catch {
          throw bad("key is <pack_id>:<json>", "key")
        }
        return HttpResponse.json(
          visible().filter(
            c =>
              c.pack_id === packId &&
              Object.entries(natural).every(([k, v]) => String(c.key?.[k]) === String(v))
          )
        )
      }
      return HttpResponse.json(caseQuery(url))
    } catch (e) {
      return refusal(e)
    }
  }),
  http.put(`${API}/cases/:caseId/rating`, async ({ params, request }) => {
    const c = cases.get(String(params.caseId))
    if (!c) return HttpResponse.json({ error: "Case not found" }, { status: 404 })
    const body = (await request.json()) as Record<string, unknown>
    c.ratings = {
      ...(c.ratings ?? {}),
      [currentRole().email]: { ...body, rated_at: now() } as never,
    }
    return HttpResponse.json({ ok: true })
  }),
  http.post(`${API}/cases/:caseId/reply`, async ({ params, request }) => {
    const c = visible().find(x => x.case_id === params.caseId)
    if (!c) return HttpResponse.json({ error: "Case not found" }, { status: 404 })
    const body = (await request.json()) as { message?: string; decision?: string; option?: string }
    if (!body.message?.trim()) return refuse(400, "message is required")
    const result = decide(
      c,
      body.option == null ? (body.decision ?? "replied") : body.decision,
      body.option,
      Source.Console
    )
    if (result.error)
      return HttpResponse.json(
        {
          error: result.error,
          ...(result.field ? { field: result.field } : {}),
          ...(result.options ? { options: result.options } : {}),
          ...(result.repeat ? { awaiting_second_approver: true } : {}),
          ...(result.stop ? { emergency_stop: stopBody().emergency_stop } : {}),
        },
        { status: result.status }
      )
    return HttpResponse.json(
      result.awaiting
        ? { enqueued: false, case_id: c.case_id, awaiting_second_approver: true }
        : { enqueued: true, case_id: c.case_id },
      { status: 202 }
    )
  }),

  // runs ----------------------------------------------------------------------------------
  // A run replays a recorded one on the run channel (`runs.ts`): Process the case's own
  // route through the SOP, a question the assistant's answer, a rehearsal its steps with the
  // write withheld.
  http.post(`${API}/runs`, async ({ request }) => {
    const body = (await request.json()) as {
      prompt?: string
      case_id?: string
      session_id?: string
      rehearse?: boolean
      intent?: string
    }
    const prompt = body.prompt ?? ""
    const c = body.case_id ? visible().find(x => x.case_id === body.case_id) : undefined
    if (body.case_id && !c) return refuse(404, "Case not found")
    const slot = c?.case_id ?? body.session_id ?? `mock-session-${Date.now()}`
    if (c && body.intent === "process" && !access(currentRole()).process)
      return forbidden(NOT_A_CASE_WORKER)
    const stopped = stopRefused()
    if (stopped) return stopped
    const held = running.get(slot)
    if (held) return HttpResponse.json({ run_id: held.runId }, { status: 409 })
    const run = body.rehearse
      ? rehearsal(prompt)
      : c && body.intent === "process"
        ? processRun(c, autoLimit())
        : answer(prompt, c, visible())
    const runId = startRun(slot, run, c ? "console" : "chat", c)
    return HttpResponse.json({ run_id: runId, session_id: c ? undefined : slot }, { status: 202 })
  }),
  http.post(`${API}/deployment/stop`, async ({ request }) => {
    if (!isAdmin()) return forbidden("This needs a Business Operations administrator")
    const body = (await request.json().catch(() => ({}))) as { reason?: unknown }
    const reason = typeof body.reason === "string" ? body.reason.trim() : ""
    if (!reason || reason.length > 500 || /[\u0000-\u001f\u007f]/.test(reason))
      return HttpResponse.json(
        {
          error: "A reason is 1 to 500 characters on one line, with no control characters",
          field: "reason",
        },
        { status: 400 }
      )
    const at = now()
    triggerMode = "manual"
    triggerActor = `set by ${currentRole().email} (emergency stop) at ${at}`
    const runs = [...running.keys()].map(slot => {
      const run = running.get(slot)!
      const c = cases.get(slot)
      run.replay.cancel()
      running.delete(slot)
      if (!c)
        return {
          run_id: run.runId,
          case_id: null,
          session_stopped: false,
          released: false,
          parked: false,
        }
      const parked = c.status === CaseStatus.Processing
      delete c.live_run
      if (parked)
        Object.assign(c, {
          status: CaseStatus.ManualReviewRequired,
          status_reason: `Stopped by an emergency stop: ${reason}`,
          updated_at: now(),
        })
      return { run_id: run.runId, case_id: slot, session_stopped: true, released: true, parked }
    })
    lastStop = {
      stopped_at: at,
      stopped_by: `${currentRole().email} (mock-${currentRole().id})`,
      reason,
      trigger_mode_set: true,
      runs_total: runs.length,
      runs: runs.slice(0, 100),
      complete: true,
      stands: true,
      queue_held: true,
    }
    return HttpResponse.json({ stop: lastStop })
  }),
  // Ends a standing stop (7.2). The trigger mode stays where the stop left it.
  http.post(`${API}/deployment/stop/clear`, async ({ request }) => {
    if (!isAdmin()) return forbidden("This needs a Business Operations administrator")
    const body = (await request.json().catch(() => ({}))) as { reason?: unknown }
    const reason = typeof body.reason === "string" ? body.reason.trim() : ""
    if (!reason || reason.length > 500 || /[\u0000-\u001f\u007f]/.test(reason))
      return HttpResponse.json(
        {
          error: "A reason is 1 to 500 characters on one line, with no control characters",
          field: "reason",
        },
        { status: 400 }
      )
    if (!lastStop?.stands) return refuse(409, "No emergency stop stands")
    lastStop = {
      ...lastStop,
      stands: false,
      cleared_at: now(),
      cleared_by: `${currentRole().email} (mock-${currentRole().id})`,
      clear_reason: reason,
    }
    return HttpResponse.json({ stop: lastStop })
  }),
  http.post(`${API}/runs/stop`, async ({ request }) => {
    const body = (await request.json()) as { case_id?: string; session_id?: string }
    stopRun(body.case_id ?? body.session_id ?? "")
    return HttpResponse.json({ stopped: true }, { status: 202 })
  }),

  // deployment ----------------------------------------------------------------------------
  http.get(`${API}/deployment/trigger-mode`, () =>
    HttpResponse.json({
      "trigger-mode": triggerMode,
      "trigger-mode-last-changed-by": `${triggerMode} ${triggerActor}`,
      "autonomous-capable": true,
      packs: seesPack(currentRole(), ENTITLEMENT_GROUPS) ? { [PACK.id]: packMode() } : {},
    })
  ),
  http.put(`${API}/deployment/trigger-mode`, async ({ request }) => {
    if (!isAdmin()) return forbidden("This needs a Business Operations administrator")
    const body = (await request.json()) as { "trigger-mode"?: string }
    if (body["trigger-mode"] !== "auto" && body["trigger-mode"] !== "manual")
      return refuse(400, 'trigger-mode must be "auto" or "manual"')
    // No trigger mode turns to auto while a stop stands; the clear comes first.
    if (body["trigger-mode"] === "auto") {
      const stopped = stopRefused()
      if (stopped) return stopped
    }
    triggerMode = body["trigger-mode"]
    triggerActor = `set by ${currentRole().email} at ${now()}`
    return HttpResponse.json({ "trigger-mode": triggerMode })
  }),
  http.get(`${API}/deployment/settings`, () => {
    if (!seesPack(currentRole(), ENTITLEMENT_GROUPS)) return forbidden("Not entitled to any pack")
    const keys = [...new Set([...Object.keys(CONTACTS), ...Object.keys(contactOverrides)])].sort()
    return HttpResponse.json({
      contacts: {
        defaults: CONTACTS,
        overrides: contactOverrides,
        effective: Object.fromEntries(
          keys.map(k => [
            `CONTACT_${k.toUpperCase()}`,
            {
              value: contactOverrides[k] ?? CONTACTS[k] ?? null,
              kind: "contact",
              source: k in contactOverrides ? "stack contact override" : "platform.yaml",
            },
          ])
        ),
      },
      deployment: {
        mode: "workshop",
        promotion_role: promotionOnly() ? "quality" : "development",
        promotion_only: promotionOnly(),
        sap_systems: [{ name: "s4", system_role: "sandbox" }],
        non_production: true,
      },
      access: {
        may_create_pack: currentRole().canAuthor && !promotionOnly(),
        may_edit_contacts: currentRole().canAuthor,
      },
      last_stop: lastStop,
    })
  }),
  http.put(`${API}/deployment/settings`, async ({ request }) => {
    if (!currentRole().canAuthor) return forbidden("Changing contacts needs the authors group")
    const body = (await request.json()) as { contacts?: Record<string, string | null> }
    let updated = 0
    for (const [k, v] of Object.entries(body.contacts ?? {})) {
      if (v == null) delete contactOverrides[k]
      else contactOverrides[k] = v
      updated++
    }
    return HttpResponse.json({ updated, updated_by: currentRole().email })
  }),

  // registry and packs --------------------------------------------------------------------
  http.get(`${API}/registry`, () => {
    const role = currentRole()
    if (!seesPack(role, ENTITLEMENT_GROUPS)) return HttpResponse.json({ packs: [] })
    return HttpResponse.json({
      packs: [
        {
          ...PACK,
          display_name: doc.display_name,
          description: doc.description,
          access: access(role),
        },
      ],
    })
  }),
  http.get(`${API}/packs/status`, () =>
    HttpResponse.json({
      packs: seesPack(currentRole(), ENTITLEMENT_GROUPS)
        ? [
            {
              pack_id: PACK.id,
              version: packVersion,
              origin: { via: "edit", at: now(), by: "ana@example.com" },
              marked: false,
              routes: PACK.process_types,
              unroutable: [],
              last_refusal: null,
            },
          ]
        : [],
    })
  ),
  http.post(`${API}/packs`, async ({ request }) => {
    const promoted = promotionRefusal()
    if (promoted) return promoted
    if (!currentRole().canAuthor) return forbidden("Creating a pack needs the authors group")
    const body = (await request.json()) as { pack_id?: string }
    if (body.pack_id === PACK.id) return refuse(409, `${PACK.id} is already published`)
    return HttpResponse.json({ pack_id: body.pack_id, version: 0 }, { status: 201 })
  }),
  http.get(`${API}/packs/:packId`, ({ params }) => {
    const refused = packRefusal(params.packId)
    if (refused) return refused
    return HttpResponse.json({
      pack_id: PACK.id,
      version: packVersion,
      origin: { via: "edit", by: "ana@example.com", at: "2026-11-19T10:02:11+00:00" },
      pack: doc,
      parts: Object.fromEntries(
        Object.keys(doc).flatMap(k => (PART_OF[k] ? [[k, PART_OF[k]]] : []))
      ),
      constant_parts: Object.fromEntries([...LIMITS].map(k => [k, "controls"])),
      access: access(currentRole()),
    })
  }),
  http.patch(`${API}/packs/:packId`, async ({ params, request }) => {
    const refused = packRefusal(params.packId)
    if (refused) return refused
    const body = (await request.json()) as {
      pack?: Record<string, unknown>
      base_prompt?: string
      expected_version?: number
      create?: unknown
    }
    if (body.create !== undefined) return refuse(400, "A create is POST /packs")
    const promoted = promotionRefusal()
    if (promoted) return promoted
    const role = currentRole()
    if (!role.canAuthor) return forbidden("Editing a pack needs the authors group")
    const structure = Object.keys(body.pack ?? {}).filter(k => !CONTENT.has(k))
    const limits = Object.keys((body.pack?.constants as object) ?? {}).filter(k => LIMITS.has(k))
    if (structure.length || limits.length)
      return forbidden(
        `${[...structure, ...limits].join(", ")} is structure: it needs business-operations-pack-admins`
      )
    if (body.expected_version != null && body.expected_version !== packVersion)
      return refuse(409, "Someone saved this pack since you opened it. Reload to see their change.")
    for (const [k, v] of Object.entries(body.pack ?? {})) {
      if (k === "constants") Object.assign(doc.constants, v)
      else doc[k] = v
    }
    if (body.base_prompt != null) basePrompt = body.base_prompt
    packVersion++
    const changedKeys = Object.keys(body.pack ?? {})
    recordVersion(
      changedKeys.length || body.base_prompt != null ? ["procedures"] : [],
      [
        ...(changedKeys.length ? ["pack.json"] : []),
        ...(body.base_prompt != null ? ["base_prompt.txt"] : []),
      ],
      changedKeys
    )
    return HttpResponse.json({ pack_id: PACK.id, version: packVersion })
  }),
  http.get(`${API}/packs/:packId/base_prompt`, ({ params }) => {
    const refused = packRefusal(params.packId)
    if (refused) return refused
    return HttpResponse.json({ base_prompt: basePrompt, version: packVersion })
  }),

  // procedures ----------------------------------------------------------------------------
  http.get(`${API}/packs/:packId/sops`, ({ params }) => {
    const refused = packRefusal(params.packId)
    if (refused) return refused
    return HttpResponse.json({
      pack_id: PACK.id,
      version: packVersion,
      sops: (PACK.sops ?? []).map(row => ({
        ...row,
        key: row.sop,
        ...(sops.has(row.sop)
          ? {
              size: sops.get(row.sop)!.length,
              last_modified: savedAt[row.sop]?.at ?? "2026-11-05T09:12:00Z",
            }
          : {}),
      })),
      ingestion: { job_id: "mock-job", status: "COMPLETE", started_at: "2026-11-05T09:13:00Z" },
      search: { [PACK.id]: "current" },
      can_author: currentRole().canAuthor,
    })
  }),
  http.get(`${API}/packs/:packId/sops/:processType`, ({ params }) => {
    const refused = packRefusal(params.packId)
    if (refused) return refused
    const row = PACK.sops?.find(r => r.process_type === params.processType)
    const key = row?.sop
    const content = key ? sops.get(key) : undefined
    if (!row || !key || content == null)
      return refuse(404, `${PACK.id} maps no process type ${String(params.processType)}`)
    return HttpResponse.json({
      key,
      process_type: row.process_type,
      kind: row.kind,
      ...(row.alias_of ? { alias_of: row.alias_of } : {}),
      version_id: null,
      size: content.length,
      last_modified: savedAt[key]?.at ?? "2026-11-05T09:12:00Z",
      content,
      uploaded_by: savedAt[key]?.by ?? null,
      uploaded_at: savedAt[key]?.at ?? null,
      pack_version: packVersion,
      can_author: currentRole().canAuthor,
    })
  }),
  http.put(`${API}/packs/:packId/sops/:processType`, async ({ params, request }) => {
    const refused = packRefusal(params.packId)
    if (refused) return refused
    const promoted = promotionRefusal()
    if (promoted) return promoted
    if (!currentRole().canAuthor) return forbidden("Editing a SOP needs the authors group")
    const pt = String(params.processType)
    const owner = PACK.sops?.find(s => s.process_type === pt)
    if (owner?.kind === "alias") return refuse(409, `${pt} is an alias of ${owner.alias_of}`)
    const key = sopKey(pt)
    if (!key) return refuse(404, "No SOP for that process type")
    const body = (await request.json()) as { content: string; expected_version?: number }
    if (body.expected_version != null && body.expected_version !== packVersion)
      return refuse(409, "Someone saved this pack since you opened it. Reload to see their change.")
    const findings = lint(body.content)
    if (findings.length)
      return HttpResponse.json(
        { error: "The SOP check failed", details: findings },
        { status: 400 }
      )
    sops.set(key, body.content)
    packVersion++
    recordVersion(["procedures"], [`sops/${key.split("/").pop()}`])
    savedAt[key] = { at: now(), by: currentRole().name }
    return HttpResponse.json({
      pack_id: PACK.id,
      version: packVersion,
      key,
      bytes: new TextEncoder().encode(body.content).length,
      uploaded_by: currentRole().email,
    })
  }),
  http.post(`${API}/packs/:packId/sops/:processType/check`, async ({ params, request }) => {
    const refused = packRefusal(params.packId)
    if (refused) return refused
    const pt = String(params.processType)
    const key = sopKey(pt) ?? `${PACK.id}/${pt}.txt`
    const body = (await request.json()) as { content: string }
    const findings = lint(body.content)
    return HttpResponse.json({ key, ok: findings.length === 0, findings })
  }),
  http.get(`${API}/packs/:packId/rules`, ({ params }) => {
    const refused = packRefusal(params.packId)
    if (refused) return refused
    const r = rules()
    return HttpResponse.json({
      pack_id: PACK.id,
      version: packVersion,
      rules: r,
      bounds: Object.fromEntries(
        Object.keys(r).map(k => [k, [0, k.endsWith("_PCT") ? 100 : 1000000]])
      ),
      may_edit: access(currentRole()).edit.rule_values,
    })
  }),

  // versions ------------------------------------------------------------------------------
  http.get(`${API}/packs/:packId/versions`, ({ params, request }) => {
    const refused = packRefusal(params.packId)
    if (refused) return refused
    const url = new URL(request.url)
    for (const k of url.searchParams.keys())
      if (k !== "limit" && k !== "next_token")
        return HttpResponse.json({ error: `Unknown parameter ${k}`, field: k }, { status: 400 })
    const limit = Number(url.searchParams.get("limit") ?? 10)
    const token = url.searchParams.get("next_token")
    let below = Infinity
    if (token) {
      try {
        below = (JSON.parse(atob(token)) as { before: number }).before
      } catch {
        return HttpResponse.json(
          { error: "Not a token this route issued", field: "next_token" },
          { status: 400 }
        )
      }
    }
    const page = versions.filter(v => v.version < below).slice(0, limit)
    const last = page[page.length - 1]
    const more = !!last && versions.some(v => v.version < last.version)
    return HttpResponse.json({
      pack_id: PACK.id,
      active: packVersion,
      versions: page.map(versionEntry),
      next_token: more ? btoa(JSON.stringify({ before: last.version })) : null,
    })
  }),
  http.get(`${API}/packs/:packId/versions/:version`, ({ params }) => {
    const refused = packRefusal(params.packId)
    if (refused) return refused
    const n = Number(params.version)
    if (!Number.isInteger(n) || n < 0) return refuse(400, "A version is a non-negative integer")
    const v = versions.find(x => x.version === n)
    if (!v) return refuse(404, `No ${PACK.id} v${n} is stored here`)
    const previous = versions.find(x => x.version < n)
    return HttpResponse.json({
      pack_id: PACK.id,
      ...versionEntry(v),
      previous: previous
        ? { version: previous.version, sha256: versionEntry(previous).sha256 }
        : null,
      ...snapshotOf(n),
    })
  }),

  // controls and trigger mode -----------------------------------------------------------------
  http.get(`${API}/packs/:packId/controls`, ({ params }) => {
    const refused = packRefusal(params.packId)
    if (refused) return refused
    return HttpResponse.json(controls(currentRole()))
  }),
  http.put(`${API}/packs/:packId/controls`, async ({ params, request }) => {
    const refused = packRefusal(params.packId)
    if (refused) return refused
    if (!access(currentRole()).lower_limits)
      return forbidden(`You are not authorized to lower the limits of '${PACK.id}'`)
    const body = (await request.json()) as { limits?: Record<string, number | null> }
    const errors: string[] = []
    let updated = 0
    for (const [k, v] of Object.entries(body.limits ?? {})) {
      if (!LIMITS.has(k))
        errors.push(`${k} is a rule, not a limit: change it with PATCH /packs/${PACK.id}`)
      else if (v == null) delete limitOverrides[k]
      else if (v > doc.constants[k])
        errors.push(`${k} may be lowered, never raised above ${doc.constants[k]}`)
      else limitOverrides[k] = v
      updated++
    }
    if (errors.length)
      return HttpResponse.json({ error: "Invalid limits", details: errors }, { status: 400 })
    return HttpResponse.json({ updated, updated_by: currentRole().email })
  }),
  http.get(`${API}/packs/:packId/trigger-mode`, ({ params }) => {
    const refused = packRefusal(params.packId)
    if (refused) return refused
    return HttpResponse.json({ pack_id: PACK.id, ...packMode(), deployment: triggerMode })
  }),
  http.put(`${API}/packs/:packId/trigger-mode`, async ({ params, request }) => {
    const refused = packRefusal(params.packId)
    if (refused) return refused
    if (!isAdmin()) return forbidden("This needs a Business Operations administrator")
    const body = (await request.json()) as { "trigger-mode"?: "auto" | "manual" }
    if (body["trigger-mode"] !== "auto" && body["trigger-mode"] !== "manual")
      return refuse(400, 'trigger-mode must be "auto" or "manual"')
    // No trigger mode turns to auto while a stop stands; the clear comes first.
    if (body["trigger-mode"] === "auto") {
      const stopped = stopRefused()
      if (stopped) return stopped
    }
    packModes[PACK.id] = body["trigger-mode"]
    return HttpResponse.json({ pack_id: PACK.id, "trigger-mode": body["trigger-mode"] })
  }),

  // evaluations ---------------------------------------------------------------------------
  http.get(`${API}/packs/:packId/evaluations`, ({ params }) => {
    const refused = packRefusal(params.packId)
    if (refused) return refused
    const role = currentRole()
    return HttpResponse.json({
      pack_id: PACK.id,
      version: packVersion,
      example_cases: evals.map(c => ({ tags: [], ...c, group: group(c) })),
      last_run: lastRun(),
      estimate_usd: 5.8,
      may_edit: role.canAuthor,
      may_run: false,
      run_refusal: `Runs from the command line today: ops.py evals-run --pack ${PACK.id}`,
    })
  }),
  http.put(`${API}/packs/:packId/evaluations/:testId`, async ({ params, request }) => {
    const refused = packRefusal(params.packId)
    if (refused) return refused
    const promoted = promotionRefusal()
    if (promoted) return promoted
    if (!currentRole().canAuthor)
      return forbidden("Changing example cases needs the pack's authors")
    const testId = String(params.testId)
    if (!/^[a-z][a-z0-9_]{2,79}$/.test(testId))
      return refuse(400, "A test id is 3 to 80 lowercase letters, digits and underscores")
    const body = (await request.json()) as {
      case?: Record<string, unknown> & { expected?: { reason?: string }; process_type?: string }
      expected_version?: number
    }
    if (body.expected_version != null && body.expected_version !== packVersion)
      return refuse(409, "Someone saved this pack since you opened it. Reload to see their change.")
    const details: string[] = []
    if (!body.case?.expected?.reason) details.push("expected.reason is required")
    if (body.case?.process_type && !sopKey(body.case.process_type))
      details.push(`${body.case.process_type} is not a process type of this pack`)
    if (details.length)
      return HttpResponse.json({ error: "Invalid example case", details }, { status: 400 })
    const next = { ...body.case, test_id: testId }
    const at = evals.findIndex(c => c.test_id === testId)
    if (at >= 0) evals[at] = next
    else evals.push(next)
    packVersion++
    recordVersion(["evaluations"], ["evals.json"])
    return HttpResponse.json({ version: packVersion })
  }),

  // tickets -------------------------------------------------------------------------------
  http.get(`${API}/tickets/:ticketId`, ({ params }) => {
    const t = tickets.get(String(params.ticketId))
    return t
      ? HttpResponse.json(t)
      : HttpResponse.json({ error: "Ticket not found" }, { status: 404 })
  }),
  http.get(`${API}/tickets`, ({ request }) => {
    const status = new URL(request.url).searchParams.get("status")
    return HttpResponse.json({
      items: [...tickets.values()].filter(t => !status || t.status === status),
      next_token: null,
    })
  }),
  http.post(`${API}/tickets`, async ({ request }) => {
    const body = (await request.json()) as Record<string, string>
    const id = `TKT-${1000 + tickets.size + 1}`
    const t = {
      ticket_id: id,
      status: TicketStatus.Open,
      priority: "medium",
      created_at: now(),
      updated_at: now(),
      description: "",
      ...body,
    }
    tickets.set(id, t as never)
    return HttpResponse.json(t, { status: 201 })
  }),
  http.put(`${API}/tickets/:ticketId`, async ({ params, request }) => {
    const t = tickets.get(String(params.ticketId))
    if (!t) return HttpResponse.json({ error: "Ticket not found" }, { status: 404 })
    Object.assign(t, (await request.json()) as object, { updated_at: now() })
    return HttpResponse.json(t)
  }),
  http.post(`${API}/tickets/:ticketId/action`, async ({ params, request }) => {
    const t = tickets.get(String(params.ticketId))
    if (!t) return HttpResponse.json({ error: "Ticket not found" }, { status: 404 })
    const body = (await request.json()) as { action?: string; option?: string; resolution?: string }
    if (t.status !== TicketStatus.Open && t.status !== TicketStatus.Assigned)
      return refuse(409, `Ticket already has decision ${t.status}; cannot change it`)
    const c = t.case_id ? cases.get(t.case_id) : undefined
    const result = c
      ? decide(c, body.action, body.option, Source.Ticket)
      : { status: 202, awaiting: false }
    if (result.error)
      return HttpResponse.json(
        {
          error: result.error,
          ...(result.repeat ? { awaiting_second_approver: true } : {}),
          ...(result.stop ? { emergency_stop: stopBody().emergency_stop } : {}),
        },
        { status: result.status }
      )
    if (result.awaiting) {
      t.comments = [
        ...(t.comments ?? []),
        {
          author: currentRole().email,
          text: "Approved (1 of 2): a second approver is needed",
          timestamp: now(),
        },
      ]
      t.updated_at = now()
      return HttpResponse.json(
        { ticket: t, enqueued: false, case_id: t.case_id, awaiting_second_approver: true },
        { status: 202 }
      )
    }
    // An option with a write approves; one without denies (2.23).
    const chosen = (
      c as { decision_options?: { options: { id: string; label: string; write?: unknown }[] } }
    )?.decision_options?.options.find(o => o.id === body.option)
    const status = chosen
      ? chosen.write
        ? TicketStatus.Approved
        : TicketStatus.Denied
      : body.action === "denied"
        ? TicketStatus.Denied
        : body.action === "replied"
          ? TicketStatus.Replied
          : TicketStatus.Approved
    Object.assign(t, { status, resolution: body.resolution ?? null, updated_at: now() })
    return HttpResponse.json({ ticket: t, enqueued: true, case_id: t.case_id }, { status: 202 })
  }),
]
