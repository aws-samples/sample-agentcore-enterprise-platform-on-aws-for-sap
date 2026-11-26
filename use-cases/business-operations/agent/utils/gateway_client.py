# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""How the agent reaches its tools: a Gateway token from the token vault, and an MCP client.

One topology: the platform's shared AgentCore Gateway, authenticated with a token the
runtime draws from its own credential provider (`GATEWAY_CREDENTIAL_PROVIDER_NAME`),
which holds this use case's agent client. Cedar evaluates every call on that token's
scopes, and the run checks in the platform interceptor check the run grant the invoker wrote
for `x-audit-correlation-id`. The caller's JWT does not ride along: a target propagates
only its allowed request headers, so the tools see the agent's client and the
`x-audit-*` baggage.

**Not importable outside a configured runtime.** `@requires_access_token` reads
`GATEWAY_CREDENTIAL_PROVIDER_NAME` at import time and raises KeyError without it, so a
container missing its credential provider fails at start, not on the first tool call.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from bedrock_agentcore.identity.auth import requires_access_token
from bedrock_agentcore.runtime import BedrockAgentCoreContext
from bedrock_agentcore.runtime.models import ACCESS_TOKEN_HEADER
from mcp.client.streamable_http import streamablehttp_client
from strands.tools.mcp import MCPClient

from .mcp_topology import GATEWAY_GRANT_HEADER, audit_headers, build_tool_filters

if TYPE_CHECKING:
    from fastapi import Request


def bind_agentcore_workload_token(request: Request) -> None:
    """Bind Runtime's reserved workload token for AgentCore Identity decorators."""
    workload_token = request.headers.get(ACCESS_TOKEN_HEADER)
    if not workload_token:
        raise ValueError(
            "AgentCore Runtime request is missing its workload access token."
        )
    BedrockAgentCoreContext.set_workload_access_token(workload_token)


@requires_access_token(
    provider_name=os.environ["GATEWAY_CREDENTIAL_PROVIDER_NAME"],
    auth_flow="M2M",
    # Named, not omitted: with no scope Cognito grants every scope the client holds.
    scopes=os.environ.get("GATEWAY_SCOPES", "").split(),
)
def _fetch_gateway_token(access_token: str) -> str:
    return access_token


def create_gateway_client(
    allowed_tools: list[str] | None = None,
    audit_context: dict | None = None,
    read_only: bool = False,
    bearer: str | None = None,
    sap_system: str | None = None,
    run_grant: str | None = None,
) -> MCPClient:
    """`bearer`: a person's verified token (auth.forwarded_user_token) to call the
    Gateway with instead of the vault's machine token. `sap_system`: the pack's, whose
    SAP tools are the only SAP tools the run holds. `run_grant`: the secret of the
    run's grant, which the run checks require beside the run id."""
    gateway_url = os.environ["GATEWAY_URL"]
    print(f"[AGENT] Gateway URL: {gateway_url}")

    # `allowed_tools=None` (a discovery run, with no routed pack) still filters: only
    # this use case's own targets, minus `_AGENT_DENIED_TOOLS` and the SAP writes.
    tool_filters = build_tool_filters(
        allowed_tools, read_only=read_only, sap_system=sap_system
    )
    print(
        f"[AGENT] Tool filter: {allowed_tools or 'discovery (own targets, no SAP writes)'}"
    )
    if read_only:
        print("[AGENT] Read only: every side-effecting tool withheld")

    def _headers() -> dict[str, str]:
        return {
            "Authorization": f"Bearer {bearer or _fetch_gateway_token()}",
            **audit_headers(audit_context),
            **({GATEWAY_GRANT_HEADER: run_grant} if run_grant else {}),
        }

    return MCPClient(
        # 120 s per response, not the SDK's 300: a hung SAP call ends a turn sooner. The
        # SAP client inside the MCP server already gives up after 30 s.
        lambda: streamablehttp_client(
            url=gateway_url, headers=_headers(), sse_read_timeout=120
        ),
        tool_filters=tool_filters,
    )
