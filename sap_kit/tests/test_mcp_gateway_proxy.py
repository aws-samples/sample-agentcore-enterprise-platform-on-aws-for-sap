# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""sap_kit/mcp_gateway_proxy.py: the allowlist a use case starts it with, and the
stdio-to-Gateway relay. No AWS: the Gateway is a fake transport."""

from __future__ import annotations

import io
import json

import pytest

from sap_kit import mcp_gateway_proxy as proxy

OURS = "sap-s4___odata_read"
OFFERED = proxy.allowlist("hello-sap", ["odata_read", "say_hello"])


@pytest.mark.parametrize(
    "name, offered",
    [
        (OURS, True),
        ("hello-sap-greeter___say_hello", True),
        # Not one of the reads it was started with.
        ("sap-s4___odata_update", False),
        ("hello-sap-greeter___reset", False),
        # Another use case's same-named tool on the shared Gateway.
        ("hello-platform-greeter___say_hello", False),
        ("say_hello", False),
    ],
)
def test_the_proxy_offers_only_the_named_use_cases_reads(name, offered):
    assert OFFERED(name) is offered


def test_the_use_case_and_its_reads_are_required():
    with pytest.raises(SystemExit):
        proxy.main(["--project", "p", "--environment", "e", "--tool", "odata_read"])
    with pytest.raises(SystemExit):
        proxy.main(["--project", "p", "--environment", "e", "--use-case", "x"])


class FakeGateway(proxy.Gateway):
    def __init__(self, reply):
        self.sent = []
        super().__init__("https://gw", lambda: "tok", self._transport)
        self.reply = reply

    def _transport(self, url, body, headers):
        self.sent.append((json.loads(body), headers))
        return self.reply


def _reply(payload, headers=None, status=200):
    return (
        status,
        {"Content-Type": "application/json", **(headers or {})},
        json.dumps(payload).encode(),
    )


def test_tools_list_is_filtered():
    tools = [
        {"name": OURS},
        {"name": "other___odata_read"},
        {"name": "x___odata_create"},
    ]
    gw = FakeGateway(_reply({"jsonrpc": "2.0", "id": 1, "result": {"tools": tools}}))
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    out = proxy.handle(gw, message, print, OFFERED)
    assert [t["name"] for t in out["result"]["tools"]] == [OURS]


def test_a_withheld_call_never_reaches_the_gateway():
    gw = FakeGateway(_reply({}))
    message = {
        "id": 2,
        "method": "tools/call",
        "params": {"name": "sap-s4___odata_create"},
    }
    out = proxy.handle(gw, message, lambda _: None, OFFERED)
    assert out["error"]["code"] == -32601
    assert gw.sent == []


def test_a_gateway_error_is_one_failed_call():
    gw = FakeGateway((401, {}, b"bad token"))
    message = {"id": 3, "method": "tools/call", "params": {"name": OURS}}
    out = proxy.handle(gw, message, lambda _: None, OFFERED)
    assert out["error"]["code"] == -32603 and "401" in out["error"]["message"]


def test_the_session_id_is_echoed_and_sse_is_unwrapped():
    sse = b'data: {"jsonrpc":"2.0","method":"progress"}\n\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\n'
    gw = FakeGateway(
        (200, {"content-type": "text/event-stream", "mcp-session-id": "s1"}, sse)
    )
    assert gw.send({"id": 1, "method": "initialize"}) == {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {},
    }
    gw.send({"id": 2, "method": "ping"})
    assert gw.sent[1][1]["Mcp-Session-Id"] == "s1"


def test_serve_drops_junk_and_answers_the_rest():
    gw = FakeGateway(_reply({"jsonrpc": "2.0", "id": 1, "result": {}}))
    out = io.StringIO()
    proxy.serve(
        gw,
        io.StringIO('not json\n[]\n{"id":1,"method":"ping"}\n'),
        out,
        lambda _: None,
        OFFERED,
    )
    assert [json.loads(line)["id"] for line in out.getvalue().splitlines()] == [1]
