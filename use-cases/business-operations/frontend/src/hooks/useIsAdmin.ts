// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useState } from "react"
import { useAuth } from "react-oidc-context"
import { getConfig } from "@/lib/config"

/** The `roles` claim of an access token, read without verifying it (display only). */
export function accessTokenRoles(token: string | undefined): string[] {
  try {
    const part = token?.split(".")[1]
    if (!part) return []
    const json = atob(part.replace(/-/g, "+").replace(/_/g, "/"))
    const roles = JSON.parse(json).roles
    return Array.isArray(roles) ? roles.map(String) : []
  } catch {
    return []
  }
}

/**
 * Whether the signed-in operator holds the admins group: `cognito:groups` on the ID token,
 * or under direct Entra mode the platform app's `roles` on the access token. It only
 * decides what the rail shows: the server gates every admin route on the same claim.
 */
export function useIsAdmin(): boolean {
  const auth = useAuth()
  const [cfg, setCfg] = useState<{ group: string | null; access: boolean }>({
    group: null,
    access: false,
  })
  useEffect(() => {
    let active = true
    getConfig()
      .then(
        c => active && setCfg({ group: c.adminsGroup ?? null, access: c.apiToken === "access" })
      )
      .catch(() => active && setCfg({ group: null, access: false }))
    return () => {
      active = false
    }
  }, [])
  const groups = cfg.access
    ? accessTokenRoles(auth.user?.access_token)
    : auth.user?.profile["cognito:groups"]
  return !!cfg.group && Array.isArray(groups) && groups.includes(cfg.group)
}
