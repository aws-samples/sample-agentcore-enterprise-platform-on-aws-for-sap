// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"

const signinRedirect = vi.fn()
let authState: {
  isLoading: boolean
  isAuthenticated: boolean
  error?: Error
  signinRedirect: typeof signinRedirect
}

// `useAutoSignin` reads the auth context through its own module-local `useAuth`, which a
// mocked export cannot reach, so it is stubbed rather than exercised — the redirect and
// its one-attempt latch belong to the library. What these tests hold is the part this
// component decides: which of the three states it renders, and that it delegates at all.
vi.mock("react-oidc-context", () => ({
  useAuth: () => authState,
  useAutoSignin: vi.fn(() => ({ error: authState.error })),
}))

import { AutoSignin } from "@/components/auth/AutoSignin"
import { useAutoSignin } from "react-oidc-context"

function renderShell() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <AutoSignin>
        <div>workspace</div>
      </AutoSignin>
    </QueryClientProvider>
  )
}

beforeEach(() => {
  authState = { isLoading: false, isAuthenticated: false, signinRedirect }
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe("AutoSignin", () => {
  it("hands a signed-out load to the IdP instead of offering a sign-in card", () => {
    // The regression: a stored token expires an hour in, so every reload after that met a
    // button offering the one thing the reload already implied.
    const { container } = renderShell()

    expect(useAutoSignin).toHaveBeenCalled()
    expect(screen.queryByRole("button")).toBeNull()
    expect(screen.queryByText("workspace")).toBeNull()
    expect(container.querySelector(".animate-spin")).not.toBeNull()
  })

  it("keeps the workspace up when a background token renewal fails", () => {
    // Same error field, session still usable: ordering this after the authenticated check
    // is what stops a silent-renew failure from blanking a run in progress.
    authState = { ...authState, isAuthenticated: true, error: new Error("renew failed") }

    renderShell()

    expect(screen.getByText("workspace")).toBeInTheDocument()
    expect(screen.queryByText("Sign-in failed")).toBeNull()
  })

  it("offers a retry when the sign-in itself failed", () => {
    // The hook redirects once and then latches, so without this the operator would sit on
    // a spinner with nothing to act on.
    authState = { ...authState, error: new Error("state mismatch") }

    renderShell()
    fireEvent.click(screen.getByRole("button", { name: "Try again" }))

    expect(screen.getByText("state mismatch")).toBeInTheDocument()
    expect(signinRedirect).toHaveBeenCalledTimes(1)
    // The only login-adjacent screen the app owns, so it is the only place that can name
    // the hosted page's recovery link for someone who is locked out rather than unlucky.
    expect(screen.getByText("Forgot your password?")).toBeInTheDocument()
  })
})
