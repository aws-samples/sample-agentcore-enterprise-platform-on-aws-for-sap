// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { FormEvent, KeyboardEvent, useRef, useEffect } from "react"
import { Button } from "@/components/ui/button"
import { InputGroupTextarea } from "@/components/ui/input-group"
import { ArrowUp, Square } from "lucide-react"

interface ChatInputProps {
  input: string
  setInput: (input: string) => void
  handleSubmit: (e: FormEvent) => void
  isLoading: boolean
  onStop?: () => void
  className?: string
  selectedCaseCount?: number
  /** In place of the invitation, such as why questions wait while a stop stands. */
  placeholder?: string
}

export function ChatInput({
  input,
  setInput,
  handleSubmit,
  isLoading,
  onStop,
  className = "",
  selectedCaseCount = 0,
  placeholder = "Ask, or paste a document number",
}: ChatInputProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    const textarea = textareaRef.current
    if (textarea) {
      textarea.style.height = "0px"
      const scrollHeight = textarea.scrollHeight
      textarea.style.height = scrollHeight + "px"
    }
  }, [input])

  // Enter sends, Shift+Enter is a newline — both conventions, so neither is spelled out
  // in the placeholder. Ctrl+Enter inserts a paragraph break and stays undocumented: the
  // placeholder's one line of space is worth more as an invitation than as a key chart.
  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter") {
      if (e.ctrlKey) {
        setInput(`${input}\n\n`)
        e.preventDefault()
      } else if (!e.shiftKey) {
        if (input.trim()) {
          e.preventDefault()
          handleSubmit(e as unknown as FormEvent)
        }
      }
    }
  }

  return (
    <div className={`w-full p-4 ${className}`}>
      {/* Flat like every other surface, at the shell's own radius; the focus ring is the
          input's, on the whole box. */}
      <form
        onSubmit={handleSubmit}
        className="flex w-full flex-col gap-1 rounded-lg border bg-card px-3 py-2.5 transition-colors focus-within:border-ring"
      >
        {/* The input group's textarea: the stock one draws its own dark-theme fill, a darker
            box inside this one; the group's control is transparent in both themes. */}
        <InputGroupTextarea
          ref={textareaRef}
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={placeholder}
          disabled={isLoading}
          className="max-h-50 min-h-6 w-full resize-none border-0 bg-transparent px-1 py-0.5 focus-visible:ring-0"
          rows={1}
          // The composer is the surface's one input, and the dock exists to be typed into.
          // eslint-disable-next-line jsx-a11y/no-autofocus
          autoFocus
        />

        {/* Send on its own row rather than beside the text: at four lines the button used
            to ride the bottom edge of a tall box with the sentence squeezed past it, and
            what is in context belongs next to the thing that will act on it. */}
        <div className="flex items-center justify-between gap-2">
          {/* Reads as a note on the message you are about to send, so it sits inside the
              box on the same line as Send. It used to be an ink tab clamped to the top
              edge, which a rounded box has no straight edge to carry. */}
          {selectedCaseCount > 0 ? (
            <span className="truncate rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground">
              {selectedCaseCount} case{selectedCaseCount > 1 ? "s" : ""} in context
            </span>
          ) : (
            <span />
          )}
          {/* One ink circle in both states, so the control does not change shape under the
              pointer mid-run. Icon-only: at this size a word beside the glyph is what made
              it read as a toolbar button dropped into a chat box. The accessible name is
              still Send / Stop.

              Stop is ink, not destructive. Interrupting the agent undoes nothing — and a
              red button here would read as though stopping were the dangerous act. */}
          {isLoading && onStop ? (
            <Button
              type="button"
              size="icon"
              onClick={onStop}
              aria-label="Stop"
              title="Stop"
              className="size-8 rounded-full"
            >
              <Square className="size-3 fill-current" />
            </Button>
          ) : (
            <Button
              type="submit"
              size="icon"
              disabled={!input.trim() || isLoading}
              aria-label="Send"
              title="Send"
              className="size-8 rounded-full transition-transform hover:not-disabled:-translate-y-px"
            >
              <ArrowUp className="size-4" />
            </Button>
          )}
        </div>
      </form>
    </div>
  )
}
