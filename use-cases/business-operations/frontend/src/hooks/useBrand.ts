// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useState } from "react"
import { BRAND } from "@/brand"
import { getConfig } from "@/lib/config"

export type Brand = typeof BRAND

/** What `console.brand.monogram` may be: 1 to 3 letters or digits, nothing else. */
const MONOGRAM = /^[\p{L}\p{N}]{1,3}$/u

/**
 * The console's name and subtitle from `console.brand` in `aws-exports.json`, falling back to
 * `src/brand.ts` until the config is read or when it carries none. The monogram is
 * `console.brand.monogram` when set (1 to 3 letters or digits); otherwise a renamed console
 * gets one from its name's first two words. The logo and release stay `brand.ts`'s. The name
 * is also the window title.
 */
export function useBrand(): Brand {
  const [brand, setBrand] = useState<Brand>(BRAND)
  useEffect(() => {
    let active = true
    getConfig()
      .then(c => {
        if (!active || !c.brand) return
        const name = c.brand.name || BRAND.name
        setBrand({
          ...BRAND,
          name,
          subtitle: c.brand.subtitle || BRAND.subtitle,
          monogram: MONOGRAM.test(c.brand.monogram ?? "")
            ? c.brand.monogram!
            : name === BRAND.name
              ? BRAND.monogram
              : monogramOf(name),
        })
      })
      .catch(() => undefined)
    return () => {
      active = false
    }
  }, [])
  useEffect(() => {
    document.title = brand.name
  }, [brand.name])
  return brand
}

/** "Accounts Desk" → "AD", "Finance" → "FI". */
export function monogramOf(name: string): string {
  const words = name.split(/\s+/).filter(w => /\p{L}|\d/u.test(w))
  const letters =
    words.length >= 2 ? words[0][0] + words[1][0] : (words[0] ?? BRAND.monogram).slice(0, 2)
  return letters.toUpperCase()
}
