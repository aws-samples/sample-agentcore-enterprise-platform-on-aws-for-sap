// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, afterEach } from "vitest"
import { render, screen, cleanup, fireEvent } from "@testing-library/react"
import { MemoryRouter } from "react-router"
import { CaseTimeline, HumanDecisions } from "@/components/case/CaseTimeline"
import { TONE_BADGE } from "@/lib/statusTone"
import { TraceSegmentType } from "@/types/cases"
import { TRANSPORTS } from "@/lib/transports"
import type { ActionLogEntry, AgentTrace, TraceSegment } from "@/types/cases"

function toolSegment(evidence: Record<string, unknown> | undefined, extra = {}): TraceSegment {
  return {
    type: TraceSegmentType.Tool,
    tool_name: "odata_read",
    tool_input: '{"entity_set":"A_SupplierInvoice"}',
    tool_result: '{"InvoiceGrossAmount":"1250.00"}',
    ...(evidence ? { evidence } : {}),
    ...extra,
  } as TraceSegment
}

const READ_EVIDENCE = {
  kind: "sap_read",
  at: "2026-07-01T10:00:00Z",
  source: { service: "API_SUPPLIERINVOICE", entity: "A_SupplierInvoice", key: "5100001976" },
  fields: [{ name: "InvoiceGrossAmount", value: "1250.00" }],
  authz: { mode: "LOG_ONLY", outcome: "permitted" },
}

const WRITE_EVIDENCE = {
  kind: "sap_write",
  op: "update",
  source: { entity: "A_PurchaseOrderItem", key: "4500000123/10" },
  fields: [{ name: "NetPriceAmount", value: "12.50" }],
  authz: { mode: "ENFORCE", outcome: "permitted" },
}

function trace(overrides: Partial<AgentTrace> = {}): AgentTrace {
  return {
    trace_id: "trace-latest",
    timestamp: "2026-07-01T10:00:00Z",
    trigger: "poller",
    outcome: "complete",
    segments: [
      { type: TraceSegmentType.Text, content: "Looking up the invoice." } as TraceSegment,
      toolSegment(READ_EVIDENCE),
      toolSegment(WRITE_EVIDENCE, { tool_name: "odata_update" }),
      {
        type: TraceSegmentType.Text,
        content: "Variance within tolerance; released.",
      } as TraceSegment,
    ],
    ...overrides,
  }
}

afterEach(cleanup)

describe("CaseTimeline", () => {
  it("states the zero state and how to leave it", () => {
    render(<CaseTimeline traces={[]} />)
    expect(screen.getByText("No processing history yet.")).toBeInTheDocument()
    expect(screen.getByText(/Process this case/i)).toBeInTheDocument()
  })

  it("shows every step of the latest run without a click", () => {
    render(<CaseTimeline traces={[trace()]} />)
    expect(screen.getByText("Read A_SupplierInvoice 5100001976")).toBeInTheDocument()
    expect(screen.getByText("Updated A_PurchaseOrderItem 4500000123/10")).toBeInTheDocument()
    expect(screen.getByText("Variance within tolerance; released.")).toBeInTheDocument()
  })

  it("names every registered transport rather than falling back to the agent", () => {
    // The fence for the fallback, not for the table: `sap-push` shipped for releases
    // rendering as "Agent", which attributes SAP's own call — or, now, a person's
    // decision — to the agent. An id with no row is `transports.test.ts`'s subject; this
    // one asserts nothing the registry *does* know reaches the fallback.
    for (const trigger of Object.keys(TRANSPORTS)) {
      render(<CaseTimeline traces={[trace({ trigger })]} />)
      expect(screen.getByLabelText("Latest run")).toBeInTheDocument()
      expect(screen.queryByText("Agent"), trigger).not.toBeInTheDocument()
      cleanup()
    }
  })

  it("renders field values in mono, out of evidence", () => {
    render(<CaseTimeline traces={[trace()]} />)
    // The headline is visible without a click; the field values are one level in.
    fireEvent.click(screen.getByRole("button", { name: /Read A_SupplierInvoice/ }))
    expect(screen.getByText("1250.00")).toHaveClass("font-mono")
  })

  it("expands a write into a before/after diff against the read that preceded it", () => {
    const po = { service: "API_PO", entity: "A_PurchaseOrderItem", key: "4500000123/10" }
    const diffable = trace({
      segments: [
        toolSegment({
          kind: "sap_read",
          source: po,
          fields: [{ name: "NetPriceAmount", value: "12.00" }],
        }),
        toolSegment({ ...WRITE_EVIDENCE, source: po }, { tool_name: "odata_update" }),
      ],
    })
    render(<CaseTimeline traces={[diffable]} />)
    fireEvent.click(screen.getByRole("button", { name: /Updated A_PurchaseOrderItem/ }))
    expect(screen.getByText("Applied")).toBeInTheDocument()
    expect(screen.getByText("12.00")).toHaveClass("font-mono")
    expect(screen.getByText("12.50")).toHaveClass("font-mono")
  })

  it("expands a ticket carrying a proposed write into a proposal diff", () => {
    const asking = trace({
      segments: [
        toolSegment({
          kind: "notification",
          proposed_write: {
            op: "update",
            entity: "A_SupplierInvoice",
            key: "5100001976",
            fields: [{ name: "PaymentBlockingReason", current: "A", proposed: "" }],
          },
        }),
      ],
    })
    render(<CaseTimeline traces={[asking]} />)
    fireEvent.click(screen.getByRole("button", { name: /Notification sent/ }))
    expect(screen.getByText("Proposed")).toBeInTheDocument()
    expect(screen.getByText("PaymentBlockingReason")).toBeInTheDocument()
    expect(screen.getByText("A")).toHaveClass("font-mono")
  })

  it("collapses reasoning behind a disclosure and leaves the conclusion visible", () => {
    render(<CaseTimeline traces={[trace()]} />)
    expect(screen.queryByText("Looking up the invoice.")).not.toBeInTheDocument()
    const toggle = screen.getByRole("button", { name: /Reasoning \(1 step\)/ })
    expect(toggle).toHaveAttribute("aria-expanded", "false")
    fireEvent.click(toggle)
    expect(screen.getByText("Looking up the invoice.")).toBeInTheDocument()
  })

  it("puts the authz chip on the write row only", () => {
    render(<CaseTimeline traces={[trace()]} />)
    expect(screen.getAllByText("Cedar: permitted")).toHaveLength(1)
  })

  it("reports the Cedar decision, not just the mode it ran in", () => {
    const denied = trace({
      segments: [
        toolSegment(
          { ...WRITE_EVIDENCE, authz: { mode: "ENFORCE", outcome: "rejected" } },
          { tool_name: "odata_update", status: "error" }
        ),
      ],
    })
    render(<CaseTimeline traces={[denied]} />)
    // A rejected write and a permitted one previously read identically — both said
    // "Cedar: enforced", which is the mode, not the decision.
    // The tone is the claim, not the palette weight that happens to carry it.
    expect(screen.getByText("Cedar: rejected")).toHaveClass(TONE_BADGE.danger)
  })

  it("says a log-only permit did not gate the write", () => {
    const logged = trace({
      segments: [
        toolSegment(
          {
            ...WRITE_EVIDENCE,
            authz: { mode: "LOG_ONLY", outcome: "permitted" },
          },
          { tool_name: "odata_update" }
        ),
      ],
    })
    render(<CaseTimeline traces={[logged]} />)
    // Permitted, but the permit was advisory — so it must not take the success tone.
    const chip = screen.getByText(/Cedar: permitted/)
    expect(chip.textContent).toContain("(log only)")
    expect(chip).not.toHaveClass("bg-emerald-50")
  })

  it("distinguishes a write Cedar never saw from one it logged", () => {
    const off = trace({
      segments: [
        toolSegment(
          { ...WRITE_EVIDENCE, authz: { mode: "OFF", outcome: "permitted" } },
          { tool_name: "odata_update" }
        ),
        toolSegment(
          { ...WRITE_EVIDENCE, authz: { mode: "OFF", outcome: "rejected" } },
          { tool_name: "odata_function_import" }
        ),
      ],
    })
    render(<CaseTimeline traces={[off]} />)
    // Cedar off is not "logged only": nothing evaluated it. The run checks still refuse, and
    // a refusal is the Gateway's.
    expect(screen.getByText("Cedar: off")).toBeInTheDocument()
    expect(screen.getByText("Gateway: refused")).toBeInTheDocument()
  })

  it("does not report a non-authorization failure as a denial", () => {
    const timedOut = trace({
      segments: [
        toolSegment(
          { ...WRITE_EVIDENCE, authz: { mode: "ENFORCE" } },
          { tool_name: "odata_update", status: "error" }
        ),
      ],
    })
    render(<CaseTimeline traces={[timedOut]} />)
    expect(screen.getByText("Cedar: no decision")).toBeInTheDocument()
    expect(screen.queryByText("Cedar: rejected")).not.toBeInTheDocument()
  })

  it("marks a recorded tool failure as failed", () => {
    const failing = trace({
      segments: [toolSegment(READ_EVIDENCE, { status: "error" })],
    })
    render(<CaseTimeline traces={[failing]} />)
    expect(screen.getByLabelText("Step failed")).toBeInTheDocument()
  })

  it("leaves an unrecorded status unmarked rather than flagging it", () => {
    render(<CaseTimeline traces={[trace({ segments: [toolSegment(READ_EVIDENCE)] })]} />)
    expect(screen.queryByLabelText("Step failed")).not.toBeInTheDocument()
  })

  it("claims no decision when a log-only evaluation recorded no outcome", () => {
    const logged = trace({
      segments: [toolSegment({ ...WRITE_EVIDENCE, authz: { mode: "LOG_ONLY" } })],
    })
    render(<CaseTimeline traces={[logged]} />)
    // Cedar ran, but nothing came back to report. Naming the mode alone would imply
    // the write was cleared by a policy that never returned a verdict.
    expect(screen.getByText("Cedar: no decision")).toBeInTheDocument()
  })

  it("treats a stored non-array fields value as no fields", () => {
    // DynamoDB JSON, and `Evidence`'s index signature admits this. Asserting on the
    // disclosure rather than the headline: unguarded, a scalar's truthy `.length` opens
    // a row whose panel throws in `.map`.
    const malformed = trace({
      segments: [toolSegment({ kind: "sap_read", fields: "InvoiceGrossAmount" })],
    })
    render(<CaseTimeline traces={[malformed]} />)
    expect(screen.getByRole("button", { name: /Read from SAP/ })).not.toHaveAttribute(
      "aria-expanded"
    )
  })

  it("opens a row whose only detail is the sentence the agent quoted", () => {
    // The whole point of marking a citation is lost if the row holding it will not open.
    const quoted = trace({
      segments: [
        toolSegment({
          kind: "notification",
          citation: {
            quote: "Variance up to 5% MAY be released without escalation.",
            verified: true,
          },
        }),
      ],
    })
    render(<CaseTimeline traces={[quoted]} />)
    const row = screen.getByRole("button", { name: /Notification sent/ })
    expect(row).toHaveAttribute("aria-expanded", "false")
    fireEvent.click(row)
    expect(
      screen.getByText("Variance up to 5% MAY be released without escalation.")
    ).toBeInTheDocument()
  })

  it("links a quote to the SOP the run recorded, and leaves it unlinked when there is none", () => {
    const QUOTE = "Variance up to 5% MAY be released without escalation."
    const quoted = (extra: Partial<AgentTrace>) =>
      trace({
        segments: [
          toolSegment({ kind: "notification", citation: { quote: QUOTE, verified: true } }),
        ],
        ...extra,
      })

    // `sop_key` is on the trace, not the evidence, so this is the one assertion that the
    // href is threaded from the run down through the row rather than read off the citation.
    const { unmount } = render(
      <MemoryRouter>
        <CaseTimeline traces={[quoted({ sop_key: "finance_ops/invoice_matching.txt" })]} />
      </MemoryRouter>
    )
    fireEvent.click(screen.getByRole("button", { name: /Notification sent/ }))
    expect(screen.getByRole("link", { name: QUOTE })).toHaveAttribute(
      "href",
      "/packs?doc=finance_ops%2Finvoice_matching.txt"
    )
    unmount()

    // A run whose SOP did not load stores no key. The quote still renders — it is the
    // agent's claim either way — but nothing offers to open a document that is not there.
    render(
      <MemoryRouter>
        <CaseTimeline traces={[quoted({})]} />
      </MemoryRouter>
    )
    fireEvent.click(screen.getByRole("button", { name: /Notification sent/ }))
    expect(screen.getByText(QUOTE)).toBeInTheDocument()
    expect(screen.queryByRole("link", { name: QUOTE })).toBeNull()
  })

  it("treats a stored non-object citation as no citation", () => {
    // Same index signature as `fields`: a stored string would open the row on a highlighter
    // with nothing in it, which reads as a quote the agent never made.
    const malformed = trace({
      segments: [toolSegment({ kind: "notification", citation: "Variance up to 5%" })],
    })
    render(<CaseTimeline traces={[malformed]} />)
    expect(screen.getByRole("button", { name: /Notification sent/ })).not.toHaveAttribute(
      "aria-expanded"
    )
  })

  it("renders a trace with no segments without throwing", () => {
    const empty = trace({ segments: undefined as unknown as TraceSegment[] })
    render(<CaseTimeline traces={[empty]} />)
    expect(screen.getByText("This run recorded no steps.")).toBeInTheDocument()
  })

  it("does not claim a disclosure on a row with nothing behind it", () => {
    const bare = trace({
      segments: [toolSegment({ kind: "sap_write", op: "update" })],
    })
    render(<CaseTimeline traces={[bare]} />)
    expect(screen.getByRole("button", { name: /Updated to SAP/ })).not.toHaveAttribute(
      "aria-expanded"
    )
  })

  it("renders a pre-evidence trace on the tool-call path without error", () => {
    const legacy = trace({ segments: [toolSegment(undefined)] })
    render(<CaseTimeline traces={[legacy]} />)
    // This branch hands off to the chat surface's trace tick, which labels a call off
    // its name and arguments because a live turn has no evidence yet — so the row reads
    // "read A_SupplierInvoice" rather than printing the bare tool name at an operator.
    expect(screen.getByText("read")).toBeInTheDocument()
    expect(screen.getByText("A_SupplierInvoice")).toBeInTheDocument()
  })

  it("keeps prior runs collapsed under a date label", () => {
    const older = trace({
      trace_id: "trace-older",
      timestamp: "2026-06-28T09:00:00Z",
      segments: [toolSegment(READ_EVIDENCE, { tool_name: "odata_count" })],
    })
    render(<CaseTimeline traces={[trace(), older]} />)
    const runToggle = screen.getByRole("button", { name: /Jun 28/ })
    expect(runToggle).toHaveAttribute("aria-expanded", "false")
    fireEvent.click(runToggle)
    expect(runToggle).toHaveAttribute("aria-expanded", "true")
  })

  it("labels the trigger of the latest run", () => {
    render(<CaseTimeline traces={[trace()]} />)
    expect(screen.getByText("Poller")).toBeInTheDocument()
  })

  it("dates the latest run, not just its time of day", () => {
    // Otherwise a case opened days after processing reads the bare time as today.
    render(<CaseTimeline traces={[trace()]} />)
    expect(screen.getByText(/Jul 1/)).toBeInTheDocument()
  })
})

describe("the SOP and the humans behind a run", () => {
  afterEach(cleanup)

  it("names the SOP the run read, at its version, linked to its text", () => {
    render(
      <MemoryRouter>
        <CaseTimeline
          traces={[trace({ sop_key: "finance_ops/invoice_matching.txt", sop_version: "2.8" })]}
        />
      </MemoryRouter>
    )
    const link = screen.getByRole("link", { name: "Invoice matching SOP, version 2.8" })
    expect(link.getAttribute("href")).toBe("/packs?doc=finance_ops%2Finvoice_matching.txt")
  })

  it("lists each human decision with its approver and the write it authorized", () => {
    render(
      <HumanDecisions
        log={[
          { action: "awaiting_human_input", timestamp: "2026-11-03T08:36:56Z" },
          {
            action: "human_approved",
            timestamp: "2026-11-03T08:39:35Z",
            actor: "sub-1",
            actor_display: "clerk@example.com",
            source: "ticket" as ActionLogEntry["source"],
            approved_write: "8330e86b7d3c57e494c24d157d1176beecb6c30ffccfa22dbb64c62448e120e3",
          },
        ]}
      />
    )
    const items = screen.getAllByRole("listitem")
    expect(items).toHaveLength(1)
    expect(items[0].textContent).toContain("approved by clerk@example.com via ticket")
    expect(items[0].textContent).toContain("write 8330e86b7d3c")
  })

  it("names the outcome a person chose, and keeps a denial's verb", () => {
    render(
      <HumanDecisions
        log={[
          {
            action: "human_chose",
            timestamp: "2026-11-24T08:39:35Z",
            actor: "sub-1",
            actor_display: "participant@example.com",
            source: "ticket" as ActionLogEntry["source"],
            option: "hold",
            option_label: "Approve HOLD",
          },
          {
            action: "human_denied",
            timestamp: "2026-11-24T08:40:35Z",
            actor: "sub-2",
            actor_display: "clerk@example.com",
            source: "console" as ActionLogEntry["source"],
          },
        ]}
      />
    )
    const [chose, denied] = screen.getAllByRole("listitem")
    expect(chose.textContent).toContain("Approve HOLD by participant@example.com via ticket")
    expect(chose.textContent).not.toContain("chose")
    expect(denied.textContent).toContain("denied by clerk@example.com via console")
  })

  it("renders nothing when no human decided", () => {
    const { container } = render(<HumanDecisions log={[]} />)
    expect(container.innerHTML).toBe("")
  })
})
