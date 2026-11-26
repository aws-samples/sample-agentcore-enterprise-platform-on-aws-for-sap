# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""What happens to AG-UI events on their way out to the browser.

Three concerns, all about the wire rather than the agent:

* **Naming** — ``_event_name`` / ``_canonical_event`` read an event's type and dump it
  the way the encoder expects, so the rest of the runtime never has to care whether
  ``type`` is an enum or a string.
* **Redaction** — ``_sanitized_run_error`` stands between a raised exception and the
  chat bubble it would otherwise be rendered into verbatim.
* **Liveness** — ``_with_keepalive`` interleaves SSE heartbeat comments so a stream
  idling on a slow SAP call is not dropped by an intermediate hop.

``Request`` is imported only for a type annotation, so it stays under TYPE_CHECKING:
fastapi is not installed in the hermetic test environment, and these helpers are
worth testing there.
"""

from __future__ import annotations

import asyncio
import time

from ag_ui.core import EventType, RunErrorEvent


def _event_name(event) -> str:
    value = getattr(event, "type", "")
    return value.value if hasattr(value, "value") else str(value)


def _canonical_event(event) -> dict:
    return event.model_dump(mode="json", by_alias=True)


_KEEPALIVE_INTERVAL_SECONDS = 15.0

# RUN_ERROR codes this runtime constructs itself. Their messages are written for the
# user and are safe to forward; anything else came out of the adapter or the model
# and may carry internal detail.
_OWN_ERROR_CODES = frozenset(
    {
        "MAX_TURNS_REACHED",
        "MAX_TOKENS_REACHED",
        "RUN_BUDGET_EXCEEDED",
        "REPEATED_TOOL_ERRORS",
        "AGENT_STREAM_INCOMPLETE",
        "SOP_LOAD_ERROR",
        "ERP_AGENT_ERROR",
    }
)


def _sanitized_run_error(event, canonical: dict, *, run_id: str):
    """Replace an adapter-generated RUN_ERROR message with a fixed one.

    The browser renders ``RUN_ERROR.message`` directly into the chat. A failure
    raised inside the agent loop reaches here formatted by whichever library
    raised it, which can carry ARNs, endpoint hostnames, or SAP response
    fragments. Errors this runtime builds are already written for the user and
    pass through unchanged; everything else is replaced and the detail is logged
    server-side instead.

    ag-ui-strands 0.2.3 converts Strands' typed ``MaxTokensReachedException``
    into a generic ``STRANDS_ERROR``. Recognize only that pinned, fixed prefix so
    queued callers can stop safely without treating unrelated adapter failures as
    non-retryable.
    """
    code = canonical.get("code")
    if code in _OWN_ERROR_CODES:
        return event

    detail = canonical.get("message") or ""
    if code == "STRANDS_ERROR" and detail.startswith(
        "Model stopped generating due to maximum token limit."
    ):
        return RunErrorEvent(
            type=EventType.RUN_ERROR,
            message=(
                "The agent reached its configured output limit. "
                "Review the case state for actions completed before it stopped."
            ),
            code="MAX_TOKENS_REACHED",
        )

    print(f"[AG-UI] Suppressed adapter error detail for run {run_id}: {detail!r}")
    return RunErrorEvent(
        type=EventType.RUN_ERROR,
        message=(
            "The agent stopped on an internal error. "
            "Review the case state for actions completed before it stopped."
        ),
        code="AGENT_INTERNAL_ERROR",
    )


def _keepalive_frame(seq: int, started_at: float) -> str:
    """An SSE comment heartbeat.

    A comment is bytes on the wire that no conformant SSE client reads as an
    event, so it keeps the connection warm without adding anything the AG-UI
    reducer has to know about. The sequence and elapsed milliseconds make a
    disconnect correlatable to the heartbeat that should have followed.
    """
    elapsed_ms = int((time.monotonic() - started_at) * 1000)
    return f": keepalive {seq} {elapsed_ms}\n\n"


async def _with_keepalive(source, interval: float = _KEEPALIVE_INTERVAL_SECONDS):
    """Interleave heartbeat ticks into an AG-UI event stream.

    A single SAP OData call can leave the stream idle for minutes, and an
    intermediate hop will drop an idle connection — the failure this whole
    migration sits on top of. The pre-AG-UI entrypoint solved it with
    ``_stream_with_keepalive``; the adapter owns the event loop now, so the
    heartbeat has to be interleaved from outside it.

    Yields ``("event", event)`` for a source event and ``("keepalive", seq)``
    for a tick, so the caller encodes each appropriately.
    """
    queue: asyncio.Queue = asyncio.Queue()
    done = False

    async def _produce():
        nonlocal done
        try:
            async for event in source:
                await queue.put(("event", event))
        finally:
            # Signal completion even on failure; the exception stays on the task
            # and is re-raised by the consumer's `await producer` below.
            done = True
            await queue.put(None)

    async def _beat():
        seq = 0
        while not done:
            await asyncio.sleep(interval)
            if done:
                break
            seq += 1
            await queue.put(("keepalive", seq))

    producer = asyncio.create_task(_produce())
    heartbeat = asyncio.create_task(_beat())
    completed = False
    try:
        while True:
            item = await queue.get()
            if item is None:
                completed = True
                break
            yield item
    finally:
        heartbeat.cancel()
        if completed:
            # Re-raise a source failure so the caller's error handling still runs.
            await producer
        else:
            # Consumer closed early (client disconnected) — stop the source.
            producer.cancel()
