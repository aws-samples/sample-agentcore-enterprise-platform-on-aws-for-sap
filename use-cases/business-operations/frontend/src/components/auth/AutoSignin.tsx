// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { ReactNode, useEffect, useRef, useState, PropsWithChildren } from "react"
import { useQueryClient } from "@tanstack/react-query"
import { useAuth, useAutoSignin } from "react-oidc-context"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Spinner } from "@/components/ui/spinner"
import { useBrand } from "@/hooks/useBrand"
import { clearOperatorContext } from "@/lib/signOut"
import { useSilentSignInOn401 } from "@/hooks/useApi"

function AutoSigninContent({ children }: PropsWithChildren) {
  const auth = useAuth()
  const queryClient = useQueryClient()
  const brand = useBrand()
  useSilentSignInOn401()

  // Straight to the IdP rather than to a card that offers the same one choice. A stored
  // token expires an hour into a session, so an operator with a live Cognito session was
  // meeting a Sign In button on every reload after that — the button carried no
  // information the reload had not already implied. Cognito's own session cookie decides
  // what happens next: the authorize round-trip is silent while it holds and shows the
  // hosted login form once it does not. Signing out therefore lands on that form, not
  // here, because `signoutRedirect` drops the cookie on its way through.
  //
  // The hook latches after one attempt (`hasTriedSignin`) and skips a load that already
  // carries `?code=`, so a refused sign-in surfaces below instead of redirecting again.
  const { error } = useAutoSignin()

  // Drop the previous operator's cases, transcript, and focused-case URL whenever the
  // session ends. Hung on the authenticated→unauthenticated edge rather than on the
  // sign-out button, so an expired or revoked session wipes the same residue — and so
  // a failed sign-out redirect cannot leave it on screen for whoever signs in next.
  const wasAuthenticated = useRef(false)
  useEffect(() => {
    if (auth.isLoading) return
    if (auth.isAuthenticated) {
      wasAuthenticated.current = true
      return
    }
    if (!wasAuthenticated.current) return
    wasAuthenticated.current = false
    clearOperatorContext(queryClient)
  }, [auth.isAuthenticated, auth.isLoading, queryClient])

  // Authenticated first, so a failed background token renewal — which sets the same
  // error while the session is still usable — cannot blank a workspace mid-run.
  if (auth.isAuthenticated) {
    return <>{children}</>
  }

  if (error) {
    return (
      <main className="flex min-h-screen items-center justify-center px-6">
        <Card className="animate-rise-in w-full max-w-sm">
          <CardHeader>
            <CardTitle>
              <h1 className="text-xl font-semibold tracking-tight">Sign-in failed</h1>
            </CardTitle>
            <CardDescription>{error.message}</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            <Button onClick={() => auth.signinRedirect()} className="w-full">
              Try again
            </Button>
            {/* The only login-adjacent screen the app owns: everything else is the hosted
                form, which carries its own recovery link. So it points at that link rather
                than reimplementing the flow behind it: the reset codes, the policy check and
                the rate limits all belong to the pool. */}
            <p className="text-xs leading-relaxed text-muted-foreground">
              Forgotten your password? Choose <span className="font-medium">Try again</span> and use{" "}
              <span className="font-medium">Forgot your password?</span> on the sign-in page.
            </p>
          </CardContent>
        </Card>
      </main>
    )
  }

  // Loading the stored session, or waiting on the redirect that is already in flight: the
  // product mark and a spinner, not a blank page.
  return (
    <main className="flex min-h-screen flex-col items-center justify-center gap-4">
      <span
        aria-hidden="true"
        className="flex size-8 items-center justify-center rounded-lg bg-foreground text-sm font-semibold text-background"
      >
        {brand.monogram}
      </span>
      <span className="flex items-center gap-2 text-sm text-muted-foreground" role="status">
        <Spinner />
        Signing you in to {brand.name}
      </span>
    </main>
  )
}

export function AutoSignin({ children }: { children: ReactNode }) {
  const [mounted, setMounted] = useState(false)

  useEffect(() => {
    setMounted(true)
  }, [])

  if (!mounted) {
    return null
  }

  return <AutoSigninContent>{children}</AutoSigninContent>
}
