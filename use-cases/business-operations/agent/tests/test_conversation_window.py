# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The conversation window, and the cross-SDK offset bug it works around.

``removed_message_count`` means two different things to the two SDKs that share it:
Strands counts messages the agent appended, AgentCore Memory applies it to a list
already stripped of tool blocks. The first test reproduces the resulting data loss
directly against the real ``_filter_restored_tool_context``, so it fails if the SDK
ever fixes the ordering (at which point ``MemorySafeSlidingWindow`` can go).

``utils.conversation`` imports only strands, so unlike basic_agent.py it is
importable here. The wiring in ``_create_agent`` is pinned as source text (same
constraint as test_memory_session_config.py).
"""

import itertools
import re
from pathlib import Path

from strands.agent.conversation_manager import SlidingWindowConversationManager
from utils.conversation import MemorySafeSlidingWindow

_AGENT = Path(__file__).resolve().parents[1] / "basic_agent.py"


def _tool_heavy_history(pairs: int) -> list[dict]:
    """`pairs` investigation rounds: a tool call and its result, then a text answer."""
    messages: list[dict] = [{"role": "user", "content": [{"text": "process case 42"}]}]
    for i in range(pairs):
        messages.append(
            {
                "role": "assistant",
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": f"t{i}",
                            "name": "odata_read",
                            "input": {},
                        }
                    }
                ],
            }
        )
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "toolUseId": f"t{i}",
                            "status": "success",
                            # Over 2 * _PRESERVE_CHARS (200), or truncation is a no-op.
                            "content": [{"text": "x" * 1200}],
                        }
                    }
                ],
            }
        )
        messages.append({"role": "assistant", "content": [{"text": f"step {i} done"}]})
    return messages


def test_a_nonzero_offset_would_discard_restored_history():
    """Why the offset is suppressed: the two SDKs count different message spaces.

    AgentCoreMemorySessionManager.list_messages filters tool blocks and THEN slices
    messages[offset:]. Feeding it a count taken over unfiltered messages skips past
    the end of the filtered list — the case's whole history, dropped with no error.
    """
    from bedrock_agentcore.memory.integrations.strands.session_manager import (
        AgentCoreMemorySessionManager,
    )
    from strands.types.session import SessionMessage

    messages = _tool_heavy_history(pairs=6)
    stored = [SessionMessage.from_message(m, i) for i, m in enumerate(messages)]

    filtered = AgentCoreMemorySessionManager._filter_restored_tool_context(
        object.__new__(AgentCoreMemorySessionManager), stored
    )
    assert len(filtered) < len(stored), (
        "filter_restored_tool_context no longer drops tool blocks — re-check whether "
        "the offset mismatch MemorySafeSlidingWindow works around still exists"
    )

    # Run one eviction to get a real count, then persist it the way sync_agent does.
    window = MemorySafeSlidingWindow(window_size=4)
    agent = type("_A", (), {"messages": list(messages)})()
    window.apply_management(agent)
    state = window.get_state()

    assert state["removed_message_count"] > 0, (
        "window_size=4 over 19 messages must evict — the test setup is wrong"
    )
    assert filtered[state["removed_message_count"] :] == [], (
        "the offset happens to stay in range here, so this test no longer demonstrates "
        "the failure it guards; re-derive it against a longer history"
    )

    # What the next invocation actually restores with.
    resumed = MemorySafeSlidingWindow(window_size=120)
    resumed.restore_from_session(state)
    assert resumed.removed_message_count == 0, (
        "restore must report 0 to list_messages, or the offset over-skips the filtered "
        "list and the session resumes with no history"
    )
    assert filtered[resumed.removed_message_count :] == filtered


def test_restore_survives_state_written_by_the_bare_default():
    # The agent ran on the silent default, so every live session's persisted state
    # says SlidingWindowConversationManager. ConversationManager.restore_from_session
    # raises ValueError on a __name__ mismatch, so a rename would break every resume.
    written_before = SlidingWindowConversationManager(window_size=40)
    written_before.removed_message_count = 7

    restored = MemorySafeSlidingWindow(window_size=120)
    restored.restore_from_session(written_before.get_state())

    assert restored.removed_message_count == 0


def test_state_written_by_the_subclass_restores_into_the_base_class():
    # ...and the reverse, so rolling this change back does not strand the sessions
    # written while it was deployed.
    written_after = MemorySafeSlidingWindow(window_size=120)
    written_after.removed_message_count = 3

    rolled_back = SlidingWindowConversationManager(window_size=40)
    rolled_back.restore_from_session(written_after.get_state())

    assert rolled_back.removed_message_count == 3


def test_truncating_tool_results_does_not_count_as_eviction():
    """The lever the window actually relies on, and why it is safe.

    An oversized tool result is what blows the context window, and reduce_context
    truncates the oldest one instead of evicting messages — leaving both the cache
    prefix and removed_message_count untouched.
    """
    messages = _tool_heavy_history(pairs=2)
    window = MemorySafeSlidingWindow(window_size=120, should_truncate_results=True)
    agent = type("_A", (), {"messages": messages})()

    window.reduce_context(agent, e=RuntimeError("context overflow"))

    assert len(agent.messages) == 7, "no message should have been evicted"
    assert window.removed_message_count == 0
    assert "[truncated:" in str(agent.messages[2]), "the oldest tool result must shrink"


def _roles(messages: list) -> list[str]:
    return [m.to_message()["role"] for m in messages]


def test_the_sdk_filter_leaves_a_message_list_bedrock_rejects():
    """The defect ``sanitize_restored_messages`` exists for.

    Every tool round-trip is an assistant toolUse and a user toolResult. Filtering
    empties the user half and drops it, so a valid alternating history restores as a
    run of consecutive assistant messages — which Converse rejects. Fails if the SDK
    starts preserving alternation, at which point the sanitizer can go.
    """
    from bedrock_agentcore.memory.integrations.strands.session_manager import (
        AgentCoreMemorySessionManager,
    )
    from strands.types.session import SessionMessage

    stored = [
        SessionMessage.from_message(m, i)
        for i, m in enumerate(_tool_heavy_history(pairs=4))
    ]
    filtered = AgentCoreMemorySessionManager._filter_restored_tool_context(
        object.__new__(AgentCoreMemorySessionManager), stored
    )

    roles = _roles(filtered)
    assert any(a == b for a, b in itertools.pairwise(roles)), (
        "the SDK filter no longer breaks role alternation — re-check whether "
        "sanitize_restored_messages is still needed"
    )


def test_sanitizing_restores_strict_alternation_without_losing_text():
    from bedrock_agentcore.memory.integrations.strands.session_manager import (
        AgentCoreMemorySessionManager,
    )
    from strands.types.session import SessionMessage
    from utils.conversation import sanitize_restored_messages

    stored = [
        SessionMessage.from_message(m, i)
        for i, m in enumerate(_tool_heavy_history(pairs=4))
    ]
    filtered = AgentCoreMemorySessionManager._filter_restored_tool_context(
        object.__new__(AgentCoreMemorySessionManager), stored
    )

    sanitized = sanitize_restored_messages(filtered)

    roles = _roles(sanitized)
    assert roles == ["user", "assistant"], roles
    # Every step's answer survives the merge: losing them would make the agent
    # re-investigate a case it already worked.
    merged = str(sanitized[-1].to_message())
    for i in range(4):
        assert f"step {i} done" in merged


def test_sanitizing_drops_restored_reasoning_and_orphan_user_turns():
    """Both shapes observed in the live session that could not resume.

    Restored reasoningContent carries an empty ``text`` with the earlier run's
    signature, and an aborted run leaves a user turn with no answer after it — which
    the incoming prompt would follow with a second user message.
    """
    from strands.types.session import SessionMessage
    from utils.conversation import sanitize_restored_messages

    history = [
        {"role": "user", "content": [{"text": "process case 42"}]},
        {
            "role": "assistant",
            "content": [
                {
                    "reasoningContent": {
                        "reasoningText": {"text": "", "signature": "sig"}
                    }
                }
            ],
        },
        {"role": "assistant", "content": [{"text": "done"}]},
        {"role": "user", "content": [{"text": "hello"}]},
    ]
    stored = [SessionMessage.from_message(m, i) for i, m in enumerate(history)]

    sanitized = sanitize_restored_messages(stored)

    assert _roles(sanitized) == ["user", "assistant"]
    assert sanitized[-1].to_message()["content"] == [{"text": "done"}], (
        "the empty reasoning block must not survive into the replayed request"
    )


def test_sanitizing_an_all_assistant_history_yields_nothing_rather_than_an_invalid_list():
    from strands.types.session import SessionMessage
    from utils.conversation import sanitize_restored_messages

    stored = [
        SessionMessage.from_message({"role": "assistant", "content": [{"text": t}]}, i)
        for i, t in enumerate(["a", "b"])
    ]
    assert sanitize_restored_messages(stored) == []


def _create_agent_source() -> str:
    text = _AGENT.read_text()
    start = text.index("def _create_agent(")
    # `agent_stream` is what follows, so the bound has to accept `async def` too.
    end = re.compile(r"\n(?:async )?def ").search(text, start + 1).start()
    return text[start:end]


def test_the_agent_states_its_conversation_manager():
    # Agent() defaults to SlidingWindowConversationManager(window_size=40) when none is
    # passed, and ag_ui_strands._extract_agent_kwargs DOES forward conversation_manager
    # to the per-thread clone — so an unset manager is not "no management", it is
    # unstated management on the agent that serves the turn.
    source = _create_agent_source()
    match = re.search(
        r"conversation_manager=MemorySafeSlidingWindow\(\s*window_size=(\d+)", source
    )
    assert match, "_create_agent must pass MemorySafeSlidingWindow explicitly"
    assert int(match.group(1)) > 40, (
        "a window at or below the Strands default re-introduces routine mid-run "
        "eviction, which moves the cached prompt prefix and re-triggers the restore "
        "offset mismatch"
    )


def _denied(code: str):
    from botocore.exceptions import ClientError

    def read(self, *args, **kwargs):
        raise ClientError({"Error": {"Code": code, "Message": "x"}}, "ListEvents")

    return read


def test_a_denied_legacy_actor_lookup_is_not_found_and_other_errors_raise(monkeypatch):
    # The runtime role may list only actor ids under MEMORY_ACTOR_PREFIX; the SDK's
    # fallback to `session_<id>`/`agent_<id>` on every new session is refused.
    import pytest
    from bedrock_agentcore.memory.integrations.strands.session_manager import (
        AgentCoreMemorySessionManager as Base,
    )
    from botocore.exceptions import ClientError
    from utils.conversation import MemorySafeSessionManager

    manager = object.__new__(MemorySafeSessionManager)
    for name in ("read_session", "read_agent"):
        monkeypatch.setattr(Base, name, _denied("AccessDeniedException"))
        assert getattr(manager, name)("s", "a") is None
        monkeypatch.setattr(Base, name, _denied("ThrottlingException"))
        with pytest.raises(ClientError):
            getattr(manager, name)("s", "a")


def test_the_sdk_error_line_for_a_denied_lookup_is_dropped_and_others_kept(
    monkeypatch, caplog
):
    # The SDK logs "Failed to list events: ...AccessDeniedException..." at ERROR
    # before raising; inside the lookup that line is noise, outside it is not.
    import logging

    from bedrock_agentcore.memory.integrations.strands.session_manager import (
        AgentCoreMemorySessionManager as Base,
    )
    from botocore.exceptions import ClientError
    from utils.conversation import MemorySafeSessionManager

    sdk = logging.getLogger("bedrock_agentcore.memory.client")

    def read(self, *args, **kwargs):
        err = ClientError({"Error": {"Code": "AccessDeniedException"}}, "ListEvents")
        sdk.error("Failed to list events: %s", err)
        raise err

    monkeypatch.setattr(Base, "read_session", read)
    manager = object.__new__(MemorySafeSessionManager)
    with caplog.at_level(logging.ERROR):
        assert manager.read_session("s") is None
        assert "AccessDeniedException" not in caplog.text
        sdk.error("Failed to list events: AccessDeniedException")
    assert "AccessDeniedException" in caplog.text
