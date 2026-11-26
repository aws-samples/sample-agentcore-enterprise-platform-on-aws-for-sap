// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// GENERATED from use-cases/business-operations/types/cases.schema.json. Do not edit:
// change the schema, then run `deploy.sh usecase run business-operations types`.

/**
 * One case: an item in the use case's cases table.
 */
export interface WorkItem {
  /**
   * Case identity and the table's sole partition key: {short_id}-{26 base32 chars}, a readable pack
   * prefix and a digest of `key`, so any natural key of any arity gets an id that is legal in a
   * URL, an SQS MessageGroupId, a Memory session id and an AppSync channel segment. Opaque: what
   * the case IS lives in `key`. Built and parsed only through the case_key codec
   * (agent/utils/case_key.py, frontend/src/lib/caseKey.ts).
   */
  case_id: string
  /**
   * The case's natural key in the source system — SAP field name to value. The names are the ones
   * the pack declares (the `source.key` block of packs/<pack_id>/pack.json), but this is a map and
   * so carries no order: DynamoDB does not preserve a map's key order, and a `Scan` returns an AP
   * key declared `SupplierInvoice, FiscalYear` as `FiscalYear, SupplierInvoice`. Anything that
   * renders these values in order reads the order off `source.key` in `GET /registry`, which is an
   * array for that reason. This is what identifies the case: `case_id` is a digest of it, so the
   * record has to carry it back for display, logs, and the GET /cases?key= lookup — none of which
   * depend on the order (`api/cases/index.py` sorts for the filter and re-checks whole-map
   * equality). Any arity: two fields for an AP invoice, three for a material/plant/batch case.
   */
  key: { [key: string]: string }
  /**
   * SAP document number (e.g. supplier invoice). A descriptive attribute, not identity: SAP calls
   * and display need it. Optional: a pack keyed on something that is not a document (a material, a
   * cost center) has none. Top-level rather than in `attributes` because a key spec's `as` writes
   * it.
   */
  document_number?: string
  /**
   * Item identifier within the document. A descriptive attribute, not identity, and top-level for
   * the same reason as document_number.
   */
  item_id?: string
  /**
   * Which use case pack this item belongs to; see the PackId definition. The table's
   * pack-status-index is keyed on it.
   */
  pack_id: string
  /**
   * The process type the case was created with, which routes its first run: the pack's
   * `source.process_type.default` (`invoice_matching`, the triage type, in finance_ops) unless a
   * `source.process_type.rules` entry matched. Written once by the case's producer and never
   * rewritten; the type the agent identified is `exception_type`.
   */
  process_type: string
  /**
   * The exception type the agent identified: the process type of the last exception SOP a run of
   * this case was given, either the SOP it was routed to or one `load_sop` served (an alias
   * recorded as the type it aliases). Written by the agent runtime in the statement that appends
   * that run's trace (agent/utils/case_persistence.py), never through update_case_state, and only
   * for a type the case's own pack maps to an exception SOP: a triage or shared SOP is never
   * recorded (registry.sop_kinds). Absent until a run identifies one; a later run that identifies
   * another replaces it.
   */
  exception_type?: string
  /**
   * Present when the seeder posted the documents this case is about (the console's test data page
   * or `ops.py seed`): its record of the post, copied by the case's producer or by the seeder,
   * never from a request. Absent on every other case.
   */
  test_data?: TestDataMark
  /**
   * Human-readable summary for display.
   */
  title?: string
  /**
   * Short detail text.
   */
  description?: null | string
  priority?: Priority
  status: CaseStatus
  /**
   * Why the case is in its current status, when the status alone does not say. Server-owned and
   * written only by the paths that move a case without the agent asking them to: the invoker when a
   * run stops at a processing limit or exhausts its redrive, and case_persistence when the runtime
   * parks a case itself. Not writable through update_case_state: a model-authored reason would let
   * a run explain away a status the platform assigned it.
   */
  status_reason?: null | string
  /**
   * What was decided, as opposed to the fact that the case finished: a short snake_case label the
   * agent records when it closes a case — `released`, `rejected_duplicate`, `escalated_to_buyer`.
   * The status says a case is `complete`; only this says what completing it meant, which is why the
   * precedent row a resolved case produces carries it (`agent_knowledge.precedent.disposition`, NOT
   * NULL) and why `get_precedent` returns it as evidence. Model-authored, unlike `status_reason`,
   * and deliberately open rather than an enum: what a disposition can be is a property of the SOP,
   * so an enum here would have to be edited to add a pack. A SOP that names one is what makes it
   * appear — `packs/finance_ops/sops/duplicate_invoice.txt` requires `rejected_duplicate`. Absent
   * where no SOP asked for one; the exemplar builder then derives a mechanical stand-in from the
   * case's own SAP writes rather than recording the constant its status already states.
   */
  disposition?: null | string
  created_at: string
  updated_at: string
  /**
   * Set to `sap-push` when a sender's typed records made the case, so its key and attributes are
   * what the sender said. The run checks never let a pushed amount lift the approval limit. Absent
   * on a case the poller or a nudge read from SAP itself.
   */
  origin?: Origin
  /**
   * What this case's pack knows about it that the platform does not: the values the pack's
   * `field_map` pulled out of SAP (`source.field_map` in `packs/<pack_id>/pack.json`), under the
   * names that config chose. Open by design — an AP case carries `amount` and `supplier_number`, a
   * material case carries `plant` and `base_unit`, and neither shape appears here, so adding a pack
   * costs a config file rather than a schema edit and a regenerated type in two languages. Scalars
   * only: these are display and prompt values, and a nested object would reach a label as `[object
   * Object]`. The names are checked at build time against the pack config that writes them
   * (business_operations/validate_packs.py).
   */
  attributes?: { [key: string]: boolean | null | number | string }
  /**
   * Related demo ticket ID while the case awaits a supervised response.
   */
  ticket_id?: null | string
  /**
   * The response_type of the ticket in ticket_id, stamped with it by demo_create_ticket
   * (tickets_tool/handler.py), so a list tells a case waiting on an approval from one waiting on an
   * answer without reading its ticket. inquiry_sent_at cannot: it is stamped on every move to
   * awaiting_human_input.
   */
  ticket_response_type?: null | string
  /**
   * ISO timestamp of the outbound inquiry that put the case into awaiting_human_input.
   * Server-owned: stamped by the case management tool on entry to that status and cleared on
   * leaving it, never taken from model-authored updates — it is what lets the handover claim
   * 'waiting 6d' rather than 'last activity 6d'.
   */
  inquiry_sent_at?: null | string
  /**
   * Append-only audit trail: one entry per update_case_state call (case_management/handler.py) and
   * one per human decision on the case (api/layer/python/decisions.py), each written by the server,
   * never taken from the caller.
   */
  action_log?: ActionLogEntry[]
  /**
   * DynamoDB TTL (epoch seconds). For seeded synthetic cases only, so they self-clean; a real case
   * never carries one, and the model cannot write it.
   */
  ttl?: null | number
  /**
   * Human ratings of the resolution, keyed by the rater's `sub`. One per rater; rating again
   * replaces your own. Written only by `PUT /cases/{case_id}/rating`.
   */
  ratings?: { [key: string]: CaseRating }
  /**
   * Historical agent thought-process traces, one per invocation.
   */
  agent_traces?: AgentTrace[]
  /**
   * Per-case accumulated agent cost. Written by agent/utils/case_persistence.py.
   */
  cost_summary?: CostSummary
  /**
   * Count of oldest traces evicted by the per-case trace cap, so the UI can state that history was
   * thinned rather than thinning it silently.
   */
  traces_dropped?: null | number
  /**
   * The run currently streaming on this case. Written every ~2s while it runs and REMOVEd in the
   * same update that appends the finished trace. Absent means no run is in flight; an updated_at
   * older than 60s means the run was interrupted.
   */
  live_run?: LiveRun
  /**
   * The outcomes a person may choose between while the case waits on a decision, each with the
   * exact write it would make. Written only by update_case_state, in the update that sets
   * awaiting_human_input; any other status write, and the invoker as a run starts, removes it. A
   * decision route takes one option's id and approves that option's write and nothing else
   * (api/layer/python/decision_options.py).
   */
  decision_options?: DecisionOptions
}

/**
 * Set to `sap-push` when a sender's typed records made the case, so its key and attributes are what
 * the sender said. The run checks never let a pushed amount lift the approval limit. Absent on a
 * case the poller or a nudge read from SAP itself.
 */
export enum Origin {
  SapPush = "sap-push",
}

export enum Priority {
  High = "high",
  Medium = "medium",
  Low = "low",
}

/**
 * `error` is terminal-until-retried: the SOPs and skill prompts instruct the agent to write it when
 * a tool call fails irrecoverably, so it must be a first-class state rather than a value the UI
 * silently renders as 'Detected'.
 */
export enum CaseStatus {
  Detected = "detected",
  Processing = "processing",
  AwaitingHumanInput = "awaiting_human_input",
  SapUpdated = "sap_updated",
  Complete = "complete",
  ManualReviewRequired = "manual_review_required",
  Error = "error",
}

export interface ActionLogEntry {
  /**
   * Action name the caller supplied, e.g. sap_updated, invoice_released.
   */
  action: string
  timestamp: string
  /**
   * The deciding human's `sub`, from the authorizer. Human decisions only.
   */
  actor?: string
  /**
   * That human's display claim (email first) at the time.
   */
  actor_display?: string
  /**
   * Which route recorded the decision.
   */
  source?: Source
  /**
   * sha256 of the write the approver saw: the chosen option's write, or else the latest
   * proposed_write in the case's traces. Absent when there was none.
   */
  approved_write?: string
  /**
   * On an approval: the run whose grant carried it, set by the invoker as it writes that grant. An
   * approval authorizes one run, so a second run on it is unapproved.
   */
  consumed_by_run?: string
  /**
   * On a decision that chose one of the case's decision_options: its id.
   */
  option?: string
  /**
   * That option's label, as the person saw it.
   */
  option_label?: string
  /**
   * That option's write, copied by the decision route as the person saw it; approved_write is its
   * sha256. The invoker builds the run's grant from it. Absent for an option that makes no write.
   */
  option_write?: ProposedWrite
}

/**
 * Which route recorded the decision.
 */
export enum Source {
  Console = "console",
  Ticket = "ticket",
}

export interface AgentTrace {
  trace_id: string
  timestamp: string
  /**
   * What initiated this invocation.
   */
  trigger?: string
  prompt?: string
  /**
   * Invocation result: complete, stopped, cancelled, error, disconnected.
   */
  outcome?: string
  /**
   * Version the injected SOP declared in its own header, read at resolve_skill time. Names the
   * authority this run actually followed, which a precedent row citing the case must reproduce even
   * after the SOP is revised.
   */
  sop_version?: string
  /**
   * The SOP object key `sop_version` was read from — the document this run was given, so a reader
   * of the case can open the text it was governed by (the `/packs?doc=` link behind a quoted
   * clause). Absent on a run whose SOP did not load, and on every run persisted before this was
   * recorded. Names the *injected* SOP only: `sop_baseline` also accumulates whatever
   * `load_sop`/`search_sap_sops` returned and keeps no per-document provenance, so a quote lifted
   * from a discovered SOP still resolves here — which is why the UI offers this as 'the SOP this
   * run was given' and never as where a given quote came from.
   */
  sop_key?: string
  /**
   * The S3 VersionId of the SOP bytes this run was given. `sop_version` is typed by the author;
   * this names exactly what the run read, in a bucket that denies version deletes.
   */
  sop_version_id?: string
  /**
   * The exception type this run identified, when it did: the case's `exception_type` as this run
   * set it. Absent on a run that stayed in triage or a shared SOP.
   */
  exception_type?: string
  /**
   * `ask` on a conversation turn (`POST /runs` without `intent: process`), which withheld every
   * tool that changes SAP, a case, a ticket or sends mail. Absent on a run that could write:
   * Process, a resume after a decision or a reply, a queued run.
   */
  intent?: Intent
  /**
   * who this run acted for — the human's subject for an interactive run, the machine principal for
   * a queued one. Persisted so a SAP write made during the run is attributable from the case alone.
   */
  initiator?: string
  /**
   * The AG-UI run id this trace came from, so the live turn a reader is already rendering from
   * `live_run` can be deduped against the stored trace. Different value from trace_id, which is
   * minted at write time.
   */
  correlation_id?: string
  /**
   * Wall-clock duration of the invocation.
   */
  latency_ms?: number
  input_tokens?: number
  output_tokens?: number
  cache_read_tokens?: number
  cache_write_tokens?: number
  /**
   * Cost of this invocation alone. `cost_summary` on the case is the running total across
   * invocations.
   */
  estimated_cost_usd?: number
  /**
   * One entry per write-verifier call this invocation made. Its cost is inside
   * `estimated_cost_usd`; its tokens are not in the token fields above, which are the agent's.
   */
  verifier_calls?: VerifierCall[]
  segments: TraceSegment[]
}

/**
 * `ask` on a conversation turn (`POST /runs` without `intent: process`), which withheld every tool
 * that changes SAP, a case, a ticket or sends mail. Absent on a run that could write: Process, a
 * resume after a decision or a reply, a queued run.
 */
export enum Intent {
  Ask = "ask",
}

export interface VerifierCall {
  tier?: string
  model_id: string
  verdict: Verdict
  /**
   * The verifier's one sentence, or why it gave no usable verdict — every failure is a disagree.
   */
  reason: string
  tool?: string
  /**
   * The function import's name, trimmed and upper-cased; empty for an odata_update.
   */
  function_name?: string
  /**
   * What the write does by the guards' classification: the function name, RELEASE for an
   * odata_update (it clears the payment block), CANCEL for a reversal. What the benchmark grades
   * against.
   */
  write?: string
  /**
   * No answer in time. Amazon Bedrock may still have billed it, so the zero cost is a lower bound.
   */
  timed_out?: boolean
  input_tokens?: number
  output_tokens?: number
  cache_read_tokens?: number
  cache_write_tokens?: number
  cost_usd?: number
  latency_ms?: number
}

export enum Verdict {
  Agree = "agree",
  Disagree = "disagree",
}

export interface TraceSegment {
  type: Type
  content?: string
  tool_name?: string
  tool_input?: string
  tool_result?: string
  /**
   * SDK toolUseId. Joins a stream-folded segment to the evidence the AfterToolCallEvent hook
   * extracted.
   */
  tool_call_id?: string
  /**
   * ToolResult.status as reported by the SDK. On the segment rather than inside evidence because it
   * is SDK-native, not derived.
   */
  status?: Status
  /**
   * Structured provenance for this tool step. Absent on any trace stored before structured evidence
   * was added.
   */
  evidence?: Evidence
  [property: string]: any
}

export enum Type {
  Text = "text",
  Tool = "tool",
}

/**
 * ToolResult.status as reported by the SDK. On the segment rather than inside evidence because it
 * is SDK-native, not derived.
 */
export enum Status {
  Success = "success",
  Error = "error",
}

/**
 * Deterministic provenance for one tool call. Only `kind` is required — an unknown tool falls
 * through to `computation` with no source rather than failing.
 */
export interface Evidence {
  kind: EvidenceKind
  /**
   * ISO timestamp of the tool call.
   */
  at?: string
  /**
   * sap_write only — the three write shapes do not render as the same kind of diff.
   */
  op?: WriteOp
  source?: EvidenceSource
  /**
   * Field values read or written. The timeline renders these, never model prose.
   */
  fields?: EvidenceField[]
  authz?: EvidenceAuthz
  /**
   * sop_lookup only — numbered clauses found in the retrieved SOP text. A human locator, not a
   * verification key: most SOPs carry no numbers, so `citation` is what grades.
   */
  clauses_retrieved?: string[]
  /**
   * notification and case_update only — the SOP sentence the agent quoted, checked against the text
   * this run was given.
   */
  citation?: Citation
  /**
   * sap_write only — SAP's own answer, which `authz` cannot state: a refused write is `permitted`
   * too, because Cedar did allow the call to be made. Absent when the provider returned no
   * `success` flag.
   */
  sap_result?: SapResult
  /**
   * True when the tool_input or tool_result preview hit its size budget.
   */
  truncated?: boolean
  /**
   * notification only — the write the agent is asking permission to make. The one model-supplied
   * key in an otherwise deterministic model, so its `current` values are verified client-side
   * against the run's reads rather than trusted.
   */
  proposed_write?: ProposedWrite
  [property: string]: any
}

/**
 * What the provider said happened, as distinct from what policy allowed. The SAP MCP wrapper
 * reports a rejected write as a *successful tool call* carrying `success: false`, so this is the
 * only thing that separates a write that landed from one SAP refused.
 */
export interface SapResult {
  /**
   * The wrapper's own `success` flag. False means SAP refused the write — the case may still have
   * reached a completed status, because the agent is free to misread the refusal.
   */
  accepted: boolean
  /**
   * The provider's message, truncated. Carries SAP's stated reason on a refusal.
   */
  message?: string
  [property: string]: any
}

/**
 * A quoted SOP sentence and whether it is present in the text the run was given. Absent when the
 * agent quoted nothing gradeable — which is a different state from quoting something absent.
 */
export interface Citation {
  /**
   * The span as the agent wrote it, before normalization.
   */
  quote: string
  /**
   * False means the span is not in what the agent was shown. It is NOT on its own proof the SOP was
   * violated — a compliant agent that paraphrases lands here too, so the render must never present
   * it as one.
   */
  verified: boolean
  /**
   * The number of the SOP clause the quote starts in, as the SOP numbers it (`2.2`, `1.2a`): the
   * nearest clause marker above the quote in the text it verified against (agent/utils/evidence.py,
   * `section_of`). Null when the quote is not verified, sits before the first clause, the SOP
   * numbers nothing, or it was found only in a retrieval chunk whose line breaks were collapsed.
   * Absent on a trace recorded before it existed; read that as null.
   */
  section?: null | string
  [property: string]: any
}

/**
 * A write declared as structured intent at escalation time, so an approval can render as a diff
 * instead of prose.
 */
export interface ProposedWrite {
  /**
   * Shares the record half's discriminator — a proposal and a record describe the same three write
   * shapes.
   */
  op: WriteOp
  service?: string
  entity?: string
  key?: string
  fields: ProposedField[]
  [property: string]: any
}

export interface ProposedField {
  name: string
  /**
   * Absent on a create, and absent whenever the agent did not read the value in this run.
   */
  current?: string
  proposed: string
  [property: string]: any
}

/**
 * Drives the timeline's row rendering, so the UI never pattern-matches tool names.
 */
export enum EvidenceKind {
  SapRead = "sap_read",
  SapWrite = "sap_write",
  SopLookup = "sop_lookup",
  CaseUpdate = "case_update",
  Notification = "notification",
  Computation = "computation",
}

export enum WriteOp {
  Update = "update",
  Create = "create",
  FunctionImport = "function_import",
}

/**
 * Where a value came from in SAP.
 */
export interface EvidenceSource {
  service?: string
  entity?: string
  key?: string
  [property: string]: any
}

export interface EvidenceField {
  name: string
  value?: string
  [property: string]: any
}

/**
 * The authorization facts that are actually available. The matched Cedar policy id is not
 * obtainable from the Gateway.
 */
export interface EvidenceAuthz {
  /**
   * The deployment's Cedar mode: OFF (not enabled) and LOG_ONLY refuse nothing, ENFORCE does. The
   * run checks refuse in every mode.
   */
  mode?: Mode
  /**
   * Absent when a call failed for a non-authorization reason.
   */
  outcome?: Outcome
  [property: string]: any
}

/**
 * The deployment's Cedar mode: OFF (not enabled) and LOG_ONLY refuse nothing, ENFORCE does. The run
 * checks refuse in every mode.
 */
export enum Mode {
  Off = "OFF",
  LogOnly = "LOG_ONLY",
  Enforce = "ENFORCE",
}

/**
 * Absent when a call failed for a non-authorization reason.
 */
export enum Outcome {
  Permitted = "permitted",
  Rejected = "rejected",
}

/**
 * Per-case accumulated agent cost and token counts, summed across every invocation.
 */
export interface CostSummary {
  total_cost_usd?: number
  total_input_tokens?: number
  total_output_tokens?: number
  total_cache_read_tokens?: number
  invocation_count?: number
  [property: string]: any
}

/**
 * A snapshot of the run in flight, so a tab that opens or reloads mid-run renders the turn instead
 * of a gap. Best-effort and self-expiring: nothing sweeps it, so a stale updated_at is the only
 * interruption signal there is.
 */
export interface LiveRun {
  /**
   * AG-UI run id. Recorded on the stored trace as correlation_id, which is what a reader dedupes
   * the live turn against — the trace's own trace_id is a different value.
   */
  run_id: string
  trigger?: string
  /**
   * The prompt as sent, same as AgentTrace.prompt — a reader strips the client's scaffolding the
   * same way.
   */
  prompt?: string
  started_at: string
  /**
   * Bumped on every throttled write. Older than `x-stale-after-seconds` and the run is gone.
   */
  updated_at: string
  segments: TraceSegment[]
  /**
   * Segments trimmed from the front by the 40-segment cap. Absent when nothing was dropped.
   */
  segments_dropped?: number
  [property: string]: any
}

/**
 * Case-level resolution quality rating from a human reviewer.
 */
export enum UserRating {
  Positive = "positive",
  Negative = "negative",
}

export interface CaseRating {
  rating: UserRating
  /**
   * The rater's display claim (email first) when they rated.
   */
  name: string
  /**
   * ISO timestamp of when the rating was submitted.
   */
  at: string
  /**
   * Optional comment explaining the rating, at most 5000 characters.
   */
  comment?: string
  /**
   * Set when the rater may author the case's pack. Only such a rater's comment on a negative rating
   * becomes a lesson (exemplar builder).
   */
  author?: boolean
}

/**
 * A seed the seeder recorded, as the case it made carries it.
 */
export interface TestDataMark {
  /**
   * `<UTC second>#<8 hex>`, the seeder's record of the post.
   */
  post_id: string
  posted_at: string
  /**
   * The console caller's name from their token, or what `ops.py seed` said about itself.
   */
  posted_by?: string
  /**
   * The label of the seed preset the inputs started from, when they did.
   */
  preset?: string
  /**
   * The seed's inputs as posted.
   */
  inputs?: { [key: string]: any }
  /**
   * What the pack's checker and this stack's limits said the case should end as, when posted; null
   * for a pack with no checker.
   */
  expected?: null | { [key: string]: any }
  [property: string]: any
}

export interface DecisionOptions {
  /**
   * sha256 of the options' canonical JSON, first 16 hex characters. A decision is recorded only
   * while the case still offers the set it chose from.
   */
  set_id: string
  recorded_at: string
  options: DecisionOption[]
}

export interface DecisionOption {
  id: string
  /**
   * At most 80 characters.
   */
  label: string
  /**
   * One line, at most 240 characters, on what happens when this option is chosen.
   */
  effect: string
  /**
   * The exact write this option makes. Absent for an outcome that makes none.
   */
  write?: ProposedWrite
}

export const CASE_STATUSES = [
  "detected",
  "processing",
  "awaiting_human_input",
  "sap_updated",
  "complete",
  "manual_review_required",
  "error",
] as const
export const PLATFORM_CASE_STATUSES = [
  "detected",
  "processing",
] as const
export const CASE_ID_PATTERN = "^[a-z][a-z0-9]{1,7}-[a-z2-7]{26}$" as const
export const PACK_ID_PATTERN = "^[a-z][a-z0-9_]*$" as const
export const SHORT_ID_PATTERN = "^[a-z][a-z0-9]{1,7}$" as const
export const LIVE_RUN_STALE_SECONDS = 60 as const
