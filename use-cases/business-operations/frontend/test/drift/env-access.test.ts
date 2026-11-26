// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Vite is the only thing that substitutes env vars into this bundle, and it only
 * substitutes `import.meta.env.VITE_*`. `process.env` is not defined in a browser, so a
 * `process.env.X` read does not throw — it yields `undefined`, and whatever it configured
 * silently falls back to a default. That is the failure this guards: no error, no log, just
 * a feature quietly off in the deployed app.
 *
 * Was a fast-check property that sampled 100 files at random from the source tree, which
 * meant a real slip could sit in a file the sampler never drew. Every source file is checked
 * now — there are only a few hundred, and a guard that reads all of its inputs is the whole
 * point.
 */

import { describe, it, expect } from "vitest"
import * as fs from "fs"
import * as path from "path"

function getAllSourceFiles(dir: string, fileList: string[] = []): string[] {
  for (const file of fs.readdirSync(dir)) {
    const filePath = path.join(dir, file)
    if (fs.statSync(filePath).isDirectory()) {
      if (!file.startsWith(".") && file !== "node_modules" && file !== "build") {
        getAllSourceFiles(filePath, fileList)
      }
    } else if (file.match(/\.(ts|tsx|js|jsx)$/)) {
      fileList.push(filePath)
    }
  }
  return fileList
}

const SRC_DIR = path.resolve(__dirname, "../../src")
const SOURCE_FILES = getAllSourceFiles(SRC_DIR)

describe("environment variable access", () => {
  it("finds the source tree to check", () => {
    // Without this, a moved directory turns every assertion below into a pass over
    // an empty list.
    expect(SOURCE_FILES.length).toBeGreaterThan(50)
  })

  it("reads env vars only through import.meta.env", () => {
    const offenders = SOURCE_FILES.filter(f =>
      /process\.env/.test(fs.readFileSync(f, "utf-8"))
    ).map(f => path.relative(SRC_DIR, f))

    expect(
      offenders,
      `process.env is undefined in the browser — use import.meta.env.VITE_*`
    ).toEqual([])
  })

  it("exposes no NEXT_PUBLIC_ prefixed vars", () => {
    // `NEXT_PUBLIC_` is Next.js's client-exposure prefix and means nothing to Vite: a var
    // named that way is never substituted, so it reads as undefined rather than failing.
    const nextPublic = Object.keys(import.meta.env).filter(k => k.startsWith("NEXT_PUBLIC_"))
    expect(nextPublic).toEqual([])
  })
})
