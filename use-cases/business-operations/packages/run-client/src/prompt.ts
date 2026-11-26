// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import type { Message } from "./types.ts"

/**
 * Build a context-enriched prompt by prepending recent conversation history.
 * This ensures the agent has conversational context even if server-side memory
 * (AgentCore Memory) fails to load or has eventual-consistency lag.
 */
export function buildPromptWithHistory(userMessage: string, messages: Message[]): string {
  if (messages.length === 0) return userMessage

  // Collapse each run of same-role messages into one turn before windowing. One agent
  // turn arrives as many assistant messages — one per text block or tool call — so a
  // flat last-10 over fragments can be entirely assistant, which is how a verbose turn
  // pushed every `User:` line out of the history and left the agent unable to see what
  // it had been asked. The window counts turns now, not fragments.
  const turns: { role: "User" | "Assistant"; text: string }[] = []
  for (const message of messages) {
    const text = message.content.trim()
    if (!text) continue
    const role = message.role === "user" ? "User" : "Assistant"
    const last = turns[turns.length - 1]
    if (last?.role === role) last.text += `\n${text}`
    else turns.push({ role, text })
  }

  const recent = turns
    .slice(-10)
    .map(({ role, text }) => {
      // Per turn rather than per fragment, so a merged turn cannot smuggle the whole
      // transcript back in.
      const truncated = role === "Assistant" && text.length > 500 ? text.slice(0, 500) + "…" : text
      return `${role}: ${truncated}`
    })
    .join("\n")

  if (!recent) return userMessage

  return `<conversation_history>\n${recent}\n</conversation_history>\n\nUser: ${userMessage}`
}

/**
 * Recover the user's own words from a prompt that was wrapped for the agent.
 *
 * A trace stores the prompt as sent, scaffolding and all, so replaying one verbatim
 * would show the operator a bubble full of `<conversation_history>` instead of what
 * they typed. Inverse of the wrapping above and of the workspace's `[Context: ...]`
 * preamble; anything else passes through untouched.
 *
 * Returns `""` when the prompt contains nothing a human wrote — a resume built by the
 * agent for a console decision, a ticket action, or an inbound webhook. `tracesToMessages`
 * reads an empty result as "no user turn", which is the honest rendering: nobody typed at
 * the agent, and what did happen is already on the record as the trace's own trigger.
 */
export function stripPromptScaffolding(prompt: string): string {
  let text = prompt.replace(/^\[Context:[\s\S]*?\n\]\n+/, "")
  const close = "</conversation_history>"
  const end = text.lastIndexOf(close)
  if (end !== -1) text = text.slice(end + close.length).trimStart()
  if (text.startsWith("Resume case: ")) return humanWordsInResume(text)
  return text.startsWith("User: ") ? text.slice("User: ".length) : text
}

/**
 * The human-authored prose inside a machine-built resume prompt, if there is any.
 *
 * `basic_agent.py`'s resume prompts are addressed to the agent: they open with
 * `Resume case: <id>`, state the event, and close with an instruction to continue per SOP.
 * The one part a person wrote — an operator's reason for denying, a reviewer's reply, the
 * body of an inbound email — is the untrusted content, and the agent fences exactly that
 * with `<external_data>`. So the fence doubles as the marker for "these are somebody's own
 * words", and everything outside it is plumbing the operator has no reason to read.
 *
 * The decision itself is deliberately not reconstructed here. It is already on screen twice
 * — as the trigger chip on the timeline row and as the case's own status — and spelling it a
 * third time would mean this client owning a second copy of the agent's wording.
 */
function humanWordsInResume(text: string): string {
  const fenced = /<external_data[^>]*>\n?([\s\S]*?)\n?<\/external_data>/.exec(text)
  return fenced ? fenced[1].trim() : ""
}
