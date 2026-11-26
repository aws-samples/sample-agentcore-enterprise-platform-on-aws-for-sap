// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useCallback, useState } from "react"

/**
 * A preference kept in `localStorage` under a `ui.` key, so `lib/signOut.ts`'s allowlist
 * decides whether it survives sign-out. Reads once on mount; a value that does not parse
 * falls back to the default.
 */
export function useStoredState<T>(key: string, fallback: T | (() => T)): [T, (next: T) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = window.localStorage.getItem(key)
      if (raw != null) return JSON.parse(raw) as T
    } catch {
      // A malformed value is the same as none.
    }
    return typeof fallback === "function" ? (fallback as () => T)() : fallback
  })
  const set = useCallback(
    (next: T) => {
      setValue(next)
      try {
        window.localStorage.setItem(key, JSON.stringify(next))
      } catch {
        // Persisting is best-effort; never fail a render over it.
      }
    },
    [key]
  )
  return [value, set]
}
