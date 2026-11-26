// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The demo scenario set mock mode serves, in the shapes of the API's own types.
 *
 * The pack is the shipped finance_ops pack, read from its file, so mock mode shows the
 * real fields, rules and procedures. The cases are made up, one per state a screen has to
 * handle: released under the limit, waiting for approval over it, waiting for a second
 * approver, waiting for a reply, denied to manual review, failed on a SAP refusal, and a
 * queue of new cases.
 */
import shippedPack from "../../../packs/finance_ops/pack.json"
import type { UseCasePack, FieldType } from "@/lib/registry"
import {
  CaseStatus,
  EvidenceKind,
  WriteOp,
  SegmentStatus,
  Type as SegmentType,
  type TraceSegment,
  type WorkItem,
} from "@/types/cases"
import { Source } from "@/types/generated-cases"
import { ResponseType, TicketPriority, TicketStatus, type Ticket } from "@/types/generated-tickets"

const PACK_ID = "finance_ops"
const SERVICE = "API_SUPPLIERINVOICE_PROCESS_SRV"

interface ShippedPack {
  display_name: string
  description: string
  constants: Record<string, number>
  entitlement: { groups: string[] }
  process_type_to_sop: Record<string, string>
  shared_sops?: string[]
  source: {
    label: string
    short_id: string
    kind: string
    service: string
    entity: string
    field_map: Record<string, { path: string }>
    key: { field: string }[]
    process_type?: { default?: string }
  }
  fields: Record<
    string,
    { label: string; type: string; currency_field?: string; role?: string; panel?: boolean }
  >
}
const P = shippedPack as unknown as ShippedPack

import basePrompt from "../../../packs/finance_ops/base_prompt.txt?raw"
import shippedEvals from "../../../packs/finance_ops/evals.json"

/** The pack document as `GET /packs/{id}` serves it: the shipped `pack.json`, whole. */
export const PACK_DOC: Record<string, unknown> = structuredClone(
  shippedPack as unknown as Record<string, unknown>
)
/** The shipped example cases, as `evals.json` holds them. */
export const EVALS: Record<string, unknown>[] = structuredClone(
  shippedEvals as unknown as Record<string, unknown>[]
)

/** The pack's documents `GET /packs/{id}/{field}` serves. */
export const PACK_DOCUMENTS: Record<string, string> = { base_prompt: basePrompt }
export const CONSTANTS: Record<string, number> = P.constants as Record<string, number>
export const CONTACTS: Record<string, string> = {
  ap_team: "ap-team@example.com",
  finance_approvals: "finance-approvals@example.com",
  procurement: "procurement@example.com",
  warehouse: "warehouse@example.com",
}
export const ENTITLEMENT_GROUPS: string[] = P.entitlement.groups

/**
 * Each process type's kind, by the rule `GET /registry` derives them with: triage is the
 * source's default type, a key in `shared_sops` is shared, a second key on the same file is
 * an alias of the first (triage's own file aliases triage), and the rest are exception SOPs.
 */
function sopKinds(): UseCasePack["sops"] {
  const triage = P.source.process_type?.default
  const shared = new Set(P.shared_sops ?? [])
  const firstFor = new Map<string, string>()
  const entries = Object.entries(P.process_type_to_sop)
  if (triage) firstFor.set(P.process_type_to_sop[triage], triage)
  for (const [pt, file] of entries) if (shared.has(pt)) firstFor.set(file, pt)
  for (const [pt, file] of entries) if (!firstFor.has(file)) firstFor.set(file, pt)
  return entries.map(([pt, file]) => {
    const owner = firstFor.get(file)!
    if (owner !== pt) return { process_type: pt, kind: "alias", sop: file, alias_of: owner }
    const kind = pt === triage ? "triage" : shared.has(pt) ? "shared" : "exception"
    return { process_type: pt, kind, sop: file }
  })
}

export const PACK: UseCasePack = {
  id: PACK_ID,
  display_name: P.display_name,
  description: P.description,
  label: P.source.label,
  short_id: P.source.short_id,
  process_types: Object.keys(P.process_type_to_sop).sort(),
  sops: sopKinds(),
  sap_system: { name: "s4", system_role: "sandbox" },
  source: {
    kind: P.source.kind,
    service: P.source.service,
    entity: P.source.entity,
    key: P.source.key.map(k => k.field),
  },
  fields: Object.entries(P.fields).map(([attribute, spec]) => ({
    attribute,
    label: spec.label,
    type: spec.type as FieldType,
    sap_field: P.source.field_map[attribute]?.path ?? "",
    ...(spec.currency_field ? { currency_field: spec.currency_field } : {}),
    ...(spec.role ? { role: spec.role as "reason" } : {}),
    ...(spec.panel === false ? { panel: false } : {}),
  })),
}

/** The shipped SOP files, by file name. */
const sopFiles = import.meta.glob("../../../packs/finance_ops/sops/*.txt", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>
export const SOPS: Record<string, string> = Object.fromEntries(
  Object.entries(sopFiles).map(([path, text]) => [`${PACK_ID}/${path.split("/").pop()}`, text])
)

// --- cases -------------------------------------------------------------------------------

const ago = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString()

interface Spec {
  doc: string
  year?: string
  status: CaseStatus
  exception?: string
  amount: number
  currency?: string
  supplier: string
  supplierName: string
  block: string
  opened: number
  updated: number
  disposition?: string
  reason?: string
  segments?: TraceSegment[]
  log?: WorkItem["action_log"]
  ticket?: string
  /** The outcomes the SOP allows, as the agent recorded them when it parked the case (2.23). */
  options?: boolean
  /** The ticket asks a question (free text) rather than for an approval. */
  question?: boolean
  /** Minutes since the case was parked on a person, when not `updated`. */
  inquiry?: number
  cost?: number
  /** Posted from the test data page, so the case carries the seeder's mark. */
  testData?: boolean
}

function read(entity: string, key: string, fields: [string, string][]): TraceSegment {
  return {
    type: SegmentType.Tool,
    tool_name: "sap-s4___odata_read",
    tool_input: JSON.stringify({ service: SERVICE, entity, key }),
    tool_result: JSON.stringify(Object.fromEntries(fields)),
    status: SegmentStatus.Success,
    evidence: {
      kind: EvidenceKind.SapRead,
      source: { service: SERVICE, entity, key },
      fields: fields.map(([name, value]) => ({ name, value })),
    },
  }
}

function text(content: string): TraceSegment {
  return { type: SegmentType.Text, content }
}

function proposal(
  doc: string,
  to: string,
  why: string,
  section: string | null = null
): TraceSegment {
  return {
    type: SegmentType.Tool,
    tool_name: "business-operations-notifications___send_notification",
    tool_input: JSON.stringify({ to, subject: `Approval needed: invoice ${doc}` }),
    tool_result: JSON.stringify({ sent: true }),
    status: SegmentStatus.Success,
    evidence: {
      kind: EvidenceKind.Notification,
      citation: { quote: why, verified: true, section },
      proposed_write: {
        op: WriteOp.FunctionImport,
        service: SERVICE,
        entity: "Release",
        key: `${doc}/2026`,
        fields: [{ name: "PaymentBlockingReason", current: "R", proposed: "" }],
      },
    },
  }
}

function released(doc: string): TraceSegment {
  return {
    type: SegmentType.Tool,
    tool_name: "sap-s4___odata_function_import",
    tool_input: JSON.stringify({ service: SERVICE, function: "Release", key: `${doc}/2026` }),
    tool_result: JSON.stringify({ accepted: true }),
    status: SegmentStatus.Success,
    evidence: {
      kind: EvidenceKind.SapWrite,
      op: WriteOp.FunctionImport,
      source: { service: SERVICE, entity: "Release", key: `${doc}/2026` },
      sap_result: { accepted: true },
    },
  }
}

function refused(doc: string): TraceSegment {
  return {
    type: SegmentType.Tool,
    tool_name: "sap-s4___odata_read",
    tool_input: JSON.stringify({
      service: "API_PURCHASEORDER_PROCESS_SRV",
      entity: "A_PurchaseOrderItem",
    }),
    tool_result: JSON.stringify({ error: "403: not authorized for purchase order 4500000188" }),
    status: SegmentStatus.Error,
    evidence: {
      kind: EvidenceKind.SapRead,
      source: { service: "API_PURCHASEORDER_PROCESS_SRV", entity: "A_PurchaseOrderItem", key: doc },
      sap_result: { accepted: false, message: "Not authorized for purchase order 4500000188" },
    },
  }
}

function invoiceRead(doc: string, amount: number, po: string): TraceSegment {
  return read("A_SupplierInvoice", `${doc}/2026`, [
    ["SupplierInvoice", doc],
    ["InvoiceGrossAmount", amount.toFixed(2)],
    ["PaymentBlockingReason", "R"],
    ["PurchaseOrder", po],
  ])
}

const SPECS: Spec[] = [
  {
    doc: "5100001972",
    status: CaseStatus.Complete,
    exception: "price_variance",
    amount: 4400,
    supplier: "17300002",
    supplierName: "Domestic US Supplier 2",
    block: "R",
    opened: 90,
    updated: 25,
    disposition: "released",
    cost: 0.18,
    segments: [
      invoiceRead("5100001972", 4400, "4500000140"),
      text("The price is within tolerance, so the SOP releases the block."),
      released("5100001972"),
      // The closing summary, in Markdown as the agent writes it: the case page renders it.
      text(
        "Released invoice **5100001972**. The price is **within tolerance**: 0.4 % against a 2 % limit, so no approval was needed."
      ),
    ],
    log: [{ action: "released", timestamp: ago(25), actor: "agent", actor_display: "Agent" }],
  },
  {
    doc: "5100001976",
    status: CaseStatus.AwaitingHumanInput,
    options: true,
    exception: "price_variance",
    amount: 52500,
    supplier: "17300001",
    supplierName: "Domestic US Supplier 1",
    block: "R",
    opened: 120,
    updated: 110,
    ticket: "TKT-1001",
    cost: 0.21,
    segments: [
      invoiceRead("5100001976", 52500, "4500000123"),
      read("A_PurchaseOrderItem", "4500000123/10", [
        ["NetPriceAmount", "5000.00"],
        ["OrderQuantity", "10"],
      ]),
      text(
        "The invoice bills 5,250.00 per unit against a PO price of 5,000.00: 5 % over, above tolerance."
      ),
      proposal(
        "5100001976",
        CONTACTS.procurement,
        "The agent MUST raise an approval ticket to Procurement with PO#, invoice#, and the price difference.",
        "3.3"
      ),
    ],
  },
  {
    doc: "5100002011",
    status: CaseStatus.AwaitingHumanInput,
    exception: "price_variance",
    amount: 151000,
    supplier: "17300001",
    supplierName: "Domestic US Supplier 1",
    block: "R",
    opened: 420,
    updated: 60,
    ticket: "TKT-1002",
    cost: 0.33,
    segments: [
      invoiceRead("5100002011", 151000, "4500000131"),
      proposal(
        "5100002011",
        CONTACTS.finance_approvals,
        "Above the dual approval limit, two approvers are required."
      ),
    ],
    // The first of two approvals, recorded after the run that proposed the write.
    log: [
      {
        action: "human_approved",
        timestamp: ago(50),
        actor: "mock-approver",
        actor_display: "Priya Shah",
        source: Source.Console,
      },
    ],
  },
  {
    doc: "5100001981",
    status: CaseStatus.AwaitingHumanInput,
    exception: "price_variance",
    amount: 18240,
    supplier: "17300002",
    supplierName: "Domestic US Supplier 2",
    block: "R",
    opened: 1500,
    updated: 1440,
    ticket: "TKT-1003",
    question: true,
    inquiry: 1440,
    cost: 0.14,
    segments: [
      invoiceRead("5100001981", 18240, "4500000131"),
      text(
        "The PO references contract 4600000012; the SOP asks Procurement which pricing tier applies."
      ),
    ],
  },
  {
    doc: "5100001994",
    status: CaseStatus.ManualReviewRequired,
    exception: "price_variance",
    amount: 4310,
    currency: "EUR",
    supplier: "17300010",
    supplierName: "Inlandslieferant DE 1",
    block: "R",
    opened: 300,
    updated: 200,
    disposition: "escalated",
    reason: "Denied by Priya Shah: the supplier did not agree the price.",
    cost: 0.12,
    segments: [invoiceRead("5100001994", 4310, "4500000152")],
    log: [
      {
        action: "human_denied",
        timestamp: ago(200),
        actor: "mock-approver",
        actor_display: "Priya Shah",
        source: Source.Console,
      },
    ],
  },
  {
    doc: "5100001952",
    status: CaseStatus.Error,
    amount: 9600,
    supplier: "17300003",
    supplierName: "Domestic US Supplier 3",
    block: "R",
    opened: 600,
    updated: 580,
    reason: "SAP refused a read of purchase order 4500000188.",
    cost: 0.05,
    segments: [invoiceRead("5100001952", 9600, "4500000188"), refused("5100001952")],
  },
  ...["5100002031", "5100002032", "5100002033", "5100002034"].map((doc, i) => ({
    doc,
    status: CaseStatus.Detected,
    amount: [3480, 27000, 860, 12200][i],
    supplier: "17300005",
    supplierName: `Domestic US Supplier ${5 + i}`,
    block: ["R", "R", "A", "R"][i],
    opened: 10 + i * 7,
    updated: 10 + i * 7,
    testData: i === 0,
  })),
  {
    doc: "5100001859",
    year: "2025",
    status: CaseStatus.Detected,
    amount: 12200,
    supplier: "17300003",
    supplierName: "Domestic US Supplier 3",
    block: "R",
    opened: 540,
    updated: 540,
  },
]

/**
 * The price variance SOP's three outcomes, as `update_case_state` records them (2.23): release
 * as billed, which writes; short pay and deny, which write nothing from this decision.
 */
export function priceVarianceOptions(doc: string, amount: number) {
  const supported = Math.round((amount / 1.05) * 100) / 100
  const money = (n: number) =>
    n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  const options = [
    {
      id: "release",
      label: "Approve as billed",
      effect: `Release the invoice at ${money(amount)} USD.`,
      write: {
        op: WriteOp.FunctionImport,
        service: SERVICE,
        entity: "Release",
        key: `${doc}/2026`,
        fields: [{ name: "PaymentBlockingReason", current: "R", proposed: "" }],
      },
    },
    {
      id: "short_pay",
      label: "Short pay",
      effect: `Pay what the PO supports, ${money(supported)} USD; the agent asks the supplier for a credit memo for ${money(amount - supported)} USD.`,
    },
    { id: "deny", label: "Deny", effect: "No change in SAP; the AP team reviews it." },
  ]
  return {
    set_id: `${doc.slice(-4)}a1be27d44c1f`.slice(0, 16),
    recorded_at: new Date().toISOString(),
    options,
  }
}

/** A stable id in the server's format (`<short_id>-` and 26 base32 characters). The real
 *  one is a digest of the key; this one only has to look like it and never collide. */
function fakeCaseId(seed: string): string {
  const alphabet = "abcdefghijklmnopqrstuvwxyz234567"
  let h = 2166136261
  let out = ""
  for (let i = 0; i < 26; i++) {
    h = Math.imul(h ^ seed.charCodeAt(i % seed.length) ^ i, 16777619) >>> 0
    out += alphabet[h % 32]
  }
  return `${P.source.short_id}-${out}`
}

function toCase(s: Spec): WorkItem {
  const year = s.year ?? "2026"
  const caseId = fakeCaseId(`${s.doc}/${year}`)
  return {
    case_id: caseId,
    key: { SupplierInvoice: s.doc, FiscalYear: year },
    document_number: s.doc,
    pack_id: PACK_ID,
    process_type: "invoice_matching",
    ...(s.exception ? { exception_type: s.exception } : {}),
    title: `Invoice ${s.doc} / FY ${year}`,
    status: s.status,
    status_reason: s.reason ?? null,
    disposition: s.disposition ?? null,
    created_at: ago(s.opened),
    updated_at: ago(s.updated),
    ticket_id: s.ticket ?? null,
    ticket_response_type: s.ticket
      ? s.question
        ? ResponseType.FreeText
        : ResponseType.Approval
      : null,
    // Stamped on every park, an approval's as much as a question's.
    inquiry_sent_at:
      s.status === CaseStatus.AwaitingHumanInput ? ago(s.inquiry ?? s.updated) : null,
    attributes: {
      supplier_number: `${s.supplier} ${s.supplierName}`,
      amount: s.amount,
      currency: s.currency ?? "USD",
      sap_block_reason: s.block,
      company_code: s.currency === "EUR" ? "1010" : "1710",
      document_date: ago(s.opened + 1440),
      posting_date: ago(s.opened + 1440),
      external_reference: `INV-${s.doc.slice(-4)}`,
    },
    action_log: [
      { action: "detected", timestamp: ago(s.opened), actor: "poller", actor_display: "Poller" },
      ...(s.log ?? []),
    ],
    agent_traces: s.segments
      ? [
          {
            trace_id: `trace-${s.doc}`,
            timestamp: ago(s.updated),
            trigger: "poller",
            outcome: s.status === CaseStatus.Error ? "error" : "complete",
            sop_key: s.exception
              ? `${PACK_ID}/${s.exception}.txt`
              : `${PACK_ID}/invoice_matching.txt`,
            ...(s.exception ? { exception_type: s.exception } : {}),
            estimated_cost_usd: s.cost ?? 0,
            segments: s.segments,
          },
        ]
      : [],
    cost_summary: { total_cost_usd: s.cost ?? 0, invocation_count: s.segments ? 1 : 0 },
    // The seeder's mark on a case it posted the documents for.
    ...(s.options ? { decision_options: priceVarianceOptions(s.doc, s.amount) } : {}),
    ...(s.testData
      ? {
          test_data: {
            post_id: `${ago(s.opened)}#7c1d02aa`,
            posted_at: ago(s.opened),
            posted_by: "Sam Lee",
            preset: "Price variance 5%",
          },
        }
      : {}),
  }
}

export const CASES: WorkItem[] = SPECS.map(toCase)

// --- tickets -----------------------------------------------------------------------------

export const TICKETS: Ticket[] = CASES.filter(c => c.ticket_id).map(c => {
  const question = c.ticket_response_type === ResponseType.FreeText
  return {
    ticket_id: c.ticket_id!,
    title: `Approval needed: invoice ${c.document_number}`,
    description: "The invoice price is above the purchase order price by more than the tolerance.",
    status: question ? TicketStatus.Open : TicketStatus.Assigned,
    priority: TicketPriority.Medium,
    created_by: "agent",
    assigned_to: question ? CONTACTS.procurement : CONTACTS.finance_approvals,
    case_id: c.case_id,
    category: "approval",
    response_type: question ? ResponseType.FreeText : ResponseType.Approval,
    created_at: c.updated_at,
    updated_at: c.updated_at,
  }
})
