// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useCallback, useEffect } from "react"
import { useAuth } from "react-oidc-context"
import { getConfig } from "@/lib/config"
import { onUnauthorized } from "@/lib/apiFetch"

type Tokens = { id_token?: string; access_token?: string } | null | undefined

/** The token the API takes off a user: `apiToken: "access"` in direct Entra mode. */
async function apiTokenOf(user: Tokens): Promise<string | undefined> {
  return (await getConfig()).apiToken === "access" ? user?.access_token : user?.id_token
}

/**
 * Returns a getter for the token our own REST API and run channel take: the id token on
 * Cognito, the access token when aws-exports.json says `apiToken: "access"` (direct Entra
 * mode). Undefined when signed out, or when the session expired and could not renew.
 *
 * `automaticSilentRenew` keeps tokens fresh in the background, but its timer can be
 * throttled while the tab is backgrounded, so a user returning to an idle tab can still
 * hold a stale one. Call the getter right before each request rather than reading
 * `auth.user` at render; it renews silently when the token is expired or about to be.
 */
export function useApiToken(): () => Promise<string | undefined> {
  const auth = useAuth()
  return useCallback(async () => {
    const user = auth.user
    if (!user) return undefined
    if (user.expired || (user.expires_in !== undefined && user.expires_in < 60)) {
      try {
        return await apiTokenOf(await auth.signinSilent())
      } catch {
        // Already expired and renewal failed: report no token rather than one that will
        // only 401. Still inside the pre-expiry buffer: it is valid for a few more seconds.
        return user.expired ? undefined : apiTokenOf(user)
      }
    }
    return apiTokenOf(user)
  }, [auth])
}

/**
 * Let a 401 from the API renew the session silently and retry. A token the clock says is
 * fresh can still be refused (revoked, or the tab slept through its renewal), and the
 * refresh-token grant answers that without leaving the page. Mounted once, under auth.
 */
export function useSilentSignInOn401(): void {
  const auth = useAuth()
  useEffect(() => {
    onUnauthorized(async () => apiTokenOf(await auth.signinSilent()))
    return () => onUnauthorized(null)
  }, [auth])
}

/** `useApiToken` for a request that cannot go without one: the getter throws instead. */
export function useApi(): () => Promise<string> {
  const getApiToken = useApiToken()
  return useCallback(async () => {
    const token = await getApiToken()
    if (!token) throw new Error("Not authenticated")
    return token
  }, [getApiToken])
}
