# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Use-case checks in the Gateway interceptor: allow or deny, own targets only.

gateway.interceptor_extensions lists check modules the platform bundles into
its interceptor and runs before the guardrail. Four guards, because the
interceptor sits in front of every use case's tools:

1. an extension can only allow or deny — what it returns or mutates is never
   forwarded;
2. it runs only on the target prefixes it declares;
3. request headers reach only an extension that declared it needs them;
4. one that raises (or fails to load) refuses its own targets, never another's.
"""

from __future__ import annotations

import ast
import base64
import copy
import importlib.util
import json
import os
import re
import shutil
import sys
import textwrap
import uuid
from pathlib import Path

import aws_cdk as cdk
import pytest
import yaml
from aws_cdk.assertions import Template

from infra_utils.platform_config import PlatformConfig, discover_use_cases
from stacks.gateway_stack import GatewayStack

REPO = Path(__file__).resolve().parents[1]
HANDLER = REPO / "tools" / "egress_interceptor" / "handler.py"

EXTENSIONS = {
    # Refuses everything it governs.
    "deny_all": "def check(body, headers):\n    return 'not on my targets'\n",
    # Records what it was given, then allows.
    "recorder": textwrap.dedent(
        """
        SEEN = []
        def check(body, headers):
            SEEN.append(headers)
            return None
        """
    ),
    # Tries to rewrite the request in place and return a transformation.
    "rewriter": textwrap.dedent(
        """
        def check(body, headers):
            body["params"]["arguments"]["amount"] = 0
            return {"body": body}
        """
    ),
    # Mutates in place and allows: the mutation must not leak.
    "sneaky": textwrap.dedent(
        """
        def check(body, headers):
            body["params"]["arguments"]["amount"] = 0
            return None
        """
    ),
    "raiser": "def check(body, headers):\n    raise RuntimeError('table missing')\n",
    "broken": "raise ImportError('missing dependency')\n",
}


def _load(tmp_path, monkeypatch, specs: list[dict]):
    """Import a fresh handler with these extensions bundled beside it."""
    root = tmp_path / uuid.uuid4().hex
    (root / "extensions").mkdir(parents=True)
    (root / "extensions" / "__init__.py").write_text("")
    for spec in specs:
        pkg = root / "extensions" / spec["package"]
        pkg.mkdir()
        (pkg / "__init__.py").write_text(EXTENSIONS[spec["package"]])
    shutil.copy(HANDLER, root / "handler.py")
    return _import(root, monkeypatch, specs)


def _import(root: Path, monkeypatch, specs: list[dict]):
    """Import the handler in `root`, with `extensions/` beside it."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("INTERCEPTOR_EXTENSIONS", json.dumps(specs))
    monkeypatch.syspath_prepend(str(root))
    for name in [
        m for m in sys.modules if m == "extensions" or m.startswith("extensions.")
    ]:
        monkeypatch.delitem(sys.modules, name)
    spec = importlib.util.spec_from_file_location(
        f"handler_{root.name}", root / "handler.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ext(package, prefixes=("tenant-a",), headers=False) -> dict:
    return {
        "package": package,
        "target_prefixes": list(prefixes),
        "request_headers": headers,
    }


def _call(tool: str, method: str = "tools/call") -> dict:
    return {
        "mcp": {
            "gatewayRequest": {
                "headers": {"Authorization": "Bearer abc.def.ghi", "x-run": "r1"},
                "body": {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": method,
                    "params": {"name": tool, "arguments": {"amount": 100}},
                },
            }
        }
    }


def _refused(out: dict) -> bool:
    response = out["mcp"].get("transformedGatewayResponse")
    return bool(response and response["body"]["result"]["isError"])


def _forwarded(out: dict) -> dict:
    return out["mcp"]["transformedGatewayRequest"]


# 1. allow or deny only


def test_a_returned_transformation_is_discarded_and_refuses(tmp_path, monkeypatch):
    handler = _load(tmp_path, monkeypatch, [_ext("rewriter")])
    out = handler.handler(_call("tenant-a___post"), None)
    assert _refused(out)
    assert "transformedGatewayRequest" not in out["mcp"]


def test_an_in_place_mutation_never_reaches_the_target(tmp_path, monkeypatch):
    handler = _load(tmp_path, monkeypatch, [_ext("sneaky")])
    out = handler.handler(_call("tenant-a___post"), None)
    assert _forwarded(out)["body"]["params"]["arguments"]["amount"] == 100


def test_a_refusal_carries_the_reason_and_the_request_id(tmp_path, monkeypatch):
    handler = _load(tmp_path, monkeypatch, [_ext("deny_all")])
    out = handler.handler(_call("tenant-a___post"), None)
    body = out["mcp"]["transformedGatewayResponse"]["body"]
    assert body["id"] == 7
    assert "not on my targets" in body["result"]["content"][0]["text"]


def test_a_check_on_the_run_grant_library_bundles_and_gates(tmp_path, monkeypatch):
    # The fixture check reaches sap_kit/run_grant.py through a link in its package;
    # the bundle must carry the file itself, or the check fails to load.
    import hashlib

    path = "layers/sap/tests/fixtures/vendor-lookup/vendor_lookup_check"
    root = Path(
        GatewayStack._bundle_extensions("tools/egress_interceptor", [{"path": path}])
    )
    try:
        assert not (root / "extensions/vendor_lookup_check/run_grant.py").is_symlink()
        handler = _import(
            root, monkeypatch, [_ext("vendor_lookup_check", ("vendor-lookup-",), True)]
        )
    finally:
        shutil.rmtree(root)
    check = sys.modules["extensions.vendor_lookup_check"]
    secret = "s3cret"

    class Table:
        def get_item(self, Key, ConsistentRead):
            assert ConsistentRead
            return {
                "Item": {
                    "run_id": "r1",
                    "client_id": "app",
                    "expires_at": 2**40,
                    "secret_sha256": hashlib.sha256(secret.encode()).hexdigest(),
                }
            }

    monkeypatch.setattr(check, "TABLE", Table())

    def call(tool, **headers):
        event = _call(tool)
        event["mcp"]["gatewayRequest"]["headers"] = {
            "Authorization": "Bearer h.eyJjbGllbnRfaWQiOiJhcHAifQ.s",  # client_id app
            **headers,
        }
        return handler.handler(event, None)

    run = {"x-audit-correlation-id": "r1"}
    assert not _refused(call("vendor-lookup-tools___lookup"))  # not gated
    assert _refused(call(check.GATED))  # no run
    assert _refused(call(check.GATED, **run))  # the run id alone
    assert _refused(call(check.GATED, **run, **{check.GRANT_HEADER: "other"}))
    assert not _refused(call(check.GATED, **run, **{check.GRANT_HEADER: secret}))


# 2. own target prefixes only


def test_an_extension_runs_only_on_its_own_targets(tmp_path, monkeypatch):
    handler = _load(tmp_path, monkeypatch, [_ext("deny_all")])
    assert _refused(handler.handler(_call("tenant-a-orders___post"), None))
    other = handler.handler(_call("tenant-b___post"), None)
    assert _forwarded(other)["body"]["params"]["name"] == "tenant-b___post"
    # The prefix matches the target name, not a tool that happens to contain it.
    assert not _refused(handler.handler(_call("web-search___tenant-a"), None))


def test_non_call_methods_pass(tmp_path, monkeypatch):
    handler = _load(tmp_path, monkeypatch, [_ext("deny_all")])
    assert not _refused(handler.handler(_call("tenant-a___x", "tools/list"), None))


# 3. headers only on request


def test_headers_reach_only_an_extension_that_declared_them(tmp_path, monkeypatch):
    handler = _load(
        tmp_path,
        monkeypatch,
        [_ext("recorder", headers=True), _ext("sneaky", prefixes=("other",))],
    )
    recorder = sys.modules["extensions.recorder"]
    handler.handler(_call("tenant-a___post"), None)
    assert recorder.SEEN == [{"Authorization": "Bearer abc.def.ghi", "x-run": "r1"}]

    handler = _load(tmp_path, monkeypatch, [_ext("recorder", headers=False)])
    handler.handler(_call("tenant-a___post"), None)
    assert sys.modules["extensions.recorder"].SEEN == [None]


def test_headers_are_never_forwarded_or_logged(tmp_path, monkeypatch, caplog):
    handler = _load(tmp_path, monkeypatch, [_ext("recorder", headers=True)])
    with caplog.at_level("INFO"):
        out = handler.handler(_call("tenant-a___post"), None)
    assert "headers" not in _forwarded(out)
    assert "abc.def.ghi" not in caplog.text


def test_each_call_logs_its_verdict_and_reason_but_never_its_arguments(
    tmp_path, monkeypatch, caplog
):
    handler = _load(
        tmp_path, monkeypatch, [_ext("deny_all"), _ext("raiser", prefixes=("t-c",))]
    )
    with caplog.at_level("INFO"):
        for tool in ("tenant-a___post", "tenant-b___post", "t-c___post"):
            handler.handler(_call(tool), None)
    lines = [
        json.loads(r.getMessage().removeprefix("interceptor "))
        for r in caplog.records
        if r.getMessage().startswith("interceptor {")
    ]
    assert [(x["tool"], x["verdict"]) for x in lines] == [
        ("tenant-a___post", "refused"),
        ("tenant-b___post", "allowed"),
        ("t-c___post", "refused"),
    ]
    assert lines[0]["reason"] == "deny_all: not on my targets"
    assert lines[2]["reason"] == "extension raiser failed: RuntimeError"
    assert {x["request_id"] for x in lines} == {7}
    assert {x["method"] for x in lines} == {"tools/call"}
    assert "amount" not in caplog.text


# 4. fail closed for own targets only


def test_a_raising_extension_refuses_only_its_own_targets(tmp_path, monkeypatch):
    handler = _load(tmp_path, monkeypatch, [_ext("raiser")])
    assert _refused(handler.handler(_call("tenant-a___post"), None))
    assert not _refused(handler.handler(_call("tenant-b___post"), None))


def test_an_extension_that_fails_to_load_refuses_only_its_own_targets(
    tmp_path, monkeypatch
):
    handler = _load(
        tmp_path,
        monkeypatch,
        [_ext("broken"), _ext("recorder", prefixes=("tenant-b",))],
    )
    assert _refused(handler.handler(_call("tenant-a___post"), None))
    assert not _refused(handler.handler(_call("tenant-b___post"), None))


@pytest.mark.parametrize(
    "body",
    [
        [{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}}],
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": ["tenant-a"]},
    ],
    ids=["batch", "params-not-an-object"],
)
def test_a_malformed_body_is_refused_not_raised(tmp_path, monkeypatch, body):
    handler = _load(tmp_path, monkeypatch, [_ext("recorder", prefixes=("other",))])
    out = handler.handler({"mcp": {"gatewayRequest": {"body": body}}}, None)
    assert _refused(out)
    assert sys.modules["extensions.recorder"].SEEN == []


@pytest.mark.parametrize("point", ["gatewayRequest", "gatewayResponse"])
def test_a_guardrail_block_answers_with_a_tool_error(tmp_path, monkeypatch, point):
    handler = _load(tmp_path, monkeypatch, [])

    def block(text, source):
        raise handler.GuardrailBlocked("content filter PROMPT_ATTACK blocked")

    monkeypatch.setattr(handler, "_apply_guardrail", block)
    monkeypatch.setattr(handler, "_GUARDRAIL_ID", "gr-test")
    body = _call("tenant-a___post")["mcp"]["gatewayRequest"]["body"]
    out = handler.handler({"mcp": {point: {"statusCode": 200, "body": body}}}, None)
    assert _refused(out)
    assert "transformedGatewayRequest" not in out["mcp"]
    assert out["mcp"]["transformedGatewayResponse"]["body"]["id"] == 7


def test_a_tool_list_is_not_scanned(tmp_path, monkeypatch):
    handler = _load(tmp_path, monkeypatch, [])

    def scanned(text, source):
        raise AssertionError("a tool list reached the guardrail")

    monkeypatch.setattr(handler, "_apply_guardrail", scanned)
    monkeypatch.setattr(handler, "_GUARDRAIL_ID", "gr-test")
    response = {
        "statusCode": 200,
        "body": {"id": 1, "result": {"tools": [{"name": "a"}]}},
    }
    request = {"body": {"id": 1, "method": "tools/list"}}
    event = {"mcp": {"gatewayRequest": request, "gatewayResponse": response}}
    out = handler.handler(event, None)
    assert out["mcp"]["transformedGatewayResponse"] == response


def test_a_tool_calls_answer_with_a_tools_key_is_still_scanned(tmp_path, monkeypatch):
    # The exemption is the request's method, never the response's shape: any target
    # may put a `tools` key in a tool call's result.
    handler = _load(tmp_path, monkeypatch, [])
    seen = []
    monkeypatch.setattr(
        handler, "_apply_guardrail", lambda texts, source: seen.extend(texts) or {}
    )
    monkeypatch.setattr(handler, "_GUARDRAIL_ID", "gr-test")
    event = _documented_response(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "t"}},
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "text", "text": "123-45-6789"}],
                "tools": [],
            },
        },
    )
    handler.handler(event, None)
    assert "123-45-6789" in seen


def _documented_response(request_body: dict, response_body: dict) -> dict:
    """A RESPONSE event as the Gateway sends it: the original request rides along.
    https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-interceptors-types.html
    (Response interceptor input payload example)."""
    return {
        "interceptorInputVersion": "1.0",
        "mcp": {
            "rawGatewayRequest": {"body": json.dumps(request_body)},
            "gatewayRequest": {
                "path": "/mcp",
                "httpMethod": "POST",
                "headers": {"Authorization": "Bearer abc.def.ghi"},
                "body": request_body,
            },
            "gatewayResponse": {
                "statusCode": 200,
                "headers": {"Mcp-Session-Id": "s1"},
                "body": response_body,
            },
        },
    }


def test_a_documented_tool_list_response_comes_back_whole(tmp_path, monkeypatch):
    handler = _load(tmp_path, monkeypatch, [_ext("deny_all", prefixes=("",))])

    def scanned(texts, source):
        raise AssertionError("a tool list reached the guardrail")

    monkeypatch.setattr(handler, "_apply_guardrail", scanned)
    monkeypatch.setattr(handler, "_GUARDRAIL_ID", "gr-test")
    event = _documented_response(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "a___b"}]}},
    )
    out = handler.handler(event, None)
    assert out["interceptorOutputVersion"] == "1.0"
    sent = event["mcp"]["gatewayResponse"]
    # The documented output is statusCode and body; the Gateway refuses echoed headers.
    assert out["mcp"] == {
        "transformedGatewayResponse": {
            "statusCode": sent["statusCode"],
            "body": sent["body"],
        }
    }


def test_a_documented_tool_call_response_is_masked_as_output(tmp_path, monkeypatch):
    handler = _load(tmp_path, monkeypatch, [_ext("recorder")])
    sources = []

    def guardrail(texts, source):
        sources.append(source)
        return {t: t.replace("ap@corp.example", "{EMAIL}") for t in texts if "@" in t}

    monkeypatch.setattr(handler, "_apply_guardrail", guardrail)
    monkeypatch.setattr(handler, "_GUARDRAIL_ID", "gr-test")
    request = _call("tenant-a___post")["mcp"]["gatewayRequest"]["body"]
    response = _response("Supplier contact ap@corp.example")["body"]
    out = handler.handler(_documented_response(request, response), None)
    sent = out["mcp"]["transformedGatewayResponse"]
    assert "transformedGatewayRequest" not in out["mcp"]
    assert sent["statusCode"] == 200
    assert sent["body"]["result"]["content"][0]["text"] == "Supplier contact {EMAIL}"
    assert sources == ["OUTPUT"]
    # Extensions decide requests; the response of an allowed call is not re-judged.
    assert sys.modules["extensions.recorder"].SEEN == []


SKIP = ["tenant-a-sops___load_sop", "tenant-a-knowledge___"]


@pytest.mark.parametrize(
    "tool", ["tenant-a-sops___load_sop", "tenant-a-knowledge___search_sops"]
)
def test_a_skipped_tools_response_comes_back_whole(tmp_path, monkeypatch, tool):
    monkeypatch.setenv("EGRESS_SKIP_TOOLS", json.dumps(SKIP))
    handler = _load(tmp_path, monkeypatch, [])

    def scanned(texts, source):
        raise AssertionError("a skipped tool's response reached the guardrail")

    monkeypatch.setattr(handler, "_apply_guardrail", scanned)
    monkeypatch.setattr(handler, "_GUARDRAIL_ID", "gr-test")
    request = _call(tool)["mcp"]["gatewayRequest"]["body"]
    response = _response("Post to G/L account 2010131")["body"]
    out = handler.handler(_documented_response(request, response), None)
    assert out["mcp"] == {
        "transformedGatewayResponse": {"statusCode": 200, "body": response}
    }


@pytest.mark.parametrize(
    "tool",
    ["tenant-a-sops___load_sop_v2", "tenant-a-sops___other", "sap-s4___odata_read"],
)
def test_a_tool_not_listed_is_scanned_both_ways(tmp_path, monkeypatch, tool):
    monkeypatch.setenv("EGRESS_SKIP_TOOLS", json.dumps(SKIP[1:]))
    handler = _load(tmp_path, monkeypatch, [])
    sources = []
    monkeypatch.setattr(
        handler, "_apply_guardrail", lambda t, s: sources.append(s) or {}
    )
    monkeypatch.setattr(handler, "_GUARDRAIL_ID", "gr-test")
    event = _call(tool)
    handler.handler(event, None)
    request = event["mcp"]["gatewayRequest"]["body"]
    handler.handler(_documented_response(request, _response("x")["body"]), None)
    assert sources == ["INPUT", "OUTPUT"]


def test_a_skipped_tools_request_is_still_scanned(tmp_path, monkeypatch):
    monkeypatch.setenv("EGRESS_SKIP_TOOLS", json.dumps(SKIP))
    handler = _load(tmp_path, monkeypatch, [])
    sources = []
    monkeypatch.setattr(
        handler, "_apply_guardrail", lambda t, s: sources.append(s) or {}
    )
    monkeypatch.setattr(handler, "_GUARDRAIL_ID", "gr-test")
    handler.handler(_call("tenant-a-sops___load_sop"), None)
    assert sources == ["INPUT"]


@pytest.mark.parametrize("entry", ["", "tenant-a", "___load_sop"])
def test_a_skip_entry_must_name_its_target(entry):
    with pytest.raises(ValueError, match="name the target in full"):
        PlatformConfig.model_validate(
            {"security": {"egress_unscanned_responses": [entry]}}
        )


def test_the_unscanned_responses_key_has_no_old_name():
    tools = ["tenant-a-sops___load_sop"]
    c = PlatformConfig.model_validate(
        {"security": {"egress_unscanned_responses": tools}}
    )
    assert c.security.egress_unscanned_responses == tools
    with pytest.raises(ValueError, match="egress_skip_tools"):
        PlatformConfig.model_validate({"security": {"egress_skip_tools": tools}})


class _Guardrail:
    """A stand-in bedrock-runtime client: records each ApplyGuardrail call and
    answers with the assessment `respond` builds from the texts."""

    def __init__(self, respond=lambda texts: {}):
        self.calls: list[list[str]] = []
        self.respond = respond

    def apply_guardrail(self, **kw):
        texts = [line for c in kw["content"] for line in c["text"]["text"].split("\n")]
        self.calls.append(texts)
        assessment = self.respond(texts)
        return {
            "action": "GUARDRAIL_INTERVENED" if assessment else "NONE",
            "assessments": [assessment] if assessment else [],
        }


def _guarded(tmp_path, monkeypatch, respond=lambda texts: {}):
    handler = _load(tmp_path, monkeypatch, [])
    guardrail = _Guardrail(respond)
    monkeypatch.setattr(handler, "_bedrock", guardrail)
    monkeypatch.setattr(handler, "_GUARDRAIL_ID", "gr-test")
    return handler, guardrail


def _pii(type_, match, action):
    return {
        "sensitiveInformationPolicy": {
            "piiEntities": [{"type": type_, "match": match, "action": action}]
        }
    }


def _response(text: str, headers=None) -> dict:
    body = {
        "jsonrpc": "2.0",
        "id": 7,
        "result": {"content": [{"type": "text", "text": text}]},
    }
    return {"statusCode": 200, "headers": headers or {}, "body": body}


def test_every_leaf_of_a_payload_goes_in_one_call(tmp_path, monkeypatch):
    handler, guardrail = _guarded(tmp_path, monkeypatch)
    event = _call("tenant-a___post")
    event["mcp"]["gatewayRequest"]["body"]["params"]["arguments"] = {
        "a": "one",
        "b": ["two", "three", "one"],
    }
    handler.handler(event, None)
    assert len(guardrail.calls) == 1
    assert sorted(guardrail.calls[0]) == sorted(
        ["2.0", "tools/call", "tenant-a___post", "one", "two", "three"]
    )


def test_a_payload_over_the_chunk_size_is_split(tmp_path, monkeypatch):
    handler, guardrail = _guarded(tmp_path, monkeypatch)
    big = handler._CHUNK_CHARS // 2 + 1
    response = _response("a" * big)
    response["body"]["result"]["more"] = ["b" * big, "c" * big]
    handler.handler({"mcp": {"gatewayResponse": response}}, None)
    assert len(guardrail.calls) == 3
    assert all(sum(map(len, c)) <= handler._CHUNK_CHARS for c in guardrail.calls)


def test_a_block_in_any_leaf_refuses_the_whole_response(tmp_path, monkeypatch):
    handler, _ = _guarded(
        tmp_path,
        monkeypatch,
        lambda texts: (
            _pii("US_SOCIAL_SECURITY_NUMBER", "078-05-1120", "BLOCKED")
            if any("078-05-1120" in t for t in texts)
            else {}
        ),
    )
    out = handler.handler(
        {"mcp": {"gatewayResponse": _response("header text SSN 078-05-1120")}}, None
    )
    assert _refused(out)


def test_an_anonymized_value_is_masked_only_where_it_is(tmp_path, monkeypatch):
    handler, _ = _guarded(
        tmp_path,
        monkeypatch,
        lambda texts: _pii("EMAIL", "ap@corp.example", "ANONYMIZED"),
    )
    event = _call("tenant-a___post")
    args = {"to": "ap@corp.example", "subject": "Invoice 5105600123"}
    event["mcp"]["gatewayRequest"]["body"]["params"]["arguments"] = args
    out = handler.handler(event, None)
    sent = _forwarded(out)["body"]["params"]["arguments"]
    assert sent == {"to": "{EMAIL}", "subject": "Invoice 5105600123"}


def test_a_mask_that_cannot_be_placed_refuses(tmp_path, monkeypatch):
    handler, _ = _guarded(
        tmp_path,
        monkeypatch,
        lambda texts: _pii("NAME", "not in the payload", "ANONYMIZED"),
    )
    out = handler.handler({"mcp": {"gatewayResponse": _response("hello")}}, None)
    assert _refused(out)


def test_response_headers_never_reach_the_guardrail(tmp_path, monkeypatch):
    handler, guardrail = _guarded(tmp_path, monkeypatch)
    response = _response("ok", headers={"Authorization": "Bearer abc.def.ghi"})
    out = handler.handler({"mcp": {"gatewayResponse": response}}, None)
    assert not any("Bearer" in t for c in guardrail.calls for t in c)
    assert set(out["mcp"]["transformedGatewayResponse"]) == {"statusCode", "body"}


def test_without_extensions_the_body_is_forwarded_unchanged(tmp_path, monkeypatch):
    handler = _load(tmp_path, monkeypatch, [])
    event = _call("tenant-a___post")
    # The live Gateway request carries these too; echoing them is rejected.
    event["mcp"]["gatewayRequest"] |= {
        "path": "/mcp",
        "httpMethod": "POST",
        "context": None,
    }
    out = handler.handler(json.loads(json.dumps(event)), None)
    assert _forwarded(out) == {"body": event["mcp"]["gatewayRequest"]["body"]}


# Synthesis


def _gateway(tmp_path, **kw) -> dict:
    app = cdk.App()
    stack = GatewayStack(
        app,
        "Gw",
        project_name="proj",
        environment="dev",
        cognito_issuer_url="https://issuer.example.com",
        cognito_allowed_clients=["client"],
        env=cdk.Environment(account="111111111111", region="us-east-1"),
        **kw,
    )
    return Template.from_stack(stack).to_json()


def _package(tmp_path) -> str:
    pkg = tmp_path / "use_case_checks"
    pkg.mkdir(exist_ok=True)
    (pkg / "__init__.py").write_text(EXTENSIONS["deny_all"])
    return str(pkg)


def _interceptor(template) -> dict | None:
    gw = next(
        r
        for r in template["Resources"].values()
        if r["Type"] == "AWS::BedrockAgentCore::Gateway"
    )
    configs = gw["Properties"].get("InterceptorConfigurations")
    return configs[0] if configs else None


def test_no_interceptor_without_filter_or_extensions(tmp_path):
    assert _interceptor(_gateway(tmp_path)) is None


def test_extensions_alone_create_a_request_only_interceptor(tmp_path):
    template = _gateway(
        tmp_path,
        interceptor_extensions=[
            {
                "path": _package(tmp_path),
                "target_prefixes": ["tenant-a"],
                "request_headers": True,
            }
        ],
    )
    config = _interceptor(template)
    assert config["InterceptionPoints"] == ["REQUEST"]
    assert config["InputConfiguration"]["PassRequestHeaders"] is True
    fn = next(
        r
        for r in template["Resources"].values()
        if r["Type"] == "AWS::Lambda::Function"
    )
    env = fn["Properties"]["Environment"]["Variables"]
    assert json.loads(env["INTERCEPTOR_EXTENSIONS"]) == [
        {
            "package": "use_case_checks",
            "target_prefixes": ["tenant-a"],
            "request_headers": True,
        }
    ]
    assert "GUARDRAIL_ID" not in env
    # The bootstrap execution role, resolved in the template, so a check can pin it.
    assert env["CFN_EXEC_ROLE_ARN"] == {
        "Fn::Sub": "arn:${AWS::Partition}:iam::111111111111:role/"
        "cdk-hnb659fds-cfn-exec-role-111111111111-us-east-1"
    }
    names = {
        r["Properties"]["Name"]
        for r in template["Resources"].values()
        if r["Type"] == "AWS::SSM::Parameter"
    }
    assert "/proj/dev/gateway/interceptor-role-arn" in names


def test_headers_stay_off_unless_an_extension_asks(tmp_path):
    template = _gateway(
        tmp_path,
        enable_egress_filter=True,
        interceptor_extensions=[
            {"path": _package(tmp_path), "target_prefixes": ["tenant-a"]}
        ],
    )
    config = _interceptor(template)
    assert config["InterceptionPoints"] == ["REQUEST", "RESPONSE"]
    assert config["InputConfiguration"]["PassRequestHeaders"] is False


def test_egress_filter_alone_is_unchanged(tmp_path):
    template = _gateway(tmp_path, enable_egress_filter=True)
    config = _interceptor(template)
    assert config["InterceptionPoints"] == ["REQUEST", "RESPONSE"]
    assert config["InputConfiguration"]["PassRequestHeaders"] is False
    fn = next(
        r
        for r in template["Resources"].values()
        if r["Type"] == "AWS::Lambda::Function"
    )
    assert set(fn["Properties"]["Environment"]["Variables"]) == {
        "GUARDRAIL_ID",
        "GUARDRAIL_VERSION",
    }


def test_the_skip_list_reaches_the_interceptor(tmp_path):
    template = _gateway(tmp_path, enable_egress_filter=True, egress_skip_tools=SKIP)
    (fn,) = [
        r
        for r in template["Resources"].values()
        if r["Type"] == "AWS::Lambda::Function"
    ]
    env = fn["Properties"]["Environment"]["Variables"]
    assert json.loads(env["EGRESS_SKIP_TOOLS"]) == SKIP


def test_a_directory_without_a_package_fails_the_synth(tmp_path):
    with pytest.raises(ValueError, match="not a Python package"):
        _gateway(
            tmp_path,
            interceptor_extensions=[{"path": str(tmp_path), "target_prefixes": ["x"]}],
        )


# Schema


@pytest.mark.parametrize(
    "extension",
    [
        {"path": "/abs/run_checks", "target_prefixes": ["a"]},
        {"path": "../run_checks", "target_prefixes": ["a"]},
        {"path": "tools/x/my-checks", "target_prefixes": ["a"]},
        {"path": "tools/x/run_checks", "target_prefixes": []},
        {"path": "tools/x/run_checks", "target_prefixes": [""]},
        {"path": "tools/x/run_checks", "target_prefixes": ["*"]},
        {"path": "tools/x/run_checks", "target_prefixes": ["a"], "transform": True},
    ],
)
def test_schema_refuses_unsafe_extensions(extension):
    with pytest.raises(ValueError):
        PlatformConfig.model_validate(
            {"gateway": {"interceptor_extensions": [extension]}}
        )


def test_schema_refuses_colliding_package_names():
    with pytest.raises(ValueError, match="collide"):
        PlatformConfig.model_validate(
            {
                "gateway": {
                    "interceptor_extensions": [
                        {"path": "tools/a/run_checks", "target_prefixes": ["a"]},
                        {"path": "tools/b/run_checks", "target_prefixes": ["b"]},
                    ]
                }
            }
        )


def test_a_request_carrying_an_empty_gateway_response_still_runs_the_checks(
    tmp_path, monkeypatch
):
    # Seen live: REQUEST events carry a `gatewayResponse` key. They must still be checked.
    handler = _load(tmp_path, monkeypatch, [_ext("deny_all", prefixes=("",))])
    for empty in (None, {}, {"statusCode": None}):
        event = {
            "mcp": {
                "gatewayRequest": {
                    "body": {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "any___tool", "arguments": {}},
                    }
                },
                "gatewayResponse": empty,
            }
        }
        out = handler.handler(event, None)
        assert _refused(out), empty


def test_mcp_meta_is_never_scanned_or_changed(tmp_path, monkeypatch):
    # A traceparent whose random id holds a Luhn-valid digit run read as a card number
    # and blocked every call of one live run.
    handler = _load(tmp_path, monkeypatch, [])
    seen = []
    monkeypatch.setattr(
        handler,
        "_apply_guardrail",
        lambda texts, source: seen.extend(texts) or {"100x": "{NAME}"},
    )
    monkeypatch.setattr(handler, "_GUARDRAIL_ID", "gr-test")
    event = _call("tenant-a-sap___odata_read")
    params = event["mcp"]["gatewayRequest"]["body"]["params"]
    params["arguments"] = {"note": "100x"}
    meta = {"traceparent": "00-4532015112830366a1b2c3d4e5f6a7b8-0123456789abcdef-01"}
    params["_meta"] = meta
    out = handler.handler(event, None)
    sent = out["mcp"]["transformedGatewayRequest"]["body"]["params"]
    assert meta["traceparent"] not in seen and "100x" in seen
    assert sent["_meta"] == meta and sent["arguments"] == {"note": "{NAME}"}


def test_a_meta_key_in_tool_data_is_scanned_both_ways(tmp_path, monkeypatch):
    # Only the protocol's params._meta and result._meta are skipped.
    handler = _load(tmp_path, monkeypatch, [])
    seen = []
    monkeypatch.setattr(
        handler, "_apply_guardrail", lambda texts, source: seen.extend(texts) or {}
    )
    monkeypatch.setattr(handler, "_GUARDRAIL_ID", "gr-test")
    event = _call("tenant-a-sap___odata_read")
    params = event["mcp"]["gatewayRequest"]["body"]["params"]
    params["arguments"] = {"_meta": {"note": "4111111111111111"}}
    params["_meta"] = {"traceparent": "00-trace-01"}
    handler.handler(event, None)
    assert "4111111111111111" in seen and "00-trace-01" not in seen
    seen.clear()
    response = _documented_response(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "t"}},
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "_meta": {"progressToken": "p-1"},
                "structuredContent": {"_meta": {"ssn": "123-45-6789"}},
            },
        },
    )
    handler.handler(response, None)
    assert "123-45-6789" in seen and "p-1" not in seen


@pytest.mark.parametrize("point", ["request", "response"])
def test_a_guardrail_that_cannot_be_applied_refuses(tmp_path, monkeypatch, point):
    # A throttle or a quota is not a pass: whatever the Gateway does with a raising
    # interceptor, the call is refused here, with a tool error.
    handler = _load(tmp_path, monkeypatch, [])

    def throttled(texts, source):
        raise RuntimeError("ThrottlingException")

    monkeypatch.setattr(handler, "_apply_guardrail", throttled)
    monkeypatch.setattr(handler, "_GUARDRAIL_ID", "gr-test")
    if point == "request":
        event = _call("tenant-a-sap___odata_read")
        event["mcp"]["gatewayRequest"]["body"]["params"]["arguments"] = {"a": "b"}
        out = handler.handler(event, None)
        answer = out["mcp"]["transformedGatewayResponse"]
    else:
        event = _documented_response(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "t"},
            },
            {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"text": "x"}]}},
        )
        answer = handler.handler(event, None)["mcp"]["transformedGatewayResponse"]
    assert "blocked by the egress guardrail" in json.dumps(answer)


def test_a_request_with_many_values_stays_under_the_units_quota(tmp_path, monkeypatch):
    handler = _load(tmp_path, monkeypatch, [])
    calls = []
    monkeypatch.setattr(
        handler, "_apply_guardrail", lambda texts, source: calls.append(texts) or {}
    )
    monkeypatch.setattr(handler, "_GUARDRAIL_ID", "gr-test")
    event = _call("tenant-a-sap___odata_read")
    values = {f"k{i}": f"v{i}" for i in range(1_200)}
    event["mcp"]["gatewayRequest"]["body"]["params"]["arguments"] = values
    handler.handler(event, None)
    assert all(len(texts) <= handler._UNITS_PER_CALL for texts in calls)
    assert sum(len(texts) for texts in calls) >= len(values)


def test_a_requests_values_reach_the_guardrail_one_block_each(tmp_path, monkeypatch):
    # Joined one per line, a purchase order number beside its field names read as a
    # card number and the read was refused; alone, it does not.
    handler = _load(tmp_path, monkeypatch, [])
    calls = []

    class Bedrock:
        def apply_guardrail(self, **kw):
            calls.append((kw["source"], kw["content"]))
            return {"action": "NONE"}

    monkeypatch.setattr(handler, "_bedrock", Bedrock())
    monkeypatch.setattr(handler, "_GUARDRAIL_ID", "gr-test")
    event = _call("tenant-a-sap___odata_read")
    event["mcp"]["gatewayRequest"]["body"]["params"]["arguments"] = {
        "entity_set": "A_PurchaseOrder",
        "filter": "PurchaseOrder eq '4500004818'",
    }
    handler.handler(event, None)
    source, content = calls[0]
    assert source == "INPUT" and len(content) > 1
    assert {"text": {"text": "PurchaseOrder eq '4500004818'"}} in content
    handler.handler(
        _documented_response(
            event["mcp"]["gatewayRequest"]["body"], _response("a")["body"]
        ),
        None,
    )
    assert len(calls[-1][1]) == 1  # a response stays one joined block


def _block_mode(tmp_path, monkeypatch):
    handler, _ = _guarded(
        tmp_path,
        monkeypatch,
        lambda texts: (
            _pii("CREDIT_DEBIT_CARD_NUMBER", "4111", "ANONYMIZED")
            if any("4111" in t for t in texts)
            else {}
        ),
    )
    monkeypatch.setattr(handler, "_REQUEST_ACTION", "block")
    return handler


def _arguments_call(value: str) -> dict:
    event = _call("tenant-a___t")
    event["mcp"]["gatewayRequest"]["body"]["params"]["arguments"] = {"po": value}
    return event


def test_block_mode_refuses_a_request_it_would_rewrite(tmp_path, monkeypatch, caplog):
    handler = _block_mode(tmp_path, monkeypatch)
    out = handler.handler(_arguments_call("PO 4111"), None)
    assert _refused(out)
    text = out["mcp"]["transformedGatewayResponse"]["body"]["result"]["content"][0][
        "text"
    ]
    assert "refused rather than rewritten" in text
    assert "4111" not in text + caplog.text


def test_block_mode_allows_a_request_it_would_not_rewrite(tmp_path, monkeypatch):
    handler = _block_mode(tmp_path, monkeypatch)
    out = handler.handler(_arguments_call("PO 99"), None)
    assert out["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"] == {
        "po": "PO 99"
    }


def test_mask_mode_still_rewrites_a_request(tmp_path, monkeypatch):
    handler = _block_mode(tmp_path, monkeypatch)
    monkeypatch.setattr(handler, "_REQUEST_ACTION", "mask")
    out = handler.handler(_arguments_call("PO 4111"), None)
    args = out["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"]
    assert args == {"po": "PO {CREDIT_DEBIT_CARD_NUMBER}"}


def test_block_mode_leaves_a_response_masked(tmp_path, monkeypatch):
    handler = _block_mode(tmp_path, monkeypatch)
    event = _call("tenant-a___t")
    event["mcp"]["gatewayResponse"] = _response("PO 4111")
    out = handler.handler(event, None)
    assert "{CREDIT_DEBIT_CARD_NUMBER}" in json.dumps(out)
    assert "isError" not in json.dumps(out)


def test_an_unrecognized_tool_call_shape_is_refused(tmp_path, monkeypatch):
    handler, _ = _guarded(tmp_path, monkeypatch)
    odd = {"mcp": {"somethingNew": {"body": {"method": "tools/call"}}}}
    assert _refused(handler.handler(odd, None))
    assert _refused(handler.handler({"mcp": {}}, None))


def test_an_unrecognized_shape_of_another_method_passes(tmp_path, monkeypatch):
    handler, _ = _guarded(tmp_path, monkeypatch)
    odd = {"mcp": {"somethingNew": {"body": {"method": "tools/list"}}}}
    assert handler.handler(odd, None)["mcp"] == odd["mcp"]


def test_tools_list_is_untouched_in_block_mode(tmp_path, monkeypatch):
    handler = _block_mode(tmp_path, monkeypatch)
    event = _call("", method="tools/list")
    event["mcp"]["gatewayRequest"]["body"]["params"] = {"cursor": "4111"}
    out = handler.handler(event, None)
    assert not _refused(out)


def test_request_action_defaults_to_mask_and_refuses_others():
    assert PlatformConfig.model_validate({}).security.egress_request_action == "mask"
    with pytest.raises(ValueError):
        PlatformConfig.model_validate({"security": {"egress_request_action": "x"}})


# 5. every installed use case's extension leaves other use cases' calls alone


def _installed_extensions() -> list[dict]:
    """Each extension an installed use case ships, from its platform.example.yaml,
    with every prefix any of them gives it. A new drop-in is covered by listing its
    check there, which it must do for an operator to find it."""
    prefixes: dict[str, set[str]] = {}
    for name in discover_use_cases():
        example = REPO / "use-cases" / name / "platform.example.yaml"
        raw = (yaml.safe_load(example.read_text()) if example.exists() else {}) or {}
        for e in (raw.get("gateway") or {}).get("interceptor_extensions") or []:
            prefixes.setdefault(e["path"], set()).update(e["target_prefixes"])
    return [
        {"path": p, "target_prefixes": sorted(v), "request_headers": True}
        for p, v in sorted(prefixes.items())
    ]


def _names_it_mentions(package: Path) -> set[str]:
    """Every identifier-shaped string literal in the package: the tool names a check
    keys on are among them, so a check that matches names alone is caught."""
    return {
        node.value
        for py in package.rglob("*.py")
        for node in ast.walk(ast.parse(py.read_text()))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and re.fullmatch(r"[a-z][a-z0-9_]{2,63}", node.value)
    }


def _no_io(monkeypatch) -> list:
    """Make every AWS, network and deployment-environment read fail, and record it."""
    import socket

    import boto3

    attempts: list = []

    def refuse(*args, **kwargs):
        attempts.append(args[:2])
        raise AssertionError("I/O on another use case's call")

    for owner, attr in (
        (boto3, "client"),
        (boto3, "resource"),
        (boto3.session.Session, "client"),
        (boto3.session.Session, "resource"),
        (socket.socket, "connect"),
        (socket, "create_connection"),
        (socket, "getaddrinfo"),
    ):
        monkeypatch.setattr(owner, attr, refuse)
    for key in [k for k in os.environ if k.startswith("AWS_")] + [
        "SSM_PREFIX",
        "CFN_EXEC_ROLE_ARN",
    ]:
        monkeypatch.delenv(key, raising=False)
    return attempts


def _bearer(claims: dict) -> str:
    def part(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"Bearer {part({'alg': 'none'})}.{part(claims)}.sig"


@pytest.mark.parametrize("ext", _installed_extensions(), ids=lambda e: e["path"])
def test_an_installed_extension_returns_early_for_tools_outside_its_prefixes(
    ext, monkeypatch
):
    # The interceptor calls a check only for its prefixes; the check must not rely on
    # that alone: an operator's broader prefix, or another use case whose name starts
    # like this one's, would hand it calls that are not its own.
    package = os.path.basename(ext["path"].rstrip("/"))
    spec = {**ext, "package": package}
    del spec["path"]
    staged = Path(GatewayStack._bundle_extensions("tools/egress_interceptor", [ext]))
    try:
        handler = _import(staged, monkeypatch, [spec])
        ((_, check),) = handler._EXTENSIONS
        assert check is not None, f"{ext['path']} did not import as the Lambda does"
        names = sorted(_names_it_mentions(staged / "extensions" / package))
    finally:
        shutil.rmtree(staged, ignore_errors=True)
    attempts = _no_io(monkeypatch)
    targets = ["zz-another-use-case"] + [
        f"zz-{p}target" for p in ext["target_prefixes"]
    ]
    assert not any(t.startswith(tuple(ext["target_prefixes"])) for t in targets)
    headers = {
        "Authorization": _bearer({"client_id": "another-app", "azp": "another-app"}),
        "x-audit-correlation-id": "run-1",
    }
    for tool in [f"{t}___{n}" for t in targets for n in names] + names:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": {"case_id": "c1", "amount": 100}},
        }
        before = copy.deepcopy(body)
        assert check(body, copy.deepcopy(headers)) is None, tool
        assert body == before, tool
        assert handler._run_extensions({"headers": headers, "body": body}) is None
    assert attempts == []
