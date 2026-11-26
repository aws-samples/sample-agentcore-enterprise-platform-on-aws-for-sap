// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useCallback } from "react"
import { usePreference } from "@/hooks/usePreference"

const NONE: string[] = []

/**
 * The packs a person starred on the packs index, which the sidebar lists (decision 17).
 * A preference of this browser, kept across sign-out like the theme, and shared, so a star
 * shows in the sidebar at once.
 */
export function useStarredPacks(): [string[], (packId: string) => void] {
  const [starred, setStarred] = usePreference<string[]>("ui.packs.starred", NONE)
  const toggle = useCallback(
    (packId: string) =>
      setStarred(
        starred.includes(packId) ? starred.filter(id => id !== packId) : [...starred, packId]
      ),
    [starred, setStarred]
  )
  return [starred, toggle]
}
