// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Every API call a service makes, by path and method, is one `openapi.yaml` declares. The
 * chat's feedback buttons once posted to a `/feedback` route that was never mounted, and every
 * click failed in the browser as "Failed to fetch" with nothing to say why. The API's own
 * tests check the contract against the mounted routes; this checks the console against the
 * contract.
 *
 * A path parameter in a service call (`${packId}`) matches only a templated segment in the
 * contract (`{pack_id}`), never a literal one, so `/packs/${packId}` cannot pass by matching
 * `/packs/status`.
 */

import { describe, it, expect } from "vitest"
import * as fs from "fs"
import * as path from "path"

const SERVICES = path.resolve(__dirname, "../../src/services")
const OPENAPI = path.resolve(__dirname, "../../../openapi.yaml")

/** `"/packs/{pack_id}" → Set { "GET", "PATCH" }`, from the paths block's indentation. */
function declared(): Map<string, Set<string>> {
  const out = new Map<string, Set<string>>()
  let current: Set<string> | null = null
  for (const line of fs.readFileSync(OPENAPI, "utf8").split("\n")) {
    const p = line.match(/^ {2}(\/\S*):\s*$/)
    if (p) {
      current = new Set()
      out.set(p[1], current)
      continue
    }
    if (/^\S/.test(line)) current = null
    const m = line.match(/^ {4}(get|put|post|patch|delete):\s*$/)
    if (m && current) current.add(m[1].toUpperCase())
  }
  return out
}

interface Call {
  file: string
  path: string
  method: string
  segments: (string | null)[]
}

/** Each `${apiUrl}/…` template and the `method:` in the same call (GET when none is given). */
function called(): Call[] {
  const out: Call[] = []
  for (const file of fs
    .readdirSync(SERVICES)
    .filter(f => f.endsWith(".ts") && !f.includes(".test."))) {
    const src = fs.readFileSync(path.join(SERVICES, file), "utf8")
    const matches = [...src.matchAll(/`\$\{apiUrl\}(\/[^`?]*)/g)]
    matches.forEach((m, i) => {
      const end = matches[i + 1]?.index ?? src.length
      const call = src.slice(m.index!, Math.min(end, src.indexOf("\n}", m.index!) + 1 || end))
      const method = call.match(/method:\s*"([A-Z]+)"/)?.[1] ?? "GET"
      const p = m[1].replace(/\$\{[^}]*\}/g, "{}")
      out.push({
        file,
        path: p,
        method,
        segments: p.split("/").map(s => (s === "{}" ? null : s)),
      })
    })
  }
  return out
}

function matches(call: Call, contract: string): boolean {
  const parts = contract.split("/")
  if (parts.length !== call.segments.length) return false
  return parts.every((part, i) => {
    const want = call.segments[i]
    const templated = /^\{[^}]+\}$/.test(part)
    return want === null ? templated : !templated && part === want
  })
}

/**
 * Routes the backend track has shaped in its contract note and built on its branch, which the
 * console calls (mock mode serves them) before they land in this `openapi.yaml`. Each entry
 * goes once the route lands; the test then holds it to the contract like any other.
 */
const PENDING: Record<string, string[]> = {}

describe("console services and the REST contract", () => {
  const contract = declared()
  for (const [p, methods] of Object.entries(PENDING))
    if (!contract.has(p)) contract.set(p, new Set(methods))

  it("lists no pending route that has landed", () => {
    for (const [p, methods] of Object.entries(PENDING)) {
      const landed = declared().get(p)
      expect(landed && methods.every(m => landed.has(m)), `${p} has landed: drop it`).toBeFalsy()
    }
  })

  it("finds the contract's paths and the services' calls", () => {
    expect(contract.size).toBeGreaterThan(5)
    expect(called().length).toBeGreaterThan(5)
  })

  it("does not let a path parameter match a literal segment", () => {
    const call: Call = { file: "", path: "/packs/{}", method: "GET", segments: ["", "packs", null] }
    expect(matches(call, "/packs/status")).toBe(false)
    expect(matches(call, "/packs/{pack_id}")).toBe(true)
  })

  it.each(called().map(c => [`${c.file} ${c.method} ${c.path}`, c] as const))(
    "%s is declared",
    (_, c) => {
      const hit = [...contract].filter(([p]) => matches(c, p))
      expect(hit.map(([p]) => p)).not.toEqual([])
      expect(hit.some(([, methods]) => methods.has(c.method))).toBe(true)
    }
  )
})
