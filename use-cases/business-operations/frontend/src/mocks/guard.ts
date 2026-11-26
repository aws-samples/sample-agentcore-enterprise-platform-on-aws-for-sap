// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Mock mode: the console against made-up data, with a fake sign-in, for building and
 * reviewing screens without AWS or SAP.
 *
 * Two conditions, both required. `import.meta.env.DEV` is false in `npm run build`, so Vite
 * drops every branch that tests this and the mock code never reaches the bundle (the
 * `noMockInBuild` plugin in vite.config.ts fails the build if it does). `VITE_MOCK=1` is
 * the purpose-named opt-in: a plain `npm run dev` talks to a real deployment.
 */
export const MOCK_REQUESTED = import.meta.env.DEV && import.meta.env.VITE_MOCK === "1"

const LOOPBACK = new Set(["localhost", "127.0.0.1", "[::1]"])

/**
 * Fail closed: the fake sign-in admits anyone, so it refuses to start anywhere but a dev
 * build served from this machine.
 */
export function assertMockAllowed(): void {
  if (!import.meta.env.DEV || !LOOPBACK.has(window.location.hostname)) {
    throw new Error("Mock mode runs only in a development build served on localhost.")
  }
}
