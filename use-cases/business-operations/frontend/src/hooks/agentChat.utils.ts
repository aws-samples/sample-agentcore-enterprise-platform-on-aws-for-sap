// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The piece of `useAgentChat` that stands on its own: where the case in context comes
 * from. Everything else in that file is the run lifecycle and belongs with it.
 */

import { useLocation, useSearchParams } from "react-router"
import { tryNormalizeCaseId } from "@/lib/caseKey"

/**
 * The case the assistant is talking about, taken from the URL on whatever route is
 * mounted: `?case=` in the workspace, `?case_id=` on the tickets dashboard.
 *
 * Reading the URL rather than accepting a prop is what makes the panel ambient. Every
 * route that has a case already puts it in the address bar — so the assistant knows
 * which case without the page telling it, and a shared link carries the context too.
 */
export function useCaseInContext(): string | null {
  const [params] = useSearchParams()
  const { pathname } = useLocation()
  // The case page (`/cases/:caseId`) names it in the path; older links in the query.
  const inPath = /^\/cases\/([^/]+)/.exec(pathname)?.[1]
  return tryNormalizeCaseId(
    (inPath ? decodeURIComponent(inPath) : null) ?? params.get("case") ?? params.get("case_id")
  )
}
