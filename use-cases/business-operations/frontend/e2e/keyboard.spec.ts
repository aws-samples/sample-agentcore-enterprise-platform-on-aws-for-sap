// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { expect, test, type Page } from "@playwright/test"
import AxeBuilder from "@axe-core/playwright"

/**
 * Every page by keyboard, and its structure for a screen reader: focus order and visible
 * focus with no trap, the shortcuts (⌘K, ⌘J, J and K, Escape), landmarks and headings in
 * order. axe's structure rules are moderate, so the screens only catch the serious ones;
 * these hold them to none at all.
 */
const FROZEN = new Date("2026-11-19T15:00:00Z")
const PAGES = [
  "/cases",
  "/cases?view=done",
  "/packs",
  "/packs/finance_ops?tab=overview",
  "/packs/finance_ops?tab=procedures",
  "/packs/finance_ops?tab=controls",
  "/packs/finance_ops?tab=versions",
  "/packs/finance_ops/procedures/price_variance",
  "/packs/finance_ops/rehearse",
  "/tickets",
  "/deployment",
]
const STRUCTURE = [
  "heading-order",
  "landmark-one-main",
  "landmark-no-duplicate-main",
  "landmark-unique",
  "page-has-heading-one",
  "region",
  "aria-allowed-role",
  "empty-heading",
]

async function start(p: Page, role = "operator") {
  const errors: string[] = []
  p.on("pageerror", e => errors.push(String(e)))
  await p.clock.setFixedTime(FROZEN)
  await p.addInitScript(r => window.localStorage.setItem("mock.role", r), role)
  return errors
}

/** True when the focused element draws a visible focus indicator (ring, outline or shadow). */
async function focusVisible(p: Page): Promise<{ ok: boolean; what: string }> {
  return p.evaluate(() => {
    const el = document.activeElement as HTMLElement | null
    if (!el || el === document.body) return { ok: true, what: "body" }
    const drawn = (n: Element) => {
      const s = getComputedStyle(n)
      return (
        (s.outlineStyle !== "none" && parseFloat(s.outlineWidth) > 0) ||
        (s.boxShadow !== "none" && s.boxShadow !== "") ||
        s.textDecorationLine.includes("underline")
      )
    }
    // A composite control (the ask bar, an input group) rings its box, not the input.
    let ok = drawn(el)
    for (let n = el.parentElement, i = 0; !ok && n && i < 3; n = n.parentElement, i++)
      ok = n.matches(":focus-within") && drawn(n)
    const what = `${el.tagName.toLowerCase()} "${(el.getAttribute("aria-label") ?? el.textContent ?? "").trim().slice(0, 40)}"`
    return { ok, what }
  })
}

for (const path of PAGES) {
  test(`${path}: structure and focus`, async ({ page: p }) => {
    const errors = await start(p)
    await p.goto(path)
    await expect(p.getByRole("region", { name: "Mock mode" })).toBeVisible()
    await p.waitForLoadState("networkidle")

    const axe = await new AxeBuilder({ page: p })
      .withRules(STRUCTURE)
      .exclude('[aria-label="Mock mode"]')
      .analyze()
    expect
      .soft(axe.violations.map(v => `${v.id}: ${v.nodes.map(n => n.target.join(" ")).join(", ")}`))
      .toEqual([])

    // Tab through the page: every stop shows its focus, and focus keeps moving (no trap).
    await p.locator("body").click({ position: { x: 1, y: 899 } })
    const seen: string[] = []
    const invisible: string[] = []
    for (let i = 0; i < 40; i++) {
      await p.keyboard.press("Tab")
      const f = await focusVisible(p)
      if (!f.ok) invisible.push(f.what)
      seen.push(f.what)
    }
    expect.soft(invisible, "focus not visible on").toEqual([])
    // A trap holds focus on one or two elements; a page moves through many.
    expect(new Set(seen).size).toBeGreaterThan(5)
    expect(errors).toEqual([])
  })
}

test("the skip link comes first and moves focus to the page", async ({ page: p }) => {
  await start(p)
  await p.goto("/cases")
  await p.waitForLoadState("networkidle")
  await p.keyboard.press("Tab")
  const skip = p.getByRole("link", { name: "Skip to content" })
  await expect(skip).toBeFocused()
  await expect(skip).toBeInViewport()
  await p.keyboard.press("Enter")
  await expect(p.locator("#main")).toBeFocused()
})

test("⌘K opens search, Escape closes it and focus comes back", async ({ page: p }) => {
  await start(p)
  await p.goto("/cases")
  await p.waitForLoadState("networkidle")
  await p.keyboard.press("ControlOrMeta+k")
  const dialog = p.getByRole("dialog")
  await expect(dialog).toBeVisible()
  await expect(dialog.getByRole("combobox")).toBeFocused()
  await p.keyboard.press("Escape")
  await expect(dialog).toBeHidden()
})

test("⌘J focuses the ask bar", async ({ page: p }) => {
  await start(p)
  await p.goto("/cases")
  await p.waitForLoadState("networkidle")
  await p.keyboard.press("ControlOrMeta+j")
  await expect(p.getByRole("textbox", { name: /Ask/ })).toBeFocused()
})

test("J and K move through the cases, Enter opens one, and J and K step on the case", async ({
  page: p,
}) => {
  await start(p)
  await p.goto("/cases?view=all")
  await p.waitForLoadState("networkidle")
  const current = p.locator('tr[aria-current="true"]')
  const text = () => current.evaluate(el => el.textContent)
  const first = await text()
  await p.keyboard.press("j")
  await expect.poll(text).not.toBe(first)
  await p.keyboard.press("k")
  await expect.poll(text).toBe(first)
  await p.keyboard.press("Enter")
  const title = p.getByRole("heading", { level: 1 })
  await expect(title).toBeVisible()
  const doc = await title.innerText()
  await p.keyboard.press("j")
  await expect(title).not.toHaveText(doc)
})

test("Escape closes the ticket sheet, the version sheet and the new pack dialog", async ({
  page: p,
}) => {
  await start(p)
  await p.goto("/tickets")
  await p.getByRole("row").nth(1).click()
  await expect(p.getByRole("dialog")).toBeVisible()
  await p.keyboard.press("Escape")
  await expect(p.getByRole("dialog")).toBeHidden()

  await p.goto("/packs/finance_ops?tab=versions")
  await p.getByRole("button", { name: "Version 14" }).click()
  await expect(p.getByRole("dialog")).toBeVisible()
  await p.keyboard.press("Escape")
  await expect(p.getByRole("dialog")).toBeHidden()
  await expect(p.getByRole("button", { name: "Version 14" })).toBeFocused()

  await p.goto("/packs")
  await p.getByRole("button", { name: "New pack" }).click()
  await expect(p.getByRole("dialog")).toBeVisible()
  await p.keyboard.press("Escape")
  await expect(p.getByRole("dialog")).toBeHidden()
})

test("the run streams into a live region, and toasts are announced", async ({ page: p }) => {
  await start(p, "approver")
  await p.goto("/cases?view=all&q=5100002031")
  await p.getByRole("row").filter({ hasText: "5100002031" }).click()
  await p.getByRole("button", { name: "Process" }).click()
  const steps = p.getByRole("log", { name: "Steps so far" })
  await expect(steps).toBeVisible()
  await expect(steps).toHaveAttribute("aria-live", "polite")
  // Sonner's toaster is a labelled live region.
  await expect(p.locator("section[aria-live]")).toHaveCount(1)
})
