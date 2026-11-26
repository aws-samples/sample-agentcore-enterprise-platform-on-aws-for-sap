// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest"
import * as fs from "fs"
import * as path from "path"

/**
 * Mock mode serves every path the console's services call. A service that gains a call
 * with no handler would show an error screen in mock mode only, which is where nobody
 * expects one; this names the gap instead. Read from source, so the test needs no MSW
 * runtime.
 */
const SERVICES = path.resolve(__dirname, "../../src/services")
const HANDLERS = fs.readFileSync(path.resolve(__dirname, "../../src/mocks/handlers.ts"), "utf8")

const handled = [...HANDLERS.matchAll(/http\.(get|post|put|patch|delete)\(`\$\{API\}(\/[^`]*)`/g)].map(m => ({
  method: m[1].toUpperCase(),
  // `:caseId` and friends match one path segment, as MSW does.
  pattern: new RegExp(`^${m[2].replace(/:[A-Za-z]+/g, "[^/]+")}$`),
}))

function called(): string[] {
  const out = new Set<string>()
  for (const file of fs.readdirSync(SERVICES).filter(f => f.endsWith(".ts") && !f.includes(".test."))) {
    const src = fs.readFileSync(path.join(SERVICES, file), "utf8")
    for (const m of src.matchAll(/`\$\{apiUrl\}(\/[^`?]*)/g)) out.add(m[1].replace(/\$\{[^}]*\}/g, "x"))
  }
  return [...out]
}

describe("mock mode's handlers", () => {
  it("finds handlers and calls", () => {
    expect(handled.length).toBeGreaterThan(10)
    expect(called().length).toBeGreaterThan(10)
  })

  it.each(called())("serves %s", p => {
    expect(handled.some(h => h.pattern.test(p))).toBe(true)
  })

  it("serves the console's config file", () => {
    expect(HANDLERS).toContain('http.get("/aws-exports.json"')
  })
})
