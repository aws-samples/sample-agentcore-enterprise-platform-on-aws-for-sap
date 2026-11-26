# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The run's AG-UI events, published to the AppSync Events channel its key names.

The key is the run's case id, or its session id when there is no case, so a case-less
conversation gets a channel of its own rather than none: `/runs/<short_id>/<case_id>`
or `/runs/<session_id>`, the grammar `run_events_auth/` authorizes subscribes against.

A second copy of the stream the response body already carries, so a browser that did
not start the run — or that arrived late — can still watch it token by token. Every
failure here is swallowed: losing the copy must never cost the run.

Transport is injected for the same reason utils/live_run.py takes its table as an
argument: basic_agent.py pulls in strands/mcp and is not importable in the hermetic
test env, so the pump is only testable if the network is a callable.
"""

import copy
import json
import os
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from urllib.request import Request, urlopen

from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.session import get_session

from .agent_metrics import emit_failure_metric

# The one metric every swallowed failure here counts against, `Kind` saying which one.
# Every failure below is a lost copy of the run's stream, and a lost copy looks exactly
# like a quiet run from every other angle: the run still succeeds, the trace still lands,
# and the only trace is a `[CHANNEL]` log line nobody greps until someone complains. The
# alarm in business_operations/alarms.py reads this name — keep them together.
PUBLISH_FAILURE_METRIC = "RunChannelPublishFailure"

# AppSync Events rejects a call carrying more than five events.
MAX_EVENTS_PER_CALL = 5

# Per-event ceiling, the event's own id included. A larger event is dropped silently
# by the service, so an oversized payload is trimmed here — where the trim can be
# logged — rather than vanishing.
MAX_EVENT_BYTES = 240 * 1024

# What `_fit` actually measures against, deliberately under the ceiling above. The id
# the service appends counts toward that ceiling, so an envelope sized to exactly 240 KB
# is over budget by the time the id lands — and an over-budget event is dropped by the
# service without a word. 1 KB covers the id and the keys wrapped around it.
_EVENT_BUDGET_BYTES = MAX_EVENT_BYTES - 1024

# Text deltas arrive per token. One call per token would spend the whole call budget
# animating text, so they are batched to this cadence. Every other event type goes out
# immediately: a tool call or a terminal event is what a reader is actually waiting for.
DELTA_FLUSH_SECONDS = 0.1

# Long enough for a signed POST plus the TLS handshake urllib pays on every publish — it
# opens a fresh connection each time — and short enough that a hung publish cannot
# outlive the segment it belongs to.
PUBLISH_TIMEOUT_SECONDS = 5

# Channel segments are alphanumeric-and-dash, up to 50 characters — AppSync's grammar,
# not ours, and narrower than the case-id contract in `case_key.py`, which permits `_`
# and any length. Both halves of `/{namespace}/{channel key}` are checked against it —
# the key being the case id or, with no case, the session id; a segment that does not fit
# names no channel, so no publisher is built for it.
_CHANNEL_SEGMENT = re.compile(r"^[A-Za-z0-9-]{1,50}$")

_THROTTLED_EVENT_TYPES = frozenset({"TEXT_MESSAGE_CONTENT"})

# The event keys big enough to be worth cutting. Same job as evidence.truncate_segment,
# different unit: that one budgets a stored segment, this one an event on the wire.
_TRUNCATABLE_KEYS = ("content", "delta", "message", "result")

# What is left of an over-budget string, before the marker.
_TRUNCATED_TO = 1024


def _fit(envelope: dict) -> dict | None:
    """`envelope` inside the per-event budget, or None when it cannot be made to fit."""
    if len(json.dumps(envelope).encode("utf-8")) <= _EVENT_BUDGET_BYTES:
        return envelope
    trimmed = copy.deepcopy(envelope)
    event = trimmed.get("event")
    if isinstance(event, dict):
        for key in _TRUNCATABLE_KEYS:
            value = event.get(key)
            if isinstance(value, str) and len(value) > _TRUNCATED_TO:
                event[key] = value[:_TRUNCATED_TO] + "…[truncated]"
    if len(json.dumps(trimmed).encode("utf-8")) <= _EVENT_BUDGET_BYTES:
        return trimmed
    return None


def build_publish_request(
    *,
    http_domain: str,
    channel: str,
    events: list[str],
    region: str,
    credentials,
) -> AWSRequest:
    """A SigV4-signed POST /event for `events` on `channel`.

    Separate from the send so the signing and the wire shape are testable without a
    network. AppSync Events has no boto3 client, hence the hand-rolled request.
    """
    request = AWSRequest(
        method="POST",
        url=f"https://{http_domain}/event",
        data=json.dumps({"channel": channel, "events": events}),
        headers={
            "Host": http_domain,
            "Content-Type": "application/json; charset=UTF-8",
            "Content-Encoding": "amz-1.0",
            "Accept": "application/json, text/javascript",
        },
    )
    SigV4Auth(credentials, "appsync", region).add_auth(request)
    return request


def sigv4_publisher(
    *, http_domain: str, region: str
) -> Callable[[str, list[str]], None]:
    """A publish callable that signs with this task's role and POSTs.

    Credentials are resolved per call rather than pinned at construction, so a
    container credential refresh is picked up mid-run.
    """
    session = get_session()

    def publish(channel: str, events: list[str]) -> None:
        credentials = session.get_credentials()
        if credentials is None:
            raise RuntimeError("no AWS credentials available")
        request = build_publish_request(
            http_domain=http_domain,
            channel=channel,
            events=events,
            region=region,
            credentials=credentials.get_frozen_credentials(),
        )
        # No caller can pick the scheme or the target: `https://` is a literal in
        # build_publish_request and the host is a CDK-supplied env var. The pragma has to
        # be a trailing comment on the flagged line — bandit matches it by line number, so
        # one sitting on the line above suppresses nothing, which was the original bug
        # here. Prose after the test id is fine; the sibling pragmas carry some.
        urlopen(  # nosec B310  # nosemgrep: dynamic-urllib-use-detected
            Request(
                request.url,
                # Already bytes: AWSRequest encodes the str it was given, and the signed
                # body must go out byte-for-byte or the signature stops matching.
                data=request.body,
                headers=dict(request.headers),
                method="POST",
            ),
            timeout=PUBLISH_TIMEOUT_SECONDS,
        ).close()

    return publish


class RunChannelPublisher:
    """One run's events, on the channel its case — or its session — names.

    Never raises. Publishing runs on a single worker thread so the event loop never waits
    on the network, and so the batches leave in the order they were built. They do not
    necessarily arrive that way: each batch is its own request, and AppSync Events promises
    delivery but not ordering. Which is what the envelope's `seq` is for — a subscriber puts
    them back in order (`RunFollower.accept`, packages/run-client/src/runFollower.ts).
    """

    def __init__(
        self,
        *,
        channel: str,
        run_id: str,
        publish: Callable[[str, list[str]], None],
        clock: Callable[[], float] = time.monotonic,
        flush_interval: float = DELTA_FLUSH_SECONDS,
    ):
        self._channel = channel
        # The channel's key is on every envelope: one wildcard subscription serves every
        # channel, so a subscriber needs the event itself to say which one it belongs to.
        # Named `_case_id` because that is the envelope field, and the browser keys its
        # slots `caseId ?? sessionId` — the same value under a case-shaped name.
        self._case_id = channel.rsplit("/", 1)[-1]
        self._run_id = run_id
        self._publish = publish
        self._clock = clock
        self._flush_interval = flush_interval
        self._pending: list[str] = []
        # The envelope's sequence number. One counter per publisher, and there is one
        # publisher per request, so the sequence is per run — which is the scope the browser
        # dedups and orders at, a number from one run meaning nothing to another. No lock:
        # `publish()` has exactly one caller, inside `basic_agent.py`'s `emit()`, reached
        # only from the event loop thread, so a lock here would be cargo cult.
        self._seq = 0
        self._last_flush: float | None = None
        self._closed = False
        self._worker = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="run-channel"
        )

    def publish(self, event: dict) -> None:
        """Queue one AG-UI event, flushing when the event or the clock says so."""
        if self._closed:
            return
        try:
            # Numbered before the fit and committed only after it. An event `_fit` rejects is
            # dropped here and never reaches a subscriber, so consuming a number for it would
            # leave the delivered sequence holed where nothing was actually lost — and a gap
            # check downstream could not tell that from a frame the transport ate.
            seq = self._seq + 1
            envelope = _fit(
                {
                    "caseId": self._case_id,
                    "runId": self._run_id,
                    "seq": seq,
                    "event": event,
                }
            )
            if envelope is None:
                event_type = event.get("type")
                print(f"[CHANNEL] event too large for {self._channel}: {event_type}")
                self._count_failure("TooLarge")
                return
            self._seq = seq
            self._pending.append(json.dumps(envelope))
            now = self._clock()
            window_open = (
                self._last_flush is None
                or now - self._last_flush >= self._flush_interval
            )
            batch_full = len(self._pending) >= MAX_EVENTS_PER_CALL
            throttled = event.get("type") in _THROTTLED_EVENT_TYPES
            if not throttled or window_open or batch_full:
                self._flush(now)
        # Distinct from the send's own failure below: this is an event that never got
        # onto the wire at all — an unserializable payload from the caller, say.
        except Exception as error:  # noqa: BLE001
            print(f"[CHANNEL] event not queued for {self._channel}: {error}")
            self._count_failure("NotQueued")

    def close(self) -> None:
        """Flush what is left, then stop accepting events."""
        if self._closed:
            return
        try:
            self._flush(self._clock())
        except Exception as error:  # noqa: BLE001
            print(f"[CHANNEL] final flush failed for {self._channel}: {error}")
            self._count_failure("FinalFlush")
        self._closed = True
        # Queued sends still run — shutdown only declines new work — so the last
        # batch is not thrown away by returning immediately. Ordered after the flush
        # above for the same reason.
        self._worker.shutdown(wait=False)

    def _flush(self, now: float) -> None:
        if not self._pending:
            return
        self._last_flush = now
        batch, self._pending = self._pending, []
        for start in range(0, len(batch), MAX_EVENTS_PER_CALL):
            self._worker.submit(self._send, batch[start : start + MAX_EVENTS_PER_CALL])

    def _send(self, events: list[str]) -> None:
        try:
            self._publish(self._channel, events)
        except Exception as error:  # noqa: BLE001
            print(f"[CHANNEL] publish failed for {self._channel}: {error}")
            self._count_failure("Publish")

    def _count_failure(self, kind: str) -> None:
        """Count one lost-events failure, on the worker rather than the caller's thread.

        The worker exists so the event loop never waits on the network, and PutMetricData
        is a network call like any other — `publish()` runs on that thread, and an
        oversized event can arrive per token. Nothing waits on the result.
        """
        try:
            self._worker.submit(emit_failure_metric, PUBLISH_FAILURE_METRIC, kind)
        except RuntimeError:
            # The worker is shut down, so this is a batch that failed after close() —
            # already off the event loop, and the end of a run is the last moment worth
            # dropping the count. emit_failure_metric swallows its own failures.
            emit_failure_metric(PUBLISH_FAILURE_METRIC, kind)


def run_channel_publisher(
    case_id: str, run_id: str, session_id: str = ""
) -> RunChannelPublisher | None:
    """A publisher for this run, or None when the channel is not configured.

    The channel is keyed by case id where there is one and by the conversation's
    session id where there is not — the browser keys its slots the same way, so both
    land on the channel it is already watching.

    None is the normal state for a deployment without the Event API, and for a run
    with neither key — neither is an error worth a log line per run. A segment the
    channel grammar rejects is the third case and not routine: it is a
    misconfiguration that would otherwise publish nothing, for every run on every
    case of that shape, in silence. So that one logs.
    """
    http_domain = os.environ.get("RUN_EVENTS_HTTP_DOMAIN")
    namespace = os.environ.get("RUN_EVENTS_NAMESPACE", "runs")
    key = case_id or session_id
    if not http_domain or not key:
        return None
    # `key` comes from data, `namespace` from configuration, and both are
    # interpolated into the channel below — so both are checked. Same message shape
    # as the signing failure further down, because the documented diagnostic is to
    # grep the bare `[CHANNEL]` prefix: a construction that returns None without a
    # line reads as a healthy channel.
    for label, segment in (("channel key", key), ("namespace", namespace)):
        if not _CHANNEL_SEGMENT.match(segment):
            print(
                f"[CHANNEL] no publisher for {key}: "
                f"{label} {segment!r} is not a legal channel segment"
            )
            return None
    region = (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-east-1"
    )
    # The construction is inside the guard, not just the signing: this is called on the
    # request path, so anything that escapes here costs the run the module exists not to
    # cost. Nothing in the constructor raises today — the executor spawns its thread on
    # first submit, not here — which is exactly why leaving it outside would read as
    # deliberate.
    # A case's channel also names its pack, by the short id every one of the pack's
    # case ids starts with: the subscribe check admits `/runs/<short>/*` to the pack's
    # users only. A session's channel names no pack and is held by its unguessable id.
    channel = (
        f"/{namespace}/{case_id.split('-', 1)[0]}/{key}"
        if case_id
        else f"/{namespace}/{key}"
    )
    try:
        publish = sigv4_publisher(http_domain=http_domain, region=region)
        return RunChannelPublisher(channel=channel, run_id=run_id, publish=publish)
    except Exception as error:  # noqa: BLE001
        print(f"[CHANNEL] no publisher for {key}: {error}")
        return None
