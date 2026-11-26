// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest"
import { subscribeRunEvents, type RunEventEnvelope } from "../channel.ts"

/**
 * The socket is injected, so these are real assertions about the handshake rather
 * than a mock of the thing under test: nothing here stubs eventsChannel itself.
 */
class FakeSocket {
  static instances: FakeSocket[] = []
  sent: string[] = []
  closed = false
  onopen?: () => void
  onmessage?: (event: { data: string }) => void
  onclose?: () => void

  constructor(
    public url: string,
    public protocols: string[]
  ) {
    FakeSocket.instances.push(this)
  }
  send(data: string) {
    this.sent.push(data)
  }
  close() {
    this.closed = true
    this.onclose?.()
  }
  /** Server → client. */
  deliver(message: unknown) {
    this.onmessage?.({ data: JSON.stringify(message) })
  }
  parsed(): Record<string, unknown>[] {
    return this.sent.map(s => JSON.parse(s) as Record<string, unknown>)
  }
}

/** Every subscription is closed in afterEach so no watchdog timer outlives its test. */
const opened: { close: () => void }[] = []

function connect(
  onEvent: (e: RunEventEnvelope) => void = () => {},
  getIdToken: () => Promise<string> = async () => "id-token"
) {
  const subscription = subscribeRunEvents({
    httpDomain: "abc.appsync-api.us-east-1.amazonaws.com",
    realtimeDomain: "abc.appsync-realtime-api.us-east-1.amazonaws.com",
    channel: "/runs/*",
    getIdToken,
    onEvent,
    socketFactory: (url, protocols) => new FakeSocket(url, protocols) as unknown as WebSocket,
  })
  opened.push(subscription)
  return subscription
}

/** Let the pending token fetch settle so the socket exists. */
const settle = () => new Promise(resolve => setTimeout(resolve, 0))

beforeEach(() => {
  FakeSocket.instances = []
})

afterEach(() => {
  opened.forEach(subscription => subscription.close())
  opened.length = 0
  vi.useRealTimers()
})

describe("subscribeRunEvents", () => {
  it("presents the id token and the HTTP domain in the handshake", async () => {
    connect()
    await settle()
    const socket = FakeSocket.instances[0]

    expect(socket.url).toBe("wss://abc.appsync-realtime-api.us-east-1.amazonaws.com/event/realtime")
    expect(socket.protocols[0]).toBe("aws-appsync-event-ws")
    const header = JSON.parse(
      atob(
        socket.protocols[1]
          .replace(/^header-/, "")
          .replace(/-/g, "+")
          .replace(/_/g, "/")
      )
    )
    // The host is the HTTP domain even though the socket is the realtime one — the
    // service signs against the API's domain, and this is the difference between a
    // working subscription and a silent 401.
    expect(header).toEqual({
      Authorization: "id-token",
      host: "abc.appsync-api.us-east-1.amazonaws.com",
    })
  })

  it("keeps the encoded auth header legal in a subprotocol name", async () => {
    // `~` and `?` at these offsets are what make btoa emit `+` and `/`; the trailing
    // character is what makes it pad. A real JWT cannot produce `+` or `/` at all, but it
    // pads two times in three — and `/` and `=` are both illegal in a subprotocol name, so
    // an unsubstituted header makes the browser throw SyntaxError instead of connecting.
    const token = "id-token~yy?a"
    connect(
      () => {},
      async () => token
    )
    await settle()

    const raw = btoa(
      JSON.stringify({ Authorization: token, host: "abc.appsync-api.us-east-1.amazonaws.com" })
    )
    // Guard: the pin below says nothing unless the raw encoding really carries all three.
    expect(raw).toContain("+")
    expect(raw).toContain("/")
    expect(raw).toContain("=")

    const encoded = FakeSocket.instances[0].protocols[1]
    expect(encoded).toMatch(/^header-[A-Za-z0-9\-_]+$/)
    // Legal is not the same as decodable, and the charset above cannot tell the two apart:
    // mapping `+` to `_` and `/` to `-` — the substitution backwards — passes that regex
    // and hands the service bytes it reads as a different string, which it answers with a
    // subscribe_error and nothing else. So the header is decoded here with base64url's own
    // inverse rather than with whatever mapping the encoder happens to use.
    expect(
      JSON.parse(
        atob(
          encoded
            .replace(/^header-/, "")
            .replace(/-/g, "+")
            .replace(/_/g, "/")
        )
      )
    ).toEqual({ Authorization: token, host: "abc.appsync-api.us-east-1.amazonaws.com" })
  })

  it("subscribes to the channel once the connection is acknowledged", async () => {
    let fetches = 0
    connect(
      () => {},
      async () => {
        fetches++
        return "id-token"
      }
    )
    await settle()
    const socket = FakeSocket.instances[0]
    socket.onopen?.()
    expect(socket.parsed()[0]).toEqual({ type: "connection_init" })

    socket.deliver({ type: "connection_ack", connectionTimeoutMs: 300000 })
    await settle()

    const subscribe = socket.parsed()[1]
    expect(subscribe.type).toBe("subscribe")
    expect(subscribe.channel).toBe("/runs/*")
    // Per-connection unique, and the service rejects anything outside this grammar.
    expect(subscribe.id as string).toMatch(/^[a-zA-Z0-9-_+]{1,128}$/)
    // The same auth the handshake carried, built by a single fetch: re-deriving it here
    // costs a round trip and adds a failure path for a token the socket already holds.
    expect(subscribe.authorization).toEqual({
      Authorization: "id-token",
      host: "abc.appsync-api.us-east-1.amazonaws.com",
    })
    expect(fetches).toBe(1)
  })

  it("hands the caller every envelope in a data frame", async () => {
    const seen: RunEventEnvelope[] = []
    connect(e => seen.push(e))
    await settle()
    const socket = FakeSocket.instances[0]
    socket.onopen?.()
    socket.deliver({ type: "connection_ack" })
    await settle()

    // A frame may carry one payload or a batch of them.
    socket.deliver({
      type: "data",
      event: JSON.stringify({ caseId: "c-1", runId: "r-1", event: { type: "RUN_STARTED" } }),
    })
    socket.deliver({
      type: "data",
      event: [
        JSON.stringify({ caseId: "c-1", runId: "r-1", event: { type: "TEXT_MESSAGE_CONTENT" } }),
        JSON.stringify({ caseId: "c-2", runId: "r-2", event: { type: "RUN_STARTED" } }),
      ],
    })

    expect(seen.map(e => [e.caseId, e.event.type])).toEqual([
      ["c-1", "RUN_STARTED"],
      ["c-1", "TEXT_MESSAGE_CONTENT"],
      ["c-2", "RUN_STARTED"],
    ])
  })

  it("keeps an envelope numbered zero", async () => {
    // `seq` is a number, and the obvious guard — `!envelope.seq` alongside the other three
    // — discards `0` with it. The publisher starts at 1 today, so this costs nothing today;
    // it is the trap that gets sprung the day someone starts the counter at 0, and it would
    // show up as one silently missing frame per run rather than as an error.
    const seen: RunEventEnvelope[] = []
    connect(e => seen.push(e))
    await settle()
    const socket = FakeSocket.instances[0]
    socket.onopen?.()
    socket.deliver({ type: "connection_ack" })
    await settle()

    socket.deliver({
      type: "data",
      event: JSON.stringify({
        caseId: "c-1",
        runId: "r-1",
        seq: 0,
        event: { type: "RUN_STARTED" },
      }),
    })

    expect(seen.map(e => e.seq)).toEqual([0])
  })

  it("keeps an envelope whose number is null", async () => {
    // `null` is the one shape where a missing number would have cost the frame rather than
    // just the dedup: it is not `undefined`, so an absent-check reads it as present, and it
    // is not a number, so a type-check rejects the whole envelope. Nothing on this wire
    // sends it — the point is that an envelope carrying no usable number renders either way.
    const seen: RunEventEnvelope[] = []
    connect(e => seen.push(e))
    await settle()
    const socket = FakeSocket.instances[0]
    socket.onopen?.()
    socket.deliver({ type: "connection_ack" })
    await settle()

    socket.deliver({
      type: "data",
      event: JSON.stringify({
        caseId: "c-1",
        runId: "r-1",
        seq: null,
        event: { type: "RUN_STARTED" },
      }),
    })

    expect(seen.map(e => e.event.type)).toEqual(["RUN_STARTED"])
  })

  it("survives a frame it cannot read", async () => {
    const seen: RunEventEnvelope[] = []
    connect(e => seen.push(e))
    await settle()
    const socket = FakeSocket.instances[0]
    socket.onopen?.()
    socket.deliver({ type: "connection_ack" })
    await settle()

    socket.onmessage?.({ data: "not json" })
    socket.deliver({ type: "data", event: "{oops" })
    socket.deliver({ type: "ka" })
    socket.deliver({
      type: "data",
      event: JSON.stringify({ caseId: "c-1", runId: "r-1", event: { type: "RUN_FINISHED" } }),
    })

    expect(socket.closed).toBe(false)
    expect(seen).toHaveLength(1)
  })

  it("reconnects after the socket drops", async () => {
    vi.useFakeTimers()
    connect()
    await vi.advanceTimersByTimeAsync(0)
    FakeSocket.instances[0].onclose?.()

    await vi.advanceTimersByTimeAsync(60_000)
    expect(FakeSocket.instances.length).toBeGreaterThan(1)
  })

  it("opens one socket when two drops each schedule a retry", async () => {
    vi.useFakeTimers()
    connect()
    await vi.advanceTimersByTimeAsync(0)
    // A second `onclose` on an already-reaped socket — the service's close and the
    // watchdog's, say — used to strand the first retry timer rather than replace it, so
    // both fired and two connects raced for the single `socket` slot. Two live sockets
    // means duplicate subscriptions and a survivor that `close()` can never reap, because
    // it only ever holds the last one.
    FakeSocket.instances[0].onclose?.()
    FakeSocket.instances[0].onclose?.()

    await vi.advanceTimersByTimeAsync(60_000)
    expect(FakeSocket.instances).toHaveLength(2)
  })

  it("stops reconnecting once the caller closes it", async () => {
    vi.useFakeTimers()
    const subscription = connect()
    await vi.advanceTimersByTimeAsync(0)
    subscription.close()

    await vi.advanceTimersByTimeAsync(60_000)
    expect(FakeSocket.instances).toHaveLength(1)
    expect(FakeSocket.instances[0].closed).toBe(true)
  })

  it("closes a connection that has gone quiet", async () => {
    vi.useFakeTimers()
    connect()
    await vi.advanceTimersByTimeAsync(0)
    const socket = FakeSocket.instances[0]
    socket.onopen?.()
    socket.deliver({ type: "connection_ack", connectionTimeoutMs: 300000 })

    // A `ka` inside the window keeps it alive. Silence past the window does not: a
    // server that goes away without a TCP FIN never fires onclose, so silence is the
    // only signal there is.
    await vi.advanceTimersByTimeAsync(200_000)
    socket.deliver({ type: "ka" })
    await vi.advanceTimersByTimeAsync(200_000)
    expect(socket.closed).toBe(false)

    await vi.advanceTimersByTimeAsync(300_001)
    expect(socket.closed).toBe(true)
  })

  it("retries when the token fetch fails before the socket exists", async () => {
    vi.useFakeTimers()
    let fetches = 0
    connect(
      () => {},
      async () => {
        if (fetches++ === 0) throw new Error("token refresh failed")
        return "id-token"
      }
    )

    // The token is fetched before the socket is opened, so this failure never reaches the
    // factory: there is nothing to close and nothing to go quiet, and the backoff is the
    // only thing that can bring the subscription back.
    await vi.advanceTimersByTimeAsync(0)
    expect(FakeSocket.instances).toHaveLength(0)

    await vi.advanceTimersByTimeAsync(1_000)
    expect(FakeSocket.instances).toHaveLength(1)
  })

  it("re-handshakes when the service rejects the subscribe", async () => {
    vi.useFakeTimers()
    connect()
    await vi.advanceTimersByTimeAsync(0)
    const socket = FakeSocket.instances[0]
    socket.onopen?.()
    socket.deliver({ type: "connection_ack" })
    await vi.advanceTimersByTimeAsync(0)

    // Otherwise the socket stays open, keepalive-fed, and permanently silent. The
    // usual cause is an expired id token, and only a reconnect fetches a fresh one.
    socket.deliver({ type: "subscribe_error", errors: [{ errorType: "Unauthorized" }] })
    expect(socket.closed).toBe(true)
  })

  it("re-handshakes on a connection error and on a bare error frame", async () => {
    vi.useFakeTimers()
    // Both used to fall through the handler entirely — no branch matched them — which
    // left the socket open and keepalive-fed with no subscription behind it. That is
    // indistinguishable from a healthy but idle run, so nothing ever recovered it.
    for (const type of ["connection_error", "error"]) {
      connect()
      await vi.advanceTimersByTimeAsync(0)
      const socket = FakeSocket.instances[FakeSocket.instances.length - 1]
      socket.onopen?.()
      socket.deliver({ type, errors: [{ errorType: "Unauthorized" }] })
      expect(socket.closed).toBe(true)
    }
  })

  it("delivers the rest of a batch when the consumer throws on one payload", async () => {
    // The consumer call used to sit inside the parse try, so a throwing consumer was
    // reported as nothing at all and took every later payload in the same frame with it.
    const errors = vi.spyOn(console, "error").mockImplementation(() => {})
    const seen: string[] = []
    connect(e => {
      if (e.caseId === "c-bad") throw new Error("consumer blew up")
      seen.push(e.caseId)
    })
    await settle()
    const socket = FakeSocket.instances[0]
    socket.onopen?.()
    socket.deliver({ type: "connection_ack" })
    await settle()

    socket.deliver({
      type: "data",
      event: [
        JSON.stringify({ caseId: "c-bad", runId: "r-1", event: { type: "RUN_STARTED" } }),
        JSON.stringify({ caseId: "c-good", runId: "r-2", event: { type: "RUN_STARTED" } }),
      ],
    })

    expect(seen).toEqual(["c-good"])
    expect(socket.closed).toBe(false)
    // The frame the consumer threw on is not silent: a malformed payload stays silent, and
    // that difference is the whole reason the two catches are separate.
    expect(errors).toHaveBeenCalledTimes(1)
    errors.mockRestore()
  })
})
