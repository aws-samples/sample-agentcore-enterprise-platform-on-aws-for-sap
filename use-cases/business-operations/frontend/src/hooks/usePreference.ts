// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useCallback, useSyncExternalStore } from "react"

/**
 * A preference kept in `localStorage` under a `ui.` key and shared by every component that
 * reads it, so starring a pack on the packs index moves it into the sidebar at once.
 * `lib/signOut.ts`'s allowlist decides whether it survives sign-out. `useStoredState` is the
 * same thing for a value only one component reads.
 */
const listeners = new Map<string, Set<() => void>>()
const cache = new Map<string, { raw: string | null; value: unknown }>()

function read<T>(key: string, fallback: T): T {
  let raw: string | null = null
  try {
    raw = window.localStorage.getItem(key)
  } catch {
    // Storage can throw in private browsing: the default stands.
  }
  const hit = cache.get(key)
  if (hit && hit.raw === raw) return hit.value as T
  let value: T = fallback
  if (raw != null) {
    try {
      value = JSON.parse(raw) as T
    } catch {
      // A malformed value is the same as none.
    }
  }
  cache.set(key, { raw, value })
  return value
}

export function setPreference<T>(key: string, value: T): void {
  try {
    window.localStorage.setItem(key, JSON.stringify(value))
  } catch {
    // Best effort; the in-memory value still changes for this tab.
    cache.set(key, { raw: JSON.stringify(value), value })
  }
  listeners.get(key)?.forEach(l => l())
}

export function usePreference<T>(key: string, fallback: T): [T, (next: T) => void] {
  const subscribe = useCallback(
    (listener: () => void) => {
      const set = listeners.get(key) ?? new Set()
      set.add(listener)
      listeners.set(key, set)
      const onStorage = (e: StorageEvent) => e.key === key && listener()
      window.addEventListener("storage", onStorage)
      return () => {
        set.delete(listener)
        window.removeEventListener("storage", onStorage)
      }
    },
    [key]
  )
  const value = useSyncExternalStore(
    subscribe,
    () => read(key, fallback),
    () => fallback
  )
  const set = useCallback((next: T) => setPreference(key, next), [key])
  return [value, set]
}
