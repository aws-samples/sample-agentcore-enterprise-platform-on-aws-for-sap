// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The colour pairs the console draws, measured from the tokens in globals.css in both
 * themes (WCAG 2.2 contrast): text 4.5:1 on what it sits on, a status dot or a focus ring
 * 3:1 against the page. axe in the Playwright screens measures what is on screen; this
 * measures every tone on its wash, including the ones no fixture shows.
 */

import { describe, expect, it } from "vitest"
import { readFileSync } from "fs"
import { resolve } from "path"

const css = readFileSync(resolve(__dirname, "../../src/styles/globals.css"), "utf-8")

function block(selector: string): Record<string, string> {
  const start = css.indexOf(`${selector} {`)
  const body = css.slice(start, css.indexOf("\n}", start))
  return Object.fromEntries(
    [...body.matchAll(/--([a-z0-9-]+):\s*([^;]+);/g)].map(m => [m[1], m[2].trim()])
  )
}

const LIGHT = block(":root")
const DARK = { ...LIGHT, ...block(".dark") }

function hex(theme: Record<string, string>, name: string): string {
  let v = theme[name]
  for (let i = 0; v?.startsWith("var(") && i < 5; i++) v = theme[v.slice(6, -1)]
  if (!v || !/^#[0-9a-f]{6}$/i.test(v)) throw new Error(`--${name} is not a hex colour: ${v}`)
  return v
}

function luminance(h: string): number {
  const [r, g, b] = [1, 3, 5].map(i => {
    const c = parseInt(h.slice(i, i + 2), 16) / 255
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
  })
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

function ratio(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x)
  return (hi + 0.05) / (lo + 0.05)
}

/** [foreground, background, minimum]: text 4.5, a graphic 3. */
const PAIRS: [string, string, number][] = [
  ["foreground", "background", 4.5],
  ["muted-foreground", "background", 4.5],
  ["muted-foreground", "card", 4.5],
  ["muted-foreground", "muted", 4.5],
  ["muted-foreground", "sidebar", 4.5],
  ["sidebar-foreground", "sidebar-accent", 4.5],
  ["destructive", "background", 4.5],
  ["primary-foreground", "primary", 4.5],
  // Each status badge: its ink on its wash.
  ["tone-progress-ink", "tone-progress-wash", 4.5],
  ["tone-attention-ink", "tone-attention-wash", 4.5],
  ["tone-success-ink", "tone-success-wash", 4.5],
  ["tone-danger-ink", "tone-danger-wash", 4.5],
  ["foreground", "tone-neutral-wash", 4.5],
  // A tone as text with no wash under it.
  ["tone-progress-text", "background", 4.5],
  ["tone-success-text", "background", 4.5],
  ["tone-danger-text", "background", 4.5],
  // Dots and the focus ring are graphics.
  ["tone-progress", "background", 3],
  ["tone-attention", "background", 3],
  ["tone-success", "background", 3],
  ["tone-danger", "background", 3],
  ["tone-neutral", "background", 3],
  ["ring", "background", 3],
  ["subtle-foreground", "background", 3],
]

describe.each([
  ["light", LIGHT],
  ["dark", DARK],
] as const)("contrast in the %s theme", (_, theme) => {
  it.each(PAIRS)("%s on %s clears %s:1", (fg, bg, min) => {
    const r = ratio(hex(theme, fg), hex(theme, bg))
    expect(Number(r.toFixed(2)), `${fg} on ${bg}`).toBeGreaterThanOrEqual(min)
  })
})
