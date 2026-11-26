// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { expect, test } from "@playwright/test"
import AxeBuilder from "@axe-core/playwright"

/**
 * Every page renders in mock mode, in both themes, with no console error and no serious
 * accessibility finding; and a screenshot of each for review. Screenshots go to
 * `e2e/.screens/` (gitignored), named `<page>-<theme>.png`, at 1440 × 900 with the clock
 * frozen so ages and greetings do not change between runs.
 */
const PAGES = [
  { name: "cases", path: "/cases", crumb: "Cases" },
  { name: "not-started", path: "/cases?view=not-started", crumb: "Cases" },
  { name: "team", path: "/cases?view=team", crumb: "Cases" },
  { name: "packs", path: "/packs", crumb: "Packs" },
  ...["overview", "intake", "procedures", "controls", "agent", "evaluations", "versions"].map(
    tab => ({
      name: `pack-${tab}`,
      path: `/packs/finance_ops?tab=${tab}`,
      crumb: "Finance — Accounts Payable",
    })
  ),
  {
    name: "procedure",
    path: "/packs/finance_ops/procedures/price_variance",
    crumb: "Price variance",
  },
  {
    name: "procedure-triage",
    path: "/packs/finance_ops/procedures/invoice_matching",
    crumb: "Invoice matching",
  },
  { name: "rehearse", path: "/packs/finance_ops/rehearse?sop=price_variance", crumb: "Rehearse" },
  { name: "tickets", path: "/tickets", crumb: "Tickets" },
  { name: "deployment", path: "/deployment", crumb: "Deployment settings" },
]
const THEMES = ["light", "dark"] as const
const FROZEN = new Date("2026-11-19T15:00:00Z")

for (const theme of THEMES) {
  for (const page of PAGES) {
    test(`${page.name} renders (${theme})`, async ({ page: p }) => {
      await p.setViewportSize({ width: 1440, height: 900 })
      const errors: string[] = []
      p.on("pageerror", e => errors.push(String(e)))
      p.on("console", m => m.type() === "error" && errors.push(m.text()))
      await p.clock.setFixedTime(FROZEN)
      await p.addInitScript(t => window.localStorage.setItem("ui.theme", t), theme)
      await p.goto(page.path)
      await expect(p.getByRole("region", { name: "Mock mode" })).toBeVisible()
      // The page asked for, not a redirect away from it.
      await expect(p.locator('[data-slot="breadcrumb-page"]')).toHaveText(page.crumb)
      await p.waitForLoadState("networkidle")
      await p.screenshot({ path: `e2e/.screens/${page.name}-${theme}.png` })

      const axe = await new AxeBuilder({ page: p }).exclude('[aria-label="Mock mode"]').analyze()
      const serious = axe.violations.filter(v => v.impact === "serious" || v.impact === "critical")
      expect.soft(serious.map(v => `${v.id}: ${v.nodes.length}`)).toEqual([])
      expect(errors).toEqual([])
    })
  }
}

for (const theme of THEMES) {
  test(`case page renders (${theme})`, async ({ page: p }) => {
    const errors: string[] = []
    p.on("pageerror", e => errors.push(String(e)))
    await p.clock.setFixedTime(FROZEN)
    await p.addInitScript(t => window.localStorage.setItem("ui.theme", t), theme)
    await p.goto("/cases")
    await p.getByRole("row").nth(1).click()
    const title = p.getByRole("heading", { level: 1 })
    await expect(title).toBeVisible()
    // The last crumb names the document, as the title does.
    const doc = await title.locator(".font-mono").first().innerText()
    await expect(p.locator('[data-slot="breadcrumb-page"]')).toHaveText(doc)
    await p.waitForLoadState("networkidle")
    await p.screenshot({ path: `e2e/.screens/case-${theme}.png` })
    const axe = await new AxeBuilder({ page: p }).exclude('[aria-label="Mock mode"]').analyze()
    const serious = axe.violations.filter(v => v.impact === "serious" || v.impact === "critical")
    expect.soft(serious.map(v => `${v.id}: ${v.nodes.length}`)).toEqual([])
    expect(errors).toEqual([])
  })
}

for (const theme of THEMES) {
  test(`procedure editor renders (${theme})`, async ({ page: p }) => {
    const errors: string[] = []
    p.on("pageerror", e => errors.push(String(e)))
    await p.clock.setFixedTime(FROZEN)
    await p.addInitScript(t => {
      window.localStorage.setItem("ui.theme", t)
      window.localStorage.setItem("mock.role", "analyst")
    }, theme)
    await p.goto("/packs/finance_ops/procedures/price_variance")
    await p.getByRole("button", { name: "Edit" }).click()
    await expect(p.getByRole("textbox", { name: "Price variance SOP text" })).toBeVisible()
    await expect(p.getByText("Found nothing")).toBeVisible()
    await p.screenshot({ path: `e2e/.screens/procedure-edit-${theme}.png` })
    const axe = await new AxeBuilder({ page: p }).exclude('[aria-label="Mock mode"]').analyze()
    const serious = axe.violations.filter(v => v.impact === "serious" || v.impact === "critical")
    expect.soft(serious.map(v => `${v.id}: ${v.nodes.length}`)).toEqual([])
    expect(errors).toEqual([])
  })
}

/** The decision panel's states (preview 1.4), each on the case that shows it, as who sees it. */
const DECISION_STATES = [
  { name: "decide-approval", doc: "5100001976", role: "approver", text: "Approve as billed" },
  { name: "decide-two-approvers", doc: "5100002011", role: "operator", text: "1 of 2 approvals" },
  { name: "decide-reply", doc: "5100001981", role: "approver", text: "Send and resume" },
  { name: "decide-not-entitled", doc: "5100001976", role: "worker", text: "Approve as billed" },
  {
    name: "decide-nothing-yet",
    doc: "5100002031",
    role: "approver",
    text: "Nothing to decide yet",
    process: true,
  },
]

for (const theme of THEMES) {
  for (const s of DECISION_STATES) {
    test(`${s.name} renders (${theme})`, async ({ page: p }) => {
      const errors: string[] = []
      p.on("pageerror", e => errors.push(String(e)))
      await p.clock.setFixedTime(FROZEN)
      await p.addInitScript(
        ([t, r]) => {
          window.localStorage.setItem("ui.theme", t)
          window.localStorage.setItem("mock.role", r)
        },
        [theme, s.role]
      )
      await p.goto(`/cases?view=all&q=${s.doc}`)
      await p.getByRole("row").filter({ hasText: s.doc }).click()
      if ("process" in s) await p.getByRole("button", { name: "Process" }).click()
      await expect(p.getByText(s.text).first()).toBeVisible()
      await p.waitForLoadState("networkidle")
      await p.screenshot({ path: `e2e/.screens/${s.name}-${theme}.png`, fullPage: true })
      const axe = await new AxeBuilder({ page: p }).exclude('[aria-label="Mock mode"]').analyze()
      const serious = axe.violations.filter(v => v.impact === "serious" || v.impact === "critical")
      expect.soft(serious.map(v => `${v.id}: ${v.nodes.length}`)).toEqual([])
      expect(errors).toEqual([])
    })
  }
}

/** The decision panel keeps its title, the reason and the button in view at both widths. */
for (const width of [1280, 1440]) {
  test(`decision panel fits the fold at ${width} px`, async ({ page: p }) => {
    await p.setViewportSize({ width, height: 900 })
    await p.clock.setFixedTime(FROZEN)
    await p.addInitScript(() => window.localStorage.setItem("mock.role", "approver"))
    await p.goto("/cases?view=all&q=5100001976")
    await p.getByRole("row").filter({ hasText: "5100001976" }).click()
    const panel = p.locator("aside").filter({ hasText: "Your decision" })
    await expect(panel.getByText("Your decision")).toBeInViewport()
    await expect(panel.getByRole("textbox", { name: /Reason/ })).toBeInViewport()
    await expect(panel.getByRole("button", { name: "Approve as billed" })).toBeInViewport()
    const box = await panel.boundingBox()
    expect(box!.width).toBeGreaterThanOrEqual(width >= 1440 ? 380 : 360)
    await p.screenshot({ path: `e2e/.screens/decision-fold-${width}.png` })
  })
}

for (const width of [1280, 1440]) {
  test(`the mock toolbar covers no band or header text at ${width} px`, async ({ page: p }) => {
    await p.setViewportSize({ width, height: 900 })
    await p.clock.setFixedTime(FROZEN)
    await p.goto("/packs/finance_ops/procedures/price_variance")
    const bar = p.getByRole("region", { name: "Mock mode" })
    await expect(bar).toBeVisible()
    const box = (await bar.boundingBox())!
    const text = [
      p.getByRole("region", { name: "Sandbox" }),
      p.locator('[data-slot="breadcrumb-list"]'),
      p.locator("header").locator("button").last(),
    ]
    for (const t of text) {
      const b = (await t.boundingBox())!
      const overlaps =
        box.x < b.x + b.width &&
        b.x < box.x + box.width &&
        box.y < b.y + b.height &&
        b.y < box.y + box.height
      // The band spans the page, so only its text matters: the bar sits below it.
      expect(
        overlaps,
        `the mock toolbar overlaps ${await t.evaluate(e => e.outerHTML.slice(0, 60))}`
      ).toBe(false)
    }
  })
}

/** The Cases view tabs and Briefing keep every label whole, with counts as wide as a live
 *  deployment's and the assistant open. Narrower still, the tabs scroll. */
const LIVE_COUNTS = ["4", "4", "2912", "0", "5", "0", "2921"]
for (const width of [1280, 1440]) {
  test(`the Cases tab row clips no label at ${width} px`, async ({ page: p }) => {
    await p.setViewportSize({ width, height: 900 })
    await p.clock.setFixedTime(FROZEN)
    // With the assistant open beside the page, as it is once a briefing has been asked for.
    await p.addInitScript(() => window.localStorage.setItem("ui.assistant.open", "true"))
    await p.goto("/cases")
    await expect(p.getByRole("complementary", { name: "Assistant" })).toBeVisible()
    const tabs = p.getByRole("tablist")
    await expect(tabs.getByRole("tab")).toHaveCount(LIVE_COUNTS.length)
    await tabs.evaluate((list, counts) => {
      list.querySelectorAll('[role="tab"] > span').forEach((s, i) => {
        s.textContent = counts[i]
      })
    }, LIVE_COUNTS)
    const briefing = p.getByRole("button", { name: "Briefing" })
    // Nothing spills past the row, where the page cuts it off ("Briefin").
    const row = await briefing.evaluate(b => {
      const r = b.parentElement!
      const tabsBox = r.querySelector('[role="tablist"]')!.parentElement!
      return {
        spills: r.scrollWidth > r.clientWidth,
        briefingClipped: b.scrollWidth > b.clientWidth,
        // Every tab whole: at these widths the tabs fit on their line, without scrolling.
        tabsFit: tabsBox.scrollWidth <= tabsBox.clientWidth,
        scrolls: getComputedStyle(tabsBox).overflowX === "auto",
      }
    })
    expect(row).toEqual({ spills: false, briefingClipped: false, tabsFit: true, scrolls: true })
    await p.screenshot({ path: `e2e/.screens/cases-tabs-${width}.png` })
  })
}
