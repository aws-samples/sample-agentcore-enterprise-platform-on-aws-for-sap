// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { ScrollArea, ScrollBar } from "@/components/ui/scroll-area"
import { prettyJson } from "@/lib/prettyJson"
import { cn } from "@/lib/utils"

/**
 * A tool payload or a document, printed: mono `text-xs`, indented and unhighlighted, in a
 * scroll area a keyboard can reach. `pre`, not `pre-wrap`: wrapping a long value mid-token
 * re-flows the indentation that is the point of printing it, so long lines scroll.
 *
 * Not JSON? Then it renders as the text it is, wrapped. A SAP error string is not a failure
 * to format.
 */
export function JsonBlock({
  text,
  className,
  fit = false,
}: {
  text: string
  className?: string
  /**
   * As tall as what it prints, so the page's own scroll reads it, as on Run details. Otherwise
   * it scrolls inside itself past 24 rem: the cap sits on the viewport, which is what scrolls.
   */
  fit?: boolean
}) {
  const pretty = prettyJson(text)

  return (
    <div className={cn("flex flex-col gap-2 rounded-md bg-muted font-mono text-xs", className)}>
      <ScrollArea
        className={fit ? undefined : "[&>[data-slot=scroll-area-viewport]]:max-h-96"}
        aria-label="Printed payload"
      >
        <div className="p-3">
          {pretty ? (
            <pre className="text-foreground">{pretty.text}</pre>
          ) : (
            <pre className="whitespace-pre-wrap break-words text-foreground">{text}</pre>
          )}
        </div>
        <ScrollBar orientation="horizontal" />
      </ScrollArea>
      {pretty?.repaired && (
        // The payload is a preview with a byte budget, so a cut one is normal and says nothing
        // about the run. What would be wrong is printing a closed object and letting it pass
        // for the whole record.
        <p className="mx-3 mb-2 border-t pt-1.5 font-sans text-muted-foreground">
          Cut short in storage, and closed here so it prints.
        </p>
      )}
    </div>
  )
}
