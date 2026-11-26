// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from "vitest"
import { RunFollower, type AppliedFrame, type RunFollowerHost } from "../runFollower.ts"
import type { RunEventEnvelope } from "../channel.ts"
import { liveTurn, storedTurns } from "../traces.ts"
import type { Message } from "../types.ts"

/**
 * What the wire does to a subscriber, without a subscriber.
 *
 * Every case here is a delivery hazard rather than a rendering choice: the same envelope
 * twice, two envelopes swapped, one that never arrives, a word split across frames, and a
 * run already half on screen from the record. The transcript store is a Map because that
 * is all the host contract asks for — no framework is involved in any of it.
 */

const SLOT = "5100001976-2026"
const RUN = "run-8f1c"
const MESSAGE = "msg-4a2b"

function harness(seed: Message[] = []) {
  const transcripts = new Map<string, Message[]>([[SLOT, seed]])
  const frames: AppliedFrame[] = []
  const ended: string[][] = []
  const counts = { idle: 0 }

  const host: RunFollowerHost = {
    readTranscript: slot => transcripts.get(slot) ?? [],
    writeTranscript: (slot, update) => {
      transcripts.set(slot, update(transcripts.get(slot) ?? []))
    },
    onFrame: frame => frames.push(frame),
    onRunEnd: (slot, runId) => ended.push([slot, runId]),
    onIdle: () => {
      counts.idle++
    },
    now: () => "2026-08-27T12:00:00.000Z",
  }

  return {
    follower: new RunFollower(host),
    frames,
    ended,
    counts,
    /** The slot's messages, as they would render. */
    messages: () => transcripts.get(SLOT) ?? [],
    /** The assistant text on screen for one message id. */
    textOf: (id: string) => transcripts.get(SLOT)?.find(m => m.id === id)?.content,
  }
}

const frame = (seq: number | undefined, event: Record<string, unknown>): RunEventEnvelope => ({
  caseId: SLOT,
  runId: RUN,
  seq,
  event,
})

const started = (seq: number) => frame(seq, { type: "RUN_STARTED" })
const delta = (seq: number, text: string) =>
  frame(seq, { type: "TEXT_MESSAGE_CONTENT", messageId: MESSAGE, delta: text })
const finished = (seq: number) => frame(seq, { type: "RUN_FINISHED" })

describe("RunFollower", () => {
  it("renders a run token by token", () => {
    const h = harness()
    h.follower.accept(started(1))
    h.follower.accept(delta(2, "Posted "))
    h.follower.accept(delta(3, "the invoice."))
    h.follower.accept(finished(4))

    expect(h.textOf(MESSAGE)).toBe("Posted the invoice.")
    expect(h.follower.following).toBe(false)
    expect(h.ended).toEqual([[SLOT, RUN]])
    expect(h.counts.idle).toBe(1)
  })

  it("drops an envelope the channel delivers twice", () => {
    const h = harness()
    h.follower.accept(started(1))
    h.follower.accept(delta(2, "Hel"))
    h.follower.accept(delta(3, "lo"))
    // At-most-once is not on offer, and `appendText` accumulates — so a redelivery that
    // reached the reducer would render an interior slice of the reply a second time.
    h.follower.accept(delta(2, "Hel"))
    h.follower.accept(delta(3, "lo"))
    // Dropped rather than held: an already-applied number held instead would sit in the
    // buffer until something released it, and then render its fragment twice.
    h.follower.accept(delta(4, " there"))
    h.follower.accept(finished(5))

    expect(h.textOf(MESSAGE)).toBe("Hello there")
    expect(h.frames).toHaveLength(5)
  })

  it("drops a terminal frame the channel delivers twice", () => {
    const h = harness()
    h.follower.accept(started(1))
    h.follower.accept(finished(2))
    // The duplicate with the most to undo: a second terminal frame re-fires the refetch
    // and re-runs the idle transition, so the mark has to outlive the run it ended.
    h.follower.accept(finished(2))

    expect(h.ended).toEqual([[SLOT, RUN]])
    expect(h.counts.idle).toBe(1)
  })

  it("holds a frame that arrives before the one it belongs behind", () => {
    const h = harness()
    h.follower.accept(started(1))
    h.follower.accept(delta(3, " world"))
    // Nothing renders yet: applied in arrival order these transpose, and no later frame
    // repairs assistant text that `appendText` has already concatenated.
    expect(h.textOf(MESSAGE)).toBeUndefined()

    h.follower.accept(delta(2, "hello"))
    // Both land the moment the gap fills, in sequence order rather than arrival order.
    expect(h.textOf(MESSAGE)).toBe("hello world")
    expect(h.frames.map(f => f.event.delta)).toEqual([undefined, "hello", " world"])
  })

  it("holds a whole transposed batch and releases it in order", () => {
    const h = harness()
    h.follower.accept(started(1))
    // The agent POSTs five events per call, each on its own connection, so a transposed
    // pair of calls delivers one batch entirely after the batch that follows it.
    for (const [seq, text] of [
      [7, "f"],
      [8, "g"],
      [9, "h"],
      [10, "i"],
      [11, "j"],
    ] as const) {
      h.follower.accept(delta(seq, text))
    }
    expect(h.textOf(MESSAGE)).toBeUndefined()

    for (const [seq, text] of [
      [2, "a"],
      [3, "b"],
      [4, "c"],
      [5, "d"],
      [6, "e"],
    ] as const) {
      h.follower.accept(delta(seq, text))
    }
    expect(h.textOf(MESSAGE)).toBe("abcdefghij")
  })

  it("stops waiting on a gap that never fills", () => {
    const h = harness()
    h.follower.accept(started(1))
    // 2 is lost, not late. Twenty held frames is four batches' worth of patience; the
    // twenty-first says the transport dropped it and the reply should render short.
    const letters = "bcdefghijklmnopqrstuv".split("")
    letters.forEach((letter, index) => {
      h.follower.accept(delta(index + 3, letter))
      if (index < 20) expect(h.textOf(MESSAGE)).toBeUndefined()
    })

    expect(letters).toHaveLength(21)
    expect(h.textOf(MESSAGE)).toBe(letters.join(""))
  })

  it("stops waiting once the run's terminal frame arrives over a gap", () => {
    const h = harness()
    h.follower.accept(started(1))
    h.follower.accept(delta(3, "and the rest"))
    h.follower.accept(finished(4))

    // A terminal frame is the run's last, so nothing is still in flight under it.
    expect(h.textOf(MESSAGE)).toBe("and the rest")
    expect(h.ended).toEqual([[SLOT, RUN]])
  })

  it("keeps a word split across frames in one text segment", () => {
    const h = harness()
    h.follower.accept(started(1))
    // Amazon Bedrock breaks tokens where it likes: a delta can end mid-word, and two of them
    // rendered as separate segments put a paragraph break inside "Invoice".
    h.follower.accept(delta(2, "Invo"))
    h.follower.accept(delta(3, "ice 51000"))
    h.follower.accept(delta(4, "01976 is blocked"))
    h.follower.accept(finished(5))

    const message = h.messages().find(m => m.id === MESSAGE)
    expect(message?.content).toBe("Invoice 5100001976 is blocked")
    expect(message?.segments).toEqual([{ type: "text", content: "Invoice 5100001976 is blocked" }])
  })

  it("keeps a word whole when its halves arrive swapped", () => {
    const h = harness()
    h.follower.accept(started(1))
    h.follower.accept(delta(3, "ice posted"))
    h.follower.accept(delta(2, "Invo"))
    h.follower.accept(finished(4))

    expect(h.textOf(MESSAGE)).toBe("Invoice posted")
  })

  it("applies an envelope that carries no number where it lands", () => {
    const h = harness()
    // A run in flight across a deploy publishes old-code envelopes with no `seq`. Rejecting
    // them would blank the transcript for the length of that run.
    h.follower.accept(frame(undefined, { type: "RUN_STARTED" }))
    h.follower.accept(
      frame(undefined, { type: "TEXT_MESSAGE_CONTENT", messageId: MESSAGE, delta: "ok" })
    )

    expect(h.textOf(MESSAGE)).toBe("ok")
  })

  it("renders a run it joined mid-flight", () => {
    const h = harness()
    // No replay on this channel, so the first frame seen may be any number. Insisting on 1
    // would stall the run for good.
    h.follower.accept(delta(94, "picked up"))
    h.follower.accept(delta(95, " here"))

    expect(h.textOf(MESSAGE)).toBe("picked up here")
  })

  describe("splicing onto a case already on screen", () => {
    // What a mid-run join actually renders from: the case's stored turns, plus the run in
    // flight from its `live_run` snapshot, both already on screen before a frame arrives.
    const seeded = () =>
      harness([
        ...storedTurns([
          {
            trace_id: "trace-earlier",
            timestamp: "2026-08-27T11:00:00.000Z",
            prompt: "Why is this blocked?",
            segments: [{ type: "text", content: "Quantity mismatch." }],
          },
        ]),
        ...liveTurn({
          run_id: RUN,
          started_at: "2026-08-27T11:59:00.000Z",
          updated_at: "2026-08-27T11:59:58.000Z",
          segments: [{ type: "text", content: "Checking the goods receipt" }],
        }),
      ])

    it("keeps the stored turns and the live head under one run", () => {
      const h = seeded()
      expect(h.messages().map(m => m.id)).toEqual([undefined, "trace-earlier", RUN])

      h.follower.accept(delta(41, " — one moment."))

      // The live turn is claimed by run id, so the follower renders over it rather than
      // beside it; everything the record supplied above it survives untouched.
      const ids = h.messages().map(m => m.id)
      expect(ids).toEqual([undefined, "trace-earlier", RUN, MESSAGE])
      expect(h.textOf(RUN)).toBe("Checking the goods receipt")
      expect(h.textOf(MESSAGE)).toBe(" — one moment.")
    })

    it("drops the live head once a snapshot supersedes it", () => {
      const h = seeded()
      h.follower.accept(delta(41, " — one moment."))
      // A snapshot carrying assistant content is a superset of the record's placeholder, so
      // keeping both would render the same text twice for the rest of the run.
      h.follower.accept(
        frame(42, {
          type: "MESSAGES_SNAPSHOT",
          messages: [
            { id: MESSAGE, role: "assistant", content: "Checking the goods receipt — one moment." },
          ],
        })
      )

      expect(h.messages().map(m => m.id)).toEqual([undefined, "trace-earlier", MESSAGE])
      expect(h.textOf(MESSAGE)).toBe("Checking the goods receipt — one moment.")
    })

    it("leaves the replay alone for a snapshot with no assistant content", () => {
      const h = seeded()
      // A run's first snapshot holds only the client's own message and arrives before any
      // assistant text — superseding on that one would wipe the head of the turn.
      h.follower.accept(
        frame(41, {
          type: "MESSAGES_SNAPSHOT",
          messages: [{ id: "user-1", role: "user", content: "Why is this blocked?" }],
        })
      )

      expect(h.textOf(RUN)).toBe("Checking the goods receipt")
    })
  })

  it("suppresses a dismissed run and still refetches when it ends", () => {
    const h = harness()
    h.follower.accept(started(1))
    h.follower.accept(delta(2, "half a rep"))
    h.follower.dismiss(SLOT)

    // Clear cannot stop the run, so its remaining frames still arrive. Splicing them into
    // the emptied slot puts the assistant's half-written turn back with no user message
    // above it.
    h.follower.accept(delta(3, "ly"))
    h.follower.accept(finished(4))

    expect(h.frames.map(f => f.event.delta)).toEqual([undefined, "half a rep"])
    expect(h.ended).toEqual([[SLOT, RUN]])
    expect(h.counts.idle).toBe(1)
    expect(h.follower.following).toBe(false)
  })

  it("renders the next run on a dismissed slot", () => {
    const h = harness()
    h.follower.accept(started(1))
    h.follower.dismiss(SLOT)
    // Scoped to the run that was dismissed: a slot-wide mark needs something to lift it,
    // and a RUN_STARTED published during a reconnect is simply gone.
    const next: RunEventEnvelope = {
      caseId: SLOT,
      runId: "run-next",
      seq: 1,
      event: { type: "TEXT_MESSAGE_CONTENT", messageId: "msg-next", delta: "fresh" },
    }
    h.follower.accept(next)

    expect(h.textOf("msg-next")).toBe("fresh")
    expect(h.follower.runOn(SLOT)).toBe("run-next")
  })

  it("idles only when the last followed slot ends", () => {
    const h = harness()
    const other = { caseId: "5100001977-2026", runId: "run-other", seq: 1 }
    h.follower.accept(started(1))
    h.follower.accept({ ...other, event: { type: "RUN_STARTED" } })

    h.follower.accept(finished(2))
    // Finishing one case must not blank the other's activity rail.
    expect(h.counts.idle).toBe(0)

    h.follower.accept({ ...other, seq: 2, event: { type: "RUN_FINISHED" } })
    expect(h.counts.idle).toBe(1)
  })

  it("ignores an envelope with no slot", () => {
    const h = harness()
    h.follower.accept({ caseId: "", runId: RUN, seq: 1, event: { type: "RUN_STARTED" } })

    expect(h.frames).toHaveLength(0)
    expect(h.follower.following).toBe(false)
  })
})
