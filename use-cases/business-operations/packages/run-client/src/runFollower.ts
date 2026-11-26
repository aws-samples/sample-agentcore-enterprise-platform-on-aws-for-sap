// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import type { RunEventEnvelope } from "./channel.ts"
import {
  reduceAguiEvent,
  spliceTurnIntoHistory,
  type AguiEvent,
  type AguiProjection,
} from "./reducer.ts"
import type { Message } from "./types.ts"

/**
 * The run protocol, as a plain object: envelopes in, transcript writes out.
 *
 * Everything the wire forces on a subscriber lives here — `seq` dedup, out-of-order
 * buffering, per-run projections, the splice onto whatever is already on screen, and the
 * suppression of a run the operator dismissed. None of it is UI, which is why none of it
 * is in a component: AppSync Events guarantees neither ordering nor at-most-once
 * delivery (`./channel.ts`), and the reducer's text deltas append rather than assign
 * (`./reducer.ts`), so any client that renders a run token by token has to do all of it.
 *
 * A "slot" is the channel key a run publishes under: the case id, or the run's session id
 * when there is no case. One follower handles every slot on one subscription.
 */

/**
 * How many out-of-order envelopes one run may hold before the follower stops waiting for
 * the gap below them to fill.
 *
 * The agent POSTs its deltas five to a call, each on a connection of its own, so a
 * transposed pair of calls leaves a gap of about five. Twenty is four of those: past that
 * the gap is a frame the transport lost rather than one still in flight, and rendering
 * short beats not rendering at all.
 */
const MAX_PENDING_FRAMES = 20

/** One frame, after it has been reduced and written. */
export interface AppliedFrame {
  slot: string
  runId: string
  /** The envelope's AG-UI event, as reduced. */
  event: Record<string, unknown>
  /** This run's projected messages, as just spliced into the slot. */
  turn: Message[]
  /** RUN_FINISHED or RUN_ERROR — the run's last frame. */
  terminal: boolean
}

export interface RunFollowerHost {
  /** Everything currently on screen for a slot. Read on the first frame of a run, so the
   *  projection starts out holding what it is about to write over. */
  readTranscript(slot: string): Message[]
  /** Apply an update to a slot. Given an updater rather than a value because the caller
   *  may have an uncommitted append in flight — see `spliceTurnIntoHistory`. */
  writeTranscript(slot: string, update: (previous: Message[]) => Message[]): void
  /** Every frame, after its write. Where a UI derives "the agent is thinking" and where a
   *  client watches for a tool result it has to act on. */
  onFrame?(frame: AppliedFrame): void
  /** A run's terminal frame, including one whose transcript was dismissed. The run's
   *  trace is written by now, so this is when to refetch the record. */
  onRunEnd?(slot: string, runId: string): void
  /** No run is being followed any more, on any slot. */
  onIdle?(): void
  /** The timestamp given to events that carry none. Injectable so a test can pin it. */
  now?(): string
}

interface FollowedRun {
  runId: string
  projection: AguiProjection
  ownedIds: Set<string>
}

interface SequenceMark {
  runId: string
  /** How far in order this run has been applied. Not the highest number seen. */
  seq: number
  pending: Map<number, Record<string, unknown>>
}

export class RunFollower {
  // Runs being followed, keyed by slot. A run this client did not start still has to
  // render token by token, and the reducer needs somewhere to keep its projection between
  // frames.
  private readonly runs = new Map<string, FollowedRun>()

  // Slot → the run that was on screen when the operator dismissed it. Dismissing cannot
  // stop a run, so its remaining frames still arrive and have to be dropped. Keyed by run
  // and not by slot so the mark cannot outlive that run: nothing has to arrive to end it.
  private readonly dismissed = new Map<string, string>()

  // Slot → how far in order the run currently publishing to it has been applied, plus the
  // envelopes that arrived past that point. The channel can deliver the same event twice
  // and can deliver two events out of order, and the reducer's text deltas append rather
  // than assign — so a redelivery renders an interior slice of the reply a second time,
  // and a frame applied early splices its text into the wrong place. Both are fixed by the
  // same number: `seq` is the only thing on the wire that says where a frame belongs. The
  // run id travels with it because the sequence restarts at 1 per run, so comparing across
  // runs would drop a new run's opening frames. Its own map rather than a field on `runs`
  // above, which is deleted on the run's terminal frame — a mark kept there would be gone
  // exactly when a redelivered RUN_FINISHED arrives, the duplicate with the most to undo.
  //
  // Never evicted, and it does not grow the way `runs` does — the subscription is
  // a wildcard over each visible pack's `/runs/<short_id>/*`, so this takes an entry for every case anyone runs on while
  // the client is up, not only the ones this operator opened. Two strings, a number and at
  // most MAX_PENDING_FRAMES held envelopes each, bounded by the process's life: worth
  // naming, not worth an eviction policy.
  private readonly marks = new Map<string, SequenceMark>()

  private readonly host: RunFollowerHost

  // Assigned in the body rather than declared as `constructor(private readonly host: …)`.
  // A parameter property emits code, so Node's type stripping rejects it exactly as it
  // rejects an `enum` — and this package is meant to run on bare Node with no build step
  // (see examples/follow-run.ts).
  constructor(host: RunFollowerHost) {
    this.host = host
  }

  /** Whether any run is still being followed. */
  get following(): boolean {
    return this.runs.size > 0
  }

  /** The run currently publishing to a slot, if one is. */
  runOn(slot: string): string | undefined {
    return this.runs.get(slot)?.runId
  }

  /**
   * Put the channel's envelopes back in the order the agent published them, then render.
   *
   * The subscription is the only caller. AppSync Events guarantees neither ordering nor
   * at-most-once delivery, and the agent hands the service five events per call, each on a
   * connection of its own — so a transposed pair of calls delivers a whole batch of deltas
   * after the batch that follows it. Both hazards are the same fact from opposite sides, and
   * `seq` is the only thing on the wire that resolves either.
   *
   * Comparing against a high-water mark alone handles the duplicate and loses the
   * transposition: the lower number reads as already-seen and is dropped, silently, and on a
   * delta payload nothing puts it back — `appendText` is the only writer of assistant text
   * there, and a case seeds its transcript once per session, so the terminal refetch
   * corrects the record without re-rendering it. The reply stays short by that batch until a
   * reload. So an early frame is held rather than dropped, and the mark tracks how far the
   * run has been applied *in order* rather than the highest number seen.
   */
  accept({ caseId: slot, runId, seq, event }: RunEventEnvelope): void {
    if (!slot) return
    // An envelope from a run that started before this client deployed carries no number.
    // Rejecting it would blank the transcript for the length of that run, and there is
    // nothing to order it against, so it applies where it lands.
    if (typeof seq !== "number") {
      this.applyFrame(slot, runId, event)
      return
    }
    const mark = this.marks.get(slot)
    // The first frame seen for a run starts the mark wherever it lands. Insisting on 1
    // would stall a run this client joined mid-flight — the channel has no replay — or one
    // whose opening batch the transport lost, and stall it for good.
    if (mark?.runId !== runId) {
      this.marks.set(slot, { runId, seq, pending: new Map() })
      this.applyFrame(slot, runId, event)
      return
    }
    // Already applied, or already waiting. Dropped here because everything downstream is
    // unsafe to run twice: the reduce appends text deltas rather than assigning them, and a
    // second terminal frame re-fires the case refetch, re-runs the idle transition, and —
    // the first one having already lifted the dismissed mark — rebuilds an entry for a run
    // the operator dismissed.
    if (seq <= mark.seq || mark.pending.has(seq)) return
    mark.pending.set(seq, event)
    // A terminal frame is the run's last, so a gap under it is a loss and not a late
    // arrival; a full buffer says the same thing about a gap that has outlasted four
    // batches. Either way stop waiting and render what there is — a reply short by one
    // batch beats a reply that stops at the hole.
    // That leaves one case uncovered, the run's own tail. A terminal frame that
    // overtakes the batch below it releases immediately and those deltas are then refused as
    // already-applied, so that reply still renders short until a reload. Closing it means
    // waiting a beat on the terminal frame before giving up, and a timer to end the wait —
    // worth it only if the tail turns out to transpose as often as the middle does.
    const releaseAll =
      event.type === "RUN_FINISHED" ||
      event.type === "RUN_ERROR" ||
      mark.pending.size > MAX_PENDING_FRAMES
    // In sequence order, which is the whole point: out of order these corrupt the reply
    // exactly as the dropped frame did. Contiguously from the mark unless something above
    // says the gap will never fill, so a frame that arrives early waits for the one it
    // belongs behind, and the pair renders the moment that one lands.
    for (const next of [...mark.pending.keys()].sort((a, b) => a - b)) {
      if (!releaseAll && next !== mark.seq + 1) break
      const held = mark.pending.get(next)
      mark.pending.delete(next)
      mark.seq = next
      if (held) this.applyFrame(slot, runId, held)
    }
  }

  /**
   * Stop rendering the run currently on this slot, without stopping the run.
   *
   * The Clear control. Splicing the rest of a dismissed run into the emptied slot puts the
   * assistant's half-written turn back on screen with no user message above it. Nothing to
   * record if no run is on screen: the entry exists from the first frame taken, so its
   * absence means there is nothing left to re-splice.
   */
  dismiss(slot: string): void {
    const runId = this.runs.get(slot)?.runId
    if (runId) this.dismissed.set(slot, runId)
  }

  /**
   * Give up the projection for a slot, because a new run is starting on it.
   *
   * Dropping it rather than reusing it keeps the new run from inheriting the old one's
   * claimed ids — the entry rebuilds itself from the transcript on the first frame.
   */
  takeOver(slot: string): void {
    this.runs.delete(slot)
  }

  /** Forget every projection. For a subscription going away. */
  reset(): void {
    this.runs.clear()
  }

  /**
   * Fold one channel event into its slot's transcript, at its place in the run.
   *
   * Called only from `accept` above, which owns the ordering: every frame that reaches here
   * is the next one this run has not applied, and no frame reaches here twice. That is a
   * precondition, not a convenience — the reduce appends text deltas, so applying one early
   * or twice is a corrupt reply rather than a retry.
   *
   * Claiming a message id means taking responsibility for everything rendered under it:
   * `spliceTurnIntoHistory` drops every owned id from the history and appends the turn in
   * its place. So an entry starts out holding whatever is already on screen for its run, and
   * never claims an id it cannot re-render.
   *
   * This is the only renderer. A run this client started is followed here exactly like one
   * started by the shift handover or another operator's tab — the client holds no second
   * copy of the events to de-duplicate against.
   *
   * Which is why the seed exists, and why it is only a placeholder: it keeps the head of a
   * mid-run turn on screen so the case does not read as blank, or lose its replay, until the
   * first MESSAGES_SNAPSHOT takes the turn over. That is a short life — a real run publishes
   * its first snapshot before any assistant text and thirteen more after.
   *
   * The takeover is not guaranteed, and where it never happens the placeholder is
   * never superseded — so the run reads as head-bubble plus tail-bubble until a reload
   * replays the record. Two ways to get there: a snapshot over the channel's 240 KB limit is
   * dropped rather than trimmed, and the adapter suppresses snapshots entirely on a delta
   * payload, which is every run after the first in a case's session, since this client sends
   * one message and the session restores the rest.
   *
   * That is degradation rather than error — a missing frame, not a wrong render. It holds for
   * a tool spanning the join because the seed and the wire share one id space: a stored tool
   * segment carries the SDK `tool_call_id` it was published under, so this run's own
   * TOOL_CALL_RESULT settles the card already on screen instead of opening a second one
   * (`mapSegment`, ./traces.ts).
   */
  private applyFrame(slot: string, runId: string, event: Record<string, unknown>): void {
    // Dismissed stays dismissed, for the run that was dismissed. Scoped to the run id
    // rather than to the slot because a slot-wide mark needs something to lift it, and the
    // obvious candidate can fail to arrive: the channel has no replay, so a RUN_STARTED
    // published during a reconnect is gone, and the mark would latch and swallow the whole
    // of the next followed run. A run id needs no lift: a frame belonging to any other run
    // was never suppressed to begin with.
    if (this.dismissed.get(slot) === runId) {
      if (isTerminal(event)) {
        this.dismissed.delete(slot)
        // The same two steps the terminal branch below runs, because this return skips it.
        // A dismissed run still holds this slot's entry, and an empty map is the only thing
        // that idles a UI's activity rail — so leaving the entry behind strands it lit for
        // the rest of the session, with nothing left to arrive that could clear it.
        //
        // Only the delete is the dismissed run's to do, and only while the entry is still
        // its own. Dismissing does not stop the run, so the operator can start a second run
        // on this slot while the first is still publishing: the entry then belongs to the
        // new run, and dropping it would rebuild the next frame's projection with a fresh
        // `ownedIds` that no longer claims what that run has already spliced in. The idle
        // check is outside the guard because the entry can be *absent* rather than newer — a
        // start deletes it before it POSTs, so a refused or failed start leaves the map
        // empty with nothing left to rebuild it, and gating the repair on ownership then
        // strands the rail lit with nothing else due to arrive. Idling is right in the
        // newer-run case too: the rail is showing the dismissed run's activity, and a live
        // run relights it from its next frame.
        if (this.runs.get(slot)?.runId === runId) this.runs.delete(slot)
        if (this.runs.size === 0) this.host.onIdle?.()
        // The transcript stays cleared; the record should not stay behind it. This is the
        // dismissed run's last frame, so its trace is written — and refetching now is what
        // keeps a case list, and a later reload of this case, honest about what it did.
        this.host.onRunEnd?.(slot, runId)
      }
      return
    }

    let entry = this.runs.get(slot)
    if (!entry || entry.runId !== runId) {
      // What this entry is about to write over. The record's live_run renders this run under
      // the bare run_id (`liveTurn` → `id: trace.trace_id`), and this event's own message may
      // already be on screen from an entry a takeover dropped mid-run. Both are claimed, so
      // both have to be held: a claim without the content is a deletion at the splice.
      const holds = new Set([runId])
      if (typeof event.messageId === "string") holds.add(event.messageId)
      entry = {
        runId,
        projection: {
          messages: this.host.readTranscript(slot).filter(m => m.id && holds.has(m.id)),
        },
        ownedIds: holds,
      }
      this.runs.set(slot, entry)
    }

    entry.projection = reduceAguiEvent(
      entry.projection,
      event as AguiEvent,
      this.host.now?.() ?? new Date().toISOString(),
      runId
    )
    // A snapshot carries the whole accumulated turn under the adapter's own ids, so it is a
    // superset of the record's placeholder — which stays claimed, and so leaves the screen
    // at the splice. Keeping it would render the same text, and the same tool call, twice
    // for the rest of the run. Not the reducer's business: it projects events, and knows
    // nothing about where a seed came from.
    //
    // Only a snapshot carrying assistant content supersedes anything: a run's first arrives
    // before any assistant text, holding just the client's own message, and superseding on
    // that one would wipe the replay. Read off this event rather than the projection, which
    // by now also holds the message the deltas opened.
    if (
      event.type === "MESSAGES_SNAPSHOT" &&
      Array.isArray(event.messages) &&
      event.messages.some(m => (m as { role?: unknown })?.role === "assistant")
    ) {
      entry.projection = {
        ...entry.projection,
        messages: entry.projection.messages.filter(m => m.id !== runId),
      }
    }

    const terminal = isTerminal(event)
    // Nothing settles the projection here, and nothing needs to: both terminal branches of
    // the reduce above already mark unresolved tool calls incomplete, and every run ends on
    // a terminal event, because there is no response body left to break off mid-turn.
    const turn = entry.projection.messages
    const ownedIds = entry.ownedIds
    for (const message of turn) {
      if (message.id) ownedIds.add(message.id)
    }
    this.host.writeTranscript(slot, previous => spliceTurnIntoHistory(previous, turn, ownedIds))
    this.host.onFrame?.({ slot, runId, event, turn, terminal })
    if (terminal) {
      this.runs.delete(slot)
      // Only idle when no other slot is still being followed, so finishing case A does not
      // blank case B's rail. The entry for this slot is already gone, so an empty map means
      // this was the last run on screen.
      if (this.runs.size === 0) this.host.onIdle?.()
      this.host.onRunEnd?.(slot, runId)
    }
  }
}

function isTerminal(event: Record<string, unknown>): boolean {
  return event.type === "RUN_FINISHED" || event.type === "RUN_ERROR"
}
