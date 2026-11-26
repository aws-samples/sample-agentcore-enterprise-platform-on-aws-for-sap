// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { WebStorageStateStore } from "oidc-client-ts"
import { getConfig } from "./config"

/**
 * Query params oidc-client-ts adds to the redirect URI on the way back from Cognito.
 * They're single-use — Cognito's `code` and the request's `state` are both consumed
 * the first time they're processed, so leaving them in the URL breaks a later refresh.
 */
const OIDC_CALLBACK_PARAMS = ["code", "state", "session_state"]

/** Drops the OIDC callback params from a query string, keeping every other param intact. */
export function stripOidcCallbackParams(search: string): string {
  const params = new URLSearchParams(search)
  for (const key of OIDC_CALLBACK_PARAMS) params.delete(key)
  return params.toString()
}

/**
 * Both OIDC stores, pinned to `sessionStorage`.
 *
 * Pinning *both* is not redundant — oidc-client-ts (3.5.0) splits its defaults: `userStore`
 * defaults to `sessionStorage`, but `stateStore` defaults to `localStorage`. So the PKCE
 * verifier lands in `localStorage` on the library's own default, and dropping the `userStore`
 * override alone would leave it there.
 *
 * `sessionStorage` is per-tab and cleared on tab close, so an XSS that reads the store gets a
 * credential that dies with the tab instead of a refresh token that outlives it. The trade is
 * deliberate and user-visible: closing the tab signs the operator out, and a new tab starts
 * cold. `automaticSilentRenew` covers the session while the tab is open, and it renews through
 * the **refresh-token grant** (`UserManager.signinSilent` takes that branch whenever the stored
 * user has a `refresh_token`, which a Cognito code grant always issues) — a plain `fetch`, not
 * an iframe. That is why the distribution's CSP can carry `frame-src 'none'`; the library's
 * iframe fallback needs a `silent_redirect_uri` this app has never configured.
 *
 * Each store takes an explicit prefix because `State.clearStaleState` iterates every key in the
 * *state* store and removes whatever it cannot parse as a `State` — on the shared default prefix
 * that sweep can see the stored user. The two must be siblings rather than nested: `getAllKeys`
 * filters by `startsWith`, so leaving the user on the default `oidc.` and the state under
 * `oidc.state.` would make every state key enumerable as a user key.
 */
function sessionStores(): Pick<CognitoAuthConfig, "userStore" | "stateStore"> {
  if (typeof window === "undefined") return {}
  return {
    userStore: new WebStorageStateStore({ store: window.sessionStorage, prefix: "oidc.user." }),
    stateStore: new WebStorageStateStore({ store: window.sessionStorage, prefix: "oidc.state." }),
  }
}

/** Shape of the OIDC config produced by {@link createCognitoAuthConfig}. */
export interface CognitoAuthConfig {
  authority?: string
  metadataUrl?: string
  client_id?: string
  redirect_uri?: string
  post_logout_redirect_uri?: string
  response_type?: string
  scope?: string
  automaticSilentRenew?: boolean
  userStore?: WebStorageStateStore
  stateStore?: WebStorageStateStore
}

/** Creates the OIDC auth config from aws-exports.json (via the shared config loader). */
export async function createCognitoAuthConfig(): Promise<CognitoAuthConfig> {
  const cfg = await getConfig()
  return {
    authority: cfg.authority,
    metadataUrl: cfg.metadataUrl,
    client_id: cfg.client_id,
    redirect_uri: cfg.redirect_uri,
    post_logout_redirect_uri: cfg.post_logout_redirect_uri,
    response_type: cfg.response_type || "code",
    scope: cfg.scope || "openid email profile",
    automaticSilentRenew: cfg.automaticSilentRenew ?? true,
    ...sessionStores(),
  }
}
