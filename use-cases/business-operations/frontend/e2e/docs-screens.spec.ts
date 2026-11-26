// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { expect, test, type Page } from "@playwright/test"

/**
 * The screenshots the use case's docs show (`../docs/screenshots/`), taken from mock mode at
 * 1440 × 900 in the light theme with the clock frozen. Opt-in, so a normal run never rewrites
 * them: `DOCS_SCREENS=1 PW_CHANNEL=chrome npx playwright test e2e/docs-screens.spec.ts`.
 */
test.skip(!process.env.DOCS_SCREENS, "set DOCS_SCREENS=1 to retake the docs screenshots")

const OUT = "../docs/screenshots"
const FROZEN = new Date("2026-11-19T15:00:00Z")

async function start(p: Page, role: string) {
  await p.setViewportSize({ width: 1440, height: 900 })
  await p.clock.setFixedTime(FROZEN)
  await p.addInitScript(r => {
    window.localStorage.setItem("ui.theme", "light")
    window.localStorage.setItem("mock.role", r)
    window.localStorage.setItem("ui.firstRun.dismissed", "true")
  }, role)
}

/** Past every entry animation, so nothing is captured half faded. */
async function settle(p: Page) {
  await p.waitForLoadState("networkidle")
  await p.evaluate(() =>
    Promise.all(document.getAnimations().map(a => a.finished.catch(() => undefined)))
  )
  await p.mouse.move(0, 899)
}

test("console-briefing: Cases, with the briefing in the assistant", async ({ page: p }) => {
  await start(p, "operator")
  await p.goto("/cases")
  await p.getByRole("button", { name: "Briefing" }).click()
  await expect(p.getByRole("complementary", { name: "Assistant" })).toBeVisible()
  // The recorded answer streams a few words at a time; wait for it to finish.
  await expect(p.getByRole("button", { name: "Stop" })).toBeHidden({ timeout: 30_000 })
  await settle(p)
  await p.screenshot({ path: `${OUT}/console-briefing.png` })
})

test("console-case-run: a completed case and its steps", async ({ page: p }) => {
  await start(p, "operator")
  await p.goto("/cases?view=done")
  await p.getByRole("row").filter({ hasText: "5100001972" }).click()
  await p.getByRole("tab", { name: "Activity" }).click()
  await settle(p)
  await p.screenshot({ path: `${OUT}/console-case-run.png` })
})

test("console-approval: a case waiting for approval", async ({ page: p }) => {
  await start(p, "approver")
  await p.goto("/cases?view=needs-you")
  await p.getByRole("row").filter({ hasText: "5100001976" }).click()
  await expect(p.getByRole("button", { name: "Approve as billed" })).toBeVisible()
  await settle(p)
  await p.screenshot({ path: `${OUT}/console-approval.png` })
})

test("console-packs: a pack's Procedures part", async ({ page: p }) => {
  await start(p, "analyst")
  await p.goto("/packs/finance_ops?tab=procedures")
  await expect(p.getByRole("row", { name: /PRICE_VARIANCE_PCT/ })).toContainText("Price variance")
  await settle(p)
  await p.screenshot({ path: `${OUT}/console-packs.png` })
})
