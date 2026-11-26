#!/usr/bin/env python3

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""A stdio MCP server that proxies to the platform's Gateway as a use case's reader.

The Gateway is already an MCP server. What an MCP host cannot do for itself is mint
the bearer: a Cognito `client_credentials` exchange for the use case's `reader`
client (`gateway_clients` in its manifest), which expires hourly. This proxy is that
adapter: stdio in, because every host speaks it; streamable HTTP out, with the token
refreshed underneath.

    mcp_gateway_proxy.py --use-case <name> --tool <read> [--tool <read> ...]

A use case's `mcp-config` verb prints the block to paste into a host, with its reads
filled in (`./scripts/deploy.sh usecase run business-operations mcp-config`).

**Read-only, and only that use case's tools.** The Gateway is shared, so `tools/list`
also carries other use cases' targets; this proxy offers a tool only when its target
starts with `<name>-` or `sap-` (the SAP layer's) and the tool is one of the reads it
was started with. There is no write mode: a use case lists the reads an MCP host can
complete, and leaves out what its own run checks refuse outside a run. The allowlist is a
filter for a model, not a boundary: whoever can read the reader client's secret can
call the Gateway without this file. Under Cedar `ENFORCE` the reader's
scope-conditioned permits are what bound it.

Needs Python with boto3 (the repo's `.venv`) and AWS credentials that can read the
platform's SSM parameters and the reader client's secret.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from typing import IO, Any

CLIENT = "reader"

# A refused tool reads as "no such tool": the filter already removed it from
# `tools/list`, so that is what the host saw.
_TOOL_NOT_FOUND = -32601
_INTERNAL_ERROR = -32603

Transport = Callable[[str, bytes, dict[str, str]], tuple[int, dict[str, str], bytes]]


class GatewayError(RuntimeError):
    """A non-2xx from the Gateway. Reported, never retried: the token is refreshed a
    minute before it expires, so a 401 means the client or its secret is wrong."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"Gateway returned {status}: {body[:500]}")
        self.status = status


def urllib_transport(
    url: str, body: bytes, headers: dict[str, str]
) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        # The URL is the platform's own SSM parameter, not input.
        with urllib.request.urlopen(request, timeout=120) as response:  # nosec B310  # nosemgrep: dynamic-urllib-use-detected
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()


def allowlist(use_case: str, tools: Iterable[str]) -> Callable[[str], bool]:
    """Whether `<target>___<tool>` is offered: a target of `use_case` or of the SAP
    layer (which holds the reader to that use case's claim), and one of `tools`."""
    prefixes = (f"{use_case}-", "sap-")
    reads = frozenset(tools)

    def offered(name: str) -> bool:
        target, _, tool = name.partition("___")
        return bool(tool) and target.startswith(prefixes) and tool in reads

    return offered


def header(headers: dict[str, str], name: str) -> str | None:
    wanted = name.lower()
    return next((v for k, v in headers.items() if k.lower() == wanted), None)


def decode(body: bytes, content_type: str) -> dict[str, Any] | None:
    """One JSON-RPC message from a JSON body or an SSE stream; None for an empty body."""
    text = body.decode("utf-8", "replace").strip()
    if not text:
        return None
    if "text/event-stream" not in content_type:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    # Progress notifications can precede the response; the response carries an id.
    answer: dict[str, Any] | None = None
    for line in text.splitlines():
        if line.startswith("data:"):
            frame = json.loads(line[5:].strip())
            if isinstance(frame, dict) and "id" in frame:
                answer = frame
    return answer


class Gateway:
    """One MCP session against the Gateway, with the bearer kept fresh."""

    def __init__(
        self,
        url: str,
        token: Callable[[], str],
        transport: Transport = urllib_transport,
    ) -> None:
        self.url = url
        self.token = token
        self.transport = transport
        self.session_id: str | None = None

    def send(self, message: dict[str, Any]) -> dict[str, Any] | None:
        headers = {
            "Authorization": f"Bearer {self.token()}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        status, response_headers, body = self.transport(
            self.url, json.dumps(message).encode(), headers
        )
        session = header(response_headers, "Mcp-Session-Id")
        if session:
            self.session_id = session
        if status >= 400:
            raise GatewayError(status, body.decode("utf-8", "replace"))
        return decode(body, header(response_headers, "Content-Type") or "")


def error_response(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def handle(
    gateway: Gateway,
    message: dict[str, Any],
    log: Callable[[str], None],
    offered: Callable[[str], bool],
) -> dict[str, Any] | None:
    """One request in, at most one response out."""
    method = message.get("method")
    request_id = message.get("id")
    if method == "tools/call":
        name = str((message.get("params") or {}).get("name", ""))
        if not offered(name):
            log(f"refused {name}: not one of the use case's read tools")
            return error_response(
                request_id,
                _TOOL_NOT_FOUND,
                f"Tool {name!r} is not offered by this proxy: it serves only "
                "the use case's read tools.",
            )
    try:
        response = gateway.send(message)
    except Exception as exc:  # noqa: BLE001 - everything in send() is remote
        # One failed call, not a dead process: a host reads an escaped exception as
        # the server dying mid-session.
        detail = (
            str(exc)
            if isinstance(exc, GatewayError)
            else f"{type(exc).__name__}: {exc}"
        )
        log(detail)
        return (
            None
            if request_id is None
            else error_response(request_id, _INTERNAL_ERROR, detail)
        )
    if response is not None and method == "tools/list":
        result = response.get("result")
        if isinstance(result, dict) and isinstance(result.get("tools"), list):
            kept = [t for t in result["tools"] if offered(str(t.get("name", "")))]
            if len(kept) < len(result["tools"]):
                log(f"withheld {len(result['tools']) - len(kept)} tool(s)")
            result["tools"] = kept
    return response


def serve(
    gateway: Gateway,
    stdin: IO[str],
    stdout: IO[str],
    log: Callable[[str], None],
    offered: Callable[[str], bool],
) -> None:
    """Newline-delimited JSON-RPC in, answers out. stdout carries protocol only."""
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            log(f"dropping unparseable line: {line[:200]}")
            continue
        if not isinstance(message, dict):
            log("dropping non-object JSON-RPC message")
            continue
        response = handle(gateway, message, log, offered)
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()


class ReaderToken:
    """client_credentials for the reader client, cached until a minute before expiry."""

    def __init__(
        self, project: str, environment: str, use_case: str, region: str | None
    ) -> None:
        import boto3

        ssm = boto3.client("ssm", region_name=region)
        base = f"/{project}/{environment}"
        clients = f"{base}/gateway/use-case-clients/{use_case}/{CLIENT}"

        def param(name: str) -> str:
            return ssm.get_parameter(Name=name)["Parameter"]["Value"]

        self.gateway_url = param(f"{base}/gateway/url")
        self._token_url = param(f"{base}/auth/domain-url").rstrip("/") + "/oauth2/token"
        client_id = param(f"{clients}/client-id")
        secret = boto3.client("secretsmanager", region_name=region).get_secret_value(
            SecretId=param(f"{clients}/client-secret-name")
        )["SecretString"]
        self._basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
        self._token = ""
        self._expires = 0.0

    def __call__(self) -> str:
        if time.time() < self._expires - 60:
            return self._token
        request = urllib.request.Request(
            self._token_url,
            data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {self._basic}",
            },
        )
        # The platform's own hosted domain, from SSM.
        with urllib.request.urlopen(request, timeout=15) as response:  # nosec B310  # nosemgrep: dynamic-urllib-use-detected
            payload = json.loads(response.read())
        self._token = payload["access_token"]
        self._expires = time.time() + float(payload.get("expires_in") or 3600)
        return self._token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mcp_gateway_proxy.py",
        description="Expose a use case's read tools on the platform Gateway over stdio.",
    )
    parser.add_argument("--use-case", required=True, help="the use case's name")
    parser.add_argument(
        "--tool",
        action="append",
        required=True,
        help="a read tool to offer, by its name after `___`; repeat for each",
    )
    parser.add_argument("--project", default=os.environ.get("PROJECT_NAME"))
    parser.add_argument("--environment", default=os.environ.get("ENVIRONMENT"))
    args = parser.parse_args(argv)
    if not (args.project and args.environment):
        parser.error(
            "pass --project and --environment, or set PROJECT_NAME and ENVIRONMENT"
        )

    def log(message: str) -> None:
        print(f"[mcp-gateway-proxy] {message}", file=sys.stderr, flush=True)

    # botocore's region chain reads AWS_DEFAULT_REGION, not AWS_REGION.
    region = os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION")
    try:
        token = ReaderToken(args.project, args.environment, args.use_case, region)
    except Exception as exc:  # noqa: BLE001 - name the cause and stop
        log(
            f"cannot read /{args.project}/{args.environment}/ in {region or '<no region>'}: "
            f"{type(exc).__name__}: {exc}"
        )
        log(f"check the names, the Region, and that {args.use_case} is deployed")
        return 1
    log(
        f"gateway={token.gateway_url} use_case={args.use_case} client={CLIENT} "
        f"reads: {', '.join(sorted(set(args.tool)))}"
    )
    offered = allowlist(args.use_case, args.tool)
    serve(Gateway(token.gateway_url, token), sys.stdin, sys.stdout, log, offered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
