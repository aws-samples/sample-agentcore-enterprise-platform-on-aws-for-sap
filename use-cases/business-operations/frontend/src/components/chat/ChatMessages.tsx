// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { RefObject } from "react"
import { Loader2 } from "lucide-react"
import { Message } from "./types"
import { ChatMessage } from "./ChatMessage"

interface ChatMessagesProps {
  messages: Message[]
  messagesEndRef: RefObject<HTMLDivElement | null>
  /** A run is live and has produced nothing yet. See `PendingTurn`. */
  pending?: boolean
}

/**
 * The turn that has been asked for and has not begun.
 *
 * Between submit and the first token this surface said nothing: the operator's own line sat
 * there with empty space under it, which reads as a message that failed to send rather than
 * as one being worked on. Shaped as an assistant turn — same indent, same spine — so when
 * the first token lands it takes this row's place instead of displacing it.
 *
 * The spine is ink, as it is on any turn with a call in flight, and the spinner is the
 * app's one loop. That does not breach the motion budget's "entrances only": the rule is
 * there so no spinner outlives its run, and this row exists only while the run does.
 */
function PendingTurn() {
  return (
    <div className="relative pl-5 text-sm text-muted-foreground">
      <span aria-hidden="true" className="absolute top-1 bottom-1 left-0 w-px bg-foreground" />
      {/* `status`, not `alert`: it is worth announcing and not worth interrupting for. */}
      <span role="status" className="flex items-center gap-1.5">
        <Loader2 className="size-3.5 motion-safe:animate-spin" />
        Working…
      </span>
    </div>
  )
}

export function ChatMessages({ messages, messagesEndRef, pending = false }: ChatMessagesProps) {
  return (
    <div
      // gap-6, not 4: a turn carries its own left rule, and at 1rem two consecutive rules
      // read as one broken line rather than two postings.
      className={`flex h-full w-full flex-col gap-6 p-4 ${
        messages.length > 0 ? "overflow-y-auto" : "overflow-hidden"
      }`}
    >
      {messages.length === 0 && !pending ? (
        // Not while a run is live: "start a conversation" is an invitation, and offering it
        // over a run already working is the same wrong claim as the silence above.
        <div className="flex items-center justify-center h-full text-muted-foreground">
          Start a new conversation
        </div>
      ) : (
        messages.map((message, index) => (
          <ChatMessage
            key={message.id ?? index}
            message={message}
            // One answer arrives as several messages: the run protocol opens a fresh
            // message id per model iteration, so a turn that reads an invoice and then
            // reports on it is three of them. To the operator that is one posting, so
            // the rule runs through the whole group and only the last one is stamped —
            // otherwise a single answer reads as three runs seconds apart, each with a
            // stub of rule beside it.
            continuesPrevious={
              message.role === "assistant" && messages[index - 1]?.role === "assistant"
            }
            continuedByNext={
              message.role === "assistant" && messages[index + 1]?.role === "assistant"
            }
          />
        ))
      )}
      {pending && <PendingTurn />}
      {/* After the pending row, so the autoscroll lands past it rather than above it. */}
      <div ref={messagesEndRef} />
    </div>
  )
}
