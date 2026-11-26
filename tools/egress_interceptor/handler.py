# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""AgentCore Gateway egress interceptor.

Applies a Bedrock Guardrail to Gateway traffic for defense-in-depth egress control:
  - REQUEST  interception point: guardrail runs with source=INPUT before the target is called
    (blocks prompt-injection, masks PII leaving the agent toward the tool/target).
  - RESPONSE interception point: guardrail runs with source=OUTPUT before the response returns
    to the caller (masks PII coming back from the target).

Interceptor contract (per AgentCore Gateway docs / CDK LambdaInterceptor):
  event  = {"mcp": {"gatewayRequest": {...}}}   for REQUEST
           {"mcp": {"gatewayRequest": {...},     for RESPONSE: the original request
                    "gatewayResponse": {...}}}   rides along with the response
  return = {"interceptorOutputVersion": "1.0",
            "mcp": {"transformedGatewayRequest": {...}}}   for REQUEST
           {"interceptorOutputVersion": "1.0",
            "mcp": {"transformedGatewayResponse": {...}}}  for RESPONSE

Extensions (platform.yaml gateway.interceptor_extensions) run first, on REQUEST only, each
for the tools/call targets it declares. An extension can only allow or refuse: it gets a copy
of the request body (and the headers, only if it declared it needs them), and anything it
returns other than None (allow) or a reason string (refuse) refuses the call and is otherwise
discarded. An extension that raises, or failed to load, refuses calls to its own targets only.

Each call logs one line: the point, method, tool, request id, verdict and reason. Never the
body, its arguments or the headers, because they hold what the guardrail exists to mask.

NOTE: the exact nested shape of gatewayRequest/gatewayResponse depends on the target type and
is treated generically here (all string leaves are scanned). Validate against live Gateway
traces before relying on it in production; the guardrail logic itself is target-agnostic.
"""

import copy
import importlib
import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID", "")
_GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION", "DRAFT")
_OUTPUT_VERSION = "1.0"
# security.egress_unscanned_responses: tools whose responses are content this deployment
# authored, not scanned. Their requests are.
_SKIP_TOOLS = tuple(json.loads(os.environ.get("EGRESS_SKIP_TOOLS", "[]")))
# security.egress_request_action: "mask" rewrites what the guardrail anonymizes in a
# tools/call request; "block" refuses the call instead, so the target never gets a
# body other than the one an extension approved.
_REQUEST_ACTION = os.environ.get("EGRESS_REQUEST_ACTION", "mask")

_bedrock = boto3.client("bedrock-runtime")


def _load_extensions() -> list[tuple[dict, object]]:
    """(spec, check) per configured extension; check is None when it failed to load,
    so its own targets fail closed and every other target is unaffected."""
    loaded = []
    for spec in json.loads(os.environ.get("INTERCEPTOR_EXTENSIONS", "[]")):
        try:
            check = importlib.import_module(f"extensions.{spec['package']}").check
        except Exception:  # a broken extension must not stop the others
            logger.exception("interceptor extension %s failed to load", spec["package"])
            check = None
        loaded.append((spec, check))
    return loaded


_EXTENSIONS = _load_extensions()


def _log_verdict(point: str, body, verdict: str, reason: str | None = None) -> None:
    body = body if isinstance(body, dict) else {}
    params = body.get("params") if isinstance(body.get("params"), dict) else {}
    line = {
        "point": point,
        "method": body.get("method"),
        "tool": params.get("name"),
        "request_id": body.get("id"),
        "verdict": verdict,
        "reason": reason,
    }
    level = logging.INFO if verdict in ("allowed", "passthrough") else logging.WARNING
    logger.log(level, "interceptor %s", json.dumps(line, default=str))


def _refusal(request_id, reason: str) -> dict:
    """Answer the call at REQUEST time with a tool error, so the target is never called."""
    return {
        "interceptorOutputVersion": _OUTPUT_VERSION,
        "mcp": {
            "transformedGatewayResponse": {
                "statusCode": 200,
                "body": {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "isError": True,
                        "content": [
                            {
                                "type": "text",
                                "text": f"Refused by the Gateway: {reason}",
                            }
                        ],
                    },
                },
            }
        },
    }


def _run_extensions(request: dict):
    """A refusal response from the first extension that refuses, else None."""
    body = request.get("body") or {}
    if not _EXTENSIONS:
        return None
    # A batch array or a non-object params names no single target, so no extension
    # could decide it: refuse it here rather than raise for every tenant's call.
    if not isinstance(body, dict):
        _log_verdict("REQUEST", body, "refused", "malformed body")
        return _refusal(None, "the request is not a single JSON-RPC call")
    if body.get("method") != "tools/call":
        return None
    params = body.get("params") or {}
    if not isinstance(params, dict):
        _log_verdict("REQUEST", body, "refused", "malformed params")
        return _refusal(body.get("id"), "the call's params are not an object")
    target = str(params.get("name") or "").split("___", 1)[0]
    for spec, check in _EXTENSIONS:
        if not any(target.startswith(p) for p in spec["target_prefixes"]):
            continue
        headers = (
            copy.deepcopy(request.get("headers") or {})
            if spec.get("request_headers")
            else None
        )
        try:
            if check is None:
                raise RuntimeError("extension failed to load")
            verdict = check(copy.deepcopy(body), headers)
        except Exception as exc:  # noqa: BLE001 - fail closed, own targets only
            _log_verdict(
                "REQUEST",
                body,
                "refused",
                f"extension {spec['package']} failed: {type(exc).__name__}",
            )
            return _refusal(body.get("id"), "the call could not be authorized")
        if verdict is None:
            continue
        reason = (
            verdict if isinstance(verdict, str) else "the call could not be authorized"
        )
        _log_verdict("REQUEST", body, "refused", f"{spec['package']}: {reason}")
        return _refusal(body.get("id"), reason)
    return None


class GuardrailBlocked(Exception):
    """Raised when the guardrail hard-blocks content (e.g. prompt injection, blocked PII)."""


# Characters per ApplyGuardrail call: 50 text units of 1,000 characters. The default
# quotas cap one request at 1,000 units for the content and sensitive-information
# policies and 200 for a classic-tier topic policy, and a second at 200 content units.
_CHUNK_CHARS = 50_000
# Text units per call. A content block bills at least one unit however short, so a
# request's one-block-per-value batch is held to this count as well as to the
# characters: a call with thousands of short values would otherwise pass the quota.
_UNIT_CHARS = 1_000
_UNITS_PER_CALL = _CHUNK_CHARS // _UNIT_CHARS

# Every list an assessment reports a per-item action in.
_POLICY_ITEMS = (
    ("contentPolicy", "filters"),
    ("topicPolicy", "topics"),
    ("wordPolicy", "customWords"),
    ("wordPolicy", "managedWordLists"),
    ("sensitiveInformationPolicy", "piiEntities"),
    ("sensitiveInformationPolicy", "regexes"),
)


def _apply_guardrail(texts: list[str], source: str) -> dict[str, str]:
    """One ApplyGuardrail call over several texts. Returns {text: masked} for the
    texts it anonymized, or raises GuardrailBlocked.

    source is "INPUT" (REQUEST) or "OUTPUT" (RESPONSE). A request's few short values
    go as a block each: joined one per line, the detector read a purchase order
    number beside its field names as a card number (seen live, reproduced with
    ApplyGuardrail), and refused the read. A response's many values go as one block,
    one per line, which is several times faster.
    The response cannot say which text an output came from, so the masks are
    applied here from the matches the assessments report, and a match found in no
    single text refuses the payload rather than letting the value through.
    """
    resp = _bedrock.apply_guardrail(
        guardrailIdentifier=_GUARDRAIL_ID,
        guardrailVersion=_GUARDRAIL_VERSION,
        source=source,
        content=(
            [{"text": {"text": t}} for t in texts]
            if source == "INPUT"
            else [{"text": {"text": "\n".join(texts)}}]
        ),
    )
    if resp.get("action") != "GUARDRAIL_INTERVENED":
        return {}
    masks = []
    for assessment in resp.get("assessments", []):
        for policy, key in _POLICY_ITEMS:
            for item in assessment.get(policy, {}).get(key, []):
                label = item.get("type") or item.get("name") or policy
                if item.get("action") == "BLOCKED":
                    raise GuardrailBlocked(f"{policy} {label} blocked")
                if item.get("action") == "ANONYMIZED" and item.get("match"):
                    masks.append((item["match"], "{" + label + "}"))
    if any(not any(m in t for t in texts) for m, _ in masks):
        raise GuardrailBlocked("an anonymized value could not be located")
    masked = {}
    for t in texts:
        out = t
        for match, tag in masks:
            out = out.replace(match, tag)
        if out != t:
            masked[t] = out
    return masked


#: MCP's protocol metadata, not tool data: the agent's SDK puts its OpenTelemetry
#: `traceparent` here, and a random trace id can hold a digit run the guardrail reads
#: as a card number, which used to block every call of that run. Only the protocol's
#: own places are skipped, `params._meta` on a request and `result._meta` on a
#: response: a `_meta` key inside a tool's arguments or a tool's result is tool data,
#: and is scanned like any other.
_META = "_meta"
_META_HOLDERS = ("params", "result")


def _protocol_meta(node) -> set[int]:
    """The ids of the objects whose `_meta` is the protocol's own."""
    if not isinstance(node, dict):
        return set()
    return {id(node[k]) for k in _META_HOLDERS if isinstance(node.get(k), dict)}


def _strings(node, found: dict, skip: set[int]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if not (k == _META and id(node) in skip):
                _strings(v, found, skip)
    elif isinstance(node, list):
        for v in node:
            _strings(v, found, skip)
    elif isinstance(node, str) and node.strip():
        found[node] = None


def _replace(node, masked: dict, skip: set[int]):
    if isinstance(node, dict):
        return {
            k: v if (k == _META and id(node) in skip) else _replace(v, masked, skip)
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [_replace(v, masked, skip) for v in node]
    if isinstance(node, str):
        return masked.get(node, node)
    return node


def _units(text: str) -> int:
    return max(1, -(-len(text) // _UNIT_CHARS))


def _mask_tree(node, source: str):
    """Apply the guardrail to every string leaf of a request/response object, in as
    few calls as _CHUNK_CHARS and _UNITS_PER_CALL allow. Raises GuardrailBlocked."""
    if not _GUARDRAIL_ID:
        return node
    skip = _protocol_meta(node)
    found: dict = {}
    _strings(node, found, skip)
    masked: dict = {}
    batch: list[str] = []
    size = units = 0
    for text in found:
        # A request bills each value as its own block; a response, one joined block.
        cost = _units(text) if source == "INPUT" else 0
        if batch and (
            size + len(text) + 1 > _CHUNK_CHARS or units + cost > _UNITS_PER_CALL
        ):
            masked |= _apply_guardrail(batch, source)
            batch, size, units = [], 0, 0
        batch.append(text)
        size += len(text) + 1
        units += cost
    if batch:
        masked |= _apply_guardrail(batch, source)
    return _replace(node, masked, skip) if masked else node


def _unscanned(exc: Exception) -> str:
    """The logged reason when the guardrail could not be applied at all (a throttle, a
    quota, a malformed body). The call is refused rather than left to whatever the
    Gateway does with an interceptor that raises."""
    return f"the egress guardrail could not be applied: {type(exc).__name__}"


def _blocked_response(point: str, body, reason: str):
    """Answer with a tool error instead of the real request/response. At REQUEST this is
    the same answer an extension's refusal gives, so the target is never called."""
    _log_verdict(point, body, "blocked", reason)
    request_id = body.get("id") if isinstance(body, dict) else None
    return _refusal(request_id, "blocked by the egress guardrail")


def _response_out(response, body) -> dict:
    """The documented RESPONSE output: `statusCode` and `body` only. Echoing the rest of
    `gatewayResponse` (its headers) is refused as "Received invalid response from
    interceptor", as echoing `path` and `httpMethod` is on REQUEST (seen live).
    https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-interceptors-types.html
    """
    status = response.get("statusCode", 200) if isinstance(response, dict) else 200
    return {"statusCode": status, "body": body}


def _skipped_tool(mcp: dict) -> str | None:
    """The tool the answered call names, when its response is not scanned."""
    request = mcp.get("gatewayRequest")
    body = request.get("body") if isinstance(request, dict) else None
    params = body.get("params") if isinstance(body, dict) else None
    name = params.get("name") if isinstance(params, dict) else None
    if _SKIP_TOOLS and isinstance(name, str) and name.startswith(_SKIP_TOOLS):
        return name
    return None


def _is_tool_list(mcp: dict) -> bool:
    """Whether the answered call was `tools/list`, read from the request the RESPONSE
    event carries. A response alone cannot say: any target may put a `tools` key in
    a tool call's result, and that answer must be scanned."""
    request = mcp.get("gatewayRequest")
    body = request.get("body") if isinstance(request, dict) else None
    return isinstance(body, dict) and body.get("method") == "tools/list"


def _is_tool_call(body) -> bool:
    return isinstance(body, dict) and body.get("method") == "tools/call"


def _unknown_method(mcp: dict):
    """The JSON-RPC method of a payload of an unrecognized shape, if any body in it names one."""
    for v in mcp.values():
        if isinstance(v, dict) and isinstance(v.get("body"), dict):
            return v["body"].get("method")
    return None


def handler(event, context):
    mcp = event.get("mcp", {}) or {}

    # A RESPONSE event carries the request too, and a live REQUEST event carries a
    # `gatewayResponse` key as well (seen live), so only a response with a body is a
    # RESPONSE. Anything else that has a request is a REQUEST: the checks run, failing
    # closed, rather than a request passing as a response unchecked.
    gateway_response = mcp.get("gatewayResponse")
    is_response = (
        isinstance(gateway_response, dict) and gateway_response.get("body") is not None
    )
    if "gatewayRequest" in mcp and not is_response:
        request = mcp["gatewayRequest"]
        body = request.get("body")
        refusal = _run_extensions(request)
        if refusal is not None:
            return refusal
        # Forward the body alone. The live request also carries path,
        # httpMethod, a null context and (for extensions) the headers, and the
        # Gateway rejects a transformed request that echoes them ("Received
        # invalid response from interceptor"). Headers are never scanned.
        try:
            masked = _mask_tree(body, "INPUT")
            if _REQUEST_ACTION == "block" and _is_tool_call(body) and masked != body:
                _log_verdict("REQUEST", body, "refused", "request would be rewritten")
                return _refusal(
                    body.get("id"),
                    "the egress guardrail would rewrite this request, and a request "
                    "is refused rather than rewritten",
                )
            transformed = {"body": masked}
        except GuardrailBlocked as exc:
            return _blocked_response("REQUEST", body, str(exc))
        except Exception as exc:  # noqa: BLE001 - an unscanned call never goes through
            return _blocked_response("REQUEST", body, _unscanned(exc))
        _log_verdict("REQUEST", body, "allowed")
        return {
            "interceptorOutputVersion": _OUTPUT_VERSION,
            "mcp": {"transformedGatewayRequest": transformed},
        }

    if is_response:
        response = mcp["gatewayResponse"]
        body = response.get("body") if isinstance(response, dict) else None
        # A tool list is the targets' own definitions, not data, and scanning it
        # costs one ApplyGuardrail call per string leaf: hundreds per SAP target.
        result = body.get("result") if isinstance(body, dict) else None
        skipped = _skipped_tool(mcp)
        if skipped or (_is_tool_list(mcp) and isinstance(result, dict)):
            reason = f"egress_unscanned_responses: {skipped}" if skipped else None
            _log_verdict("RESPONSE", body, "passthrough", reason)
            return {
                "interceptorOutputVersion": _OUTPUT_VERSION,
                "mcp": {"transformedGatewayResponse": _response_out(response, body)},
            }
        try:
            # The body only: the headers carry the caller's bearer token.
            transformed = _response_out(response, _mask_tree(body, "OUTPUT"))
        except GuardrailBlocked as exc:
            return _blocked_response("RESPONSE", body, str(exc))
        except Exception as exc:  # noqa: BLE001 - an unscanned answer never goes back
            return _blocked_response("RESPONSE", body, _unscanned(exc))
        _log_verdict("RESPONSE", body, "allowed")
        return {
            "interceptorOutputVersion": _OUTPUT_VERSION,
            "mcp": {"transformedGatewayResponse": transformed},
        }

    # Unknown interception payload. With a guardrail configured it passes only when it
    # names a method other than tools/call: a call that cannot be read cannot be
    # scanned, so it is refused.
    if _GUARDRAIL_ID and _unknown_method(mcp) in (None, "tools/call"):
        _log_verdict("UNKNOWN", None, "refused", "unrecognized tools/call payload")
        return _refusal(
            None, "the request's shape is not recognized, so it was not scanned"
        )
    _log_verdict("UNKNOWN", None, "passthrough")
    return {"interceptorOutputVersion": _OUTPUT_VERSION, "mcp": mcp}
