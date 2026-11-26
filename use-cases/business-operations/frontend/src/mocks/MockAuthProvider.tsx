// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useMemo, useState, type PropsWithChildren } from "react"
import { AuthContext, type AuthContextProps } from "react-oidc-context"
import type { User } from "oidc-client-ts"
import { assertMockAllowed } from "./guard"
import { ROLES, currentRole, switchRole } from "./roles"
import { SCENARIO_KEY, STACK_KEY } from "./handlers"

/** An unsigned token in the JWT shape: only mock mode's handlers ever read it. */
function fakeToken(claims: Record<string, unknown>): string {
  const enc = (o: unknown) =>
    btoa(JSON.stringify(o)).replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_")
  return `${enc({ alg: "none", typ: "JWT" })}.${enc(claims)}.mock`
}

/**
 * The signed-in state react-oidc-context would provide, for whichever role the dev toolbar
 * picked. Nothing here talks to an identity provider.
 */
export default function MockAuthProvider({ children }: PropsWithChildren) {
  assertMockAllowed()
  const role = currentRole()

  const value = useMemo<AuthContextProps>(() => {
    const exp = Math.floor(Date.now() / 1000) + 8 * 3600
    const [given_name, family_name] = role.name.split(" ")
    const profile = {
      sub: `mock-${role.id}`,
      email: role.email,
      name: role.name,
      given_name,
      family_name,
      "cognito:groups": role.groups,
      iss: "https://auth.mock.invalid",
      aud: "mock-client",
      exp,
      iat: exp - 8 * 3600,
    }
    const token = fakeToken(profile)
    const user = {
      id_token: token,
      access_token: token,
      token_type: "Bearer",
      profile,
      expires_at: exp,
      expires_in: 8 * 3600,
      expired: false,
      scopes: ["openid", "email", "profile"],
      state: undefined,
      session_state: null,
      refresh_token: undefined,
      toStorageString: () => "",
    } as unknown as User
    const noop = async () => undefined
    return {
      user,
      isLoading: false,
      isAuthenticated: true,
      activeNavigator: undefined,
      error: undefined,
      settings: {
        authority: "https://auth.mock.invalid",
        client_id: "mock-client",
        redirect_uri: "/",
      },
      events: {} as AuthContextProps["events"],
      clearStaleState: noop,
      removeUser: noop,
      signinPopup: async () => user,
      signinSilent: async () => user,
      signinRedirect: noop,
      signinResourceOwnerCredentials: async () => user,
      signoutRedirect: async () => switchRole(ROLES[0].id),
      signoutPopup: noop,
      signoutSilent: noop,
      querySessionStatus: async () => null,
      revokeTokens: noop,
      startSilentRenew: () => {},
      stopSilentRenew: () => {},
    } as AuthContextProps
  }, [role])

  return (
    <AuthContext.Provider value={value}>
      {children}
      <MockToolbar />
    </AuthContext.Provider>
  )
}

/** Where the toolbar sits: just left of the header's search, or nowhere while a modal is up. */
function useToolbarPlace(): { top: number; right: number } | null {
  const [place, setPlace] = useState<{ top: number; right: number } | null>(null)
  useEffect(() => {
    let frame = 0
    const measure = () => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(() => {
        const modal = document.querySelector(
          '[data-slot="sheet-content"], [data-slot="dialog-content"], [data-slot="alert-dialog-content"]'
        )
        const search = document.querySelector("header")?.lastElementChild
        if (modal || !search) return setPlace(null)
        const r = search.getBoundingClientRect()
        const next = {
          top: Math.round(r.top + (r.height - 24) / 2),
          right: Math.round(window.innerWidth - r.left + 8),
        }
        setPlace(p => (p && p.top === next.top && p.right === next.right ? p : next))
      })
    }
    measure()
    const observer = new MutationObserver(measure)
    observer.observe(document.body, { childList: true, subtree: true, attributes: true })
    window.addEventListener("resize", measure)
    return () => {
      cancelAnimationFrame(frame)
      observer.disconnect()
      window.removeEventListener("resize", measure)
    }
  }, [])
  return place
}

const selectStyle = { font: "inherit", borderRadius: 4, padding: "1px 4px" } as const
const hidden = { position: "absolute", width: 1, height: 1, overflow: "hidden" } as const

/**
 * Which role you are, and a switch. A small pill in the header just left of search, where
 * nothing else sits at any width, so it covers no band and no page text; it opens downwards
 * to the role, data and stack switches. It steps aside while a sheet or dialog is open.
 */
function MockToolbar() {
  const role = currentRole()
  const place = useToolbarPlace()
  const [open, setOpen] = useState(false)
  if (!place) return null
  return (
    <div
      role="region"
      aria-label="Mock mode"
      style={{
        position: "fixed",
        top: place.top,
        right: place.right,
        zIndex: 2147483647,
        font: "12px/1.4 system-ui, sans-serif",
      }}
    >
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen(o => !o)}
        style={{
          height: 24,
          padding: "0 10px",
          border: 0,
          borderRadius: 12,
          background: "#202020",
          color: "#fff",
          font: "inherit",
          cursor: "pointer",
        }}
      >
        <strong>Mock data</strong> · {role.label} {open ? "▴" : "▾"}
      </button>
      {open && (
        <div
          style={{
            position: "absolute",
            top: 28,
            right: 0,
            display: "flex",
            flexDirection: "column",
            gap: 6,
            padding: 8,
            borderRadius: 8,
            background: "#202020",
            color: "#fff",
            whiteSpace: "nowrap",
          }}
        >
          <label style={{ display: "flex", gap: 6, alignItems: "center" }}>
            <span style={hidden}>Signed in as</span>
            <select value={role.id} onChange={e => switchRole(e.target.value)} style={selectStyle}>
              {ROLES.map(r => (
                <option key={r.id} value={r.id}>
                  {r.label}: {r.name}
                </option>
              ))}
            </select>
          </label>
          <label style={{ display: "flex", gap: 6, alignItems: "center" }}>
            <span style={hidden}>Data</span>
            <select
              value={window.localStorage.getItem(SCENARIO_KEY) ?? "demo"}
              onChange={e => {
                window.localStorage.setItem(SCENARIO_KEY, e.target.value)
                window.location.reload()
              }}
              style={selectStyle}
            >
              <option value="demo">Demo cases</option>
              <option value="empty">New deployment</option>
            </select>
          </label>
          <label style={{ display: "flex", gap: 6, alignItems: "center" }}>
            <span style={hidden}>Stack</span>
            <select
              value={window.localStorage.getItem(STACK_KEY) ?? "development"}
              onChange={e => {
                window.localStorage.setItem(STACK_KEY, e.target.value)
                window.location.reload()
              }}
              style={selectStyle}
            >
              <option value="development">Development stack</option>
              <option value="quality">Quality stack</option>
            </select>
          </label>
        </div>
      )}
    </div>
  )
}
