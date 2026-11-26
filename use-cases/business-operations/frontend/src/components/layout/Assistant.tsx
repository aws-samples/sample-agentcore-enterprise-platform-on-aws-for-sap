// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react"
import { useQueryClient } from "@tanstack/react-query"
import type { WorkItem } from "@/types/cases"
import { Copy, History, MessageSquarePlus, MoreHorizontal, Sparkles, X } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Kbd, KbdGroup } from "@/components/ui/kbd"
import { Badge } from "@/components/ui/badge"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { ChatSurface } from "@/components/chat/ChatSurface"
import { useTranscript } from "@/hooks/transcript"
import type { AgentChat } from "@/hooks/agentChat.types"
import { cn } from "@/lib/utils"
import { useDeploymentSettings } from "@/hooks/usePack"
import { stopStands } from "@/components/EmergencyStop"

/**
 * The assistant (preview 1.10, decision 20): an ask bar floating at the foot of the page,
 * and a panel that opens only when an answer you asked for lands in it. The bar is the way
 * back after the panel is closed, and says when an answer is still coming or is ready.
 */

/** What both inputs say while a stop stands, before a question is refused. */
const STOPPED_PLACEHOLDER = "Questions wait until an administrator clears the stop"

/** The case's document number, from the case page's own query, else its id. */
function useCaseLabel(caseId: string | null): string | null {
  const queryClient = useQueryClient()
  if (!caseId) return null
  return queryClient.getQueryData<WorkItem>(["case", caseId])?.document_number ?? caseId
}

function lastUserQuestion(messages: { role: string; content: string }[]): string | null {
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i].role === "user" && messages[i].content.trim()) return messages[i].content.trim()
  }
  return null
}

/** True from the moment a reply arrives while the panel is closed until the panel opens. */
function useAnswerReady(chat: AgentChat, open: boolean): boolean {
  const messages = useTranscript(chat.slotKey)
  const [ready, setReady] = useState(false)
  const wasLoading = useRef(chat.isLoading)
  useEffect(() => {
    if (open) setReady(false)
    else if (wasLoading.current && !chat.isLoading && messages.length > 0) setReady(true)
    wasLoading.current = chat.isLoading
  }, [chat.isLoading, open, messages.length])
  return ready
}

export function AskBar({
  chat,
  onOpen,
  inputRef,
}: {
  chat: AgentChat
  onOpen: () => void
  inputRef: React.RefObject<HTMLInputElement | null>
}) {
  const messages = useTranscript(chat.slotKey)
  const ready = useAnswerReady(chat, false)
  const last = lastUserQuestion(messages)
  const context = useCaseLabel(chat.caseId)
  // A standing stop refuses questions too (7.2), so the bar says so before one is asked.
  const stopped = stopStands(useDeploymentSettings().data?.last_stop)

  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (!chat.input.trim()) {
      if (messages.length) onOpen()
      return
    }
    void chat.send(chat.input)
    onOpen()
  }
  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Escape") {
      if (chat.isLoading) void chat.stop()
      inputRef.current?.blur()
    }
  }

  const placeholder = chat.isLoading
    ? "Answering…"
    : stopped
      ? STOPPED_PLACEHOLDER
      : ready && last
        ? `Answer ready: ${last}`
        : last
          ? `Continue: ${last}`
          : context
            ? "Ask about this case"
            : "Ask the agent"

  return (
    <form
      onSubmit={submit}
      aria-label="Ask the assistant"
      className="absolute bottom-(--ask-bar-offset) left-1/2 z-20 flex h-(--ask-bar-height) w-[35rem] max-w-[calc(100%-3rem)] -translate-x-1/2 items-center gap-2 rounded-xl border bg-card px-3 shadow-lg focus-within:ring-3 focus-within:ring-ring/20"
    >
      {chat.isLoading || ready ? (
        <span
          aria-hidden="true"
          className="size-2 flex-none rounded-full bg-progress motion-safe:animate-pulse"
        />
      ) : last ? (
        <History aria-hidden="true" className="size-4 flex-none text-muted-foreground" />
      ) : (
        <Sparkles aria-hidden="true" className="size-4 flex-none text-progress" />
      )}
      {context && (
        <Badge variant="secondary" className="flex-none font-mono">
          {context}
        </Badge>
      )}
      <input
        ref={inputRef}
        value={chat.input}
        onChange={e => chat.setInput(e.target.value)}
        onKeyDown={onKeyDown}
        placeholder={placeholder}
        aria-label="Ask the assistant"
        className="h-full min-w-0 flex-1 bg-transparent text-sm outline-none placeholder:text-muted-foreground"
      />
      {ready || (last && !chat.input) ? (
        <Button type="button" variant="ghost" size="sm" onClick={onOpen}>
          Open
        </Button>
      ) : (
        <KbdGroup className="flex-none">
          <Kbd>⌘</Kbd>
          <Kbd>J</Kbd>
        </KbdGroup>
      )}
    </form>
  )
}

export function AssistantPanel({ chat, onClose }: { chat: AgentChat; onClose: () => void }) {
  const messages = useTranscript(chat.slotKey)
  // As the ask bar says: a standing stop refuses questions too (7.2).
  const stopped = stopStands(useDeploymentSettings().data?.last_stop)
  const caseLabel = useCaseLabel(chat.caseId)
  const copy = () =>
    void navigator.clipboard?.writeText(
      messages.map(m => `${m.role === "user" ? "You" : "Assistant"}: ${m.content}`).join("\n\n")
    )
  return (
    <aside
      aria-label="Assistant"
      className={cn(
        "flex w-(--assistant-width) flex-none flex-col border-l bg-background",
        // Pushes the page from 1280 px; lays over it below that.
        "fixed inset-y-0 right-0 z-30 shadow-lg xl:static xl:shadow-none"
      )}
    >
      <div className="flex h-(--header-height) flex-none items-center gap-2 border-b pr-2 pl-4">
        <Sparkles className="size-4 text-progress" aria-hidden="true" />
        <span className="text-sm font-semibold">Assistant</span>
        {chat.rehearsal ? (
          <Badge variant="secondary">Rehearsing {chat.rehearsal.processType}</Badge>
        ) : chat.caseId ? (
          <Badge variant="secondary" className="max-w-36 truncate font-mono">
            {caseLabel}
          </Badge>
        ) : null}
        <span className="flex-1" />
        <DropdownMenu>
          <DropdownMenuTrigger
            render={<Button variant="ghost" size="icon-sm" aria-label="Conversation menu" />}
          >
            <MoreHorizontal />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuGroup>
              <DropdownMenuItem
                onClick={chat.clear}
                disabled={chat.isLoading || messages.length === 0}
              >
                <MessageSquarePlus />
                New conversation
              </DropdownMenuItem>
              <DropdownMenuItem onClick={copy} disabled={messages.length === 0}>
                <Copy />
                Copy transcript
              </DropdownMenuItem>
            </DropdownMenuGroup>
          </DropdownMenuContent>
        </DropdownMenu>
        <Tooltip>
          <TooltipTrigger
            render={
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label="Close the assistant"
                onClick={onClose}
              />
            }
          >
            <X />
          </TooltipTrigger>
          <TooltipContent>
            Close{" "}
            <KbdGroup>
              <Kbd>⌘</Kbd>
              <Kbd>J</Kbd>
            </KbdGroup>
          </TooltipContent>
        </Tooltip>
      </div>
      <div className="flex min-h-0 flex-1 flex-col">
        <ChatSurface
          chat={chat}
          bannerClassName="mx-3 mt-2"
          placeholder={stopped ? STOPPED_PLACEHOLDER : undefined}
          empty={
            <div className="flex max-w-xs flex-col items-center gap-2 text-center">
              <Sparkles className="size-5 text-progress" aria-hidden="true" />
              <p className="text-sm font-medium">Ask about what is on screen</p>
              <p className="text-xs text-muted-foreground">
                {chat.caseId
                  ? "Why is this case waiting? What did the supplier bill last time?"
                  : "What needs me today? Which cases have waited longest?"}
              </p>
            </div>
          }
        />
      </div>
    </aside>
  )
}
