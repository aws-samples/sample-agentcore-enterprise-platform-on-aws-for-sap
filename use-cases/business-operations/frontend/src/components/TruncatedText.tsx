// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useCallback, useEffect, useRef, useState } from "react"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"

/**
 * One line of text that truncates (decision 18), with the full value in a tooltip once it is
 * cut off. The tooltip opens on hover and on keyboard focus, so the text takes a tab stop only
 * while some of it is hidden.
 */
export function TruncatedText({ text, className }: { text: string; className?: string }) {
  const [el, setEl] = useState<HTMLSpanElement | null>(null)
  const [cut, setCut] = useState(false)
  const ref = useCallback((node: HTMLSpanElement | null) => setEl(node), [])
  const elRef = useRef(el)
  elRef.current = el

  useEffect(() => {
    if (!el) return
    const measure = () => {
      const node = elRef.current
      if (node) setCut(node.scrollWidth > node.clientWidth + 1)
    }
    measure()
    // The box can stay the same width while its text widens (the web font landing), which
    // a ResizeObserver does not report, so the font load re-measures too.
    const fonts = typeof document !== "undefined" ? document.fonts : undefined
    fonts?.addEventListener?.("loadingdone", measure)
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure)
    observer?.observe(el)
    return () => {
      fonts?.removeEventListener?.("loadingdone", measure)
      observer?.disconnect()
    }
  }, [el, text])

  const classes = cn("block truncate", className)
  if (!cut)
    return (
      <span ref={ref} className={classes}>
        {text}
      </span>
    )
  return (
    <Tooltip>
      {/* Focusable only while cut off, so the full value reaches keyboard users too. */}
      <TooltipTrigger render={<span ref={ref} className={classes} />} tabIndex={0}>
        {text}
      </TooltipTrigger>
      <TooltipContent>{text}</TooltipContent>
    </Tooltip>
  )
}
