// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { defineConfig } from "@playwright/test"

/**
 * Browser tests against mock mode: no AWS, no SAP, a fake sign-in (see README, Mock mode).
 *
 * The dev server is Vite's own, on its loopback bind. `PW_CHANNEL=chrome` uses the
 * installed Chrome instead of Playwright's download (`npx playwright install chromium`).
 */
const PORT = 3100

export default defineConfig({
  testDir: "e2e",
  outputDir: "e2e/.results",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: [["list"]],
  use: {
    baseURL: `http://localhost:${PORT}`,
    viewport: { width: 1440, height: 900 },
    channel: process.env.PW_CHANNEL || undefined,
    trace: "retain-on-failure",
  },
  webServer: {
    command: `node node_modules/vite/bin/vite.js --port ${PORT} --strictPort --open false`,
    env: { VITE_MOCK: "1" },
    url: `http://localhost:${PORT}`,
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
  },
})
