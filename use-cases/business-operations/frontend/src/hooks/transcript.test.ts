// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, beforeEach } from "vitest"
import {
  clearAllTranscripts,
  clearTranscript,
  getTranscript,
  setTranscript,
} from "@/hooks/transcript"
import type { Message } from "@/components/chat/types"

const A = "5100001976-2026"
const B = "5100001977-2026"

function msg(content: string): Message {
  return { role: "assistant", content, timestamp: "2026-08-19T09:00:00Z" }
}

describe("transcript store", () => {
  beforeEach(() => {
    clearAllTranscripts()
  })

  it("keeps each case's conversation to itself", () => {
    setTranscript(A, [msg("three-way match is clean")])
    setTranscript(B, [msg("supplier is on hold")])
    expect(getTranscript(A)).toEqual([msg("three-way match is clean")])
    expect(getTranscript(B)).toEqual([msg("supplier is on hold")])
  })

  it("returns a stable reference for an untouched slot", () => {
    // useSyncExternalStore re-renders forever if getSnapshot returns a fresh array
    // each call, so an absent slot must hand back the same empty one every time.
    expect(getTranscript(A)).toBe(getTranscript(B))
    setTranscript(A, [msg("hello")])
    const first = getTranscript(A)
    expect(getTranscript(A)).toBe(first)
  })

  it("resolves an updater against the named slot only", () => {
    setTranscript(A, [msg("first")])
    setTranscript(B, prev => [...prev, msg("only B")])
    expect(getTranscript(A)).toHaveLength(1)
    expect(getTranscript(B)).toEqual([msg("only B")])
  })

  it("clears one case without touching the others", () => {
    setTranscript(A, [msg("a")])
    setTranscript(B, [msg("b")])
    clearTranscript(A)
    expect(getTranscript(A)).toEqual([])
    expect(getTranscript(B)).toEqual([msg("b")])
  })

  it("clears every case at once", () => {
    setTranscript(A, [msg("a")])
    setTranscript(B, [msg("b")])
    clearAllTranscripts()
    expect(getTranscript(A)).toEqual([])
    expect(getTranscript(B)).toEqual([])
  })
})
