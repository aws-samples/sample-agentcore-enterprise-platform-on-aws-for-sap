// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { defineConfig } from "vitest/config"
import react from "@vitejs/plugin-react"
import path from "path"

export default defineConfig({
  plugins: [react()],
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: "./test/setup.ts",
    // Timestamps render through `toLocaleDateString`, so a runner west of UTC-9 would
    // read a fixture's date as the day before and fail an assertion on the label.
    env: { TZ: "UTC" },
    // Tests of a module sit next to it under `src/`; `test/drift/` holds the guards that
    // read repo files rather than importing anything. Both globs are load-bearing — drop
    // the second and the drift guards stop being collected, silently.
    //
    // The sibling package's tests run on this runner too. It owns the only vitest install in
    // the repo, and a second config there would be a second set of the same four options.
    include: [
      "src/**/*.{test,spec}.{ts,tsx}",
      "test/**/*.{test,spec}.{ts,tsx}",
      "../packages/*/src/**/*.{test,spec}.ts",
    ],
  },
  // The package sits outside this project root, and Vite serves nothing outside the root
  // unless it is allowed — without this the sibling tests are collected and then fail to
  // load with `Cannot find module '/@fs/…'`.
  server: { fs: { allow: [path.resolve(__dirname, ".."), __dirname] } },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
      "@business-operations/run-client": path.resolve(__dirname, "../packages/run-client/src/index.ts"),
    },
  },
})
