// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { setupWorker } from "msw/browser"
import { assertMockAllowed } from "./guard"
import { handlers } from "./handlers"

/**
 * Start the in-browser API before the app renders. The worker script is served by the dev
 * server only (`mockWorker` in vite.config.ts), never from `public/`, so a build cannot
 * ship it.
 */
export async function startMock(): Promise<void> {
  assertMockAllowed()
  await setupWorker(...handlers).start({
    onUnhandledRequest(request, print) {
      // Fonts, modules and the dev server's own requests are fine; an API call with no
      // handler is a gap in the mock, and should say so.
      if (new URL(request.url).pathname.startsWith("/__mock_api")) print.error()
    },
    quiet: true,
  })
}
