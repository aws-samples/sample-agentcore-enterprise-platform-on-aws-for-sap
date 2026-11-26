// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from "vitest"
import { CaseStatus, TraceSegmentType, type WorkItem } from "@/types/cases"
import { caseState } from "@/lib/caseViews"
import { CASES } from "@/mocks/fixtures"

// Every parked case carries `inquiry_sent_at`, a question or an approval alike: the case
// tool stamps it on each move to awaiting_human_input.
const parked: WorkItem = {
  case_id: "ap-1",
  key: {},
  pack_id: "finance_ops",
  process_type: "invoice_matching",
  status: CaseStatus.AwaitingHumanInput,
  created_at: "2026-11-19T10:00:00Z",
  updated_at: "2026-11-19T10:00:00Z",
  inquiry_sent_at: "2026-11-19T10:00:00Z",
}

const withProposal: WorkItem = {
  ...parked,
  agent_traces: [
    {
      trace_id: "t",
      timestamp: "2026-11-19T10:00:00Z",
      segments: [
        {
          type: TraceSegmentType.Tool,
          evidence: {
            kind: "notification",
            proposed_write: { op: "update", service: "S", entity: "E", key: "1", fields: [] },
          },
        },
      ],
    },
  ] as unknown as WorkItem["agent_traces"],
}

describe("caseState", () => {
  it("reads the case's ticket kind: an approval needs approval, a question a reply", () => {
    expect(caseState({ ...parked, ticket_response_type: "approval" }).label).toBe("Needs approval")
    expect(caseState({ ...parked, ticket_response_type: "free_text" }).label).toBe("Needs a reply")
  })

  it("without a ticket kind, a case offering outcomes or a write needs approval", () => {
    const options = {
      decision_options: {
        set_id: "s",
        recorded_at: "2026-11-19T10:00:00Z",
        options: [{ id: "hold", label: "Approve HOLD", effect: "Keeps the hold." }],
      },
    } as Partial<WorkItem>
    expect(caseState({ ...parked, ...options }).label).toBe("Needs approval")
    expect(caseState(withProposal).label).toBe("Needs approval")
    expect(caseState(parked).label).toBe("Needs a reply")
  })

  it("labels the mock approvals and the mock question as the console shows them", () => {
    const parkedMocks = CASES.filter(c => c.status === CaseStatus.AwaitingHumanInput)
    expect(parkedMocks.every(c => c.inquiry_sent_at)).toBe(true)
    expect(parkedMocks.map(c => caseState(c).label).sort()).toEqual([
      "Needs a reply",
      "Needs approval",
      "Needs approval",
    ])
  })
})
