# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""business-operations agent: the live-run snapshot and how the stream loop writes it."""

import importlib
import inspect
import re
import sys
from pathlib import Path

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


evidence, live_run, case_persistence = _agent_utils(
    "evidence", "live_run", "case_persistence"
)
TOOL_RESULT_MAX_BYTES = evidence.TOOL_RESULT_MAX_BYTES
LIVE_RUN_INTERVAL_SECONDS = live_run.LIVE_RUN_INTERVAL_SECONDS
LIVE_RUN_STALE_SECONDS = live_run.LIVE_RUN_STALE_SECONDS
MAX_LIVE_SEGMENTS = live_run.MAX_LIVE_SEGMENTS
LiveRunWriter = live_run.LiveRunWriter
live_run_snapshot = live_run.live_run_snapshot
_flag_case_for_review = case_persistence._flag_case_for_review
_live_run_writer = case_persistence._live_run_writer
_save_trace_to_ddb = case_persistence._save_trace_to_ddb


# ── The live-run snapshot's budgets and the writer's throttle. ──


STARTED = "2026-08-19T09:00:00+00:00"
UPDATED = "2026-08-19T09:00:02+00:00"


def _snapshot(segments: list) -> dict:
    return live_run_snapshot(
        run_id="run-1",
        trigger="manual",
        prompt="Process case 5100001692-2026",
        started_at=STARTED,
        updated_at=UPDATED,
        segments=segments,
    )


def test_snapshot_carries_the_run_identity():
    snap = _snapshot([{"type": "text", "content": "Checking the PO."}])
    assert snap["run_id"] == "run-1"
    assert snap["trigger"] == "manual"
    assert snap["prompt"] == "Process case 5100001692-2026"
    assert snap["started_at"] == STARTED
    assert snap["updated_at"] == UPDATED
    assert snap["segments"] == [{"type": "text", "content": "Checking the PO."}]
    assert "segments_dropped" not in snap


def test_snapshot_keeps_the_last_segments_and_counts_the_rest():
    segments = [
        {"type": "text", "content": f"step {i}"} for i in range(MAX_LIVE_SEGMENTS + 3)
    ]
    snap = _snapshot(segments)
    assert len(snap["segments"]) == MAX_LIVE_SEGMENTS
    assert snap["segments"][0]["content"] == "step 3"
    assert snap["segments_dropped"] == 3


def test_snapshot_truncates_its_own_copy_and_leaves_the_caller_alone():
    # The list handed in is the same one the finished trace is written from. Truncating
    # it in place would cut the stored trace down to the live preview and lose the
    # evidence.truncated flag with it.
    long_result = "x" * (TOOL_RESULT_MAX_BYTES * 3)
    segments = [
        {
            "type": "tool",
            "tool_name": "get_purchase_order",
            "tool_result": long_result,
            "evidence": {"kind": "po_header"},
        }
    ]
    snap = _snapshot(segments)

    assert len(snap["segments"][0]["tool_result"]) < len(long_result)
    assert snap["segments"][0]["evidence"]["truncated"] is True
    assert segments[0]["tool_result"] == long_result
    assert "truncated" not in segments[0]["evidence"]


def test_the_budgets_match_the_spec():
    # The spec fixes all three, and the schema prose repeats the 40 and the 60 in
    # descriptions no test can read. A reader has to use the same numbers, so drift
    # here is a rendering bug there.
    assert (MAX_LIVE_SEGMENTS, LIVE_RUN_INTERVAL_SECONDS, LIVE_RUN_STALE_SECONDS) == (
        40,
        2.0,
        60,
    )


class _FakeTable:
    """The two things the writer uses: update_item, and the option of failing."""

    def __init__(self, fail: bool = False):
        self.calls: list[dict] = []
        self._fail = fail

    def update_item(self, **kwargs):
        self.calls.append(kwargs)
        if self._fail:
            raise RuntimeError("ProvisionedThroughputExceededException")
        return {}


class _FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class _FakeIso:
    """Wall clock as the writer reads it: a later timestamp on every call, 2 s apart.

    A `now_iso` returning one frozen string makes `started_at == updated_at` true no
    matter which field the writer builds from which source, so with it the pairing of
    "when the run began" and "when this write happened" cannot be pinned at all. This
    one advances, so the run's start (read once, at construction) is distinguishable
    from the time of a write.
    """

    def __init__(self):
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        return f"2026-08-19T09:00:{2 * self.calls - 2:02d}+00:00"


def _writer(table, clock):
    return LiveRunWriter(
        table=table,
        case_key={"case_id": "5100001692-2026"},
        run_id="run-1",
        trigger="manual",
        prompt="Process case 5100001692-2026",
        clock=clock,
        now_iso=_FakeIso(),
    )


SEGMENTS = [{"type": "text", "content": "Checking the PO."}]


def test_a_masked_writer_writes_the_masked_snapshot():
    table, clock = _FakeTable(), _FakeClock()
    seen = []

    def mask(snapshot):
        seen.append(snapshot)
        return {**snapshot, "prompt": "{MASKED}"}

    writer = LiveRunWriter(
        table=table,
        case_key={"case_id": "5100001692-2026"},
        run_id="run-1",
        trigger="manual",
        prompt="pay 00012345678901",
        clock=clock,
        now_iso=_FakeIso(),
        mask=mask,
    )
    writer.maybe_write(SEGMENTS)
    (call,) = table.calls
    assert seen and call["ExpressionAttributeValues"][":live"]["prompt"] == "{MASKED}"


def test_first_write_lands_and_names_the_case_and_run():
    table, clock = _FakeTable(), _FakeClock()
    _writer(table, clock).maybe_write(SEGMENTS)

    assert len(table.calls) == 1
    call = table.calls[0]
    assert call["Key"] == {"case_id": "5100001692-2026"}
    assert call["UpdateExpression"] == "SET live_run = :live"
    assert call["ConditionExpression"] == "attribute_exists(case_id)"
    live = call["ExpressionAttributeValues"][":live"]
    assert live["run_id"] == "run-1"
    # What the reader renders as the operator's own message on a case it opened mid-run,
    # and the only copy of it until the trace is written. `trigger` sits beside it in the
    # snapshot and looks just as assignable, so the two are pinned apart here.
    assert live["prompt"] == "Process case 5100001692-2026"
    # The run's start is read once, at construction; `updated_at` is this write's own
    # time, which is what tells a reader the run is still alive.
    assert live["started_at"] == STARTED
    assert live["updated_at"] == UPDATED
    assert live["segments"] == SEGMENTS


def test_writes_are_throttled_to_the_interval():
    table, clock = _FakeTable(), _FakeClock()
    writer = _writer(table, clock)

    writer.maybe_write(SEGMENTS)
    clock.now += LIVE_RUN_INTERVAL_SECONDS / 2
    writer.maybe_write(SEGMENTS)
    assert len(table.calls) == 1, "a write inside the interval must be dropped"

    clock.now += LIVE_RUN_INTERVAL_SECONDS
    writer.maybe_write(SEGMENTS)
    assert len(table.calls) == 2


def test_nothing_is_written_before_the_run_produces_a_segment():
    # The pairing invariant: live_run exists only if segments do, and segments are what
    # make basic_agent's finally: block run the trace write that REMOVEs it.
    table, clock = _FakeTable(), _FakeClock()
    _writer(table, clock).maybe_write([])
    assert table.calls == []


def test_a_failed_write_does_not_end_the_run():
    table, clock = _FakeTable(fail=True), _FakeClock()
    writer = _writer(table, clock)
    writer.maybe_write(SEGMENTS)  # must not raise
    assert len(table.calls) == 1

    # The window is claimed before the attempt, so the failure costs one snapshot and not
    # the throttle: a table that is failing must not turn into one write per event, which
    # on a fast token stream is the failure amplifying itself.
    writer.maybe_write(SEGMENTS)
    assert len(table.calls) == 1, "a failed write still consumes its window"


def test_a_snapshot_failure_does_not_end_the_run(monkeypatch):
    # The class's whole contract is "never raises": a bug while building the snapshot
    # must cost the live copy, not the run. Nothing else pins the snapshot being built
    # inside the guarded block.
    def boom(**_kwargs):
        raise RuntimeError("snapshot blew up")

    monkeypatch.setattr(live_run, "live_run_snapshot", boom)
    table, clock = _FakeTable(), _FakeClock()
    _writer(table, clock).maybe_write(SEGMENTS)  # must not raise
    assert table.calls == []


# ── Source-text pins on how live_run is wired into the agent's streaming loop. ──


_AGENT = AGENT / "basic_agent.py"
_PERSISTENCE = AGENT / "utils" / "case_persistence.py"


def _trace_append_expressions() -> list[str]:
    """Both forms of the UpdateExpression that appends a trace: with and without the
    case's exception type. Each must keep the whole write one statement."""
    return [case_persistence.trace_append_expression(i) for i in (False, True)]


def test_the_trace_append_is_the_expression_the_writer_sends():
    text = _PERSISTENCE.read_text()
    assert "UpdateExpression=trace_append_expression(" in text, (
        "the trace write moved, so the atomicity below is no longer being checked"
    )


def test_writer_is_built_from_the_runs_own_identity():
    text = _AGENT.read_text()
    assert re.search(
        r"live_writer = _live_run_writer\(\s*case_id,\s*input_data\.run_id,\s*trigger,\s*prompt,?\s*\)",
        text,
    ), (
        "The writer must be constructed from the run's own input_data.run_id, so the "
        "live snapshot and the trace's correlation_id name the same run."
    )


def test_every_accumulated_segment_offers_a_live_write():
    text = _AGENT.read_text()
    # Comment lines are allowed between the three statements, nothing executable is:
    # anything that ran in between would see `segments` in a state the write missed.
    assert re.search(
        r"accumulate_segment\(segments, canonical\)\n"
        r"(?:\s*#[^\n]*\n)*"
        r"\s+if live_writer is not None:\n"
        r"(?:\s*#[^\n]*\n)*"
        r"\s+live_writer\.maybe_write\(segments\)\n",
        text,
    ), (
        "maybe_write must be called immediately after accumulate_segment — that is the "
        "only point where `segments` holds everything the run has produced so far."
    )


def test_remove_rides_the_trace_append():
    for update in _trace_append_expressions():
        assert update.endswith("REMOVE live_run"), (
            "The live copy must be removed in the same update_item that appends the "
            "trace. Two calls leave a window where a reader sees both (double render) "
            f"or neither (the turn vanishes). Expression was: {update}"
        )


def test_the_trace_append_expression_is_well_formed():
    # DynamoDB accepts SET / ADD / REMOVE in any order but only once each, and rejects
    # the whole statement otherwise — which would lose the trace, not just the live copy.
    for update in _trace_append_expressions():
        for section in ("SET ", " ADD ", " REMOVE "):
            assert update.count(section) == 1, (
                f"{section.strip()} must appear exactly once in the trace-append "
                f"UpdateExpression. Expression was: {update}"
            )


def test_all_three_case_writers_resolve_the_table_through_one_helper():
    # The pairing invariant — live_run is only ever written to a case whose trace write
    # will REMOVE it — holds because these share their guards. Re-inlining any of them
    # lets the guard sets drift apart silently.
    for writer in (_live_run_writer, _save_trace_to_ddb, _flag_case_for_review):
        assert "_cases_table(case_id)" in inspect.getsource(writer), (
            f"{writer.__name__} must resolve the cases table through _cases_table, not "
            "its own copy of the CASES_TABLE / to_case_key guards"
        )
