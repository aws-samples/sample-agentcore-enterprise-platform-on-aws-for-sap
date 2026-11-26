// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useRef, type ReactNode } from "react"
import { cn } from "@/lib/utils"

/**
 * The detail template (plan, Page templates): a main column and a side column for what you
 * act on, stacked once the page is narrower than 960 px. A container query, so opening the
 * assistant reflows it. The side column is 320 px, or 384 px with `wide` (the case page's
 * decision panel, preview 1.4).
 *
 * Beside the main column the side column sticks below the top of the page, and it is never
 * taller than the room between where it starts on screen and the ask bar. So a panel that
 * scrolls inside itself (`min-h-0` and an `overflow-y-auto` body) keeps its header and its
 * button in view, before the page scrolls as well as after.
 */
export function DetailLayout({
  main,
  side,
  wide = false,
}: {
  main: ReactNode
  side: ReactNode
  wide?: boolean
}) {
  const asideRef = useRef<HTMLElement>(null)

  useEffect(() => {
    const aside = asideRef.current
    if (!aside) return
    let scroller: HTMLElement | null = aside.parentElement
    while (scroller && !/(auto|scroll)/.test(getComputedStyle(scroller).overflowY))
      scroller = scroller.parentElement
    if (!scroller) return
    const root = getComputedStyle(document.documentElement)
    const rem = parseFloat(root.fontSize) || 16
    const fit = () => {
      if (!scroller) return
      const bar =
        (parseFloat(root.getPropertyValue("--ask-bar-height")) || 3) * rem +
        2 * (parseFloat(root.getPropertyValue("--ask-bar-offset")) || 1) * rem
      const bottom = scroller.getBoundingClientRect().bottom
      const top = aside.getBoundingClientRect().top
      aside.style.setProperty("--side-max", `${Math.max(240, bottom - top - bar)}px`)
    }
    fit()
    scroller.addEventListener("scroll", fit, { passive: true })
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(fit)
    observer?.observe(scroller)
    observer?.observe(aside)
    return () => {
      scroller?.removeEventListener("scroll", fit)
      observer?.disconnect()
    }
  }, [])

  return (
    <div className="@container">
      <div
        className={cn(
          "grid items-start gap-6",
          wide
            ? "@[60rem]:grid-cols-[minmax(0,1fr)_24rem]"
            : "@[60rem]:grid-cols-[minmax(0,1fr)_20rem]"
        )}
      >
        <div className="flex min-w-0 flex-col gap-4">{main}</div>
        <aside
          ref={asideRef}
          className="flex min-h-0 flex-col gap-4 @[60rem]:sticky @[60rem]:top-6 @[60rem]:max-h-(--side-max)"
        >
          {side}
        </aside>
      </div>
    </div>
  )
}
