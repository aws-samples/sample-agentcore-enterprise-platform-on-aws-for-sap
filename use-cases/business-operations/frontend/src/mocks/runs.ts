// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Recorded runs mock mode replays, so Process, Re-run, Rehearse, a decision and the assistant
 * stream as they do on a stack: the same AG-UI events, on the same run channel, into the same
 * run client and transcript store.
 *
 * Where they came from. Each run below keeps the step order, the tool names, each step's
 * status and the outcome of a recorded trace in `evals/trace_fixtures/`:
 *
 * - `escalate` from `5100001581-2026.json` (price variance escalated for approval: the case
 *   state, three reads, three calculations, a notification, the case update);
 * - `release` from the same trace, with the escalation replaced by the release the SOP makes
 *   under the limit;
 * - `manualReview` from `ap-7eqptcavu6pboqpe4zncibp5x3.json`, its first trace (no goods
 *   receipt: three failed purchase order reads, SOP lookups, the hand-off);
 * - `resume` from the same file's second trace (the run after a person's decision);
 * - `refused` from `5100001583-2026.json` (the case read refused, nothing written).
 *
 * Those fixtures elide every value (`<elided 273 chars>`), and the console reads the values:
 * tool inputs, results, evidence and the agent's words. So every value here was written by
 * hand from the demo case in `fixtures.ts`, and the rehearsal and the assistant's answer,
 * which no fixture records, were extended by hand from `escalate`'s steps.
 */
import {
  EvidenceKind,
  SegmentStatus,
  Type as SegmentType,
  WriteOp,
  type TraceSegment,
  type WorkItem,
} from "@/types/cases"
import { CONTACTS, priceVarianceOptions } from "./fixtures"

const SAP = "API_SUPPLIERINVOICE_PROCESS_SRV"
const PO = "API_PURCHASEORDER_PROCESS_SRV"

let ids = 0
const toolId = () => `tooluse_mock${(++ids).toString(36).padStart(6, "0")}`

function tool(
  name: string,
  input: Record<string, unknown>,
  result: unknown,
  extra: Partial<TraceSegment> = {}
): TraceSegment {
  return {
    type: SegmentType.Tool,
    tool_name: name,
    tool_call_id: toolId(),
    tool_input: JSON.stringify(input),
    tool_result: JSON.stringify(result),
    status: SegmentStatus.Success,
    ...extra,
  }
}

const text = (content: string): TraceSegment => ({ type: SegmentType.Text, content })

function caseState(c: WorkItem): TraceSegment {
  return tool(
    "business-operations-cases___get_case_state",
    { case_id: c.case_id },
    { case_id: c.case_id, status: c.status, attributes: c.attributes }
  )
}

function sapRead(entity: string, key: string, fields: [string, string][], service = SAP) {
  return tool(
    "sap-s4___odata_read",
    { service, entity_set: entity, key },
    Object.fromEntries(fields),
    {
      evidence: {
        kind: EvidenceKind.SapRead,
        source: { service, entity, key },
        fields: fields.map(([name, value]) => ({ name, value })),
      },
    }
  )
}

function calc(expression: string, value: string) {
  return tool(
    "calculator",
    { expression },
    { result: value },
    {
      evidence: { kind: EvidenceKind.Computation },
    }
  )
}

function updateCase(c: WorkItem, status: string, reason: string) {
  return tool(
    "business-operations-cases___update_case_state",
    { case_id: c.case_id, status, reason },
    { updated: true, status },
    { evidence: { kind: EvidenceKind.CaseUpdate } }
  )
}

const money = (n: number) =>
  n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })

function facts(c: WorkItem) {
  const doc = c.document_number ?? c.case_id
  const amount = Number(c.attributes?.amount ?? 0)
  const currency = String(c.attributes?.currency ?? "USD")
  const po = `45000${doc.slice(-5)}`
  const key = `${doc}/${c.key?.FiscalYear ?? "2026"}`
  return { doc, amount, currency, po, key, unit: amount / 10, poUnit: (amount / 10) * 0.952 }
}

function invoiceRead(c: WorkItem) {
  const f = facts(c)
  return sapRead("A_SupplierInvoice", f.key, [
    ["SupplierInvoice", f.doc],
    ["InvoiceGrossAmount", f.amount.toFixed(2)],
    ["PaymentBlockingReason", String(c.attributes?.sap_block_reason ?? "R")],
    ["PurchaseOrder", f.po],
  ])
}

function releaseWrite(c: WorkItem) {
  const f = facts(c)
  return tool(
    "sap-s4___odata_function_import",
    { service: SAP, function_name: "Release", key: f.key },
    { accepted: true },
    {
      evidence: {
        kind: EvidenceKind.SapWrite,
        op: WriteOp.FunctionImport,
        source: { service: SAP, entity: "Release", key: f.key },
        sap_result: { accepted: true },
      },
    }
  )
}

/** One recorded run, and what it leaves on the case when it ends. */
export interface RecordedRun {
  segments: TraceSegment[]
  /** The case's fields after the run; none for a run on no case. */
  after?: Partial<WorkItem>
  sopKey?: string
  outcome: string
}

/** `escalate`: above the limit, the SOP asks a person to approve the release. */
export function escalate(c: WorkItem): RecordedRun {
  const f = facts(c)
  const quote =
    "The agent MUST raise an approval ticket to Procurement with PO#, invoice#, and the price difference."
  return {
    sopKey: "finance_ops/price_variance.txt",
    outcome: "awaiting_human_input",
    segments: [
      text(`Reading case ${f.doc} before anything else.`),
      caseState(c),
      text("The SOP for invoice matching starts with the invoice, the order and the receipt."),
      invoiceRead(c),
      sapRead(
        "A_PurchaseOrderItem",
        `${f.po}/10`,
        [
          ["NetPriceAmount", f.poUnit.toFixed(2)],
          ["OrderQuantity", "10"],
        ],
        PO
      ),
      text("The order is found. Checking the goods receipt."),
      sapRead("A_MaterialDocumentItem", `${f.po}/10`, [["QuantityInEntryUnit", "10"]], PO),
      text("Comparing the billed price with the order price."),
      calc(`${f.unit.toFixed(2)} - ${f.poUnit.toFixed(2)}`, (f.unit - f.poUnit).toFixed(2)),
      calc(
        `(${f.unit.toFixed(2)} - ${f.poUnit.toFixed(2)}) / ${f.poUnit.toFixed(2)} * 100`,
        "5.04"
      ),
      calc(`${f.amount.toFixed(2)} > 50000`, "true"),
      text(
        `The invoice bills ${money(f.unit)} per unit against an order price of ${money(f.poUnit)}: 5 % over, above the 2 % tolerance. The release is above the auto-write limit, so a person decides.`
      ),
      tool(
        "business-operations-notifications___send_notification",
        { to: CONTACTS.procurement, subject: `Approval needed: invoice ${f.doc}` },
        { sent: true },
        {
          evidence: {
            kind: EvidenceKind.Notification,
            citation: { quote, verified: true, section: "3.3" },
            proposed_write: {
              op: WriteOp.FunctionImport,
              service: SAP,
              entity: "Release",
              key: f.key,
              fields: [{ name: "PaymentBlockingReason", current: "R", proposed: "" }],
            },
          },
        }
      ),
      updateCase(c, "awaiting_human_input", "Price variance above tolerance; approval requested."),
      text(
        `Invoice ${f.doc} waits for an approver: the price is 5 % above the order and the amount, ${money(f.amount)} ${f.currency}, is above the auto-write limit.`
      ),
    ],
    after: {
      status: "awaiting_human_input" as WorkItem["status"],
      exception_type: "price_variance",
      status_reason: null,
      // Stamped by the case tool on the park, as on a question.
      inquiry_sent_at: new Date().toISOString(),
      // The outcomes the SOP allows, recorded in the update that parks the case (2.23).
      decision_options: priceVarianceOptions(f.doc, f.amount),
    } as Partial<WorkItem>,
  }
}

/** `release`: under the limit, within tolerance, the agent releases the block itself. */
export function release(c: WorkItem): RecordedRun {
  const f = facts(c)
  return {
    sopKey: "finance_ops/price_variance.txt",
    outcome: "complete",
    segments: [
      text(`Reading case ${f.doc} before anything else.`),
      caseState(c),
      invoiceRead(c),
      sapRead(
        "A_PurchaseOrderItem",
        `${f.po}/10`,
        [
          ["NetPriceAmount", f.unit.toFixed(2)],
          ["OrderQuantity", "10"],
        ],
        PO
      ),
      sapRead("A_MaterialDocumentItem", `${f.po}/10`, [["QuantityInEntryUnit", "10"]], PO),
      calc(`${f.unit.toFixed(2)} - ${f.unit.toFixed(2)}`, "0.00"),
      text("Price and quantity match the order and the receipt, so the SOP releases the block."),
      releaseWrite(c),
      updateCase(c, "complete", "Released: invoice matches the order and the receipt."),
      text(`Released invoice ${f.doc}. It matched the order and the goods receipt.`),
    ],
    after: {
      status: "complete" as WorkItem["status"],
      disposition: "released",
      exception_type: "price_variance",
      status_reason: null,
    },
  }
}

/** `manualReview`: no purchase order to match against, so a person takes it. */
export function manualReview(c: WorkItem): RecordedRun {
  const f = facts(c)
  const failed = () =>
    tool(
      "sap-s4___odata_read",
      { service: PO, entity_set: "A_PurchaseOrder", key: f.po },
      { error: "404: resource not found" },
      {
        status: SegmentStatus.Error,
        evidence: {
          kind: EvidenceKind.SapRead,
          source: { service: PO, entity: "A_PurchaseOrder", key: f.po },
          sap_result: { accepted: false, message: `Purchase order ${f.po} not found` },
        },
      }
    )
  const search = (query: string) =>
    tool(
      "business-operations-sops___search_sap_sops",
      { query },
      { hits: 2 },
      {
        evidence: { kind: EvidenceKind.SopLookup },
      }
    )
  return {
    sopKey: "finance_ops/missing_po.txt",
    outcome: "manual_review_required",
    segments: [
      caseState(c),
      text("The invoice names a purchase order. Checking the block first."),
      calc(`${f.amount.toFixed(2)} * 1`, f.amount.toFixed(2)),
      search("blocked invoice purchase order missing"),
      text("Reading the purchase order the invoice names."),
      failed(),
      failed(),
      failed(),
      search("purchase order not found"),
      tool(
        "business-operations-docs___search_sap_api_docs",
        { query: "A_PurchaseOrder" },
        { hits: 1 }
      ),
      calc("3 >= 3", "true"),
      tool(
        "business-operations-docs___search_sap_api_docs",
        { query: "PurchaseOrder key" },
        { hits: 1 }
      ),
      tool(
        "business-operations-sops___load_sop",
        { process_type: "missing_po" },
        { loaded: "missing_po" }
      ),
      search("missing purchase order hand-off"),
      tool(
        "business-operations-sops___load_sop",
        { process_type: "invoice_matching" },
        { loaded: "invoice_matching" }
      ),
      text("Without the order there is nothing to match, so the SOP hands the case to a person."),
      updateCase(c, "manual_review_required", `Purchase order ${f.po} could not be read.`),
      text(`Invoice ${f.doc} needs a person: purchase order ${f.po} could not be found in SAP.`),
    ],
    after: {
      status: "manual_review_required" as WorkItem["status"],
      exception_type: "missing_po",
      disposition: "escalated",
      status_reason: `Purchase order ${f.po} could not be read.`,
    },
  }
}

/** `resume`: the run a person's decision starts. An approval releases; a denial hands over. */
export function resume(c: WorkItem, decision: string, by: string): RecordedRun {
  const f = facts(c)
  if (decision === "denied")
    return {
      outcome: "manual_review_required",
      segments: [
        text("Resuming with the approver's decision."),
        caseState(c),
        updateCase(c, "manual_review_required", `Denied by ${by}.`),
        text(`Invoice ${f.doc} stays blocked. ${by} denied the release; it goes to the AP team.`),
      ],
      after: {
        status: "manual_review_required" as WorkItem["status"],
        disposition: "escalated",
        status_reason: `Denied by ${by}.`,
      },
    }
  return {
    outcome: "complete",
    segments: [
      text("Resuming with the approver's decision."),
      caseState(c),
      invoiceRead(c),
      releaseWrite(c),
      updateCase(c, "sap_updated", `Released after approval by ${by}.`),
      text(
        `Released invoice ${f.doc} after ${by}'s ${decision === "approved" ? "approval" : "reply"}.`
      ),
    ],
    after: {
      status: "sap_updated" as WorkItem["status"],
      disposition: "released",
      status_reason: null,
    },
  }
}

/** `refused`: the case read is refused, and the run writes nothing. */
export function refusedRun(c: WorkItem): RecordedRun {
  return {
    outcome: "refused",
    segments: [
      tool(
        "business-operations-cases___get_case_state",
        { case_id: c.case_id },
        { error: "This case's pack is not in scope for this run" },
        { status: SegmentStatus.Error }
      ),
      text("The case could not be read in this run's scope, so nothing was changed."),
    ],
  }
}

/** A rehearsal: `escalate`'s reads, with the write the SOP would make withheld. */
export function rehearsal(prompt: string): RecordedRun {
  const c = {
    case_id: "rehearsal",
    document_number: "5100009999",
    key: { FiscalYear: "2026" },
    attributes: { amount: 52500, currency: "USD", sap_block_reason: "R" },
  } as unknown as WorkItem
  const f = facts(c)
  return {
    outcome: "complete",
    segments: [
      text(`Rehearsing: ${prompt.slice(0, 120)}`),
      invoiceRead(c),
      sapRead(
        "A_PurchaseOrderItem",
        `${f.po}/10`,
        [
          ["NetPriceAmount", f.poUnit.toFixed(2)],
          ["OrderQuantity", "10"],
        ],
        PO
      ),
      calc(
        `(${f.unit.toFixed(2)} - ${f.poUnit.toFixed(2)}) / ${f.poUnit.toFixed(2)} * 100`,
        "5.04"
      ),
      tool(
        "sap-s4___odata_function_import",
        { service: SAP, function_name: "Release", key: f.key },
        { withheld: "A rehearsal sends nothing that changes SAP, a case or a ticket." },
        { status: SegmentStatus.Error }
      ),
      text(
        "Under this SOP the price is 5 % over the order, above the 2 % tolerance, so the agent would ask Procurement to approve the release. Nothing was sent."
      ),
    ],
  }
}

/** The assistant's answer: what it read, then its words. Asking writes nothing. */
export function answer(prompt: string, c: WorkItem | undefined, open: WorkItem[]): RecordedRun {
  if (c) {
    const f = facts(c)
    return {
      outcome: "complete",
      segments: [
        caseState(c),
        invoiceRead(c),
        text(
          `Invoice ${f.doc} is ${String(c.status).replace(/_/g, " ")}. It bills ${money(f.amount)} ${f.currency}${c.status_reason ? `; ${c.status_reason}` : ""}. To act on it, use Process or the decision on the case.`
        ),
      ],
    }
  }
  const waiting = open.filter(x => x.status === "awaiting_human_input").length
  const fresh = open.filter(x => x.status === "detected").length
  const briefing = /brief|today|shift|needs me/i.test(prompt)
  return {
    outcome: "complete",
    segments: [
      text(
        briefing
          ? `Good morning. ${waiting} case${waiting === 1 ? "" : "s"} wait for a person, the largest above the auto-write limit and waiting for an approver. ${fresh} new case${fresh === 1 ? " has" : "s have"} not started yet. Start with the approvals in Needs you.`
          : `I read the cases you can see: ${waiting} wait for a person and ${fresh} have not started. Ask about one by its document number for the detail.`
      ),
    ],
  }
}

/** The run Process starts on a case, picked from the case as the SOP would route it. */
export function processRun(c: WorkItem, autoLimit: number): RecordedRun {
  if (c.status === "error" && c.status_reason?.includes("refused")) return refusedRun(c)
  if (c.attributes?.sap_block_reason === "A") return manualReview(c)
  const amount = Number(c.attributes?.amount ?? 0)
  return amount > autoLimit ? escalate(c) : release(c)
}
