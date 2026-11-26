# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Sanitize and fence every tool result before the model reads it (T31).

The trigger that starts a run is sanitized and fenced in `payload._build_prompt`. What a
tool returns mid-run is not: Strands appends the result to the conversation and the next
model call reads it verbatim. That is the larger untrusted surface — every SAP field the
agent looks at, written by anyone who can type into SAP — so it gets the same two
treatments as the trigger: `sanitize_external_content` and `fence_data`.

**Where, and why there.** The transform runs on the messages handed to `model.stream`, not
in an `AfterToolCallEvent` hook. A hook that rewrote `event.result` would change the stored
history, the AG-UI `TOOL_CALL_RESULT` the browser parses, the persisted trace and the
evidence `EvidenceHook` extracts from the raw result. Fencing the model's *view* changes
none of those. It is a pure function of the history, so every model call renders the same
bytes for the same history and the prompt cache prefix stays stable.

**What is not fenced.** SOP and skill retrieval (`TRUSTED_INSTRUCTION_TOOLS`) returns text
the agent is meant to follow — authored instruction, trusted by design —
and fencing it as "DATA only" would tell the model to ignore its own procedure. Everything
else is fenced, including a tool this module has never heard of and a result whose
`toolUse` is no longer in the history: the default is the safe side.
"""

from __future__ import annotations

import json
import re
from typing import Any

from strands.models import BedrockModel

from .content_filter import fence_data, sanitize_external_content

# Tools whose result is authored instruction rather than external data. Kept in step with
# the `sop_lookup` kind in evidence.py, which classifies the same tools for the same reason.
TRUSTED_INSTRUCTION_TOOLS = frozenset(
    {"load_sop", "search_sap_sops", "search_sap_api_docs", "skills"}
)

_ATTR_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")
SOURCE = "tool_result"


def _bare_name(name: str) -> str:
    # Gateway tools arrive as `<target>___<tool>`; evidence.py strips the same prefix.
    return (name or "").split("___")[-1]


def _sanitize_json(value: Any, source: str) -> Any:
    if isinstance(value, str):
        return sanitize_external_content(value, source=source)
    if isinstance(value, list):
        return [_sanitize_json(v, source) for v in value]
    if isinstance(value, dict):
        return {k: _sanitize_json(v, source) for k, v in value.items()}
    return value


def _fence_block(block: dict, tool: str) -> dict:
    label = f"{SOURCE}:{tool}"
    attrs = {"tool": _ATTR_UNSAFE.sub("_", tool) or "unknown"}
    if "text" in block:
        text = sanitize_external_content(block["text"], source=label)
        return {"text": fence_data(text, source=SOURCE, **attrs)}
    if "json" in block:
        # Sanitize the string leaves, then serialize: a pattern anchored on a line start
        # cannot match once json.dumps has escaped the newline in front of it.
        clean = _sanitize_json(block["json"], label)
        text = json.dumps(clean, ensure_ascii=False, default=str)
        return {"text": fence_data(text, source=SOURCE, **attrs)}
    # image / document blocks carry no text the model reads as instructions here.
    return block


def _tool_names_by_use_id(messages: list) -> dict[str, str]:
    names: dict[str, str] = {}
    for message in messages:
        for block in message.get("content") or []:
            use = block.get("toolUse") if isinstance(block, dict) else None
            if use and use.get("toolUseId"):
                names[use["toolUseId"]] = _bare_name(use.get("name", ""))
    return names


def fence_tool_results(messages: list) -> list:
    """Return `messages` with every untrusted toolResult sanitized and fenced.

    Never mutates its argument: a message or block is copied only when it changes, so
    Strands' own history is untouched and unchanged messages are shared, not copied.
    """
    names = _tool_names_by_use_id(messages)
    out = []
    for message in messages:
        content = message.get("content")
        if not content:
            out.append(message)
            continue
        new_content = None
        for index, block in enumerate(content):
            result = block.get("toolResult") if isinstance(block, dict) else None
            if not result:
                continue
            tool = names.get(result.get("toolUseId", ""), "")
            if tool in TRUSTED_INSTRUCTION_TOOLS:
                continue
            fenced = [
                _fence_block(b, tool or "unknown") if isinstance(b, dict) else b
                for b in result.get("content") or []
            ]
            if new_content is None:
                new_content = list(content)
            new_content[index] = {**block, "toolResult": {**result, "content": fenced}}
        out.append(
            message if new_content is None else {**message, "content": new_content}
        )
    return out


class FencedBedrockModel(BedrockModel):
    """The model the agent constructs: its `stream` reads fenced tool results."""

    async def stream(self, messages, *args, **kwargs):
        async for event in super().stream(
            fence_tool_results(messages), *args, **kwargs
        ):
            yield event
