# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Session restore for the ERP agent, minus two cross-SDK bugs.

Both come from ``AgentCoreMemoryConfig(filter_restored_tool_context=True)``, which
is on so that a case's historical toolUse/toolResult blocks — large SAP OData
responses among them — are not reloaded into context on every turn.

The first is an offset bug that deletes history. Strands and the AgentCore Memory
session manager disagree about what ``removed_message_count`` counts:

* ``RepositorySessionManager.initialize`` restores by calling
  ``list_messages(offset=agent.conversation_manager.removed_message_count)`` — an
  offset into the messages the agent actually appended.
* ``AgentCoreMemorySessionManager.list_messages`` runs
  ``_filter_restored_tool_context`` (dropping every toolUse/toolResult block, and
  any message left with no content) *before* slicing ``messages[offset:]``.

So the offset is counted in unfiltered messages and applied to a filtered list.
Trimming evicts old, tool-heavy messages — exactly the ones filtering removes — so
the offset always over-skips, and a tool-heavy session can resume with no history
at all. Reporting 0 instead turns that into restoring slightly more than was
evicted, which the next ``apply_management`` re-trims. Extra context costs a cache
write; lost context makes the agent re-derive a case it already investigated.

Only the restore offset is suppressed. Eviction itself, and the state Strands
persists, are unchanged.

The second is that the same filter leaves a message list Amazon Bedrock rejects — see
``sanitize_restored_messages``.

Separately, ``read_session`` and ``read_agent`` fall back to looking for events under
the SDK's legacy actor ids (``session_<id>``, ``agent_<id>``) when the runtime's own
actor has none, which is every new session. The runtime role may touch only actor ids
under its ``MEMORY_ACTOR_PREFIX``, so that lookup is denied and the run fails before
its first turn. Nothing of this agent's can be under a legacy id for the same reason,
so a denied lookup means "not found". The SDK logs that refusal at ERROR before it
raises (or, in ``read_agent``, instead of raising); ``_QuietDenial`` drops those lines
while one of these reads runs, so they neither trip an error-log alarm nor bury a real
denial.
"""

from __future__ import annotations

import contextvars
import logging
from dataclasses import replace
from typing import Any

from bedrock_agentcore.memory.integrations.strands.session_manager import (
    AgentCoreMemorySessionManager,
)
from botocore.exceptions import ClientError
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.types.session import SessionMessage

from . import persist_mask


class MemorySafeSlidingWindow(SlidingWindowConversationManager):
    """Sliding window that never asks AgentCore Memory to skip restored messages.

    Drop this subclass once ``filter_restored_tool_context`` and
    ``removed_message_count`` agree on a message space — either because the flag is
    off, or because ``list_messages`` slices before it filters.
    """

    # ConversationManager.restore_from_session raises ValueError when the persisted
    # `__name__` is not this class's own name, and the agent has been running on the
    # bare default, so every live session's state says SlidingWindowConversationManager.
    # Persisting the base name keeps both directions readable: sessions written before
    # this class restore into it, and sessions written by it survive a rollback.
    _PERSISTED_NAME = SlidingWindowConversationManager.__name__

    def get_state(self) -> dict[str, Any]:
        return {**super().get_state(), "__name__": self._PERSISTED_NAME}

    def restore_from_session(self, state: dict[str, Any]) -> list | None:
        result = super().restore_from_session(
            {**state, "__name__": type(self).__name__}
        )
        # initialize() reads removed_message_count immediately after this returns,
        # so zeroing here is what reaches list_messages(offset=...).
        self.removed_message_count = 0
        return result


def sanitize_restored_messages(
    messages: list[SessionMessage],
) -> list[SessionMessage]:
    """Make a filtered AgentCore Memory history valid to replay to Amazon Bedrock.

    ``_filter_restored_tool_context`` drops every toolUse and toolResult block and
    then drops any message left with no content. A tool round-trip is an
    assistant-role toolUse followed by a user-role toolResult, so filtering deletes
    the user half of every round-trip and keeps the assistant half — turning a
    valid alternating history into a run of consecutive assistant messages. Amazon Bedrock
    Converse requires strictly alternating roles, so a case session with any tool
    use restores into a request the model cannot accept. Observed live: a
    thirteen-message history restored as ``user`` followed by eleven consecutive
    ``assistant`` messages.

    Three repairs, in order:

    * Drop ``reasoningContent`` blocks. What survives the round-trip is an empty
      ``text`` plus a signature minted for the earlier run's request, which is not
      replayable and is the whole content of some restored messages.
    * Merge each run of same-role messages into one, concatenating content. Merging
      rather than dropping is what keeps the assistant's reasoning between tool
      calls, which is the case's investigation trail.
    * Trim to whole exchanges: leading non-user and trailing non-assistant
      messages. A trailing user message is a turn that never got an answer — every
      aborted run leaves one — and the incoming prompt would follow it with a
      second user message.
    """
    kept: list[SessionMessage] = []
    for session_message in messages:
        message = session_message.to_message()
        content = [
            block
            for block in message.get("content", [])
            if "reasoningContent" not in block
        ]
        if not content:
            continue
        role = message["role"]
        if kept and kept[-1].message["role"] == role:
            kept[-1].message["content"].extend(content)
            continue
        kept.append(
            replace(
                session_message,
                message={"role": role, "content": content},
                # to_message() prefers redact_message, so leaving it set would hand
                # back the unrepaired message and undo the merge above.
                redact_message=None,
            )
        )

    while kept and kept[0].message["role"] != "user":
        kept.pop(0)
    while kept and kept[-1].message["role"] != "assistant":
        kept.pop()
    return kept


#: What a tool result holds in Memory instead of the SAP data it carried.
NOT_STORED = "[not stored: tool data is kept out of Memory]"


def memory_copy(message: dict) -> dict:
    """The copy of `message` Memory stores: no tool data, and text masked.

    Restore drops every toolUse and toolResult block
    (``filter_restored_tool_context``), so their inputs and results are never read
    back, only kept: SAP records, bank and tax numbers among them, for the event
    expiry. Each block keeps its place and ids, so the stored conversation has the
    same shape. Text turns are restored, and the persistence guardrail masks them
    when this stack has one, as it masks the trace. The message the run goes on
    using is never changed.
    """
    content = []
    for block in message.get("content") or []:
        if "toolUse" in block:
            content.append({"toolUse": {**block["toolUse"], "input": {}}})
        elif "toolResult" in block:
            result = {**block["toolResult"], "content": [{"text": NOT_STORED}]}
            content.append({"toolResult": result})
        else:
            content.append(dict(block))
    texts = [b for b in content if isinstance(b.get("text"), str)]
    for block, masked in zip(
        texts, persist_mask.mask([b["text"] for b in texts]), strict=True
    ):
        block["text"] = masked
    return {**message, "content": content}


class MemorySafeSessionManager(AgentCoreMemorySessionManager):
    """Session manager whose restored history is a valid Amazon Bedrock message list, and
    whose stored events hold no tool data (``memory_copy``).

    Drop the restore half of this subclass once ``_filter_restored_tool_context``
    preserves role alternation, or once the filter is no longer needed.
    """

    def create_message(self, session_id, agent_id, session_message, **kwargs):
        return super().create_message(
            session_id, agent_id, _stored(session_message), **kwargs
        )

    def update_message(self, session_id, agent_id, session_message, **kwargs):
        return super().update_message(
            session_id, agent_id, _stored(session_message), **kwargs
        )

    def list_messages(self, *args: Any, **kwargs: Any) -> list[SessionMessage]:
        return sanitize_restored_messages(super().list_messages(*args, **kwargs))

    def read_session(self, *args: Any, **kwargs: Any) -> Any:
        return _absent_if_denied(super().read_session, *args, **kwargs)

    def read_agent(self, *args: Any, **kwargs: Any) -> Any:
        return _absent_if_denied(super().read_agent, *args, **kwargs)


def _stored(session_message: SessionMessage) -> SessionMessage:
    redacted = session_message.redact_message
    return replace(
        session_message,
        message=memory_copy(session_message.message),
        redact_message=memory_copy(redacted) if redacted is not None else None,
    )


_in_read = contextvars.ContextVar("memory_read", default=False)


class _QuietDenial(logging.Filter):
    """Drops an AccessDenied ERROR the SDK logs inside ``_absent_if_denied``."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not (_in_read.get() and "AccessDeniedException" in record.getMessage())


for _name in (
    "bedrock_agentcore.memory.client",
    "bedrock_agentcore.memory.integrations.strands.session_manager",
):
    logging.getLogger(_name).addFilter(_QuietDenial())


def _absent_if_denied(read: Any, *args: Any, **kwargs: Any) -> Any:
    """``read``, or None when the legacy-actor fallback is refused.

    A refusal on the runtime's own actor id would surface anyway: the create that
    follows a None writes under that id and is refused the same way.
    """
    token = _in_read.set(True)
    try:
        return read(*args, **kwargs)
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") != "AccessDeniedException":
            raise
        return None
    finally:
        _in_read.reset(token)
