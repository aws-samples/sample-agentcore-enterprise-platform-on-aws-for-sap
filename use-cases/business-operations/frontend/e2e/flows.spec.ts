// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { expect, test, type Page } from "@playwright/test"
import AxeBuilder from "@axe-core/playwright"

/**
 * What a person does in the console, end to end on mock mode, in both themes, each step
 * checked with axe: approve with a reason, deny, edit, check and save a SOP, and runs that
 * stream as recorded ones (Process, Rehearse, a question). Mock state lives in the page, so
 * every test starts from the fixtures.
 */
const THEMES = ["light", "dark"] as const
const FROZEN = new Date("2026-11-19T15:00:00Z")

async function start(p: Page, theme: string, role: string) {
  const errors: string[] = []
  p.on("pageerror", e => errors.push(String(e)))
  await p.clock.setFixedTime(FROZEN)
  await p.addInitScript(
    ([t, r]) => {
      window.localStorage.setItem("ui.theme", t)
      window.localStorage.setItem("mock.role", r)
    },
    [theme, role]
  )
  return errors
}

async function noSeriousFindings(p: Page) {
  const axe = await new AxeBuilder({ page: p }).exclude('[aria-label="Mock mode"]').analyze()
  const serious = axe.violations.filter(v => v.impact === "serious" || v.impact === "critical")
  expect
    .soft(serious.map(v => `${v.id}: ${v.nodes.map(n => n.target.join(" ")).join(", ")}`))
    .toEqual([])
}

async function openCase(p: Page, doc: string) {
  await p.goto("/cases?view=needs-you")
  await p.getByRole("row").filter({ hasText: doc }).click()
  await expect(p.getByRole("heading", { level: 1 })).toContainText(doc)
}

for (const theme of THEMES) {
  test(`approve with a reason (${theme})`, async ({ page: p }) => {
    const errors = await start(p, theme, "approver")
    await openCase(p, "5100001976")
    const submit = p.getByRole("button", { name: "Approve as billed" })
    await expect(submit).toHaveAttribute("aria-disabled", "true")
    await p
      .getByRole("textbox", { name: /Reason/ })
      .fill("Price agreed with the supplier on 3 Nov.")
    await noSeriousFindings(p)
    await submit.click()
    // The decided case stays: the panel says what was sent and cannot be sent again.
    const done = p.getByRole("status", { name: "Your decision, sent" })
    await expect(done).toContainText("Approve as billed")
    await expect(done).toContainText("Price agreed with the supplier on 3 Nov.")
    await expect(p.getByRole("heading", { level: 1 })).toContainText("5100001976")
    await expect(p.getByRole("button", { name: /Approve as billed/ })).toHaveCount(0)
    const next = done.getByRole("button", { name: /^Next case: \d+/ })
    await expect(next).toBeVisible()
    await p.waitForTimeout(600)
    await noSeriousFindings(p)
    await p.screenshot({ path: `e2e/.screens/decided-${theme}.png` })
    // Moving on is the person's step, and the new case's number replaces the old one.
    const nextDoc = ((await next.textContent()) ?? "").match(/\d{6,}/)![0]
    await p.keyboard.press("j")
    await expect(p.getByRole("heading", { level: 1 })).toContainText(nextDoc)
    await expect(p.getByRole("status", { name: "Your decision, sent" })).toHaveCount(0)
    await p.waitForTimeout(150)
    await p.screenshot({ path: `e2e/.screens/next-case-${theme}.png` })
    expect(errors).toEqual([])
  })

  test(`deny (${theme})`, async ({ page: p }) => {
    const errors = await start(p, theme, "approver")
    await openCase(p, "5100001976")
    await p.getByRole("radio", { name: /Deny/ }).click()
    await p.getByRole("textbox", { name: /Reason/ }).fill("The supplier did not agree the price.")
    await p.getByRole("button", { name: "Deny" }).click()
    await expect(p.getByRole("status", { name: "Your decision, sent" })).toContainText("Deny")
    // Back to the view in the app: a reload would start mock mode over.
    await p.locator("a", { hasText: "Needs you" }).click()
    // The decision resumes the run, which hands the case to manual review when it ends.
    const row = p.getByRole("row").filter({ hasText: "5100001976" })
    await expect(row).toContainText("Manual review", { timeout: 20_000 })
    await noSeriousFindings(p)
    expect(errors).toEqual([])
  })

  test(`edit, check and save a SOP (${theme})`, async ({ page: p }) => {
    const errors = await start(p, theme, "analyst")
    await p.goto("/packs/finance_ops/procedures/price_variance")
    await p.getByRole("button", { name: "Edit" }).click()
    const editor = p.getByRole("textbox", { name: "Price variance SOP text" })
    await expect(p.getByText("Found nothing")).toBeVisible()
    await editor.click()
    await p.keyboard.press("ControlOrMeta+End")
    await p.keyboard.type("\nThe agent MUST cite {{UNKNOWN_RULE}}.")
    await expect(p.getByText(/UNKNOWN_RULE/).last()).toBeVisible()
    await expect(p.getByRole("button", { name: "Save", exact: true })).toBeDisabled()
    for (let i = 0; i < "{{UNKNOWN_RULE}}.".length; i++) await p.keyboard.press("Backspace")
    await p.keyboard.type("the invoice.")
    await expect(p.getByText("Found nothing")).toBeVisible()
    await noSeriousFindings(p)
    await p.getByRole("button", { name: "Save", exact: true }).click()
    const dialog = p.getByRole("dialog")
    await expect(dialog).toContainText("The agent MUST cite the invoice.")
    await dialog.getByRole("button", { name: "Save", exact: true }).click()
    await expect(p.getByText("Saved Price variance")).toBeVisible()
    expect(errors).toEqual([])
  })
}

for (const theme of THEMES) {
  test(`process a case and watch the run stream (${theme})`, async ({ page: p }) => {
    const errors = await start(p, theme, "operator")
    await p.goto("/cases?view=not-started")
    await p.getByRole("row").filter({ hasText: "5100002032" }).click()
    await p.getByRole("button", { name: "Process" }).click()
    const steps = p.getByRole("log", { name: "Steps so far" })
    await expect(steps).toContainText("Read A_SupplierInvoice", { timeout: 10_000 })
    await noSeriousFindings(p)
    await expect(p.getByText("Released invoice 5100002032").first()).toBeVisible({
      timeout: 30_000,
    })
    await expect(p.getByRole("heading", { level: 1 })).toContainText("Done")
    expect(errors).toEqual([])
  })

  test(`rehearse a SOP and watch its steps (${theme})`, async ({ page: p }) => {
    const errors = await start(p, theme, "analyst")
    await p.goto("/packs/finance_ops/rehearse?sop=price_variance")
    await p
      .getByRole("textbox", { name: "The case" })
      .fill("Invoice for 10 units at 5,250.00 against a PO at 5,000.00. Block R.")
    await p.getByRole("button", { name: "Rehearse" }).click()
    const steps = p.getByRole("log", { name: "Steps" })
    await expect(steps).toContainText("withheld in a rehearsal", { timeout: 20_000 })
    await expect(p.getByText("Nothing was sent.").first()).toBeVisible({ timeout: 20_000 })
    await noSeriousFindings(p)
    expect(errors).toEqual([])
  })

  test(`ask the assistant and read its answer (${theme})`, async ({ page: p }) => {
    const errors = await start(p, theme, "operator")
    await p.goto("/cases")
    await p.getByRole("textbox", { name: /Ask/ }).fill("What needs me today?")
    await p.keyboard.press("Enter")
    const panel = p.getByRole("complementary", { name: "Assistant" })
    await expect(panel).toContainText("wait for a person", { timeout: 20_000 })
    await noSeriousFindings(p)
    expect(errors).toEqual([])
  })
}

test("a cut-off cell shows its full value on keyboard focus", async ({ page: p }) => {
  await start(p, "light", "operator")
  await p.goto("/cases")
  const cell = p.locator('[data-slot="tooltip-trigger"]', { hasText: "Domestic US Supplier 1" })
  // Reached by the keyboard, as a person would: Tab away and back.
  await cell.first().focus()
  await p.keyboard.press("Shift+Tab")
  await p.keyboard.press("Tab")
  await expect(p.locator('[data-slot="tooltip-content"]').last()).toContainText(
    "Domestic US Supplier"
  )
})

for (const theme of THEMES) {
  test(`first run on an empty stack (${theme})`, async ({ page: p }) => {
    const errors = await start(p, theme, "operator")
    await p.addInitScript(() => window.localStorage.setItem("mock.scenario", "empty"))
    await p.goto("/cases")
    const banner = p.getByRole("region", { name: "First case" })
    await expect(banner).toBeVisible()
    await expect(banner.getByRole("listitem").first()).toContainText("Connect SAP")
    await expect(banner.locator('[aria-current="step"]')).toContainText("Seed an exception")
    await expect(p.getByText("Nothing needs you")).toBeVisible()
    await noSeriousFindings(p)
    await p.screenshot({ path: `e2e/.screens/first-run-${theme}.png` })
    await banner.getByRole("button", { name: "Dismiss the first case steps" }).click()
    await expect(banner).toBeHidden()
    expect(errors).toEqual([])
  })
}

for (const theme of THEMES) {
  test(`a ticket decided in the sheet (${theme})`, async ({ page: p }) => {
    const errors = await start(p, theme, "approver")
    await p.goto("/tickets?status=assigned")
    await p.getByRole("button", { name: "TKT-1001" }).click()
    const sheet = p.getByRole("dialog")
    await expect(sheet.getByRole("link", { name: /Open the case/ })).toBeVisible()
    // Past the sheet's entry animation, which axe would measure half faded.
    await sheet.evaluate(el =>
      Promise.all(el.getAnimations({ subtree: true }).map(a => a.finished))
    )
    await sheet.getByRole("textbox", { name: /Reason/ }).fill("Price agreed with the supplier.")
    await noSeriousFindings(p)
    await p.screenshot({ path: `e2e/.screens/ticket-sheet-${theme}.png` })
    await sheet.getByRole("button", { name: "Approve as billed" }).click()
    await expect(p.getByText("Approve as billed: sent")).toBeVisible()
    await p.keyboard.press("Escape")
    await expect(p.getByRole("tab", { name: /Approved\s*1/ })).toBeVisible()
    expect(errors).toEqual([])
  })
}

for (const theme of THEMES) {
  test(`a promotion-only stack (${theme})`, async ({ page: p }) => {
    const errors = await start(p, theme, "analyst")
    await p.addInitScript(() => window.localStorage.setItem("mock.stack", "quality"))
    await p.goto("/packs/finance_ops/procedures/price_variance")
    await expect(p.getByText("This stack takes packs by promotion only")).toBeVisible()
    const edit = p.getByRole("button", { name: "Edit" })
    await expect(edit).toHaveAttribute("aria-disabled", "true")
    // The reason reaches a keyboard user: Tab onto the button opens its tooltip.
    await edit.focus()
    await p.keyboard.press("Shift+Tab")
    await p.keyboard.press("Tab")
    await expect(p.locator('[data-slot="tooltip-content"]').last()).toContainText("promotion only")
    await p.screenshot({ path: `e2e/.screens/promotion-only-${theme}.png` })
    await noSeriousFindings(p)
    expect(errors).toEqual([])
  })
}

for (const theme of THEMES) {
  test(`open a stored version (${theme})`, async ({ page: p }) => {
    const errors = await start(p, theme, "worker")
    await p.goto("/packs/finance_ops?tab=versions")
    await p.getByRole("button", { name: "Version 13" }).click()
    const sheet = p.getByRole("dialog")
    await expect(sheet.getByRole("button", { name: "pack.json" })).toBeVisible()
    await p.waitForTimeout(400)
    await p.screenshot({ path: `e2e/.screens/version-${theme}.png` })
    await noSeriousFindings(p)
    await p.keyboard.press("Escape")
    await expect(sheet).toBeHidden()
    expect(errors).toEqual([])
  })
}

test("the exception facet counts each type in the view", async ({ page: p }) => {
  const errors = await start(p, "light", "operator")
  await p.goto("/cases?view=all")
  await p.getByRole("button", { name: "Filter" }).click()
  const item = p.getByRole("menuitemcheckbox", { name: /Price variance|Price variance/ })
  await expect(item).toContainText(/\d/)
  await p.screenshot({ path: "e2e/.screens/facet-counts.png" })
  expect(errors).toEqual([])
})

for (const [theme, width] of [
  ["light", 1440],
  ["dark", 1440],
  ["light", 1280],
] as const) {
  test(`emergency stop ends a live run and bands the console (${theme}, ${width} px)`, async ({
    page: p,
  }) => {
    const errors = await start(p, theme, "operator")
    await p.setViewportSize({ width, height: 900 })
    await p.goto("/cases?view=not-started")
    await p.getByRole("row").filter({ hasText: "5100002032" }).click()
    await p.getByRole("button", { name: "Process" }).click()
    await expect(p.getByRole("log", { name: "Steps so far" })).toContainText(
      "Read A_SupplierInvoice",
      { timeout: 10_000 }
    )
    // In the app, not by URL: a reload would start mock mode over and drop the run.
    await p.getByRole("link", { name: "Deployment settings" }).click()
    await p.getByRole("button", { name: "Emergency stop" }).click()
    const dialog = p.getByRole("alertdialog")
    await expect(dialog).toContainText("1 case has a run in progress now")
    await dialog.getByRole("textbox", { name: "Reason" }).fill("Released the wrong supplier")
    // Past the dialog's entry animation, which axe would measure half faded.
    await p.waitForTimeout(600)
    await noSeriousFindings(p)
    await dialog.getByRole("button", { name: "Stop every run" }).click()
    const band = p.getByRole("region", { name: "Emergency stop" })
    await expect(band).toContainText("sam@example.com stopped every run")
    await expect(band).toContainText("Released the wrong supplier")
    await expect(band.getByRole("link", { name: "Deployment settings" })).toBeInViewport()
    await p.waitForTimeout(600)
    await noSeriousFindings(p)
    await p.screenshot({ path: `e2e/.screens/stopped-${theme}-${width}.png` })
    await p.getByRole("link", { name: /^Cases/ }).click()
    await expect(band).toBeVisible()
    // The case that was being worked waits for a person.
    await expect(p.getByRole("row").filter({ hasText: "5100002032" })).toContainText(
      "Manual review"
    )
    // Starting it again is refused while the stop stands, in the API's words.
    await p.getByRole("row").filter({ hasText: "5100002032" }).click()
    await p.getByRole("button", { name: "Run again" }).click()
    await expect(p.getByRole("alert").filter({ hasText: "emergency stop stands" })).toBeVisible()
    // Clearing it takes a reason, ends the band, and leaves the trigger mode manual.
    await p.getByRole("link", { name: "Deployment settings" }).first().click()
    // The trigger mode waits for the clear, and says why.
    const turnOn = p.getByRole("button", { name: "Set to auto" })
    await expect(turnOn).toHaveAttribute("aria-disabled", "true")
    await turnOn.hover()
    await expect(p.locator('[data-slot="tooltip-content"]').last()).toContainText(
      "Clear the stop first"
    )
    await p.waitForTimeout(300)
    await p.screenshot({ path: `e2e/.screens/switch-held-${theme}-${width}.png` })
    await p.mouse.move(0, 899)
    // The assistant says questions wait, in the bar and in the open panel.
    const bar = p.getByRole("textbox", { name: "Ask the assistant" })
    await expect(bar).toHaveAttribute(
      "placeholder",
      "Questions wait until an administrator clears the stop"
    )
    await bar.fill("What waits on me?")
    await bar.press("Enter")
    const panel = p.getByRole("complementary", { name: "Assistant" })
    await expect(panel).toContainText("An emergency stop stands")
    await expect(
      panel.getByPlaceholder("Questions wait until an administrator clears the stop")
    ).toBeVisible()
    await p.waitForTimeout(400)
    await noSeriousFindings(p)
    await expect(p.locator("[data-sonner-toast]")).toHaveCount(0, { timeout: 10_000 })
    await p.screenshot({ path: `e2e/.screens/panel-held-${theme}-${width}.png` })
    await panel.getByRole("button", { name: "Close the assistant" }).click()
    // A pack's own trigger mode waits for the clear too.
    // In-app navigation: mock state lives in the page and a reload would reset it.
    await p.getByRole("link", { name: "All packs" }).click()
    await p.getByRole("row").filter({ hasText: "Accounts Payable" }).click()
    await p.getByRole("tab", { name: "Overview" }).click()
    const packOn = p.getByRole("button", { name: "Set to auto" })
    await expect(packOn).toHaveAttribute("aria-disabled", "true")
    await packOn.hover()
    await expect(p.locator('[data-slot="tooltip-content"]').last()).toContainText(
      "Clear the stop first"
    )
    await p.mouse.move(0, 899)
    await p.getByRole("link", { name: "Deployment settings" }).first().click()
    await p.getByRole("button", { name: "Clear the stop" }).click()
    const clear = p.getByRole("alertdialog")
    await expect(clear).toContainText("The trigger mode stays manual")
    await clear.getByRole("textbox", { name: "Reason" }).fill("Supplier master fixed")
    await p.waitForTimeout(600)
    await noSeriousFindings(p)
    await clear.getByRole("button", { name: "Clear the stop" }).click()
    await expect(band).toBeHidden()
    await expect(p.getByText(/Cleared by sam@example.com/)).toBeVisible()
    await expect(p.getByText("Now: Off")).toBeVisible()
    expect(errors).toEqual([])
  })
}

for (const theme of THEMES) {
  test(`a version shows what it changed against the one before (${theme})`, async ({ page: p }) => {
    const errors = await start(p, theme, "worker")
    await p.setViewportSize({ width: 1440, height: 900 })
    await p.goto("/packs/finance_ops?tab=versions")
    await p.getByRole("button", { name: "Version 14" }).click()
    const sheet = p.getByRole("dialog")
    await expect(sheet.getByRole("heading", { name: "Changes from version 13" })).toBeVisible()
    await expect(sheet.getByText("Price variance SOP")).toBeVisible()
    await p.waitForTimeout(600)
    await noSeriousFindings(p)
    await p.screenshot({ path: `e2e/.screens/version-diff-${theme}.png` })
    expect(errors).toEqual([])
  })
}
