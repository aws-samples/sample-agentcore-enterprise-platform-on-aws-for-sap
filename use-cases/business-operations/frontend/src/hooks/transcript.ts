// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useCallback, useSyncExternalStore } from "react"
import type { Message } from "@/components/chat/types"

/**
 * The interactive conversation's messages, held outside React state and keyed by the
 * case they belong to.
 *
 * A module store rather than state in the shell because the shell is a layout route:
 * every navigation re-renders it, and state there would be a token behind whichever
 * run is streaming.
 *
 * Keyed because a run outlives the route that started it. With one shared array, a
 * case switch mid-run replaced the messages a still-streaming run was appending to,
 * so the turn vanished and then reappeared on the next token. Each case gets its own
 * slot, and a run writes to the slot it started in whatever the URL says by the time
 * it ends.
 *
 * Never persisted to storage: the transcript carries SAP tool results, and the
 * sign-in popup would otherwise leave them readable to the next operator on a shared
 * machine. See lib/signOut.
 */

const EMPTY: Message[] = []

// Keying widened retention from one transcript to one per case visited this session, each
// holding SAP tool results, and nothing evicts a slot before sign-out.
// Unbounded by design — a shift's browsing keeps tens of slots, which is memory
// the operator already had on screen. Cap it (LRU, or drop a slot when its case is closed
// with no run in flight) if a session ever holds enough cases for that to matter.
const slots = new Map<string, Message[]>()

// One listener set rather than one per slot: every subscriber is a whole panel and
// re-renders on any write, and per-slot sets would have to be reaped on unmount.
// Fine at one dock; split per slot if a screen ever mounts many at once.
const listeners = new Set<() => void>()

export function getTranscript(key: string): Message[] {
  return slots.get(key) ?? EMPTY
}

/** Replace one slot. Accepts an updater so a caller can append without reading first. */
export function setTranscript(
  key: string,
  next: Message[] | ((prev: Message[]) => Message[])
): void {
  const prev = getTranscript(key)
  const resolved = typeof next === "function" ? next(prev) : next
  if (resolved === prev) return
  slots.set(key, resolved)
  for (const listener of listeners) listener()
}

export function subscribeTranscript(listener: () => void): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

export function useTranscript(key: string): Message[] {
  // getSnapshot has to be stable per key, and has to return the same array between
  // writes — otherwise useSyncExternalStore re-renders in a loop.
  const getSnapshot = useCallback(() => getTranscript(key), [key])
  return useSyncExternalStore(subscribeTranscript, getSnapshot)
}

/** Drop one case's conversation — the Clear control. */
export function clearTranscript(key: string): void {
  setTranscript(key, EMPTY)
}

/**
 * Drop every conversation. Sign-out only: what the next operator must not inherit is
 * all of them, not just the one that happened to be focused.
 */
export function clearAllTranscripts(): void {
  if (slots.size === 0) return
  slots.clear()
  for (const listener of listeners) listener()
}
