# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Every `x-audit-*` header the agent sets is one the Gateway forwards.

A target's `allowedRequestHeaders` is an allowlist: the Gateway forwards exactly those
and silently drops the rest, so a header missing from it looks like working identity
propagation at the call site and arrives nowhere. The run checks read the run id from the
same header, so it has to be one the agent sets.
"""

import ast
from pathlib import Path

from utils.mcp_topology import audit_headers

USE_CASE = Path(__file__).resolve().parents[2]


def _constant(path: Path, name: str):
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == name for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{path.name} no longer declares {name}")


def test_the_agent_sets_no_header_the_sap_targets_would_drop():
    sent = set(audit_headers({"correlation_id": "r", "initiator": "i", "trigger": "t"}))
    # The SAP targets are the SAP layer's.
    layer = USE_CASE.parents[1] / "layers" / "sap" / "stack.py"
    allowed = _constant(layer, "AUDIT_HEADERS")
    assert sent == set(allowed)


def test_the_run_checks_read_the_run_id_from_a_header_the_agent_sets():
    run_id = _constant(
        USE_CASE / "business_operations_check/__init__.py", "RUN_ID_HEADER"
    )
    assert audit_headers({"correlation_id": "run-1"}) == {run_id: "run-1"}


def test_the_grant_secret_travels_on_the_headers_the_run_checks_and_invoker_name():
    from utils import mcp_topology

    gateway = _constant(
        USE_CASE / "business_operations_check/__init__.py", "GRANT_HEADER"
    )
    assert mcp_topology.GATEWAY_GRANT_HEADER == gateway
    for path in ("invoker/index.py", "business_operations/runtime.py"):
        assert mcp_topology.RUNTIME_GRANT_HEADER == _constant(
            USE_CASE / path, "RUN_GRANT_HEADER"
        )
    # No target may receive it: it is a secret, not baggage.
    layer = USE_CASE.parents[1] / "layers" / "sap" / "stack.py"
    assert gateway not in _constant(layer, "AUDIT_HEADERS")


def test_the_gateway_client_sends_the_grant_secret(monkeypatch):
    monkeypatch.setenv("GATEWAY_CREDENTIAL_PROVIDER_NAME", "p")
    monkeypatch.setenv("GATEWAY_URL", "https://gw")
    from utils import gateway_client, mcp_topology

    made = {}
    monkeypatch.setattr(
        gateway_client, "MCPClient", lambda factory, **_: made.setdefault("f", factory)
    )
    monkeypatch.setattr(
        gateway_client,
        "streamablehttp_client",
        lambda url, headers, **kw: {**headers, "_timeout": kw.get("sse_read_timeout")},
    )
    gateway_client.create_gateway_client(
        [], {"correlation_id": "run-1"}, bearer="b", run_grant="s"
    )
    headers = made["f"]()
    assert headers[mcp_topology.GATEWAY_GRANT_HEADER] == "s"
    assert headers["x-audit-correlation-id"] == "run-1"
    assert headers["_timeout"] == 120  # a hung SAP call ends the turn in 2 minutes


def test_a_read_only_client_is_never_offered_a_write(monkeypatch):
    monkeypatch.setenv("GATEWAY_CREDENTIAL_PROVIDER_NAME", "p")
    monkeypatch.setenv("GATEWAY_URL", "https://gw")
    from utils import gateway_client

    made = {}
    monkeypatch.setattr(
        gateway_client,
        "MCPClient",
        lambda factory, **kw: made.setdefault("filters", kw["tool_filters"]),
    )
    write = "sap-s4___odata_function_import"
    for read_only in (False, True):
        made.clear()
        gateway_client.create_gateway_client([write], read_only=read_only)
        (permit,) = made["filters"]["allowed"]
        tool = type("_T", (), {"mcp_tool": type("_S", (), {"name": write})()})()
        assert permit(tool) is not read_only
