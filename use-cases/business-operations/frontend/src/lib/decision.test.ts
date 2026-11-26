// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from "vitest"
import { CaseStatus, type WorkItem } from "@/types/cases"
import { AP_PACK } from "@/test/packs"
import type { PackControls } from "@/services/controlsService"
import {
  checksPhrase,
  decisionMode,
  needsTwoApprovers,
  outcomesFor,
  pendingApprovals,
  submitLabel,
} from "@/lib/decision"
import { guardLabel } from "@/lib/packParts"

const base: WorkItem = {
  case_id: "ap-1",
  key: {},
  pack_id: "finance_ops",
  process_type: "invoice_matching",
  status: CaseStatus.AwaitingHumanInput,
  created_at: "2026-11-19T10:00:00Z",
  updated_at: "2026-11-19T10:00:00Z",
  attributes: { amount: 151000, currency: "USD" },
  agent_traces: [{ trace_id: "t", timestamp: "2026-11-19T10:00:00Z", segments: [] }],
}

const limit = (value: number) => ({
  value,
  kind: "limit" as const,
  source: "",
  guarded_by: [],
  ceiling: value,
})
const controls = {
  limits: { AUTO_WRITE_LIMIT_USD: limit(50000), DUAL_APPROVAL_LIMIT_USD: limit(150000) },
  write_guards: ["po_reference", "no_duplicate", "gr_coverage"],
} as unknown as PackControls

describe("A decision", () => {
  it("keeps acronyms upper case in write check names", () => {
    expect(guardLabel("po_reference")).toBe("PO reference")
    expect(guardLabel("gr_coverage")).toBe("GR coverage")
    expect(guardLabel("within_tolerance_or_approved")).toBe("Within tolerance or approved")
    expect(checksPhrase(controls)).toBe("PO reference, no duplicate, GR coverage")
  })

  it("offers the case's own options when it carries them, else approve, deny and reply", () => {
    expect(outcomesFor(base).map(o => o.label)).toEqual(["Approve", "Deny", "Reply only"])
    const withOptions = {
      ...base,
      decision_options: {
        set_id: "3f9c0a1be27d44c1",
        recorded_at: "2026-11-20T09:14:03+00:00",
        options: [
          {
            id: "release",
            label: "Release the invoice",
            effect: "Removes the payment block.",
            write: { op: "update", fields: [] },
          },
          { id: "short_pay", label: "Short pay", effect: "Asks for a credit memo." },
        ],
      },
    } as unknown as WorkItem
    const outcomes = outcomesFor(withOptions)
    expect(outcomes.map(o => [o.label, o.decision, o.optionId])).toEqual([
      ["Release the invoice", "approved", "release"],
      ["Short pay", "chose", "short_pay"],
      ["Reply only", "replied", undefined],
    ])
    expect(submitLabel(outcomes[0], true)).toBe("Release the invoice in production")
    expect(submitLabel(outcomes[1], true)).toBe("Short pay")
  })

  it("says in production on an approval that lands there, and only then", () => {
    const [approve, deny] = outcomesFor(base)
    expect(submitLabel(approve, true)).toBe("Approve in production")
    expect(submitLabel(approve, false)).toBe("Approve")
    expect(submitLabel(deny, true)).toBe("Deny")
  })

  it("needs two approvers above the limit, or in another currency", () => {
    expect(needsTwoApprovers(base, AP_PACK, controls).needed).toBe(true)
    const small = { ...base, attributes: { amount: 1000, currency: "USD" } }
    expect(needsTwoApprovers(small, AP_PACK, controls).needed).toBe(false)
    const euro = { ...base, attributes: { amount: 1000, currency: "EUR" } }
    expect(needsTwoApprovers(euro, AP_PACK, controls).needed).toBe(true)
  })

  it("counts approvals since the last run, one per person, ended by another decision", () => {
    const log = [
      {
        action: "human_approved",
        actor: "a",
        actor_display: "Ann",
        timestamp: "2026-11-19T09:00:00Z",
      },
      { action: "human_denied", actor: "b", timestamp: "2026-11-19T10:01:00Z" },
      {
        action: "human_approved",
        actor: "c",
        actor_display: "Cy",
        timestamp: "2026-11-19T10:02:00Z",
      },
      {
        action: "human_approved",
        actor: "c",
        actor_display: "Cy",
        timestamp: "2026-11-19T10:03:00Z",
      },
    ]
    const approvals = pendingApprovals({ ...base, action_log: log } as unknown as WorkItem)
    expect(approvals.map(a => a.name)).toEqual(["Cy"])
  })

  it("asks for a reply on a question and a decision on an approval, both sent", () => {
    // The case tool stamps `inquiry_sent_at` on every park, so it tells neither apart.
    const sent = { ...base, inquiry_sent_at: "2026-11-19T10:00:00Z" }
    expect(decisionMode({ ...sent, ticket_response_type: "approval" })).toBe("decide")
    expect(decisionMode({ ...sent, ticket_response_type: "free_text" })).toBe("reply")
    expect(decisionMode(sent)).toBe("reply")
    expect(decisionMode({ ...base, status: CaseStatus.Processing })).toBe("idle")
  })
})
