// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Canonical codec for case identity — the one place `case_id` is built or read.
 *
 * TypeScript twin of `agent/utils/case_key.py` (the API layer's is a symlink to it). See
 * that module for the full rationale; both are held to `types/case-id-vectors.json`, and
 * the pattern comes from the schema. The contract in brief:
 *
 *   - A minted id is `{short}-{26 base32 chars}`, e.g. `ap-k7m2q9x4rt6bw8ncf3jhd5gzpv`:
 *     a readable pack prefix and a digest of the case's natural key. Opaque by
 *     design — a case's key can be three SAP fields holding characters no id
 *     charset accepts, so what a case *is* lives in its `key` map, not in its id.
 *   - It is safe as-is in URLs, SQS group ids, and session ids.
 *
 * Nothing here mints an id: identity is decided where a case is created, in the
 * poller. The browser reads ids and renders them.
 */

import { CASE_ID_PATTERN } from "@/types/generated-cases"

/** Between the short id and the digest. */
export const SEPARATOR = "-"

/** Anchored pattern for a case_id, the shape `derive_case_id` mints. */
export const DERIVED_CASE_ID_PATTERN: string = CASE_ID_PATTERN

const DERIVED_CASE_ID_RE = new RegExp(DERIVED_CASE_ID_PATTERN)

/** AgentCore Runtime rejects a session id shorter than this. */
export const RUNTIME_SESSION_MIN_LENGTH = 33

const SESSION_PREFIX = "erp-case-"
const SESSION_PAD = "0"

/** A case identity could not be built or parsed. */
export class CaseKeyError extends Error {
  constructor(message: string) {
    super(message)
    this.name = "CaseKeyError"
  }
}

/**
 * The declared order of a pack's key fields, as `GET /registry` publishes it.
 *
 * Structural rather than a `UseCasePack` import: this module is the identity codec, and
 * it imports nothing but the schema's pattern.
 */
export interface KeyOrder {
  source?: { key?: string[] | null } | null
}

/**
 * A case's natural key as ordered pairs — the one place that decides what comes first.
 *
 * A stored `key` is a *map*, and DynamoDB does not preserve a map's key order: the real AP
 * case declared `{SupplierInvoice, FiscalYear}` comes back off the table as `{FiscalYear,
 * SupplierInvoice}`. Unordered, that heads its row `2026 · 5105699001` — a fiscal year where
 * the invoice number belongs. So the order comes off the pack's published `source.key`,
 * which is an array for exactly this reason (see `registry.py`'s `public_view`).
 *
 * A field the pack does not name is appended rather than dropped: an order that was never
 * published, or a key that has outgrown it, must cost legibility and never a piece of the
 * case's identity.
 */
function orderedKey(
  key: Record<string, string> | null | undefined,
  pack?: KeyOrder | null
): [string, string][] {
  const map = key ?? {}
  const declared = pack?.source?.key ?? []
  const names = [
    ...declared.filter(n => n in map),
    ...Object.keys(map).filter(n => !declared.includes(n)),
  ]
  return names.map(n => [n, map[n]] as [string, string]).filter(([, v]) => v != null && v !== "")
}

/**
 * Render a case for display or a log line: the id, then the key that produced it.
 *
 * `ap-k7m2q9x4rt6bw8ncf3jhd5gzpv (SupplierInvoice=5100001976, FiscalYear=2026)`
 *
 * Ordered by {@link orderedKey}, the same derivation {@link caseHeadline} uses. This is the
 * hover text over that headline, so a pair that read in a different order there would look
 * like a different case rather than the same one spelled out.
 */
export function describeCase(
  caseId: string,
  key?: Record<string, string> | null,
  pack?: KeyOrder | null
): string {
  const pairs = orderedKey(key, pack)
  if (pairs.length === 0) return caseId
  return `${caseId} (${pairs.map(([k, v]) => `${k}=${v}`).join(", ")})`
}

/**
 * The parts of a case record that say what it is — a structural subset of `WorkItem`, so
 * every caller passes the record it already holds rather than unpacking four fields.
 */
export interface CaseIdentity {
  case_id?: string | null
  /**
   * The natural key: SAP field name to value. A *map*, so its key order is whatever the
   * store handed back — see {@link caseHeadline}, which is why the order is published
   * separately rather than read off this.
   */
  key?: Record<string, string> | null
}

/** Between the values of a multi-field key. Not {@link SEPARATOR}, which joins an *id*. */
const HEADLINE_JOIN = " · "

/**
 * The case as an operator recognises it: the values of its natural key.
 *
 * `5100001976 · 2026`, never `ap-k7m2q9x4rt6bw8ncf3jhd5gzpv`. A minted id is a digest by
 * construction, so as the loudest element of a row it identifies nothing anyone can look
 * up — in SAP, in a mailbox, or out loud. What the case *is* lives in `key`, and this is
 * that map's values, ordered by {@link orderedKey} — which is where the reason the order
 * cannot come off the map itself is written down.
 *
 * The field *names* stay out of it deliberately. They are SAP's spelling
 * (`SupplierInvoice`), no pack declares a label for a key field — the `fields` block covers
 * `field_map` attributes only — and camel-splitting one here would be the browser
 * inventing vocabulary it was never given. {@link describeCase} is what spells them out,
 * so a caller pairs the two: this on the line, `describeCase` in its `title`, which is
 * also where the id stays recoverable for a log search.
 *
 * Display only, and so with no counterpart in `case_key.py`: nothing server-side renders
 * a row.
 *
 * Falls back to the id itself, so a record carrying no key still has to be clickable
 * rather than blank.
 */
export function caseHeadline(c: CaseIdentity, pack?: KeyOrder | null): string {
  const values = orderedKey(c.key, pack).map(([, v]) => v)
  if (values.length > 0) return values.join(HEADLINE_JOIN)
  return c.case_id ?? ""
}

/** The id, trimmed, if it is a case id. */
export function normalizeCaseId(caseId: string): string {
  const value = (caseId ?? "").trim()
  if (!DERIVED_CASE_ID_RE.test(value)) throw new CaseKeyError(`case_id="${value}" is not a case id`)
  return value
}

/** Best-effort {@link normalizeCaseId} — `null` instead of throwing. */
export function tryNormalizeCaseId(caseId: string | null | undefined): string | null {
  try {
    return normalizeCaseId(caseId ?? "")
  } catch {
    return null
  }
}

/** True if `value` is a well-formed case id. */
export function isCaseId(value: string | null | undefined): boolean {
  return tryNormalizeCaseId(value) !== null
}

/**
 * Derive a stable AgentCore session id for a case.
 *
 * Deterministic, so every turn for one case lands in the same AgentCore Memory
 * session instead of a fresh random one. Right-padded because AgentCore Runtime
 * rejects ids shorter than {@link RUNTIME_SESSION_MIN_LENGTH}.
 */
export function toRuntimeSessionId(caseId: string): string {
  let base = `${SESSION_PREFIX}${normalizeCaseId(caseId)}`
  if (base.length < RUNTIME_SESSION_MIN_LENGTH) {
    base += SEPARATOR + SESSION_PAD.repeat(RUNTIME_SESSION_MIN_LENGTH - base.length - 1)
  }
  return base
}
