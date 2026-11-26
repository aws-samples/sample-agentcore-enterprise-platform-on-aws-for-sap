// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Times, in the one spelling every page uses: relative within a day ("12 min ago"), then a
 * short date ("Nov 19", with the year when it is not this year), and the full time for a
 * tooltip (`formatStamp`). `components/When.tsx` puts the two together.
 */

const DAY = 86_400_000

function valid(iso?: string | null): Date | null {
  if (!iso) return null
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? null : d
}

/** "Nov 19", or "Nov 19, 2025" in another year, in the reader's locale. */
export function shortDate(d: Date, now = Date.now()): string {
  const sameYear = d.getFullYear() === new Date(now).getFullYear()
  return d.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    ...(sameYear ? {} : { year: "numeric" }),
  })
}

/**
 * When something happened: "just now", "12 min ago", "3 hours ago" within a day, then
 * "Nov 19". `""` when there is no timestamp, so a caller renders nothing rather than a
 * made-up time; a malformed stamp comes back as it arrived.
 */
export function timeAgo(iso?: string | null, now = Date.now()): string {
  if (!iso) return ""
  const d = valid(iso)
  if (!d) return iso
  const diff = Math.max(0, now - d.getTime())
  if (diff >= DAY) return shortDate(d, now)
  const mins = Math.floor(diff / 60_000)
  if (mins < 1) return "just now"
  if (mins < 60) return `${mins} min ago`
  const hrs = Math.floor(mins / 60)
  return `${hrs} ${hrs === 1 ? "hour" : "hours"} ago`
}

/**
 * How long something has waited, as a bare magnitude for a dense right-aligned column:
 * "12m", "3h", "2d". The full time goes in the cell's tooltip.
 */
export function shortAge(iso: string, now = Date.now()): string {
  const minutes = Math.max(0, Math.round((now - new Date(iso).getTime()) / 60_000))
  if (minutes < 60) return `${minutes}m`
  const hours = Math.round(minutes / 60)
  if (hours < 48) return `${hours}h`
  return `${Math.round(hours / 24)}d`
}

/**
 * The full time, in the reader's locale and zone, for a tooltip: "Nov 19, 2026, 5:00 AM".
 * A malformed stamp returns as it arrived rather than as "Invalid Date".
 */
export function formatStamp(iso?: string | null): string {
  if (!iso) return "—"
  const d = valid(iso)
  if (!d) return iso
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })
}
