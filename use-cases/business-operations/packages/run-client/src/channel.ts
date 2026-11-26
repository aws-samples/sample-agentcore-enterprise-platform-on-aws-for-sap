// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * A subscription to an AppSync Events channel, over the `aws-appsync-event-ws`
 * subprotocol.
 *
 * Hand-rolled because the alternative is adding aws-amplify to a frontend that has
 * never carried it — the protocol is a handshake, a subscribe, and a data frame.
 *
 * The socket's lifetime is capped by the service at 24 hours and a network blip drops
 * it sooner, so reconnect is part of the contract rather than a refinement.
 */

export interface RunEventEnvelope {
  /**
   * The channel key this event's run publishes under: the case id, or the run's session
   * id when there is no case. Named for the case because that is the field on the wire
   * and the common one; the browser keys its slots `caseId ?? sessionId` to match.
   */
  caseId: string
  /** The AG-UI run id, which most event types do not carry themselves. */
  runId: string
  /**
   * This event's number within its run, from 1, assigned at publish time and contiguous
   * over what was actually sent. AppSync Events guarantees neither at-most-once delivery
   * nor ordering, and the reducer's text deltas append rather than assign — so a subscriber
   * has to drop an envelope it has already seen and hold one that arrives early, and this is
   * the only thing on the wire it can do either by. Both live in `RunFollower.accept`
   * (`./runFollower.ts`) rather than here: a redelivered event costs more than a duplicated
   * delta, and reordering needs somewhere to put the frames it is waiting on.
   *
   * Optional, though the publisher always sends it. During a deploy the agent and this
   * bundle do not change over at the same instant, so a run already in flight can publish
   * old-code envelopes to a browser running new code; one without a `seq` applies exactly
   * as it does today and simply takes no part in dedup. `null` is admitted for the same
   * reason and treated identically — nothing sends it, but it is what a JSON encoder makes
   * of an unset field, and the subscriber tolerates it rather than lose the frame.
   */
  seq?: number | null
  /** One canonical AG-UI event, camelCase keys, as the reducer already consumes it. */
  event: Record<string, unknown>
}

export type RunChannelStatus = "connecting" | "subscribed" | "closed"

export interface SubscribeRunEventsOptions {
  /** The API's HTTP domain. Also the handshake's `host` — not the realtime domain. */
  httpDomain: string
  realtimeDomain: string
  /** A channel or a pack's wildcard, e.g. `/runs/ap/*`. `/runs/*` is refused. */
  channel: string
  /** The id token: Cognito auth is scoped by appIdClientRegex, which matches `aud`. */
  getIdToken: () => Promise<string | undefined>
  onEvent: (envelope: RunEventEnvelope) => void
  onStatus?: (status: RunChannelStatus) => void
  /** Injected so a test can drive a fake socket. */
  socketFactory?: (url: string, protocols: string[]) => WebSocket
}

/** The service reads the auth object out of a subprotocol name, so it must be b64url. */
function base64url(value: string): string {
  return btoa(value).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "")
}

/** A data frame's `event` is one stringified payload or a batch of them. */
function payloadsOf(event: unknown): string[] {
  if (typeof event === "string") return [event]
  if (Array.isArray(event)) return event.filter((item): item is string => typeof item === "string")
  return []
}

export function subscribeRunEvents(options: SubscribeRunEventsOptions): { close: () => void } {
  const factory =
    options.socketFactory ?? ((url: string, protocols: string[]) => new WebSocket(url, protocols))
  let socket: WebSocket | null = null
  let stopped = false
  let attempt = 0
  let retry: ReturnType<typeof setTimeout> | undefined
  let idle: ReturnType<typeof setTimeout> | undefined
  /**
   * AWS's documented default connection timeout for an Event API, replaced by whatever
   * the ack reports. It is a floor on silence rather than a cadence: the service sends
   * `ka` about every 60s and the watchdog re-arms on every frame, so five keepalives have
   * to go missing before it can fire — which is the point, since it exists for the server
   * that vanishes without a TCP FIN and not for a slow one.
   */
  let idleTimeoutMs = 300_000

  const authorization = async () => ({
    Authorization: (await options.getIdToken()) ?? "",
    host: options.httpDomain,
  })

  const emit = (payload: string) => {
    let envelope: Partial<RunEventEnvelope>
    try {
      envelope = JSON.parse(payload) as Partial<RunEventEnvelope>
    } catch {
      // A malformed payload is one lost frame, never a reason to drop the socket.
      return
    }
    // `seq` is checked by type and not for truthiness: it is a number, and `!envelope.seq`
    // would discard `0`. Absent is allowed through rather than rejected — see the field's
    // own comment for why an envelope without one still has to render. `null` counts as
    // absent for the same reason: it carries no usable number either way, and rejecting
    // the envelope over it would cost the frame instead of just the dedup.
    if (!envelope.caseId || !envelope.runId || !envelope.event) return
    if (envelope.seq != null && typeof envelope.seq !== "number") return
    try {
      options.onEvent(envelope as RunEventEnvelope)
    } catch (error) {
      // The consumer's own bug, and deliberately not the parse catch above: sharing one
      // made a throwing consumer indistinguishable from a bad frame and silent either
      // way. Caught per payload rather than per frame so one throw does not eat the rest
      // of a batch, and reported rather than swallowed because nothing else would ever
      // say it happened.
      console.error("[run-events] onEvent threw for a delivered envelope:", error)
    }
  }

  const handle = (ws: WebSocket, auth: { Authorization: string; host: string }, raw: string) => {
    let message: { type?: string; event?: unknown; connectionTimeoutMs?: unknown }
    try {
      message = JSON.parse(raw)
    } catch {
      return
    }
    if (message.type === "connection_ack") {
      if (typeof message.connectionTimeoutMs === "number") {
        idleTimeoutMs = message.connectionTimeoutMs
      }
      ws.send(
        JSON.stringify({
          type: "subscribe",
          id: crypto.randomUUID(),
          channel: options.channel,
          // The connection's own auth, not a fresh fetch: `connect` built this one
          // milliseconds ago, and re-deriving it would add a second failure path for a
          // credential this socket already carries.
          authorization: auth,
        })
      )
      return
    }
    if (message.type === "subscribe_success") {
      // Reset the backoff here, not on `connection_ack`: a connection that acks and
      // then fails to subscribe every time would restart the backoff on every cycle
      // and hammer the service at the floor delay forever.
      attempt = 0
      options.onStatus?.("subscribed")
      return
    }
    // `connection_error` and a bare `error` are the same situation as a failed subscribe:
    // the service refused, and otherwise the socket stays open, keepalive-fed, and
    // permanently silent. The usual cause is an expired id token, and closing is what gets
    // a fresh one, because reconnecting re-runs getIdToken.
    if (
      message.type === "subscribe_error" ||
      message.type === "connection_error" ||
      message.type === "error"
    ) {
      ws.close()
      return
    }
    if (message.type === "data") {
      for (const payload of payloadsOf(message.event)) emit(payload)
    }
  }

  /**
   * The service sends `ka` about every 60s, and a server that goes away without a TCP
   * FIN never fires `onclose` — so silence, not an error, is the only signal that a
   * subscription has gone dead. Closing feeds the reconnect path below.
   */
  const armWatchdog = (ws: WebSocket) => {
    if (idle !== undefined) clearTimeout(idle)
    idle = setTimeout(() => ws.close(), idleTimeoutMs)
  }

  const scheduleRetry = () => {
    if (stopped) return
    // Replaced, not stacked. `onclose` can fire more than once — the watchdog closes a
    // socket the service is also closing — and a stranded timer is a second connect
    // racing this one for the single `socket` slot below.
    if (retry !== undefined) clearTimeout(retry)
    attempt += 1
    // Capped at 30s, half of it jittered, so a service blip does not turn every open
    // tab into a synchronized retry storm.
    const ceiling = Math.min(30_000, 500 * 2 ** Math.min(attempt, 6))
    retry = setTimeout(() => void connect(), ceiling / 2 + Math.random() * (ceiling / 2))
  }

  const connect = async () => {
    if (stopped || socket !== null) return
    options.onStatus?.("connecting")
    let auth: { Authorization: string; host: string }
    try {
      auth = await authorization()
    } catch {
      scheduleRetry()
      return
    }
    // One socket at a time, checked again here and not only on entry: the assignment below
    // happens after an await, so a connect that started while this one was fetching a token
    // would otherwise overwrite `socket` and leave the loser live but unreachable —
    // `close()` reaps one handle and the shared watchdog handle strands the other's timer.
    // No retry from here: a socket already exists, which is what a retry would be for.
    if (stopped || socket !== null) return
    const ws = factory(`wss://${options.realtimeDomain}/event/realtime`, [
      "aws-appsync-event-ws",
      `header-${base64url(JSON.stringify(auth))}`,
    ])
    socket = ws
    ws.onopen = () => ws.send(JSON.stringify({ type: "connection_init" }))
    ws.onmessage = (frame: MessageEvent) => {
      // Armed before handling, and on every frame including `ka`: the watchdog measures
      // silence, not errors. Arming afterwards would re-arm on a socket that a
      // `subscribe_error` had just closed, stranding a timer for the whole idle window.
      armWatchdog(ws)
      handle(ws, auth, String(frame.data))
    }
    ws.onclose = () => {
      if (socket === ws) socket = null
      if (idle !== undefined) clearTimeout(idle)
      options.onStatus?.("closed")
      scheduleRetry()
    }
    // Armed before the ack, so a server that accepts the socket and then says nothing
    // is caught too.
    armWatchdog(ws)
  }

  void connect()

  return {
    close: () => {
      stopped = true
      if (retry !== undefined) clearTimeout(retry)
      if (idle !== undefined) clearTimeout(idle)
      const open = socket
      socket = null
      open?.close()
    },
  }
}
