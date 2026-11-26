// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useRef } from "react"
import type { FormEvent, ReactNode } from "react"
import { cn } from "@/lib/utils"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { ChatInput } from "@/components/chat/ChatInput"
import { ChatMessages } from "@/components/chat/ChatMessages"
import { useTranscript } from "@/hooks/transcript"
import type { AgentChat } from "@/hooks/agentChat.types"

/**
 * One conversation, rendered: the transcript, the error, and the box you type in.
 *
 * There are two places an operator talks to the agent — the dock beside the workspace and
 * the briefing that *is* the conversation — and they were near-identical copies of this
 * block, with the dock carrying a second copy of the input for its empty state. Three
 * copies of one autoscroll effect is how a fix to the streaming scroll lands on one
 * surface and not the others.
 *
 * The transcript is subscribed here rather than arriving on `chat`, so a streamed token
 * re-renders this surface and nothing else. See `lib/transcript`.
 *
 * A fragment, not a box: each caller's own flex column owns the layout, and the two are
 * not the same shape.
 */
export interface ChatSurfaceProps {
  chat: AgentChat
  /**
   * Rendered in place of the transcript while the slot is empty and no run is live — the
   * dock's "Ready when you are". Omitted, `ChatMessages` shows its own empty state, which
   * is what a full-width page wants.
   */
  empty?: ReactNode
  /** The error banner's gutters. A dock's are tighter than a page's. */
  bannerClassName?: string
  /** Extra classes for the input's row — the briefing rules a line above it. */
  inputClassName?: string
  /** The input's placeholder, when the surface has something to say before you type. */
  placeholder?: string
}

export function ChatSurface({
  chat,
  empty,
  bannerClassName,
  inputClassName,
  placeholder,
}: ChatSurfaceProps) {
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const messages = useTranscript(chat.slotKey)

  // scrollIntoView on a hidden element scrolls the page instead — which is why the dock
  // used to guard this on `collapsed`. It renders no surface while collapsed, so the
  // guard is now structural: no mounted surface, no effect.
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" })
  }, [messages])

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault()
    void chat.send(chat.input)
  }

  // A live run that has produced nothing yet. `id` is what settles "nothing yet": the
  // reducer opens every streamed message with the run's message id, and the client's own
  // notices — the briefing's opening sentence, the 409 explanation — are appended without
  // one. So an assistant message with no id is something we said, not something arriving,
  // and a run behind it is still a run with nothing to show.
  const last = messages[messages.length - 1]
  const pending = chat.isLoading && !(last?.role === "assistant" && last.id !== undefined)

  const input = (
    <ChatInput
      input={chat.input}
      setInput={chat.setInput}
      handleSubmit={handleSubmit}
      isLoading={chat.isLoading}
      onStop={chat.stop}
      selectedCaseCount={chat.contextCaseCount}
      placeholder={placeholder}
    />
  )

  return (
    <>
      {chat.error && (
        <Alert variant="destructive" className={bannerClassName}>
          <AlertDescription>{chat.error}</AlertDescription>
        </Alert>
      )}

      {empty && messages.length === 0 && !pending ? (
        // The invitation and the input centre together rather than the input pinning to
        // the foot of an empty panel, where it reads as a dead end.
        <div className="flex flex-1 flex-col items-center justify-center gap-3 px-3">
          {empty}
          <div className="w-full">{input}</div>
        </div>
      ) : (
        <>
          <div className="min-h-0 flex-1 overflow-hidden">
            <ChatMessages messages={messages} messagesEndRef={messagesEndRef} pending={pending} />
          </div>
          <div className={cn("flex-none", inputClassName)}>{input}</div>
        </>
      )}
    </>
  )
}
