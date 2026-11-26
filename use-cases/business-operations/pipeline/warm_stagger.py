# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Enqueue a burst of cases so the first run of each pack warms the prompt cache.

Amazon Bedrock caches a run's shared prefix (tool schemas plus system prompt, ~20k tokens)
per pack, and a cache write costs 12.5-25x a cache read. A burst enqueued at once
starts every run's first model call before any of them has written that prefix, so
each one writes it. Sending one case per pack, waiting, then sending the rest lets
the rest read what the first wrote.

The wait has to be here, in the producer: the agent queue is FIFO, and FIFO queues
refuse per-message `DelaySeconds`. Callers are `sap_poller` and
`batch_runner`, never `erp_events`, which answers SAP synchronously
behind API Gateway's 29 s limit, where a sleep would read as a timeout and make SAP
redeliver.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Generic, TypeVar

T = TypeVar("T")

# Left for the caller's own work after the wait, so the wait never runs a Lambda
# into its timeout with the rest of the burst still unsent.
TIMEOUT_MARGIN_SECONDS = 30


class Stagger(Generic[T]):
    """`offer` sends each pack's first item at once and holds the rest; `flush` waits
    out what is left of `warm_delay` since the first send, then sends the rest.

    The first item goes immediately, not at flush: a case created but never enqueued
    sits in `detected`, and a later poll's conditional put skips it — nothing on a
    default deploy would enqueue it again. `send` returns True only when it actually
    enqueued; in the `manual` trigger mode it sends nothing, so there is nothing to warm and
    no wait. One failed send is logged and does not stop the rest.
    """

    def __init__(
        self, pack_of: Callable[[T], str], send: Callable[[T], bool], warm_delay: float
    ):
        self._pack_of = pack_of
        self._send_fn = send
        self._warm_delay = warm_delay
        self._warmed: set[str] = set()
        self._rest: list[T] = []
        self._first_at: float | None = None
        self.sent = 0

    def _send(self, item: T) -> bool:
        try:
            if self._send_fn(item):
                self.sent += 1
                return True
        except Exception as e:  # noqa: BLE001 — one case, not the burst
            # The pack, not the item: an item can carry SAP attribute values.
            print(
                f"Enqueue failed for a {self._pack_of(item)} case: {type(e).__name__}: {e}"
            )
        return False

    def offer(self, item: T) -> None:
        pack = self._pack_of(item)
        if pack in self._warmed:
            self._rest.append(item)
            return
        self._warmed.add(pack)
        if self._send(item) and self._first_at is None:
            self._first_at = time.monotonic()

    def flush(self, remaining_seconds: float | None = None) -> int:
        """Send what `offer` held. Returns the total sent. `remaining_seconds` is the
        Lambda's time left; the wait is capped to leave TIMEOUT_MARGIN_SECONDS of it."""
        if self._rest and self._first_at is not None:
            wait = self._warm_delay - (time.monotonic() - self._first_at)
            if remaining_seconds is not None:
                wait = min(wait, remaining_seconds - TIMEOUT_MARGIN_SECONDS)
            if wait > 0:
                time.sleep(wait)
        for item in self._rest:
            self._send(item)
        self._rest = []
        return self.sent


def remaining_seconds(context) -> float | None:
    """A Lambda context's time left, or None outside Lambda."""
    return context.get_remaining_time_in_millis() / 1000 if context else None
