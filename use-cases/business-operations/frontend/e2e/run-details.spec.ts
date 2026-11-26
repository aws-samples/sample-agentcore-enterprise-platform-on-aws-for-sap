// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { test, expect, type Page } from "@playwright/test"
import AxeBuilder from "@axe-core/playwright"

/**
 * Run details on the case page: every run's steps, printed, read by scrolling the page as the
 * other tabs are, with nothing cut off and the last line clear of the ask bar.
 */
const FROZEN = new Date("2026-11-19T12:00:00Z")

async function start(p: Page, theme: string) {
  await p.clock.setFixedTime(FROZEN)
  await p.addInitScript(t => {
    window.localStorage.setItem("ui.theme", t)
    window.localStorage.setItem("mock.role", "operator")
  }, theme)
}

for (const [theme, width] of [
  ["light", 1280],
  ["dark", 1440],
  ["dark", 1280],
  ["light", 1440],
] as const) {
  test(`Run details scrolls with the page (${theme}, ${width} px)`, async ({ page: p }) => {
    await start(p, theme)
    await p.setViewportSize({ width, height: 900 })
    await p.goto("/cases?view=done")
    await p.getByRole("row").filter({ hasText: "5100001972" }).click()
    await p.getByRole("tab", { name: "Run details" }).click()
    const printed = p.getByLabel("Printed payload").last()
    await expect(printed).toBeVisible()
    // Nothing printed spills out of its block or hides behind it: the block is as tall as
    // what it prints, so the page's own scroll reaches every line.
    const spill = await printed.evaluate(el => {
      const pre = el.querySelector("pre")!.getBoundingClientRect()
      return pre.bottom - el.getBoundingClientRect().bottom
    })
    expect(spill).toBeLessThanOrEqual(1)
    // The page scrolls to the last line, and it sits above the ask bar.
    const scroller = p.locator(".overflow-y-auto").filter({ has: printed }).first()
    await scroller.evaluate(el => el.scrollTo({ top: el.scrollHeight }))
    const end = await printed.locator("pre").boundingBox()
    const bar = await p.getByRole("form", { name: "Ask the assistant" }).boundingBox()
    expect(end!.y + end!.height).toBeLessThanOrEqual(bar!.y)
    await p.screenshot({ path: `e2e/.screens/run-details-${theme}-${width}.png` })
    const axe = await new AxeBuilder({ page: p }).exclude('[aria-label="Mock mode"]').analyze()
    expect(
      axe.violations.filter(v => v.impact === "serious" || v.impact === "critical").map(v => v.id)
    ).toEqual([])
  })
}

for (const theme of ["light", "dark"] as const) {
  test(`What the agent found reads as prose (${theme})`, async ({ page: p }) => {
    await start(p, theme)
    await p.setViewportSize({ width: 1440, height: 900 })
    await p.goto("/cases?view=done")
    await p.getByRole("row").filter({ hasText: "5100001972" }).click()
    const found = p.locator("[data-slot='card']").filter({ hasText: "What the agent found" })
    await expect(found.locator("strong", { hasText: "within tolerance" })).toBeVisible()
    await expect(found).not.toContainText("**")
    await p.screenshot({ path: `e2e/.screens/found-${theme}.png` })
  })
}

for (const [theme, width] of [
  ["light", 1440],
  ["dark", 1280],
] as const) {
  test(`Evidence reads as one card per SAP record (${theme}, ${width} px)`, async ({ page: p }) => {
    await start(p, theme)
    await p.setViewportSize({ width, height: 900 })
    await p.goto("/cases?view=needs-you")
    await p.getByRole("row").filter({ hasText: "5100001976" }).click()
    const invoice = p.locator("[data-slot='card']").filter({ hasText: "Supplier invoice" }).first()
    await expect(invoice).toContainText("52,500.00 USD")
    await expect(invoice).toContainText(
      /Read .* from A_SupplierInvoice in API_SUPPLIERINVOICE_PROCESS_SRV/
    )
    // Labels are words; the SAP name is in the tooltip.
    await expect(invoice).not.toContainText("InvoiceGrossAmount")
    // Identifiers alone are mono; amounts and codes are not.
    const font = (text: string) =>
      invoice.getByText(text, { exact: true }).evaluate(el => getComputedStyle(el).fontFamily)
    expect(await font("4500000123")).toMatch(/Mono/)
    expect(await font("52,500.00 USD")).not.toMatch(/Mono/)
    await invoice.getByText("Amount", { exact: true }).hover()
    await expect(p.locator('[data-slot="tooltip-content"]').last()).toContainText(
      "InvoiceGrossAmount"
    )
    await p.keyboard.press("Escape")
    await p.mouse.move(0, 899)
    await p.waitForTimeout(300)
    await p.screenshot({ path: `e2e/.screens/evidence-${theme}-${width}.png` })
    const axe = await new AxeBuilder({ page: p }).exclude('[aria-label="Mock mode"]').analyze()
    expect(
      axe.violations.filter(v => v.impact === "serious" || v.impact === "critical").map(v => v.id)
    ).toEqual([])
  })
}
