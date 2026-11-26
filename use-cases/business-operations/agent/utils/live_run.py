# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The in-flight shape of a run, written onto the case item while it streams.

Stdlib plus an injected DynamoDB table, for the same reason utils/evidence.py is
plain dicts: basic_agent.py pulls in strands/mcp and is not importable in the
hermetic test env, so the snapshot is a function over plain dicts and the table is
handed in by the caller.

`agent_traces` is only written when a run ends. Until then a tab that opens the case
— a reload, a second operator, the same operator coming back — has nothing to show,
because the SSE body of an InvokeAgentRuntime call has exactly one consumer. This
attribute is that missing catch-up state.
"""

from __future__ import annotations

import copy
import time
from collections.abc import Callable
from datetime import datetime, timezone

from utils.contracts import (
    LIVE_RUN_STALE_SECONDS,  # noqa: F401 - re-exported; the spec test reads it here
)
from utils.evidence import cap_traces, truncate_segment

# Item-budget backstop. agent_traces already shares the case item's 400 KB, so the
# live copy is capped rather than allowed to grow with the run.
MAX_LIVE_SEGMENTS = 40

# Wall-clock throttle. A fast token stream must not become one write per token, and a
# slow one must still refresh often enough to look alive.
LIVE_RUN_INTERVAL_SECONDS = 2.0

# LIVE_RUN_STALE_SECONDS: 30 missed writes. Past this the container is gone, so a
# reader stops waiting on the run. The schema states it (`LiveRun.x-stale-after-seconds`)
# and every reader takes it from the generated contracts.


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def live_run_snapshot(
    *,
    run_id: str,
    trigger: str,
    prompt: str,
    started_at: str,
    updated_at: str,
    segments: list,
) -> dict:
    """Build the `live_run` attribute for a run that is still streaming.

    Deep-copies before truncating. `truncate_segment` cuts tool_result/tool_input in
    place and flags the evidence; the segments passed here are the same objects the
    finished trace is written from, so truncating them would shrink the stored trace
    to the live preview.
    """
    kept, dropped = cap_traces(segments, MAX_LIVE_SEGMENTS)
    snapshot = {
        "run_id": run_id,
        "trigger": trigger,
        "prompt": prompt,
        "started_at": started_at,
        "updated_at": updated_at,
        "segments": [truncate_segment(copy.deepcopy(s)) for s in kept],
    }
    if dropped:
        snapshot["segments_dropped"] = dropped
    return snapshot


class LiveRunWriter:
    """One run's `live_run` attribute, written at most once per interval.

    Called from the streaming loop on every event and drops the ones that arrive
    inside the interval, so the caller does not have to know the cadence.

    Never raises. A DynamoDB hiccup must not end a run that is otherwise fine, which
    is what makes the attribute best-effort — and what lets a reader treat a stale
    `updated_at` as the only interruption signal it needs.
    """

    def __init__(
        self,
        *,
        table,
        case_key: dict,
        run_id: str,
        trigger: str,
        prompt: str,
        clock: Callable[[], float] = time.monotonic,
        interval: float = LIVE_RUN_INTERVAL_SECONDS,
        now_iso: Callable[[], str] = _now_iso,
        mask: Callable[[dict], dict] | None = None,
    ):
        self._table = table
        self._case_key = case_key
        self._run_id = run_id
        self._trigger = trigger
        self._prompt = prompt
        self._clock = clock
        self._interval = interval
        self._now_iso = now_iso
        # The persistence guardrail's mask, when the stack has one: the snapshot holds
        # the prompt and the tools' inputs and results, as the finished trace does.
        self._mask = mask
        self._started_at = now_iso()
        self._last_write: float | None = None

    def maybe_write(self, segments: list) -> None:
        """Publish the run's current shape, unless the last write was too recent."""
        if not segments:
            return
        now = self._clock()
        if self._last_write is not None and now - self._last_write < self._interval:
            return
        # Set before the attempt, not after: a failed write consumes its window rather
        # than turning a failing table into one write per event.
        self._last_write = now
        try:
            snapshot = live_run_snapshot(
                run_id=self._run_id,
                trigger=self._trigger,
                prompt=self._prompt,
                started_at=self._started_at,
                updated_at=self._now_iso(),
                segments=segments,
            )
            if self._mask is not None:
                snapshot = self._mask(snapshot)
            self._table.update_item(
                Key=self._case_key,
                UpdateExpression="SET live_run = :live",
                ExpressionAttributeValues={":live": snapshot},
                ConditionExpression="attribute_exists(case_id)",
            )
        except Exception as error:  # noqa: BLE001
            print(f"[LIVE] live_run write failed for {self._case_key}: {error}")
