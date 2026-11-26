// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { defineConfig, type Plugin } from "vite"
import react from "@vitejs/plugin-react"
import path from "path"
import { readFileSync } from "fs"

const CHUNK_BUDGET = 500 * 1024

/**
 * Fails the build when a single JS chunk exceeds the budget.
 *
 * Rollup's own `chunkSizeWarningLimit` is the same number, but it only warns, and a warning
 * scrolls past in a deploy log — the app chunk had reached 1,241 kB before anyone acted on
 * it. This was a vitest assertion over `build/` for a while, which was worse than nothing:
 * the directory is gitignored and no CI job ran `npm run build`, so it passed by skipping
 * everywhere it ran. Enforcing it here puts the check where the artifact exists.
 */
function chunkBudget(): Plugin {
  return {
    name: "chunk-budget",
    generateBundle(_options, bundle) {
      const over = Object.values(bundle)
        .filter(output => output.type === "chunk")
        .map(chunk => ({ name: chunk.fileName, size: Buffer.byteLength(chunk.code) }))
        .filter(chunk => chunk.size > CHUNK_BUDGET)

      if (over.length > 0) {
        const listed = over.map(c => `${c.name} is ${Math.round(c.size / 1024)} kB`).join(", ")
        this.error(
          `chunk budget of ${CHUNK_BUDGET / 1024} kB exceeded: ${listed}. Either lazy-load ` +
            `the route with React.lazy, or give the dependency its own entry in ` +
            `build.rollupOptions.output.manualChunks.`
        )
      }
    },
  }
}

/**
 * Mock mode's service worker, served by the dev server only. Kept out of `public/`, which
 * Vite copies into every build.
 */
function mockWorker(): Plugin {
  return {
    name: "mock-worker",
    apply: "serve",
    configureServer(server) {
      server.middlewares.use("/mockServiceWorker.js", (_req, res) => {
        res.setHeader("Content-Type", "text/javascript")
        res.end(readFileSync(path.resolve(__dirname, "node_modules/msw/lib/mockServiceWorker.js")))
      })
    },
  }
}

/** Fails the build if any of mock mode reaches the bundle: its fake sign-in admits anyone. */
function noMockInBuild(): Plugin {
  return {
    name: "no-mock-in-build",
    apply: "build",
    generateBundle(_options, bundle) {
      const leaked = Object.values(bundle)
        .flatMap(output => (output.type === "chunk" ? Object.keys(output.modules) : []))
        .filter(id => id.includes("/src/mocks/") || id.includes("/node_modules/msw/"))
      if (leaked.length > 0) this.error(`mock mode reached the build: ${leaked.join(", ")}`)
    },
  }
}

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react(), chunkBudget(), mockWorker(), noMockInBuild()],

  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
      // A sibling source tree rather than an npm dependency: there is no workspace here,
      // and the package ships no build output. Vite compiles its TypeScript with the app's.
      "@business-operations/run-client": path.resolve(
        __dirname,
        "../packages/run-client/src/index.ts"
      ),
    },
  },

  build: {
    outDir: "build",
    // Off: maps would be served publicly beside the bundle.
    sourcemap: false,
    // Never inline a font as a data: URI: the CSP's `font-src 'self'` refuses it, and the
    // browser falls back silently. Everything else keeps Vite's default.
    assetsInlineLimit: file => (/\.(woff2?|ttf|otf)$/.test(file) ? false : undefined),
    rollupOptions: {
      output: {
        // By path, not by entry name: an entry list let a module both groups share land
        // in one and import the other back (`ui-vendor -> react-vendor -> ui-vendor`).
        // react-dom/client is the entry main.tsx imports; matching the package directory
        // keeps the whole renderer here instead of in the app chunk.
        manualChunks(id) {
          if (/node_modules\/(react|react-dom|scheduler|react-router)\//.test(id))
            return "react-vendor"
          if (id.includes("node_modules/@base-ui/")) return "ui-vendor"
          if (/node_modules\/(react-oidc-context|oidc-client-ts)\//.test(id)) return "auth-vendor"
        },
      },
    },
  },

  server: {
    port: 3000,
    open: true,
  },
})
