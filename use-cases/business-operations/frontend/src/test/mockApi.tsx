// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import type { ReactNode } from "react"
import { afterAll, afterEach, beforeAll } from "vitest"
import { render } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { RouterProvider, Route, Routes, createMemoryRouter, useLocation } from "react-router"
import { setupServer } from "msw/node"
import { handlers } from "@/mocks/handlers"
import { TooltipProvider } from "@/components/ui/tooltip"

/**
 * Page tests against mock mode's own API: the handlers `npm run dev:mock` serves, run in
 * Node, so a page test exercises the same shapes and the same per-role access a person
 * sees in mock mode. A test file mocks `@/lib/config` to point `apiUrl` at
 * {@link MOCK_API_URL}, and signs in by role with {@link signInAs}.
 */
export const MOCK_API_URL = `${window.location.origin}/__mock_api`

export function serveMockApi(): void {
  const server = setupServer(...handlers)
  beforeAll(() => server.listen({ onUnhandledRequest: "error" }))
  afterEach(() => server.resetHandlers())
  afterAll(() => server.close())
}

/** Mock mode's role switcher: `operator`, `analyst`, `approver`, `worker` or `none`. */
export function signInAs(role: string): void {
  window.localStorage.setItem("mock.role", role)
}

function Where() {
  const l = useLocation()
  return <output aria-label="location">{l.pathname + l.search}</output>
}

export function renderRoutes(path: string, routes: { path: string; element: ReactNode }[]) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  // A data router, as the app uses, so a page's `useBlocker` works in a test too.
  const router = createMemoryRouter(
    [
      {
        path: "*",
        element: (
          <>
            <Routes>
              {routes.map(r => (
                <Route key={r.path} path={r.path} element={r.element} />
              ))}
              <Route path="*" element={<div>elsewhere</div>} />
            </Routes>
            <Where />
          </>
        ),
      },
    ],
    { initialEntries: [path] }
  )
  return render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <RouterProvider router={router} />
      </TooltipProvider>
    </QueryClientProvider>
  )
}
