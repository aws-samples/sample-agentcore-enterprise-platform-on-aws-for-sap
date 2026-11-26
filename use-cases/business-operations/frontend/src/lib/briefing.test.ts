// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from "vitest"
import { CaseStatus } from "@/types/cases"
import type { WorkItem } from "@/types/cases"
import type { Ticket } from "@/types/tickets"
import { briefingSentence, chips, greeting, narrationPrompt, queueOrder } from "@/lib/briefing"
import { digest } from "@/lib/digest"
import { AP_PACKS } from "@/test/packs"
import { operatorFirstName, operatorName, operatorIdentities } from "@/lib/identity"

const HOUR = 3_600_000

function workItem(over: Partial<WorkItem>, attributes: Record<string, unknown> = {}): WorkItem {
  return {
    case_id: "DOC1-10",
    pack_id: "finance_ops",
    attributes,
    process_type: "invoice_block",
    status: CaseStatus.AwaitingHumanInput,
    updated_at: new Date(Date.now() - HOUR).toISOString(),
    ...over,
  } as WorkItem
}

function ticket(over: Partial<Ticket>): Ticket {
  return { ticket_id: "T-1", status: "open", assigned_to: "z@e.com", ...over } as Ticket
}

describe("greeting", () => {
  it("tracks the time of day", () => {
    expect(greeting(0)).toBe("Good morning")
    expect(greeting(11)).toBe("Good morning")
    expect(greeting(12)).toBe("Good afternoon")
    expect(greeting(17)).toBe("Good afternoon")
    expect(greeting(18)).toBe("Good evening")
    expect(greeting(23)).toBe("Good evening")
  })
})

describe("operatorName", () => {
  it("prefers name, then the parts, then username, then email, then sub", () => {
    expect(operatorName({ name: "Jane", preferred_username: "jd", email: "z@e.com" })).toBe("Jane")
    // An IdP that sends `name` is stating the full name; the parts do not overrule it.
    expect(operatorName({ name: "Jane D", given_name: "Janet", family_name: "Doe" })).toBe("Jane D")
    expect(operatorName({ given_name: "Jane", family_name: "Doe", email: "z@e.com" })).toBe(
      "Jane Doe"
    )
    expect(operatorName({ given_name: "Jane", email: "z@e.com" })).toBe("Jane")
    // A surname alone composes nothing — it falls through rather than greeting by half a name.
    expect(operatorName({ family_name: "Doe", email: "z@e.com" })).toBe("z@e.com")
    expect(operatorName({ preferred_username: "jd", email: "z@e.com" })).toBe("jd")
    expect(operatorName({ email: "z@e.com" })).toBe("z@e.com")
    expect(operatorName({ sub: "abc-123" })).toBe("abc-123")
  })

  it("returns null rather than a placeholder, so the caller decides", () => {
    expect(operatorName(undefined)).toBeNull()
    expect(operatorName({})).toBeNull()
    // Whitespace is not a name.
    expect(operatorName({ name: "   " })).toBeNull()
  })
})

describe("operatorFirstName", () => {
  it("reads given_name and nothing else", () => {
    expect(operatorFirstName({ given_name: "Jane", email: "jane.doe@example.com" })).toBe("Jane")
    expect(operatorFirstName({ given_name: " Jane " })).toBe("Jane")
  })

  it("returns null rather than falling back, so a greeting drops the name", () => {
    // The regression: "Good morning, jane.doe@example.com". An email local part is not a
    // name, and neither is a sub or a full display name that may be "Lastname, Firstname".
    expect(operatorFirstName({ email: "jane.doe@example.com" })).toBeNull()
    expect(operatorFirstName({ name: "Doe, Jane", preferred_username: "jd" })).toBeNull()
    expect(operatorFirstName({ sub: "abc-123" })).toBeNull()
    expect(operatorFirstName({ given_name: "   " })).toBeNull()
    expect(operatorFirstName(undefined)).toBeNull()
  })
})

describe("operatorIdentities", () => {
  it("lowercases email and username so a ticket match is case-insensitive", () => {
    expect([...operatorIdentities({ email: "Z@E.com", preferred_username: "JD" })]).toEqual([
      "z@e.com",
      "jd",
    ])
  })

  it("excludes sub — an opaque IdP id can only produce a false 'yours'", () => {
    expect(operatorIdentities({ sub: "abc-123", name: "Jane" }).size).toBe(0)
  })
})

describe("the queue is unwindowed while the cleared count is not", () => {
  // The one property two calls to one function can silently lose.
  const cases = [
    workItem({
      case_id: "OLD-1",
      updated_at: new Date(Date.now() - 120 * HOUR).toISOString(),
      inquiry_sent_at: new Date(Date.now() - 120 * HOUR).toISOString(),
    }),
    workItem({
      case_id: "NEW-1",
      status: CaseStatus.Complete,
      agent_traces: [
        { trace_id: "t", timestamp: "2026-08-28T00:00:00Z", segments: [], trigger: "poller" },
      ],
    }),
  ]

  it("keeps a case older than the window in the queue and in the queue's count", () => {
    const backlog = digest(cases, Number.POSITIVE_INFINITY, [])
    const recent = digest(cases, 24, [])

    expect(queueOrder(backlog, new Set()).map(e => e.row.caseId)).toEqual(["OLD-1"])
    expect(backlog.waitingCount).toBe(1)
    // The windowed call cannot see it, which is exactly why the page makes two.
    expect(recent.waitingCount).toBe(0)

    const labels = chips(backlog, recent, 0, "24")
    expect(labels.find(c => c.label === "waiting on someone")?.count).toBe(1)
    expect(labels.find(c => c.label.startsWith("cleared without you"))?.count).toBe(1)
  })

  it("labels the windowed count with the window and the queue count without one", () => {
    const backlog = digest(cases, Number.POSITIVE_INFINITY, [])
    const recent = digest(cases, 24, [])
    const labels = chips(backlog, recent, 0, "24")
    expect(labels.find(c => c.label.includes("last 24 hours"))?.label).toBe(
      "cleared without you in the last 24 hours"
    )
    expect(labels.some(c => c.label === "waiting on someone")).toBe(true)
  })
})

describe("queueOrder", () => {
  it("puts yours first, then oldest first", () => {
    const cases = [
      workItem({
        case_id: "MINE-NEW",
        ticket_id: "T-MINE",
        inquiry_sent_at: "2026-08-28T09:00:00Z",
      }),
      workItem({
        case_id: "THEIRS-OLD",
        ticket_id: "T-THEM",
        inquiry_sent_at: "2026-08-20T09:00:00Z",
      }),
      workItem({
        case_id: "MINE-OLD",
        ticket_id: "T-MINE2",
        inquiry_sent_at: "2026-08-21T09:00:00Z",
      }),
    ]
    const tickets = [
      ticket({ ticket_id: "T-MINE", assigned_to: "z@e.com" }),
      ticket({ ticket_id: "T-MINE2", assigned_to: "Z@E.com" }),
      ticket({ ticket_id: "T-THEM", assigned_to: "other@e.com" }),
    ]
    const d = digest(cases, Number.POSITIVE_INFINITY, tickets)
    const order = queueOrder(d, new Set(["z@e.com"])).map(e => e.row.caseId)
    expect(order).toEqual(["MINE-OLD", "MINE-NEW", "THEIRS-OLD"])
  })

  it("puts blocked cases after waiting ones, never dropping them", () => {
    const cases = [
      workItem({ case_id: "BLOCKED-1", status: CaseStatus.ManualReviewRequired }),
      workItem({ case_id: "WAITING-1" }),
    ]
    const d = digest(cases, Number.POSITIVE_INFINITY, [])
    expect(queueOrder(d, new Set()).map(e => [e.kind, e.row.caseId])).toEqual([
      ["waiting", "WAITING-1"],
      ["blocked", "BLOCKED-1"],
    ])
  })

  it("never calls a case with no known owner yours", () => {
    // No ticket at all: digest reports ownerKnown false, so this must stay unassigned even
    // when the operator's own address is the one we are matching against.
    const d = digest([workItem({ case_id: "ORPHAN" })], Number.POSITIVE_INFINITY, [])
    const [entry] = queueOrder(d, new Set(["z@e.com"]))
    expect(entry.mine).toBe(false)
    expect(entry.owner).toBeNull()
  })
})

describe("briefingSentence", () => {
  /** A case the agent posted itself — terminal, and the last run was the poller's. */
  const posted = (over: Partial<WorkItem> = {}, attributes: Record<string, unknown> = {}) =>
    workItem(
      {
        status: CaseStatus.Complete,
        agent_traces: [
          { trace_id: "t", timestamp: "2026-08-28T00:00:00Z", segments: [], trigger: "poller" },
        ],
        ...over,
      },
      attributes
    )
  const cleared = posted({ case_id: "DONE-1" })

  it("states the window's clearing and the whole backlog, and says so", () => {
    const cases = [cleared, workItem({ case_id: "W-1" })]
    const backlog = digest(cases, Number.POSITIVE_INFINITY, [])
    const recent = digest(cases, 24, [])
    const sentence = briefingSentence(backlog, recent, "24")
    expect(sentence).toContain("last 24 hours")
    expect(sentence).toContain("1 case is waiting on a person")
    expect(sentence).toContain("whole backlog")
  })

  it("says plainly when nothing needs a person", () => {
    const d = digest([cleared], Number.POSITIVE_INFINITY, [])
    expect(briefingSentence(d, d, "24")).toContain(
      "Nothing is waiting on a person and nothing is blocked."
    )
  })

  it("makes no value or spend claim about a window nothing happened in", () => {
    const d = digest([], Number.POSITIVE_INFINITY, [])
    const sentence = briefingSentence(d, d, "24")
    expect(sentence).toContain("Nothing is waiting on a person")
    // Zero is a value; "nothing in the window" is no claim to make.
    expect(sentence).not.toContain("agent spend")
  })

  it("makes no value claim about the posted cases when nothing was posted", () => {
    // The commonest state of all: something is waiting, nothing cleared. The window has a
    // cost, so that half stands — but "no posted case carried an amount" would be a claim
    // about a set with no members.
    const cases = [
      workItem({ case_id: "W-1", cost_summary: { total_cost_usd: 0.04 } } as Partial<WorkItem>),
    ]
    const d = digest(cases, 24, [])
    expect(d.total).toBe(1)
    expect(d.postedCount).toBe(0)
    const sentence = briefingSentence(d, d, "24")
    expect(sentence).not.toContain("posted case")
    expect(sentence).not.toContain("value posted")
    expect(sentence).toContain("in agent spend")
  })

  it("names the currency with the figure rather than summing to a bare number", () => {
    const d = digest(
      [posted({ case_id: "P-1" }, { amount: 1200, currency: "EUR" })],
      24,
      [],
      AP_PACKS
    )
    const sentence = briefingSentence(d, d, "24")
    // "unsigned" because abs_decimal drops the sign, so a credit memo adds. `signUnknown`
    // is a disclosure the retrospective already makes and the spoken sentence owes too.
    expect(sentence).toContain("1,200.00 EUR of unsigned value posted")
    // Every posted case carried an amount, so the total is a total.
    expect(sentence).not.toContain("At least 1,200.00 EUR")
  })

  it("qualifies the value as a floor when a posted case carried no amount", () => {
    const cases = [
      posted({ case_id: "P-1" }, { amount: 1200, currency: "EUR" }),
      posted({ case_id: "P-2" }),
    ]
    const d = digest(cases, 24, [], AP_PACKS)
    expect(d.valuePartial).toBe(true)
    expect(briefingSentence(d, d, "24")).toContain("At least 1,200.00 EUR")
  })
})

describe("narrationPrompt", () => {
  it("hands the model the figures rather than asking it to compute them", () => {
    const cases = [workItem({ case_id: "W-1", inquiry_sent_at: "2026-08-20T09:00:00Z" })]
    const backlog = digest(cases, Number.POSITIVE_INFINITY, [])
    const recent = digest(cases, 24, [])
    const [oldest] = queueOrder(backlog, new Set())
    const prompt = narrationPrompt("Jane", backlog, recent, "24", oldest)

    expect(prompt).toContain("Jane")
    expect(prompt).toContain("waiting on a person, whole backlog: 1")
    expect(prompt).toContain("W-1")
    // The guard that keeps the model out of the numbers business.
    expect(prompt).toContain("Do not restate them as a list and do not compute new ones")
    expect(prompt).toContain("Do not invent case ids")
    // No figure above carries an owner, so a possessive needs no invented name to be false.
    expect(prompt).toContain("Never call a case theirs")
  })

  it("omits the oldest-case line when there is no queue", () => {
    const d = digest([], Number.POSITIVE_INFINITY, [])
    expect(narrationPrompt(null, d, d, "24", undefined)).not.toContain("waited longest")
  })

  it("labels a blocked case's run outcome so it cannot be read as the case's status", () => {
    // The collision: a run that ends `complete` while the case sits in manual review.
    // digest() puts the run's outcome in `reason`, and handed over bare it had a live
    // narration advising a pull because the case was "marked complete".
    const cases = [
      workItem({
        case_id: "B-1",
        status: CaseStatus.ManualReviewRequired,
        agent_traces: [
          { trace_id: "t1", timestamp: "2026-08-20T09:00:00Z", outcome: "complete", segments: [] },
        ],
      }),
    ]
    const backlog = digest(cases, Number.POSITIVE_INFINITY, [])
    const [oldest] = queueOrder(backlog, new Set())
    const prompt = narrationPrompt("Jane", backlog, backlog, "24", oldest)

    expect(prompt).toContain("B-1, blocked (last run: complete)")
    expect(prompt).not.toContain("(complete)")
  })

  it("leaves the greeting to the band, which is the only half that knows the hour", () => {
    const d = digest([], 24, [])
    expect(narrationPrompt("Jane", d, d, "24", undefined)).toContain(
      "do not open with a greeting of your own"
    )
  })
})

describe("chips", () => {
  it("splits waiting into yours and theirs only when something is yours", () => {
    const cases = [
      workItem({ case_id: "A", ticket_id: "T-MINE" }),
      workItem({ case_id: "B", ticket_id: "T-THEM" }),
    ]
    const tickets = [
      ticket({ ticket_id: "T-MINE", assigned_to: "z@e.com" }),
      ticket({ ticket_id: "T-THEM", assigned_to: "other@e.com" }),
    ]
    const backlog = digest(cases, Number.POSITIVE_INFINITY, tickets)
    const split = chips(backlog, backlog, 1, "24").map(c => c.label)
    expect(split).toContain("ready for you")
    expect(split).toContain("waiting on someone else")
    expect(split).not.toContain("waiting on someone")
  })
})
