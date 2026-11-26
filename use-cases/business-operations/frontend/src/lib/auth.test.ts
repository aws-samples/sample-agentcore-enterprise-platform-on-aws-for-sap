// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, beforeEach, vi } from "vitest"
import { stripOidcCallbackParams, createCognitoAuthConfig } from "./auth"

// Only `getConfig` is used, and none of these assertions care what it returns — the point is
// to reach the store construction without a fetch.
vi.mock("./config", () => ({ getConfig: async () => ({}) }))

/**
 * No credential may sit in `localStorage`. Asserted through the stores' own `set` rather
 * than by reading the private field
 * they wrap, so a oidc-client-ts version bump that renames internals doesn't fail this — only
 * a change that actually puts a credential back in `localStorage` does.
 */
describe("OIDC token storage", () => {
  beforeEach(() => {
    window.sessionStorage.clear()
    window.localStorage.clear()
  })

  it("keeps the user out of localStorage", async () => {
    await (await createCognitoAuthConfig()).userStore!.set("user", "token-material")

    expect(window.sessionStorage.length).toBe(1)
    expect(window.localStorage.length).toBe(0)
  })

  it("keeps the PKCE verifier out of localStorage — its library default is localStorage", async () => {
    await (await createCognitoAuthConfig()).stateStore!.set("state-id", "code-verifier")

    expect(window.sessionStorage.length).toBe(1)
    expect(window.localStorage.length).toBe(0)
  })

  it("gives the two stores disjoint keyspaces, so clearStaleState cannot sweep the user", async () => {
    await (await createCognitoAuthConfig()).userStore!.set("k", "user")
    await (await createCognitoAuthConfig()).stateStore!.set("k", "state")

    expect(window.sessionStorage.length).toBe(2)
    expect(await (await createCognitoAuthConfig()).userStore!.getAllKeys()).toEqual(["k"])
    expect(await (await createCognitoAuthConfig()).stateStore!.getAllKeys()).toEqual(["k"])
  })
})

describe("stripOidcCallbackParams", () => {
  it("removes code/state/session_state but keeps other params", () => {
    const result = stripOidcCallbackParams("?code=abc&state=xyz&case=5100001013-2021")
    expect(result).toBe("case=5100001013-2021")
  })

  it("removes session_state too", () => {
    const result = stripOidcCallbackParams("?code=abc&state=xyz&session_state=s1&case=1")
    expect(result).toBe("case=1")
  })

  it("returns empty string when only callback params are present", () => {
    expect(stripOidcCallbackParams("?code=abc&state=xyz")).toBe("")
  })

  it("is a no-op when no callback params are present", () => {
    expect(stripOidcCallbackParams("?case=1&status=open")).toBe("case=1&status=open")
  })
})
