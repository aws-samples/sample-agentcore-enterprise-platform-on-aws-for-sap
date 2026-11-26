// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from "vitest"
import { TRANSPORTS, TRANSPORT_ICON, describeTransport, isUnattended } from "@/lib/transports"

/**
 * The registry replaced a closed `Trigger` enum and three divergent label maps, so
 * what needs pinning is the behaviour that made the enum worth removing: a transport
 * nobody has registered still renders, renders as itself, and is never credited as
 * autonomy on the strength of not being recognised.
 */

describe("describeTransport", () => {
  it("labels a registered transport from the registry", () => {
    expect(describeTransport("sap-push")).toEqual({
      id: "sap-push",
      label: "SAP push",
      kind: "push",
      known: true,
    })
  })

  it("renders an unregistered transport as its own humanised id", () => {
    // A shape a future lane could add, deliberately not registered. Until its row lands
    // it must read as itself — the old CaseTimeline fallback said "Agent", which claimed
    // the agent had started its own run. Pick an example nothing writes.
    expect(describeTransport("webhook-teams")).toEqual({
      id: "webhook-teams",
      label: "Webhook Teams",
      kind: "unknown",
      known: false,
    })
  })

  it("never borrows another transport's label", () => {
    const labels = new Set(Object.values(TRANSPORTS).map(t => t.label))
    for (const id of ["webhook-teams", "sap-idoc", "nonsense"]) {
      expect(labels.has(describeTransport(id).label)).toBe(false)
    }
  })

  it("reports an absent trigger as unknown rather than inventing one", () => {
    // agent_invoker stamps "unknown" when a message carries no trigger, and older
    // traces predate the field entirely. Both reach here.
    for (const absent of [undefined, "", "   "]) {
      expect(describeTransport(absent)).toEqual({
        id: "",
        label: "Unknown",
        kind: "unknown",
        known: false,
      })
    }
  })

  it("trims, because a padded id is the same transport", () => {
    expect(describeTransport("  poller  ")).toEqual({
      id: "poller",
      label: "Poller",
      kind: "schedule",
      known: true,
    })
  })
})

describe("isUnattended", () => {
  it("credits a clock and a source-system push", () => {
    // `sap-push` is the one the old `trigger === "poller"` test excluded, which
    // undercounted autonomy for every SAP-initiated exception.
    expect(isUnattended("poller")).toBe(true)
    expect(isUnattended("batch")).toBe(true)
    expect(isUnattended("sap-push")).toBe(true)
    expect(isUnattended("sap-nudge")).toBe(true)
  })

  it("does not credit a run a person started", () => {
    expect(isUnattended("manual")).toBe(false)
    expect(isUnattended("ticket-action")).toBe(false)
    expect(isUnattended("console-action")).toBe(false)
    expect(isUnattended("operator-rerun")).toBe(false)
  })

  it("does not credit a transport it has never heard of", () => {
    // An unrecognised id is not evidence of autonomy. Defaulting the other way would
    // let a new transport inflate the funnel before anyone had classified it.
    expect(isUnattended("webhook-teams")).toBe(false)
    expect(isUnattended(undefined)).toBe(false)
  })
})

describe("the registry itself", () => {
  it("gives every kind a glyph", () => {
    // A missing entry is an undefined className on screen, not a fallback.
    const kinds = new Set(Object.values(TRANSPORTS).map(t => t.kind))
    kinds.add("unknown")
    for (const kind of kinds) {
      expect(TRANSPORT_ICON[kind]).toBeTruthy()
    }
  })

  it("keys every row on an id the schema pattern accepts", () => {
    // types/cases.schema.json's Trigger pattern. A row the schema would reject is a
    // label for a value that can never be stored.
    for (const id of Object.keys(TRANSPORTS)) {
      expect(id).toMatch(/^[a-z][a-z0-9]*(-[a-z0-9]+)*$/)
    }
  })
})
