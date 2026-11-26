// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState, type CSSProperties } from "react"
import { When } from "@/components/When"
import { Button } from "@/components/ui/button"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { Copy, Check } from "lucide-react"
import { Message } from "./types"
import { TONE_TEXT } from "@/lib/statusTone"
import { cn } from "@/lib/utils"
import { ToolCallDisplay } from "./ToolCallDisplay"
import { MarkdownRenderer } from "./MarkdownRenderer"
import { AgentAlert } from "./AgentAlert"

interface ChatMessageProps {
  message: Message
  /** The message above is the same speaker's — the rule reaches up to meet theirs. */
  continuesPrevious?: boolean
  /** The message below is too — so this one is stamped by that one, not by itself. */
  continuedByNext?: boolean
}

export function ChatMessage({
  message,
  continuesPrevious = false,
  continuedByNext = false,
}: ChatMessageProps) {
  const [copied, setCopied] = useState(false)

  const isUser = message.role === "user"
  const segments = message.segments ?? []
  const running = segments.some(
    seg =>
      seg.type === "tool" &&
      (seg.toolCall.status === "streaming" || seg.toolCall.status === "executing")
  )

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(message.content)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      // Clipboard API can fail if page is not focused or permissions denied
    }
  }

  const renderAssistantContent = () => {
    // If segments exist, render them in order (interleaved text + tools)
    if (segments.length > 0) {
      // The tick's index within its own turn, so a burst of calls staggers in rather
      // than appearing as one block. Capped: a call that lands alone late in a long run
      // should not sit behind a second of accumulated delay before it shows up.
      let tickIndex = -1
      return segments.map((seg, i) => {
        if (seg.type === "text") {
          const trimmed = seg.content.trim()
          if (trimmed.startsWith("⚠️")) {
            return (
              <AgentAlert key={i} level="warning">
                {trimmed.replace(/^⚠️\s*/, "")}
              </AgentAlert>
            )
          }
          if (trimmed.startsWith("❌")) {
            return (
              <AgentAlert key={i} level="error">
                {trimmed.replace(/^❌\s*/, "")}
              </AgentAlert>
            )
          }
          return <MarkdownRenderer key={i} content={seg.content} />
        }
        tickIndex += 1
        return (
          <div
            key={seg.toolCall.toolUseId}
            className="relative animate-tick-in"
            style={{ "--tick-i": Math.min(tickIndex, 4) } as CSSProperties}
          >
            {/* The `├─` connector: the tick hangs off the spine rather than floating
                beside it. Decorative — the row's own text says what happened. */}
            <span aria-hidden="true" className="absolute -left-5 top-2 h-px w-3 bg-border" />
            <ToolCallDisplay
              name={seg.toolCall.name}
              args={seg.toolCall.input}
              status={seg.toolCall.status}
              result={seg.toolCall.result}
            />
          </div>
        )
      })
    }
    const trimmed = message.content.trim()
    if (trimmed.startsWith("⚠️")) {
      return <AgentAlert level="warning">{trimmed.replace(/^⚠️\s*/, "")}</AgentAlert>
    }
    if (trimmed.startsWith("❌")) {
      return <AgentAlert level="error">{trimmed.replace(/^❌\s*/, "")}</AgentAlert>
    }
    return <MarkdownRenderer content={message.content} />
  }

  return (
    <div className="group/msg flex flex-col">
      {isUser ? (
        // A wash of ink at 5%, not a filled balloon with a tail: enough for what you said
        // to read as yours at a glance, quiet enough that the agent's prose beside it is
        // still the thing with weight. The hard 2px rule this replaces was the right mark
        // for a ledger and the wrong one for a conversation — it made your own sentence
        // look like a pull-quote from the record rather than something you had just said.
        <div className="ml-auto w-fit max-w-4/5 rounded-lg bg-muted px-3.5 py-2 text-sm break-words whitespace-pre-wrap text-foreground">
          {message.content}
        </div>
      ) : (
        // No second width of its own: `--measure` is the readable bound now, and a 65ch
        // cap inside it just moved the agent's right edge 56px in from the one your own
        // words and the composer share.
        <div className="relative pl-5 text-sm break-words text-foreground">
          {/* The spine: ink while a call on this turn is in flight, rule once it has
              settled. Derived from the turn's own segments, so a turn still streaming
              prose with no call outstanding reads as at rest — which it is.
              `-top-6` reaches back across the list's `gap-6` to join the rule
              above, which is what makes a multi-message answer one posting. */}
          <span
            aria-hidden="true"
            className={cn(
              "absolute left-0 w-px transition-colors",
              running ? "bg-foreground" : "bg-border",
              continuesPrevious ? "-top-6" : "top-1",
              continuedByNext ? "bottom-0" : "bottom-1"
            )}
          />
          {renderAssistantContent()}
        </div>
      )}

      {/* Suppressed mid-group: a stamp between two halves of one answer reads as a
          second run, and the row itself breaks the rule it sits across. The copy
          control goes with it — the group's last message is where they belong,
          since that is the one holding the conclusion. */}
      <div
        className={cn(
          "mt-1 flex items-center gap-2",
          isUser ? "ml-auto pr-3.5" : "pl-5",
          continuedByNext && "hidden"
        )}
      >
        <div className="text-xs text-muted-foreground tabular-nums">
          <When at={message.timestamp} focusable={false} />
        </div>

        {/* At rest a transcript is a record to read, so the control comes out on hover, and
            `focus-within` brings it out for the keyboard, which is what keeps it reachable
            while it is invisible. */}
        {message.role === "assistant" && message.content && (
          <div className="ml-2 flex items-center gap-1 opacity-0 transition-opacity focus-within:opacity-100 group-hover/msg:opacity-100">
            <Tooltip>
              <TooltipTrigger
                render={
                  <Button
                    variant="ghost"
                    size="icon-xs"
                    onClick={handleCopy}
                    aria-label="Copy message"
                  />
                }
              >
                {copied ? <Check className={TONE_TEXT.success} /> : <Copy />}
              </TooltipTrigger>
              <TooltipContent>Copy to clipboard</TooltipContent>
            </Tooltip>
          </div>
        )}
      </div>
    </div>
  )
}
