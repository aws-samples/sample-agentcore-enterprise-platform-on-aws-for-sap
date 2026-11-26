# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Two `update_case_state` calls in one turn land in the order the model emitted them.

A benchmark run once showed it: the model emitted `processing` then `complete` in one
turn, Strands ran both concurrently, `processing` reached the store last, and the
invoker's fail-closed outcome check sent a released case to manual review.
"""

import asyncio
import importlib.util
import os
import sys
from pathlib import Path
from unittest import mock

import pytest
from strands import Agent, tool
from strands.models.model import Model
from utils.tool_order import CaseWriteOrderedExecutor

_INVOKER = Path(__file__).resolve().parents[2] / "invoker" / "index.py"


def _run_turn(tool_uses):
    """Run one agent invocation whose single model turn emits ``tool_uses``."""
    store = {"status": None, "arrivals": []}
    reads = {"active": 0, "peak": 0}

    @tool
    async def update_case_state(case_id: str, updates: str, action: str) -> str:
        """Write case state."""
        # The first-emitted write is the slow one, so concurrency would invert them.
        await asyncio.sleep(0.05 if action == "invoice_released" else 0)
        store["status"] = updates
        store["arrivals"].append(action)
        return "ok"

    @tool
    async def odata_read(entity_set: str) -> str:
        """Read SAP."""
        reads["active"] += 1
        reads["peak"] = max(reads["peak"], reads["active"])
        await asyncio.sleep(0.02)
        reads["active"] -= 1
        return "{}"

    class StubModel(Model):
        def get_config(self):
            return {}

        def update_config(self, **k):
            pass

        async def structured_output(self, *a, **k):
            raise NotImplementedError

        async def stream(self, messages, tool_specs=None, system_prompt=None, **kw):
            yield {"messageStart": {"role": "assistant"}}
            if any("toolResult" in c for c in messages[-1]["content"]):
                yield {"contentBlockStart": {"start": {}}}
                yield {"contentBlockDelta": {"delta": {"text": "Done."}}}
                yield {"contentBlockStop": {}}
                yield {"messageStop": {"stopReason": "end_turn"}}
                return
            for i, (name, args) in enumerate(tool_uses):
                tu = {"toolUseId": f"tu-{i}", "name": name, "input": {}}
                yield {"contentBlockStart": {"start": {"toolUse": tu}}}
                yield {"contentBlockDelta": {"delta": {"toolUse": {"input": args}}}}
                yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}

    agent = Agent(
        model=StubModel(),
        tools=[update_case_state, odata_read],
        tool_executor=CaseWriteOrderedExecutor(),
        callback_handler=None,
    )
    agent("go")
    return store, reads


def _write(action, status):
    return (
        "update_case_state",
        f'{{"case_id": "c", "action": "{action}", "updates": "{status}"}}',
    )


_READ = ("odata_read", '{"entity_set": "A_SupplierInvoice"}')


def test_same_turn_status_writes_apply_in_emission_order():
    store, reads = _run_turn(
        [
            _READ,
            _write("invoice_released", "processing"),
            _READ,
            _write("case_closed", "complete"),
        ]
    )
    assert store["arrivals"] == ["invoice_released", "case_closed"]
    assert store["status"] == "complete"
    # Reads in the same turn are not serialized along with the writes.
    assert reads["peak"] == 2


@pytest.fixture()
def invoker(monkeypatch):
    # The invoker imports the API layer's modules the way its Lambda does.
    monkeypatch.syspath_prepend(str(_INVOKER.parents[1] / "api" / "layer" / "python"))
    env = {"RUNTIME_ARN_PARAMETER": "/p", "CASES_TABLE": "t", "AWS_REGION": "us-east-1"}
    with (
        mock.patch.dict(os.environ, env, clear=False),
        mock.patch("boto3.client"),
        mock.patch("boto3.resource"),
    ):
        spec = importlib.util.spec_from_file_location("invoker_order", _INVOKER)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        yield module
    sys.modules.pop(spec.name, None)


def _outcome(invoker, status):
    table = mock.MagicMock()
    table.get_item.return_value = {"Item": {"status": status}}
    with mock.patch.object(invoker, "_update_status", return_value=True) as upd:
        result = invoker._verify_business_outcome(
            table, "ap-6wmb2e5eahanwik7bfz3kcmk3e", "manual"
        )
    return result, upd


def test_the_ordered_final_state_passes_the_outcome_guard(invoker):
    store, _ = _run_turn(
        [_write("invoice_released", "processing"), _write("case_closed", "complete")]
    )
    result, upd = _outcome(invoker, store["status"])
    assert result == "complete"
    upd.assert_not_called()


def test_the_guard_still_routes_a_genuinely_unfinished_case_to_review(invoker):
    store, _ = _run_turn(
        [_write("classified", "complete"), _write("invoice_released", "processing")]
    )
    # The model really did emit `processing` last, so the guard must still fire.
    assert store["status"] == "processing"
    result, upd = _outcome(invoker, store["status"])
    assert result == "manual_review_required"
    upd.assert_called_once()
