# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""business-operations agent: the run-event channel publisher."""

import importlib
import json
import sys
from pathlib import Path

import pytest
from botocore.credentials import Credentials

REPO = Path(__file__).resolve().parents[3]
UC = REPO / "use-cases" / "business-operations"
AGENT = UC / "agent"


def _agent_utils(*names):
    """Import agent/utils modules without leaving `utils` bound in sys.modules.

    The agent imports its helpers as the package `utils`, and scripts/ has a
    `utils` module of its own that other tests import, so the binding is held
    only for the import.
    """

    def ours():
        return [k for k in sys.modules if k == "utils" or k.startswith("utils.")]

    held = {k: sys.modules.pop(k) for k in ours()}
    sys.path.insert(0, str(AGENT))
    try:
        return [importlib.import_module(f"utils.{n}") for n in names]
    finally:
        sys.path.remove(str(AGENT))
        for k in ours():
            del sys.modules[k]
        sys.modules.update(held)


(run_channel,) = _agent_utils("run_channel")


# ── What the run channel owes a subscriber, and what it must never cost the run. ──


class _FakeTransport:
    """Records publish calls. `fail` makes every call raise, like a throttled API."""

    def __init__(self, fail: bool = False):
        self.calls: list[tuple[str, list[str]]] = []
        self.fail = fail

    def __call__(self, channel: str, events: list[str]) -> None:
        self.calls.append((channel, events))
        if self.fail:
            raise RuntimeError("publish rejected")


class _FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _InlineWorker:
    """The executor's interface, on the calling thread.

    A real worker makes every assertion below racy. Standing in for it rather than
    bypassing it keeps `submit` and `shutdown` on the path every test drives — the
    production calls, not a branch that only exists for the tests.
    """

    def submit(self, function, *args):
        function(*args)

    def shutdown(self, wait: bool = False) -> None:
        pass


class _ShutDownWorker:
    """A worker that has been shut down: the real executor refuses new work outright."""

    def submit(self, function, *args):
        raise RuntimeError("cannot schedule new futures after shutdown")

    def shutdown(self, wait: bool = False) -> None:
        pass


def _publisher(transport, clock):
    publisher = run_channel.RunChannelPublisher(
        channel="/runs/5100001506-2026",
        run_id="run-1",
        publish=transport,
        clock=clock,
    )
    # Replaced unused: the executor spawns its thread on first submit, so the one built
    # in the constructor here never starts.
    publisher._worker = _InlineWorker()
    return publisher


def _envelopes(transport) -> list[dict]:
    return [json.loads(payload) for _, events in transport.calls for payload in events]


@pytest.fixture(autouse=True)
def counted(monkeypatch) -> list[tuple[str, str]]:
    """Every failure metric the module emits, captured rather than sent.

    Autouse rather than opt-in: `_InlineWorker` runs a submitted call on this thread, so
    any test that drives a swallowed failure would otherwise resolve real credentials and
    make a real PutMetricData call.
    """
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        run_channel,
        "emit_failure_metric",
        lambda name, kind: calls.append((name, kind)),
    )
    return calls


def test_an_event_reaches_the_channel_named_with_its_case_and_run():
    transport, clock = _FakeTransport(), _FakeClock()
    _publisher(transport, clock).publish({"type": "RUN_STARTED", "runId": "run-1"})

    assert transport.calls[0][0] == "/runs/5100001506-2026"
    assert _envelopes(transport) == [
        {
            "caseId": "5100001506-2026",
            "runId": "run-1",
            "seq": 1,
            "event": {"type": "RUN_STARTED", "runId": "run-1"},
        }
    ]


def test_each_envelope_carries_the_next_number_in_the_run():
    # The identity the transport does not provide. AppSync Events promises neither
    # at-most-once delivery nor ordering, and the browser's `appendText` is a plain `+=` —
    # so without a number on the envelope a redelivered delta is appended twice and the
    # rendered reply ends in an interior slice of its own last sentence. One counter per
    # publisher, and there is one publisher per run, so this is the run-scoped sequence the
    # browser can dedup against.
    transport, clock = _FakeTransport(), _FakeClock()
    publisher = _publisher(transport, clock)

    publisher.publish({"type": "RUN_STARTED"})
    publisher.publish({"type": "TOOL_CALL_START", "toolCallId": "t1"})
    publisher.publish({"type": "RUN_FINISHED"})

    assert [envelope["seq"] for envelope in _envelopes(transport)] == [1, 2, 3]


def test_a_dropped_event_does_not_consume_a_sequence_number():
    # An event `_fit` cannot cut down is never sent, so numbering it would leave the
    # delivered sequence with a hole where nothing was lost — and a gap check downstream
    # could not tell that from a frame the transport ate.
    transport, clock = _FakeTransport(), _FakeClock()
    publisher = _publisher(transport, clock)

    publisher.publish({"type": "RUN_STARTED"})
    # `snapshot` is not a truncatable key, so this one can only be dropped.
    publisher.publish(
        {"type": "STATE_SNAPSHOT", "snapshot": "x" * (run_channel.MAX_EVENT_BYTES + 1)}
    )
    publisher.publish({"type": "RUN_FINISHED"})

    assert [envelope["seq"] for envelope in _envelopes(transport)] == [1, 2]


def test_the_sequence_survives_a_flush_that_spans_several_calls():
    # Order on the wire comes from the single worker thread, and `seq` is assigned at
    # publish() time — so the two must agree even where one run's events are split across
    # four signed POSTs. A subscriber that drops `seq <= last` reads a transposition as a
    # lost delta, so this is the property that keeps that from happening on a healthy run.
    transport, clock = _FakeTransport(), _FakeClock()
    publisher = _publisher(transport, clock)
    for index in range(12):
        publisher.publish({"type": "TEXT_MESSAGE_CONTENT", "delta": str(index)})
    publisher.close()

    assert len(transport.calls) > 1, "one call proves nothing about order across calls"
    assert [envelope["seq"] for envelope in _envelopes(transport)] == list(range(1, 13))
    assert [envelope["event"]["delta"] for envelope in _envelopes(transport)] == [
        str(index) for index in range(12)
    ]


def test_text_deltas_are_batched_and_everything_else_goes_out_at_once():
    transport, clock = _FakeTransport(), _FakeClock()
    publisher = _publisher(transport, clock)

    publisher.publish({"type": "TEXT_MESSAGE_CONTENT", "delta": "all "})
    publisher.publish({"type": "TEXT_MESSAGE_CONTENT", "delta": "three "})
    # First delta flushes (no window has opened yet); the second waits.
    assert len(transport.calls) == 1

    publisher.publish({"type": "TOOL_CALL_START", "toolCallId": "t1"})
    # A tool call is what a reader is waiting for, so it takes the pending delta with it.
    assert len(transport.calls) == 2
    assert [envelope["event"]["type"] for envelope in _envelopes(transport)] == [
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_CONTENT",
        "TOOL_CALL_START",
    ]


def test_a_held_delta_goes_out_once_the_flush_window_reopens():
    # The clause the whole cadence rests on. Without it a delta waits for a full batch or
    # for close(), and a reader sees text arrive in five-token clumps instead of streaming.
    transport, clock = _FakeTransport(), _FakeClock()
    publisher = _publisher(transport, clock)
    publisher.publish({"type": "TEXT_MESSAGE_CONTENT", "delta": "first"})
    publisher.publish({"type": "TEXT_MESSAGE_CONTENT", "delta": "held"})
    assert len(transport.calls) == 1

    # Past DELTA_FLUSH_SECONDS. Deliberately a literal: scaling it off the constant would
    # keep passing if the cadence were widened to something a reader would notice.
    clock.now = 0.2
    publisher.publish({"type": "TEXT_MESSAGE_CONTENT", "delta": "third"})

    assert len(transport.calls) == 2
    assert [envelope["event"]["delta"] for envelope in _envelopes(transport)] == [
        "first",
        "held",
        "third",
    ]


def test_the_budgets_match_what_the_service_enforces():
    # The two ceilings below belong to AppSync Events, not to us: it rejects a
    # `POST /event` carrying more than five events, and drops an event over 240 KB — the
    # id it appends included — without an error anywhere. Every other assertion that
    # mentions either one scales its own fixture off it, so both are otherwise only ever
    # compared to themselves: raising `MAX_EVENTS_PER_CALL` to 6 leaves the batching test
    # green while the service refuses every call, and widening `MAX_EVENT_BYTES` leaves
    # the truncation tests green while every large tool result is silently dropped.
    assert (run_channel.MAX_EVENTS_PER_CALL, run_channel.MAX_EVENT_BYTES) == (
        5,
        240 * 1024,
    )
    # Ours rather than the service's, and unpinned for the same reason — the one test that
    # asserts the timeout reads it off the constant it is checking.
    assert run_channel.PUBLISH_TIMEOUT_SECONDS == 5


def test_no_call_carries_more_than_the_api_accepts():
    transport, clock = _FakeTransport(), _FakeClock()
    publisher = _publisher(transport, clock)
    for index in range(12):
        publisher.publish({"type": "TEXT_MESSAGE_CONTENT", "delta": str(index)})
    publisher.close()

    assert transport.calls, "nothing was published"
    assert all(
        len(events) <= run_channel.MAX_EVENTS_PER_CALL for _, events in transport.calls
    )
    assert len(_envelopes(transport)) == 12


def test_close_flushes_the_last_delta():
    transport, clock = _FakeTransport(), _FakeClock()
    publisher = _publisher(transport, clock)
    publisher.publish({"type": "TEXT_MESSAGE_CONTENT", "delta": "first"})
    publisher.publish({"type": "TEXT_MESSAGE_CONTENT", "delta": "last"})
    assert len(_envelopes(transport)) == 1

    publisher.close()
    assert [envelope["event"]["delta"] for envelope in _envelopes(transport)] == [
        "first",
        "last",
    ]


def test_publishing_after_close_is_a_no_op():
    # Pins the `_closed` guard only: the worker here is the inline stand-in, so nothing
    # about the real shutdown is checked. test_the_real_worker_sends_and_shuts_down does.
    transport, clock = _FakeTransport(), _FakeClock()
    publisher = _publisher(transport, clock)
    publisher.close()
    publisher.publish({"type": "RUN_FINISHED"})

    assert transport.calls == []


def test_the_real_worker_sends_and_shuts_down():
    # The only test on the production concurrency path. Every other test nulls the worker
    # out to keep assertions deterministic, so submit() and shutdown() run nowhere else.
    transport = _FakeTransport()
    publisher = run_channel.RunChannelPublisher(
        channel="/runs/5100001506-2026",
        run_id="run-1",
        publish=transport,
        clock=_FakeClock(),
    )
    worker = publisher._worker
    publisher.publish({"type": "RUN_STARTED"})
    publisher.close()
    # close() already called shutdown(wait=False); shutdown is idempotent, so this just
    # joins the queued send rather than reaching past the interface to do it.
    worker.shutdown(wait=True)

    assert _envelopes(transport) == [
        {
            "caseId": "5100001506-2026",
            "runId": "run-1",
            "seq": 1,
            "event": {"type": "RUN_STARTED"},
        }
    ]


def test_an_oversized_event_is_trimmed_rather_than_silently_dropped():
    transport, clock = _FakeTransport(), _FakeClock()
    publisher = _publisher(transport, clock)
    publisher.publish(
        {"type": "TOOL_CALL_RESULT", "content": "x" * (run_channel.MAX_EVENT_BYTES + 1)}
    )

    published = json.dumps(_envelopes(transport)[0])
    assert len(published.encode("utf-8")) <= run_channel.MAX_EVENT_BYTES
    assert _envelopes(transport)[0]["event"]["content"].endswith("[truncated]")


def test_an_event_under_the_ceiling_but_over_budget_is_still_cut():
    # The service appends an id and counts it against the ceiling, so the budget sits
    # below it. An envelope that fit the documented 240 KB exactly would arrive over it
    # and be dropped without a word — the one outcome _fit exists to prevent.
    transport, clock = _FakeTransport(), _FakeClock()
    publisher = _publisher(transport, clock)
    publisher.publish(
        {
            "type": "TOOL_CALL_RESULT",
            "content": "x" * (run_channel.MAX_EVENT_BYTES - 512),
        }
    )

    envelope = _envelopes(transport)[0]
    assert envelope["event"]["content"].endswith("[truncated]")
    assert len(json.dumps(envelope).encode("utf-8")) <= run_channel._EVENT_BUDGET_BYTES


def test_an_event_that_cannot_be_trimmed_is_dropped_and_said_so(capsys):
    # `snapshot` is not a truncatable key, so this one cannot be cut down to size. The
    # ceiling exists to make that visible rather than let the service swallow it.
    transport, clock = _FakeTransport(), _FakeClock()
    publisher = _publisher(transport, clock)
    publisher.publish(
        {"type": "STATE_SNAPSHOT", "snapshot": "x" * (run_channel.MAX_EVENT_BYTES + 1)}
    )

    assert transport.calls == []
    assert "[CHANNEL] event too large" in capsys.readouterr().out


def test_a_failed_publish_does_not_end_the_run(capsys):
    transport, clock = _FakeTransport(fail=True), _FakeClock()
    publisher = _publisher(transport, clock)
    publisher.publish({"type": "RUN_STARTED"})
    publisher.close()  # must not raise either

    # The log is the whole diagnostic surface of a module that swallows everything, so a
    # silent swallow is a defect. This is what a first live deploy greps for.
    assert capsys.readouterr().out.count("[CHANNEL] publish failed") == 1


def test_every_swallowed_failure_is_counted(counted):
    # A log line is only found by someone already looking. Nothing else reports these at
    # all: the run succeeds either way, so there is no failed invocation and no Errors
    # datapoint, and the browser cannot tell a dead channel from a quiet one.
    transport, clock = _FakeTransport(fail=True), _FakeClock()
    publisher = _publisher(transport, clock)
    publisher.publish(
        {"type": "STATE_SNAPSHOT", "snapshot": "x" * (run_channel.MAX_EVENT_BYTES + 1)}
    )
    publisher.publish({"type": "RUN_STARTED"})
    # Unserializable, so it fails before it is ever queued — a different loss from a send
    # that was attempted and refused, which is what the Kind dimension is for.
    publisher.publish({"type": "TOOL_CALL_RESULT", "result": object()})

    assert counted == [
        (run_channel.PUBLISH_FAILURE_METRIC, "TooLarge"),
        (run_channel.PUBLISH_FAILURE_METRIC, "Publish"),
        (run_channel.PUBLISH_FAILURE_METRIC, "NotQueued"),
    ]


def test_a_batch_that_fails_after_close_is_still_counted(counted):
    # close() shuts the worker down while queued batches are still in flight, so the last
    # send of a run reports itself from a worker that no longer accepts work. Dropping the
    # count there would lose exactly the failures a whole run's copy died on.
    publisher = _publisher(_FakeTransport(), _FakeClock())
    publisher._worker = _ShutDownWorker()
    publisher._count_failure("Publish")

    assert counted == [(run_channel.PUBLISH_FAILURE_METRIC, "Publish")]


def test_the_signed_request_is_shaped_the_way_the_api_expects():
    request = run_channel.build_publish_request(
        http_domain="abc123.appsync-api.us-east-1.amazonaws.com",
        channel="/runs/5100001506-2026",
        events=['{"a":1}'],
        region="us-east-1",
        credentials=Credentials("AKIAEXAMPLE", "secret").get_frozen_credentials(),
    )

    assert request.url == "https://abc123.appsync-api.us-east-1.amazonaws.com/event"
    assert json.loads(request.body) == {
        "channel": "/runs/5100001506-2026",
        "events": ['{"a":1}'],
    }
    # SigV4 for the appsync service, or the API answers 403.
    assert "Credential=AKIAEXAMPLE/" in request.headers["Authorization"]
    assert "/us-east-1/appsync/aws4_request" in request.headers["Authorization"]
    assert request.headers["Content-Encoding"] == "amz-1.0"


class _FakeResponse:
    def close(self) -> None:
        pass


def test_the_signed_body_reaches_the_wire(monkeypatch):
    # The one path the injected transport cannot cover. AWSRequest.body is already
    # bytes, and _send swallows whatever the send raises, so re-encoding it here would
    # show up only as a channel that never carries anything.
    sent = []

    def fake_urlopen(request, timeout=None):
        sent.append((request, timeout))
        return _FakeResponse()

    monkeypatch.setattr(run_channel, "urlopen", fake_urlopen)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")

    run_channel.sigv4_publisher(
        http_domain="abc123.appsync-api.us-east-1.amazonaws.com", region="us-east-1"
    )("/runs/5100001506-2026", ['{"a":1}'])

    request, timeout = sent[0]
    assert json.loads(request.data) == {
        "channel": "/runs/5100001506-2026",
        "events": ['{"a":1}'],
    }
    assert "Credential=AKIAEXAMPLE/" in request.get_header("Authorization")
    # An unbounded publish holds a worker thread past process exit.
    assert timeout == run_channel.PUBLISH_TIMEOUT_SECONDS


def test_the_channel_is_the_namespace_and_then_the_case(monkeypatch):
    # The only place the channel string is built. Every other test here is handed one
    # already assembled, so dropping the namespace segment — publishing to
    # `/5100001506-2026`, outside the one path the agent's IAM grant covers — moves nothing
    # and shows up only as a 403 the publisher swallows. The default is the other half:
    # `runs` is the literal the CDK construct names its channel namespace with (pinned on
    # that side too), so the two must keep saying the same word for a deployment that never
    # sets the variable to publish anywhere real.
    monkeypatch.setenv(
        "RUN_EVENTS_HTTP_DOMAIN", "abc123.appsync-api.us-east-1.amazonaws.com"
    )
    monkeypatch.delenv("RUN_EVENTS_NAMESPACE", raising=False)
    publisher = run_channel.run_channel_publisher(
        "ap-k7m2q9x4rt6bw8ncf3jhd5gzpv", "run-1"
    )

    assert publisher is not None
    # The pack's short id is the middle segment: the subscribe check scopes by it.
    assert publisher._channel == "/runs/ap/ap-k7m2q9x4rt6bw8ncf3jhd5gzpv"
    assert publisher._case_id == "ap-k7m2q9x4rt6bw8ncf3jhd5gzpv"
    publisher.close()  # the real worker thread, not this file's usual inline one


def test_no_publisher_without_a_configured_domain(monkeypatch):
    monkeypatch.delenv("RUN_EVENTS_HTTP_DOMAIN", raising=False)
    assert run_channel.run_channel_publisher("5100001506-2026", "run-1") is None


def test_a_case_less_run_publishes_on_its_session_id(monkeypatch):
    monkeypatch.setenv("RUN_EVENTS_HTTP_DOMAIN", "events.test")
    session_id = "chat-" + "a" * 31

    publisher = run_channel.run_channel_publisher("", "run-1", session_id)

    assert publisher is not None
    assert publisher._channel == f"/runs/{session_id}"
    # The browser matches envelopes on caseId, and for this conversation the slot key
    # *is* the session id.
    assert publisher._case_id == session_id


def test_a_case_id_still_wins_over_the_session_id(monkeypatch):
    monkeypatch.setenv("RUN_EVENTS_HTTP_DOMAIN", "events.test")

    publisher = run_channel.run_channel_publisher(
        "ap-k7m2q9x4rt6bw8ncf3jhd5gzpv", "run-1", "chat-" + "a" * 31
    )

    assert publisher._channel == "/runs/ap/ap-k7m2q9x4rt6bw8ncf3jhd5gzpv"


def test_no_key_at_all_still_names_no_channel(monkeypatch):
    # Neither a case nor a session names a channel, so there is nothing to publish to.
    monkeypatch.setenv(
        "RUN_EVENTS_HTTP_DOMAIN", "abc123.appsync-api.us-east-1.amazonaws.com"
    )
    assert run_channel.run_channel_publisher("", "run-1", "") is None


@pytest.mark.parametrize("case_id", ["not a case id", "5100001506-2026/extra"])
def test_no_publisher_for_a_case_id_the_channel_grammar_rejects(
    monkeypatch, capsys, case_id
):
    # A channel segment is alphanumeric-and-dash only. An unusable channel is a
    # publisher that would 4xx on every event, so it is never built.
    monkeypatch.setenv(
        "RUN_EVENTS_HTTP_DOMAIN", "abc123.appsync-api.us-east-1.amazonaws.com"
    )
    assert run_channel.run_channel_publisher(case_id, "run-1") is None
    # And it says so. A silent decline here is zero events for every run on every
    # case of that shape, while the SSE path keeps working — so the documented
    # diagnostic (grep the bare `[CHANNEL]` prefix) reads a dead channel as healthy.
    assert "[CHANNEL] no publisher for" in capsys.readouterr().out


def test_no_publisher_for_a_namespace_the_channel_grammar_rejects(monkeypatch, capsys):
    # The half that comes from configuration rather than from data. It is
    # interpolated into the channel just the same, so `runs/extra` names a path the
    # API rejects on every publish.
    monkeypatch.setenv(
        "RUN_EVENTS_HTTP_DOMAIN", "abc123.appsync-api.us-east-1.amazonaws.com"
    )
    monkeypatch.setenv("RUN_EVENTS_NAMESPACE", "runs/extra")
    assert run_channel.run_channel_publisher("5100001506-2026", "run-1") is None
    assert "namespace" in capsys.readouterr().out


def test_the_two_routine_declines_stay_silent(monkeypatch, capsys):
    # A deployment without the Event API, and a run with neither key to name a channel
    # with. Both are normal, once per run — logging them would train the operator to
    # filter out the prefix the misconfiguration above is diagnosed with.
    monkeypatch.delenv("RUN_EVENTS_HTTP_DOMAIN", raising=False)
    assert run_channel.run_channel_publisher("5100001506-2026", "run-1") is None
    monkeypatch.setenv(
        "RUN_EVENTS_HTTP_DOMAIN", "abc123.appsync-api.us-east-1.amazonaws.com"
    )
    assert run_channel.run_channel_publisher("", "run-1") is None
    assert capsys.readouterr().out == ""
