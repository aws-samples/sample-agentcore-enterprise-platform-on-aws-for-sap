# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Apply one turn's case-store writes in the order the model emitted them.

Strands runs every tool call of a turn concurrently by default. Two
`update_case_state` calls in one turn — `processing` then `complete` — then race
to DynamoDB, and the one emitted first can land last: the case ends `processing`,
and the invoker's fail-closed outcome check routes a correctly finished case to
manual review. So writers run one at a time, in emission order, after the turn's
other calls (which stay concurrent — parallel SAP reads are the common case).

Serializing here is enough even though the write crosses the Gateway: each
`tools/call` returns only after the Lambda's `update_item` has, so the next write
cannot start before the previous one is durable.
"""

from collections.abc import AsyncGenerator
from typing import Any

from strands.tools.executors import ConcurrentToolExecutor, SequentialToolExecutor
from strands.tools.executors._executor import ToolExecutor

# Every tool whose Lambda writes the cases table.
CASE_STORE_WRITERS = frozenset({"update_case_state", "demo_create_ticket"})


def _is_writer(tool_use: dict) -> bool:
    # A Gateway tool not yet renamed still carries its `<target>___` prefix.
    return str(tool_use.get("name", "")).split("___")[-1] in CASE_STORE_WRITERS


class CaseWriteOrderedExecutor(ToolExecutor):
    """Concurrent for everything but case-store writers, which run in order."""

    def __init__(self) -> None:
        self._concurrent = ConcurrentToolExecutor()
        self._sequential = SequentialToolExecutor()

    async def _execute(
        self, agent, tool_uses, tool_results, *args: Any, **kwargs: Any
    ) -> AsyncGenerator:
        writes = [t for t in tool_uses if _is_writer(t)]
        if len(writes) < 2:
            async for event in self._concurrent._execute(
                agent, tool_uses, tool_results, *args, **kwargs
            ):
                yield event
            return
        others = [t for t in tool_uses if not _is_writer(t)]
        if others:
            async for event in self._concurrent._execute(
                agent, others, tool_results, *args, **kwargs
            ):
                yield event
        async for event in self._sequential._execute(
            agent, writes, tool_results, *args, **kwargs
        ):
            yield event
