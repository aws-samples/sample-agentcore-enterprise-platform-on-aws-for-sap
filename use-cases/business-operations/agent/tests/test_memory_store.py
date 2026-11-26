# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""What a run's conversation leaves in AgentCore Memory.

Restore drops every toolUse and toolResult block, so tool inputs and results (SAP
records, bank and tax numbers) were only ever kept, for the event expiry, never read
back. The session manager now stores a copy with no tool data and with text masked by
the persistence guardrail; the message the run uses is unchanged.
"""

import copy

import pytest
from strands.types.session import SessionMessage
from utils import conversation, persist_mask

ACCOUNT = "00012345678901"

MESSAGE = {
    "role": "assistant",
    "content": [
        {"text": f"The supplier's account is {ACCOUNT}."},
        {
            "toolUse": {
                "toolUseId": "t1",
                "name": "sap-s4___odata_read",
                "input": {"filter": f"BankAccount eq '{ACCOUNT}'"},
            }
        },
        {
            "toolResult": {
                "toolUseId": "t1",
                "status": "success",
                "content": [{"text": f'{{"BankAccount": "{ACCOUNT}"}}'}],
            }
        },
    ],
}


@pytest.fixture
def masking(monkeypatch):
    monkeypatch.setenv("PERSIST_GUARDRAIL_ID", "gr-persist")
    monkeypatch.setattr(
        persist_mask,
        "mask",
        lambda texts: [t.replace(ACCOUNT, "{US_BANK_ACCOUNT_NUMBER}") for t in texts],
    )


def test_no_tool_data_reaches_memory_and_text_is_masked(masking):
    original = copy.deepcopy(MESSAGE)
    stored = conversation.memory_copy(MESSAGE)
    assert ACCOUNT not in str(stored)
    text, use, result = stored["content"]
    assert text == {"text": "The supplier's account is {US_BANK_ACCOUNT_NUMBER}."}
    # The blocks keep their place and ids, so the stored conversation keeps its shape.
    assert use["toolUse"] == {
        "toolUseId": "t1",
        "name": "sap-s4___odata_read",
        "input": {},
    }
    assert result["toolResult"]["toolUseId"] == "t1"
    assert result["toolResult"]["content"] == [{"text": conversation.NOT_STORED}]
    # The run goes on with the message as it was.
    assert MESSAGE == original


def test_without_the_guardrail_text_is_kept_but_tool_data_still_is_not(monkeypatch):
    monkeypatch.delenv("PERSIST_GUARDRAIL_ID", raising=False)
    stored = conversation.memory_copy(MESSAGE)
    assert stored["content"][0] == MESSAGE["content"][0]
    assert f'"BankAccount": "{ACCOUNT}"' not in str(stored)
    assert stored["content"][1]["toolUse"]["input"] == {}


def test_the_session_manager_stores_the_copy_on_create_and_update(masking, monkeypatch):
    sent = []

    def create(self, session_id, agent_id, session_message, **kw):
        sent.append(session_message)
        return {"eventId": "e1"}

    def update(self, session_id, agent_id, session_message, **kw):
        sent.append(session_message)

    parent = conversation.AgentCoreMemorySessionManager
    monkeypatch.setattr(parent, "create_message", create)
    monkeypatch.setattr(parent, "update_message", update)
    manager = object.__new__(conversation.MemorySafeSessionManager)
    message = SessionMessage.from_message(copy.deepcopy(MESSAGE), 0)
    manager.create_message("s", "a", message)
    redacted = SessionMessage(
        message=copy.deepcopy(MESSAGE),
        message_id=1,
        redact_message=copy.deepcopy(MESSAGE),
    )
    manager.update_message("s", "a", redacted)
    assert len(sent) == 2 and all(ACCOUNT not in str(m) for m in sent)
    # The caller's message object is not the one stored.
    assert ACCOUNT in str(message.message)
