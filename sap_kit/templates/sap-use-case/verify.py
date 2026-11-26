#!/usr/bin/env python3

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Verify {{name}} through the shared Gateway, as its own `reader` client.

On the first SAP system its claim (`layers/sap/claims/{{name}}`) reads:

- a SAP read answers: the first page of the system's service catalog. A
  `success: false` answer (a SAP 401, say) is a failure;
- a SAP write is refused, because the claim names none;
- under Cedar ENFORCE, the reader is not offered any SAP write tool.

The reader signs in the way sap_kit/mcp_gateway_proxy.py does (Cognito
client_credentials). Exits 0 with `OK: ...`, 1 with `FAIL: ...`; `deploy.sh verify`
runs it when the use case is enabled in platform.yaml.
"""

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

NAME = "{{name}}"
WRITE_TOOLS = ("odata_create", "odata_function_import", "odata_update")


def _text(result: dict) -> str:
    body = result.get("result") or {}
    return " ".join(c.get("text", "") for c in body.get("content") or [])


def main() -> int:
    # Imported here so a missing dependency is reported as FAIL:, not a traceback.
    import boto3

    from infra_utils.platform_config import resolve_region
    from sap_kit.mcp_gateway_proxy import Gateway, ReaderToken

    project = os.environ.get("PROJECT_NAME", "agentcore-workshop")
    env = os.environ.get("ENVIRONMENT", "dev")
    region = resolve_region()
    ssm = boto3.client("ssm", region_name=region)

    def param(name: str):
        full = f"/{project}/{env}/{name}"
        try:
            return json.loads(ssm.get_parameter(Name=full)["Parameter"]["Value"])
        except ssm.exceptions.ParameterNotFound:
            sys.exit(f"FAIL: {full} not found; are the SAP layer and {NAME} deployed?")

    claim = param(f"layers/sap/claims/{NAME}")
    if not claim.get("read"):
        sys.exit(f"FAIL: the claim reads no SAP system; see layers.sap.access.{NAME}")
    system = claim["read"][0]
    target = param("layers/sap/systems")[system]["target"]
    enforced = param("layers/sap/check").get("cedar_enforced") is True

    token = ReaderToken(project, env, NAME, region)
    gateway = Gateway(token.gateway_url, token)

    def call(method: str, params: dict | None = None) -> dict:
        message = {"jsonrpc": "2.0", "id": method, "method": method}
        return gateway.send({**message, "params": params or {}}) or {}

    listed = {
        t["name"] for t in call("tools/list").get("result", {}).get("tools") or []
    }
    read = f"{target}___find_sap_services"
    if read not in listed:
        sys.exit(f"FAIL: tools/list for the reader lacks {read}")
    offered = sorted(
        f"{target}___{w}" for w in WRITE_TOOLS if f"{target}___{w}" in listed
    )
    if enforced and offered:
        sys.exit(
            f"FAIL: under Cedar ENFORCE the reader is offered SAP writes: {offered}"
        )

    result = call("tools/call", {"name": read, "arguments": {"top": 1}})
    try:
        answer = json.loads(_text(result))
    except ValueError:
        answer = None
    # The AWS for SAP MCP Server reports SAP's own refusal (a 401, say) as a successful call.
    if (
        "error" in result
        or (result.get("result") or {}).get("isError")
        or (isinstance(answer, dict) and answer.get("success") is False)
        or not _text(result).strip()
    ):
        sys.exit(f"FAIL: {read} did not answer: {json.dumps(result)[:300]}")

    write = {
        "name": f"{target}___odata_update",
        "arguments": {
            "service_name": "API_BUSINESS_PARTNER",
            "entity_set": "A_BusinessPartner",
            "identifier_fields": {"BusinessPartner": "verify-probe"},
            "payload": {},
        },
    }
    result = call("tools/call", write)
    # The layer's check refuses it first; under ENFORCE, Cedar may answer instead.
    if not (
        f"does not claim update on SAP system '{system}'" in _text(result)
        or (enforced and "error" in result)
    ):
        sys.exit(f"FAIL: a SAP write was not refused: {json.dumps(result)[:300]}")

    print(
        f"OK: {NAME}'s reader read SAP system {system} through {target}, was refused "
        "odata_update"
        + (", and is offered no SAP write" if enforced else " (Cedar not enforcing)")
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001 - no creds, no network, no stack: FAIL
        sys.exit(f"FAIL: {type(e).__name__}: {e}")
