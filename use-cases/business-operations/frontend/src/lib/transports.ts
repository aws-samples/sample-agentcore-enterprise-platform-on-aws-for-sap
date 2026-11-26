// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { CircleHelp, Radio, Timer, User, Webhook, type LucideIcon } from "lucide-react"

/**
 * The one place a transport id is turned into something an operator can read.
 *
 * `trigger` on a trace names the transport that started the run. It used to be a
 * closed enum in `types/cases.schema.json`, which bought nothing: the enum
 * constrained writers while three readers — CaseTimeline, SessionView, and
 * TracesPanel — each kept their own divergent label map. `sap-push` reached the
 * enum and none of the three; `webhook` was in two of them and never in the
 * enum; the invoker defaults to `unknown`, which the enum never contained. The schema is now an open
 * pattern and this is the registry it points at.
 *
 * Adding a transport is a row here plus a handler — no schema edit, no
 * regenerated type, no frontend rebuild for the value to be *accepted*.
 *
 * Class strings live in statusTone.ts's vocabulary where the meaning is a state.
 * A transport is not a state — a poller is not "info" and a webhook is not
 * "success" — so these keep their own hues, as the components did before.
 *
 * The glyph lives here rather than in each component, which is why this is the
 * one module under lib/ that imports lucide. Three private icon maps is how the
 * three label maps started.
 */

/**
 * How the run was reached, which is what the operator actually reads off the
 * badge: whether a person did it, a clock did it, or the source system did.
 * Grouping by this rather than per-transport colour is what lets an unrecognised
 * transport still render sensibly.
 */
export type TransportKind =
  /** A clock: nobody asked, the platform went looking. */
  | "schedule"
  /** A person, in the UI or a ticket. */
  | "person"
  /** An outside system called us over HTTP. */
  | "webhook"
  /** The source system published it at the moment it happened. */
  | "push"
  /** Not in the registry. Reached us anyway, and says so. */
  | "unknown"

/**
 * Glyph, by kind. `unknown` gets a question mark rather than a robot: the old
 * fallback rendered an unrecognised transport as the agent's own icon, which is
 * a claim about who started the run and was wrong every time it was used.
 */
export const TRANSPORT_ICON: Record<TransportKind, LucideIcon> = {
  schedule: Timer,
  person: User,
  webhook: Webhook,
  push: Radio,
  unknown: CircleHelp,
}

/**
 * Every transport this repo ships something that writes. Keep it that way: a row
 * with no writer is a promise the platform does not keep, and a writer with no
 * row renders as its humanised id.
 *
 * `webhook` is deliberately absent even though two components used to carry it.
 * Nothing writes it — it was a guess at a value the enum never had.
 */
export const TRANSPORTS: Record<string, { label: string; kind: TransportKind }> = {
  poller: { label: "Poller", kind: "schedule" },
  batch: { label: "Batch", kind: "schedule" },
  manual: { label: "Manual", kind: "person" },
  "ticket-action": { label: "Ticket", kind: "person" },
  "console-action": { label: "Console", kind: "person" },
  "operator-rerun": { label: "Operator re-run", kind: "person" },
  "sap-push": { label: "SAP push", kind: "push" },
  "sap-nudge": { label: "SAP nudge", kind: "push" },
}

/** `sap-idoc` → `Sap Idoc`. Every segment capitalised, so a reader can
    see the id it came from rather than a guess at prose. */
function humanise(id: string): string {
  return id
    .split("-")
    .map(word => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ")
}

export interface Transport {
  id: string
  label: string
  kind: TransportKind
  /** False when the id is not in the registry — the caller may want to say so. */
  known: boolean
}

/**
 * What to show for a transport id, for any id at all.
 *
 * An unrecognised transport renders as its own humanised id and never borrows
 * another transport's label. The old CaseTimeline fallback said "Agent", so a
 * `sap-push` run — a real SAP-initiated exception — read on screen as though the
 * agent had started itself.
 */
export function describeTransport(id?: string): Transport {
  const trimmed = (id ?? "").trim()
  if (!trimmed) return { id: "", label: "Unknown", kind: "unknown", known: false }

  const row = TRANSPORTS[trimmed]
  if (row) return { id: trimmed, label: row.label, kind: row.kind, known: true }

  return { id: trimmed, label: humanise(trimmed), kind: "unknown", known: false }
}

/**
 * Whether the run started with nobody watching — the question the autonomy funnel
 * and the digest are actually asking when they decide what to credit to the agent.
 *
 * Derived from `kind` rather than stored per row, so a new transport gets the right
 * answer from the same field that decides its colour:
 *
 * - `schedule` and `push` are unattended. A clock fired, or SAP published; no
 *   person was involved in either.
 * - `person` and `webhook` are not. Someone typed, clicked, emailed, or filed a
 *   ticket — a `complete` that follows is theirs, not the agent's.
 * - `unknown` is not. An unrecognised transport is not evidence of autonomy, and
 *   crediting one would inflate the number that exists to be trusted.
 *
 * Both callers used to compare against `poller` alone, which meant a `sap-push`
 * run — as unattended as it gets — was left out of the autonomous population.
 */
export function isUnattended(id?: string): boolean {
  const { kind } = describeTransport(id)
  return kind === "schedule" || kind === "push"
}
