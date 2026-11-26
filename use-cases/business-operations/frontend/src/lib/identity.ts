// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Who the signed-in operator is, from the OIDC profile.
 *
 * One module, three questions, deliberately answered differently. `operatorName` is what to
 * *show* on an account — the rail's footer would otherwise derive it separately, which is
 * how an app comes to call the same person two things. `operatorFirstName` is what to
 * *call them*, which is a narrower question with a stricter answer. `operatorIdentities` is
 * what to *match* against a ticket's `assigned_to`, and it is narrower again on purpose.
 */

/** Loosely typed because `react-oidc-context` types the profile as an index signature. */
type Profile = Record<string, unknown> | undefined

function str(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null
}

/**
 * Display name, in the order a human would pick one.
 *
 * Returns null rather than "Unknown" so each caller decides what absence means: a greeting
 * drops the name, the rail's footer prints its own placeholder.
 *
 * `name` still wins: an IdP that sends it is stating the full name authoritatively, and
 * composing one from the parts would second-guess it.
 */
export function operatorName(profile: Profile): string | null {
  const given = str(profile?.given_name)
  const family = str(profile?.family_name)
  return (
    str(profile?.name) ??
    (given && family ? `${given} ${family}` : null) ??
    given ??
    str(profile?.preferred_username) ??
    str(profile?.email) ??
    str(profile?.sub)
  )
}

/**
 * First name, for the one surface that addresses the operator directly.
 *
 * Only `given_name` counts, and there is deliberately no fallback: "Good morning,
 * jane.doe@example.com" is the bug this exists to fix, and an email local part is not a
 * name — `j.doe`, `admin` and `erp-agent+test` all parse into something wrong to call
 * a person. A null drops the name and leaves "Good morning", which is correct rather than
 * merely shorter.
 *
 * Kept apart from `operatorName` because the two answer different questions: the rail's
 * footer identifies an *account* and an email address is the right answer there.
 */
export function operatorFirstName(profile: Profile): string | null {
  return str(profile?.given_name)
}

/**
 * The identities a ticket's `assigned_to` may legitimately match, lowercased.
 *
 * `sub` is excluded even though `operatorName` falls back to it: `assigned_to` holds an
 * email or a username, so an opaque IdP id can only ever produce a false "yours" — and
 * telling an operator a case is theirs when it is not is the exact failure this queue
 * exists to prevent.
 */
export function operatorIdentities(profile: Profile): ReadonlySet<string> {
  const values = [str(profile?.email), str(profile?.preferred_username)]
  return new Set(values.filter((v): v is string => v !== null).map(v => v.toLowerCase()))
}
