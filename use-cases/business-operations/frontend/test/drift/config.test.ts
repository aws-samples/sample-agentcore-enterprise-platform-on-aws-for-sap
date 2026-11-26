// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from "vitest"
import { readFileSync, readdirSync } from "fs"
import { resolve } from "path"

describe("Configuration Verification Tests", () => {
  describe("pre-paint theme script", () => {
    const indexHtml = () => readFileSync(resolve(__dirname, "../../index.html"), "utf-8")
    const themeInit = () => readFileSync(resolve(__dirname, "../../public/theme-init.js"), "utf-8")
    const useTheme = () => readFileSync(resolve(__dirname, "../../src/hooks/useTheme.ts"), "utf-8")

    it("agrees with useTheme on the storage key", () => {
      expect(themeInit()).toContain('localStorage.getItem("ui.theme")')
      expect(useTheme()).toContain('const STORAGE_KEY = "ui.theme"')
    })

    it("agrees on the class and where it hangs", () => {
      expect(themeInit()).toContain('document.documentElement.classList.add("dark")')
      expect(useTheme()).toContain('document.documentElement.classList.toggle("dark"')
    })

    it("stays a same-origin file, not an inline block the CSP would have to hash", () => {
      expect(indexHtml()).toContain('<script src="/theme-init.js"></script>')
      // Inline <script> in index.html would need a sha256 in the console stack's CSP.
      // The app bundle's own module tag is the only other one, and it is src= too.
      expect(indexHtml()).not.toMatch(/<script(?![^>]*\ssrc=)[^>]*>/)
    })

    it("runs before the app bundle, and blocking, or it cannot beat first paint", () => {
      const html = indexHtml()
      expect(html.indexOf("/theme-init.js")).toBeLessThan(html.indexOf("/src/main.tsx"))
      expect(html).not.toMatch(/<script[^>]*theme-init\.js[^>]*(defer|async)/)
    })
  })

  // The type pairing is split across two files: the loader in main.tsx and the
  // token in globals.css. Either one alone fails silently (a missing @theme entry
  // makes `font-mono` a no-op class, and a missing import falls back to a system
  // face), so the pairing is only real when both halves agree on the family name.
  // Compact works by re-declaring Tailwind's own scales, so it is only real if
  // `.compact` moves the variables the utilities read through. A step declared in
  // `@theme inline` gets baked into the utility and becomes unreachable — that is
  // the failure these catch, not a typo.
  describe("type scale", () => {
    const globalsCss = () =>
      readFileSync(resolve(__dirname, "../../src/styles/globals.css"), "utf-8")

    it("has no density class: density is a table setting, not a token change", () => {
      expect(globalsCss()).not.toContain(".compact")
    })

    it("keeps Tailwind's own steps, with nothing below 12 px", () => {
      const css = globalsCss()
      expect(css).not.toMatch(/--text-(xs|sm|base|lg|xl|2xl):/)
      for (const m of css.matchAll(/--text-[23]xs:\s*([\d.]+)rem/g)) {
        expect(Number(m[1])).toBeGreaterThanOrEqual(0.75)
      }
    })

    it("sets body text at text-sm", () => {
      expect(globalsCss()).toMatch(/body \{[^}]*text-sm/)
    })
  })

  describe("type pairing", () => {
    const read = (f: string) => readFileSync(resolve(__dirname, "../..", f), "utf-8")
    const globalsCss = () => read("src/styles/globals.css")

    it("self-hosts both faces the design uses", () => {
      const main = read("src/main.tsx")
      expect(main).toContain('import "@fontsource-variable/geist/wght.css"')
      expect(main).toContain('import "@fontsource-variable/geist/wght-italic.css"')
      expect(main).toContain('import "@fontsource-variable/geist-mono/wght.css"')
    })

    it("names no third-party origin the CSP would have to allow", () => {
      expect(read("index.html")).not.toMatch(/https?:\/\//)
      expect(globalsCss()).not.toMatch(/@import\s+url\(\s*["']?https?:/)
    })

    it("declares every face the utilities read through", () => {
      // The utility side and the value side are declared in two different blocks, so
      // the pairing is only real when both halves name the same variable. A repoint
      // that renamed one and not the other would leave `font-mono` resolving to
      // nothing — which looks like a font that failed to load, not like a typo.
      const css = globalsCss()
      expect(css).toContain("--font-sans: var(--font-sans-face)")
      expect(css).toContain("--font-mono: var(--font-mono-face)")
      // The family names fontsource's variable packages declare.
      expect(css).toMatch(/--font-sans-face:\s*"Geist Variable"/)
      expect(css).toMatch(/--font-mono-face:\s*"Geist Mono Variable"/)
    })
  })
})

// Every assertion here pins the mechanism, not the number: that a token exists and
// that it derives from --spacing. A literal rem would fail on every future tuning pass.
describe("chrome sizing contract", () => {
  const globals = () => readFileSync(resolve(__dirname, "../../src/styles/globals.css"), "utf-8")
  const rootBlock = () => {
    const css = globals()
    const start = css.indexOf(":root {")
    return css.slice(start, css.indexOf("}", start))
  }

  it("declares the shell's sizes in :root", () => {
    const root = rootBlock()
    for (const token of ["--header-height", "--toolbar-height", "--assistant-width", "--gutter"]) {
      expect(root).toContain(`${token}:`)
    }
  })
})

// The rule this pins is the one thing about the tint ladder a reviewer can check
// without measuring anything, so it should be CI's job rather than a reader's:
// `--muted-foreground` measured 4.40:1 on `--muted` before it was darkened, so any
// alpha on it failed, and `/70` — 29 sites — composited to about 2.7:1. De-emphasis is
// `text-subtle-foreground`, a second measured token, never a multiplier on the first.
describe("ink takes no alpha modifier", () => {
  it("leaves no text-*/<alpha> utility", () => {
    const src = resolve(__dirname, "../../src")
    // components/ui is stock shadcn, whose own classes are its business.
    const files = readdirSync(src, { recursive: true, encoding: "utf-8" }).filter(
      f => /\.tsx?$/.test(f) && !f.startsWith("components/ui/")
    )
    const offenders = files.flatMap(f => {
      const hits = readFileSync(resolve(src, f), "utf-8").match(/\btext-[a-z0-9-]+\/\d+/g)
      return hits ? hits.map(h => `${f}: ${h}`) : []
    })
    expect(offenders).toEqual([])
  })
})
