# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The stream heartbeat that keeps an idle AG-UI connection from being dropped.

A single SAP OData call can leave the stream silent for minutes, which is the
failure this whole initiative sits on top of. These tests pin the interleaving,
the cleanup, and that a source failure still reaches the caller.

The helpers live in `utils.stream`, which imports only asyncio, time, and ag_ui —
so these tests exercise the real functions. Async coroutines are driven with
asyncio.run.
"""

import asyncio
import time

import pytest
from utils.stream import (
    _KEEPALIVE_INTERVAL_SECONDS as _INTERVAL,
)
from utils.stream import (
    _keepalive_frame,
    _with_keepalive,
)


def _run(coro):
    return asyncio.run(coro)


async def _agen(items):
    for item in items:
        yield item


async def _collect(source):
    return [item async for item in source]


def test_default_interval_keeps_gaps_well_under_a_minute():
    # Idle intermediaries drop connections on the order of a minute; the heartbeat
    # has to be comfortably more frequent than that.
    assert 0 < _INTERVAL <= 30


def test_frame_is_an_sse_comment_not_an_event():
    # A comment keeps bytes on the wire without adding anything an AG-UI client
    # would parse as an event.
    frame = _keepalive_frame(3, time.monotonic())

    assert frame.startswith(": ")
    assert "data:" not in frame
    assert frame.endswith("\n\n")
    parts = frame.strip().split()
    assert parts[0] == ":"
    assert parts[1] == "keepalive"
    assert parts[2] == "3"
    assert parts[3].isdigit(), "elapsed milliseconds should be present for correlation"


def test_source_events_pass_through_untouched_and_in_order():
    got = _run(_collect(_with_keepalive(_agen(["a", "b", "c"]), interval=60)))

    assert got == [("event", "a"), ("event", "b"), ("event", "c")]


def test_heartbeats_are_interleaved_while_the_source_is_idle():
    # The point of the change: a silent source still produces bytes.
    async def _slow():
        await asyncio.sleep(0.25)
        yield "late"

    got = _run(_collect(_with_keepalive(_slow(), interval=0.05)))
    kinds = [kind for kind, _ in got]

    assert "keepalive" in kinds, "an idle source produced no heartbeat"
    assert kinds[-1] == "event", "the source event must still arrive last"


def test_heartbeat_sequence_increments_from_one():
    async def _slow():
        await asyncio.sleep(0.22)
        yield "done"

    got = _run(_collect(_with_keepalive(_slow(), interval=0.05)))
    seqs = [payload for kind, payload in got if kind == "keepalive"]

    assert seqs, "expected at least one heartbeat"
    assert seqs[0] == 1
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs), "sequence numbers must be unique"


def test_a_source_failure_still_reaches_the_caller():
    # The heartbeat must not swallow the error the caller's handling depends on.
    async def _boom():
        yield "first"
        raise RuntimeError("source exploded")

    seen = []

    async def _drain():
        async for item in _with_keepalive(_boom(), interval=60):
            seen.append(item)

    with pytest.raises(RuntimeError, match="source exploded"):
        _run(_drain())

    assert seen == [("event", "first")]


def test_closing_early_cancels_the_heartbeat_and_the_source():
    # A client disconnect must not leave the beat or the agent loop running.
    async def _scenario():
        started = asyncio.Event()

        async def _endless():
            started.set()
            while True:
                await asyncio.sleep(0.01)
                yield "tick"

        stream = _with_keepalive(_endless(), interval=0.01)
        async for _ in stream:
            break
        await stream.aclose()
        await asyncio.sleep(0.05)

        leaked = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        return started.is_set(), [t for t in leaked if not t.done()]

    source_started, leaked = _run(_scenario())

    assert source_started
    assert leaked == [], f"tasks outlived the closed stream: {leaked}"
