// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { test, expect, type Page } from "@playwright/test"

/**
 * Type on the pack pages: mono only for identifiers, a step below the text around it. A
 * printed document (`JsonBlock`), the SOP editor and inputs that take an id are exempt: they
 * show code, or what a person types.
 */
const FROZEN = new Date("2026-11-19T12:00:00Z")
const TABS = ["overview", "intake", "procedures", "controls", "agent", "evaluations", "versions"]

async function start(p: Page, theme: string) {
  await p.clock.setFixedTime(FROZEN)
  await p.addInitScript(t => {
    window.localStorage.setItem("ui.theme", t)
    window.localStorage.setItem("mock.role", "operator")
  }, theme)
  await p.setViewportSize({ width: 1440, height: 900 })
}

/** Mono text on the page that is larger than 12 px, outside the exempt places. */
async function largeMono(p: Page): Promise<string[]> {
  return p.evaluate(() =>
    [...document.querySelectorAll<HTMLElement>("main *")]
      .filter(el => {
        if (
          el.closest("[aria-label='Printed payload'], .cm-editor, input, [aria-label='Mock mode']")
        )
          return false
        const own = [...el.childNodes].some(n => n.nodeType === 3 && n.textContent!.trim())
        const style = getComputedStyle(el)
        return own && /Mono/.test(style.fontFamily) && parseFloat(style.fontSize) > 12
      })
      .map(el => el.textContent!.trim().slice(0, 40))
  )
}

for (const theme of ["light", "dark"] as const) {
  test(`pack pages keep mono to small identifiers (${theme})`, async ({ page: p }) => {
    await start(p, theme)
    for (const tab of TABS) {
      await p.goto(`/packs/finance_ops?tab=${tab}`)
      await expect(p.getByRole("tab", { selected: true })).toBeVisible()
      await p.waitForLoadState("networkidle")
      expect(await largeMono(p), tab).toEqual([])
      await p.screenshot({ path: `e2e/.screens/pack-${tab}-${theme}.png`, fullPage: true })
    }
    await p.getByRole("button", { name: "Version 14" }).click()
    await expect(p.getByRole("heading", { name: "Changes from version 13" })).toBeVisible()
    await p.waitForTimeout(500)
    await p.screenshot({ path: `e2e/.screens/pack-version-sheet-${theme}.png` })
    await p.goto("/packs/finance_ops/procedures/price_variance")
    await expect(p.getByRole("heading", { level: 1 })).toBeVisible()
    await p.waitForLoadState("networkidle")
    expect(await largeMono(p), "procedure").toEqual([])
    await p.screenshot({ path: `e2e/.screens/pack-procedure-${theme}.png`, fullPage: true })
  })
}

for (const theme of ["light", "dark"] as const) {
  test(`SOP groups head their rows (${theme})`, async ({ page: p }) => {
    await start(p, theme)
    await p.goto("/packs/finance_ops?tab=procedures")
    const heading = p.getByRole("columnheader", { name: "Exception types" })
    const sop = p.getByRole("link", { name: "Price variance", exact: true })
    await expect(sop).toBeVisible()
    const size = (l: typeof heading) =>
      l.evaluate(el => {
        const s = getComputedStyle(el)
        return { size: parseFloat(s.fontSize), weight: Number(s.fontWeight) }
      })
    const h = await size(heading)
    const r = await size(sop)
    expect(h.size).toBeGreaterThanOrEqual(r.size)
    expect(h.weight).toBeGreaterThan(r.weight)
    await heading.scrollIntoViewIfNeeded()
    await p.screenshot({ path: `e2e/.screens/sop-groups-${theme}.png` })
  })
}
