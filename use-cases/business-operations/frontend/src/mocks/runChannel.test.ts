// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, vi, afterEach } from "vitest"
import { RunFollower, type Message } from "@business-operations/run-client"
import { listen, replay } from "./runChannel"
import { escalate, rehearsal } from "./runs"
import { CASES } from "./fixtures"

afterEach(() => vi.useRealTimers())

/** Follow a replay with the run client a real run uses, into a plain transcript. */
function follow(slot: string) {
  let transcript: Message[] = []
  const follower = new RunFollower({
    readTranscript: () => transcript,
    writeTranscript: (_, update) => {
      transcript = update(transcript)
    },
  })
  const stop = listen((s, payload) => {
    if (s === slot) follower.accept(JSON.parse(payload))
  })
  return { get: () => transcript, stop }
}

describe("A recorded run in mock mode", () => {
  it("streams through the run client as the steps and words of one turn", () => {
    vi.useFakeTimers()
    const c = CASES.find(x => x.document_number === "5100001976")!
    const run = escalate(c)
    const seen = follow(c.case_id)
    const done = vi.fn()
    replay(c.case_id, "run-1", run.segments, () => {}, done)

    vi.advanceTimersByTime(1500)
    const early = seen.get().flatMap(m => m.segments ?? [])
    expect(early.some(s => s.type === "tool")).toBe(true)
    expect(done).not.toHaveBeenCalled()

    vi.runAllTimers()
    expect(done).toHaveBeenCalledOnce()
    const tools = seen
      .get()
      .flatMap(m => m.segments ?? [])
      .filter(s => s.type === "tool")
    expect(tools).toHaveLength(run.segments.filter(s => s.type === "tool").length)
    expect(tools.every(s => s.type === "tool" && s.toolCall.status === "complete")).toBe(true)
    expect(seen.get()[seen.get().length - 1]?.content).toContain("waits for an approver")
    seen.stop()
  })

  it("hands a rehearsal's write back withheld", () => {
    vi.useFakeTimers()
    const seen = follow("session-1")
    replay(
      "session-1",
      "run-2",
      rehearsal("A price variance").segments,
      () => {},
      () => {}
    )
    vi.runAllTimers()
    const write = seen
      .get()
      .flatMap(m => m.segments ?? [])
      .find(s => s.type === "tool" && s.toolCall.name.endsWith("odata_function_import"))
    expect(write?.type === "tool" && write.toolCall.status).toBe("error")
    seen.stop()
  })

  it("sends nothing after a stop but the run's end", () => {
    vi.useFakeTimers()
    const frames: string[] = []
    const stop = listen((_, payload) => frames.push(JSON.parse(payload).event.type))
    const c = CASES.find(x => x.document_number === "5100001976")!
    const r = replay(
      c.case_id,
      "run-3",
      escalate(c).segments,
      () => {},
      () => {}
    )
    vi.advanceTimersByTime(1000)
    r.cancel()
    const at = frames.length
    vi.runAllTimers()
    expect(frames.length).toBe(at)
    expect(frames[frames.length - 1]).toBe("RUN_FINISHED")
    stop()
  })
})
