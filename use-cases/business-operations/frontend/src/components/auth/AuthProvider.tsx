// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import {
  createCognitoAuthConfig,
  stripOidcCallbackParams,
  type CognitoAuthConfig,
} from "@/lib/auth"
import { lazy, Suspense, useEffect, useState, PropsWithChildren } from "react"
import { useNavigate } from "react-router"
import { AuthProvider as OidcAuthProvider } from "react-oidc-context"
import { AutoSignin } from "./AutoSignin"

// Mock mode's fake sign-in. Null in a build, so neither it nor its import is bundled.
const MockAuthProvider =
  import.meta.env.DEV && import.meta.env.VITE_MOCK === "1"
    ? lazy(() => import("@/mocks/MockAuthProvider"))
    : null

const AuthProvider = MockAuthProvider
  ? ({ children }: PropsWithChildren) => (
      <Suspense fallback={null}>
        <MockAuthProvider>
          <AutoSignin>{children}</AutoSignin>
        </MockAuthProvider>
      </Suspense>
    )
  : OidcBackedAuthProvider

function OidcBackedAuthProvider({ children }: PropsWithChildren) {
  const [authConfig, setAuthConfig] = useState<CognitoAuthConfig | null>(null)
  const [loading, setLoading] = useState(true)
  const navigate = useNavigate()

  useEffect(() => {
    async function loadConfig() {
      try {
        const config = await createCognitoAuthConfig()
        setAuthConfig(config)
      } catch (error) {
        // No fallback: the stack writes aws-exports.json, and without it there is nothing
        // to sign in against. The card below says so.
        console.error("Failed to load auth configuration:", error)
      } finally {
        setLoading(false)
      }
    }

    loadConfig()
  }, [])

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-screen text-xl">
        Loading authentication configuration...
      </div>
    )
  }

  if (!authConfig) {
    return (
      <div className="flex items-center justify-center min-h-screen text-xl">
        Failed to load authentication configuration
      </div>
    )
  }

  return (
    <OidcAuthProvider
      {...authConfig}
      // Removes the spent `code`/`state` so a refresh doesn't replay them. Goes through
      // the router (not a raw `window.history` write) so React Router's own search-params
      // cache stays in sync — otherwise a later `setSearchParams` call elsewhere (e.g.
      // focusing a case) reads its stale cached params and resurrects the consumed code/state
      // alongside whatever it's setting.
      onSigninCallback={() => {
        navigate(
          {
            pathname: window.location.pathname,
            search: stripOidcCallbackParams(window.location.search),
          },
          { replace: true }
        )
      }}
    >
      <AutoSignin>{children}</AutoSignin>
    </OidcAuthProvider>
  )
}

export { AuthProvider }
