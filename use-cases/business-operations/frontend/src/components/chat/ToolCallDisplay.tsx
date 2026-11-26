// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState } from "react"
import { Loader2, Check, CircleHelp, ChevronRight, ChevronDown, XCircle } from "lucide-react"
import type { ToolRenderProps } from "@/components/chat/types"
import { TONE_BANNER, TONE_TEXT } from "@/lib/statusTone"
import { toolTick } from "@/lib/toolLabel"
import { JsonBlock } from "@/components/ui/json-block"
import { Button } from "@/components/ui/button"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { cn } from "@/lib/utils"

/**
 * One posting line on the trace spine: what the agent did, to what, and how it ended.
 *
 * The row says "read A_SupplierInvoice 5100001976", not "Using tool: odata_read" — see
 * lib/toolLabel.ts for where the verb and the object come from, and why past tense is
 * reserved for a call that actually completed.
 *
 * Only the exceptions carry weight. A settled read gets a hairline check in soft ink; a
 * posted write gets `released`, because that one is the outcome the operator is looking
 * for. Failed and unconfirmed render at full strength, so on a screen of
 * twelve ticks the eye goes to the three that need it.
 */
export function ToolCallDisplay({ name, args, status, result }: ToolRenderProps) {
  const [expanded, setExpanded] = useState(false)
  const tick = toolTick(name, args, status)

  return (
    <Collapsible open={expanded} onOpenChange={setExpanded} className="text-xs">
      <CollapsibleTrigger
        render={
          <Button
            variant="ghost"
            size="xs"
            className="group h-auto w-full justify-start gap-1.5 px-0 py-px text-left font-normal hover:bg-transparent"
          />
        }
      >
        {/* Twelve of these stack up on one run, so the affordance sits at a third of the
            label's weight and comes up to meet the pointer. */}
        {expanded ? (
          <ChevronDown className="shrink-0 text-subtle-foreground transition-colors group-hover:text-muted-foreground" />
        ) : (
          <ChevronRight className="shrink-0 text-subtle-foreground transition-colors group-hover:text-muted-foreground" />
        )}
        <span
          className={cn(
            "shrink-0 transition-colors group-hover:text-foreground",
            status === "streaming" || status === "executing"
              ? "text-foreground"
              : "text-muted-foreground"
          )}
        >
          {tick.verb}
        </span>
        {tick.subject && (
          <span
            className={cn(
              "min-w-0 truncate font-mono",
              tick.writes ? "text-foreground" : "text-muted-foreground"
            )}
          >
            {tick.subject}
          </span>
        )}
        {/* The outcome sits with the row it belongs to, not flushed to the far edge. On a
            wide conversation `ml-auto` put the check some 500px from the object it settled,
            which reads as a column of marks against nothing in particular.
            Live tool work reads as ink advancing down the spine, not as a status tone. */}
        {(status === "streaming" || status === "executing") && (
          <Loader2 className="shrink-0 animate-spin text-muted-foreground motion-reduce:animate-none" />
        )}
        {status === "complete" && (
          <Check
            className={cn("shrink-0", tick.writes ? TONE_TEXT.success : "text-subtle-foreground")}
            aria-label="Tool call succeeded"
          />
        )}
        {status === "incomplete" && (
          <CircleHelp
            className={`shrink-0 ${TONE_TEXT.attention}`}
            aria-label="Tool outcome not confirmed"
          />
        )}
        {status === "error" && (
          <XCircle className={`shrink-0 ${TONE_TEXT.danger}`} aria-label="Tool call failed" />
        )}
      </CollapsibleTrigger>

      {status === "incomplete" && (
        <div
          className={`mt-1 rounded-lg border-l-2 p-2 text-xs ${TONE_BANNER.attention}`}
          role="status"
        >
          Tool outcome not confirmed. Verify the target system and case state before retrying; the
          action may already have completed.
        </div>
      )}

      {/* Polite, not assertive: replayed history mounts these by the handful, and a
          settled past failure has no business interrupting a screen reader mid-sentence. */}
      {status === "error" && (
        <div
          className={`mt-1 rounded-lg border-l-2 p-2 text-xs ${TONE_BANNER.danger}`}
          role="status"
        >
          This tool call failed. The case state may not reflect the intended change.
        </div>
      )}

      <CollapsibleContent className="mt-1 flex flex-col gap-2 border-l border-border pl-3">
        {/* Both arrive minified — `args` is a bare `JSON.stringify` off the wire
              in useAgentChat — so an operator checking what the agent sent read a
              one-line blob. Printed, they are the same two panes the trace span
              detail on /analytics shows, from the same component. */}
        {args && (
          <div>
            <div className="font-medium text-muted-foreground">Input</div>
            <JsonBlock text={args} className="mt-0.5" />
          </div>
        )}
        {result && (
          <div>
            <div className="font-medium text-muted-foreground">Result</div>
            <JsonBlock text={result} className="mt-0.5" />
          </div>
        )}
      </CollapsibleContent>
    </Collapsible>
  )
}
