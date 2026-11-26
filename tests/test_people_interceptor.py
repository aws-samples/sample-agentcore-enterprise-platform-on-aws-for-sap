# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The people interceptor (gate 2): the verified person in, the token out.

Tokens are signed here with a test key, so every check runs as deployed: signature,
algorithm, issuer, expiry, token use, client, audience and subject. Each refusal has
its own test, which passes the request through, and so fails, if the check is removed.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

SRC = (
    Path(__file__).resolve().parents[1] / "tools" / "people_interceptor" / "handler.py"
)
ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_pool"
URL = "https://people.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
QUICK, COPILOT = "quickclientid", "copilotclientid"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _Keys:
    def get_signing_key(self, kid):
        if kid != "k1":
            raise jwt.PyJWKClientError("unknown kid")
        return jwt.PyJWK.from_dict(
            json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(_KEY.public_key()))
        )


@pytest.fixture
def ic(monkeypatch):
    spec = importlib.util.spec_from_file_location("people_interceptor_handler", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setenv("ISSUER", ISSUER)
    monkeypatch.setenv("ALLOWED_CLIENTS", f"{QUICK},{COPILOT}")
    monkeypatch.setenv("CLIENT_NAMES", "quick,copilot-studio")
    monkeypatch.setenv("RESOURCE_PARAMETER", "/p/e/people-gateway/url")
    monkeypatch.delenv("CLAIMS_PATH", raising=False)
    monkeypatch.setattr(mod, "_metadata", lambda: {"issuer": ISSUER, "jwks_uri": "x"})
    monkeypatch.setattr(mod, "_jwks", lambda: _Keys())
    monkeypatch.setattr(mod, "resource", lambda: URL)
    return mod


def _claims(**over):
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "sub": "6f1c-sub",
        "username": "pat",
        "client_id": QUICK,
        "token_use": "access",
        "aud": URL,
        "scope": "openid email profile",
        "exp": now + 600,
        "iat": now,
        "cognito:groups": ["business-operations-users", "business-operations-authors"],
    }
    claims.update(over)
    return {k: v for k, v in claims.items() if v is not None}


def _token(key=_KEY, kid="k1", alg="RS256", **over):
    headers = {"kid": kid} if kid else {}
    return jwt.encode(_claims(**over), key, algorithm=alg, headers=headers)


def _event(token=None, method="tools/call", arguments=None, headers=None, **extra):
    body = {"jsonrpc": "2.0", "id": 7, "method": method}
    if method == "tools/call":
        body["params"] = {
            "name": "uc-people___get_pack",
            "arguments": {"pack_id": "p"} if arguments is None else arguments,
        }
    request = {
        "body": body,
        "headers": headers
        if headers is not None
        else ({"Authorization": f"Bearer {token}"} if token else {}),
        "path": "/mcp",
        "httpMethod": "POST",
        "context": None,
    }
    request.update(extra)
    return {"mcp": {"gatewayRequest": request}}


def _refused(result) -> bool:
    body = result["mcp"].get("transformedGatewayResponse", {}).get("body") or {}
    return bool((body.get("result") or {}).get("isError"))


def _forwarded(result) -> dict:
    return result["mcp"]["transformedGatewayRequest"]["body"]


# ── allowed ──


def test_a_verified_call_reaches_the_target_with_the_caller_and_no_token(ic):
    token = _token()
    out = ic.handler(_event(token))
    assert set(out["mcp"]["transformedGatewayRequest"]) == {"body"}
    args = _forwarded(out)["params"]["arguments"]
    assert args["pack_id"] == "p"
    assert args["caller"] == {
        "sub": "6f1c-sub",
        "username": "pat",
        "client": "quick",
        "client_id": QUICK,
        "groups": ["business-operations-users", "business-operations-authors"],
    }
    assert token not in json.dumps(out)


def test_a_client_supplied_caller_is_replaced(ic):
    forged = {"sub": "someone-else", "groups": ["business-operations-admins"]}
    out = ic.handler(_event(_token(), arguments={"pack_id": "p", "caller": forged}))
    caller = _forwarded(out)["params"]["arguments"]["caller"]
    assert caller["sub"] == "6f1c-sub"
    assert "business-operations-admins" not in caller["groups"]


def test_the_people_scope_binds_a_token_without_aud(ic):
    out = ic.handler(_event(_token(aud=None, scope=f"openid {URL}/people")))
    assert not _refused(out)


def test_groups_as_cognitos_bracketed_text_are_read(ic):
    out = ic.handler(_event(_token(**{"cognito:groups": "[a b]"})))
    assert _forwarded(out)["params"]["arguments"]["caller"]["groups"] == ["a", "b"]


def test_other_methods_pass_unchanged_once_verified(ic):
    out = ic.handler(_event(_token(), method="tools/list"))
    assert _forwarded(out) == {"jsonrpc": "2.0", "id": 7, "method": "tools/list"}


def test_the_client_name_is_the_door(ic):
    out = ic.handler(_event(_token(client_id=COPILOT)))
    caller = _forwarded(out)["params"]["arguments"]["caller"]
    assert caller["client"] == "copilot-studio"


# ── refused: the token ──


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer "}, None],
)
def test_no_token_is_refused(ic, headers):
    event = _event(headers=headers or {})
    if headers is None:
        event["mcp"]["gatewayRequest"]["headers"] = "not a dict"
    assert _refused(ic.handler(event))


def test_no_token_is_refused_on_every_method(ic):
    assert _refused(ic.handler(_event(method="tools/list")))
    assert _refused(ic.handler(_event(method="initialize")))


def test_another_keys_signature_is_refused(ic):
    assert _refused(ic.handler(_event(_token(key=_OTHER))))


def test_a_tampered_payload_is_refused(ic):
    head, _, sig = _token().split(".")
    forged = jwt.utils.base64url_encode(
        json.dumps(_claims(sub="admin")).encode()
    ).decode()
    assert _refused(ic.handler(_event(f"{head}.{forged}.{sig}")))


def test_a_symmetric_algorithm_is_refused(ic):
    token = jwt.encode(
        _claims(), "secret" * 6, algorithm="HS256", headers={"kid": "k1"}
    )
    assert _refused(ic.handler(_event(token)))


def test_an_unsigned_token_is_refused(ic):
    token = jwt.encode(_claims(), None, algorithm="none", headers={"kid": "k1"})
    assert _refused(ic.handler(_event(token)))


def test_a_token_without_a_kid_is_refused(ic):
    assert _refused(ic.handler(_event(_token(kid=None))))


def test_an_unknown_kid_is_refused(ic):
    assert _refused(ic.handler(_event(_token(kid="k2"))))


def test_an_expired_token_is_refused(ic):
    assert _refused(ic.handler(_event(_token(exp=int(time.time()) - 120))))


def test_a_minute_of_skew_is_allowed(ic):
    assert not _refused(ic.handler(_event(_token(exp=int(time.time()) - 30))))


def test_another_issuer_is_refused(ic):
    assert _refused(ic.handler(_event(_token(iss="https://evil.example"))))


def test_a_discovery_document_naming_another_issuer_is_refused(ic, monkeypatch):
    monkeypatch.setattr(
        ic, "_metadata", lambda: {"issuer": "https://evil.example", "jwks_uri": "x"}
    )
    assert _refused(ic.handler(_event(_token(iss="https://evil.example"))))


def test_a_malformed_token_is_refused(ic):
    assert _refused(ic.handler(_event("not.a.jwt")))


# ── refused: what the claims say ──


def test_an_id_token_is_refused(ic):
    assert _refused(ic.handler(_event(_token(token_use="id"))))


def test_another_client_is_refused(ic):
    assert _refused(ic.handler(_event(_token(client_id="console-client"))))


def test_an_agents_token_is_refused(ic):
    # An agent's client_credentials token: another client, and no person's groups.
    token = _token(client_id="uc-reader", username=None, **{"cognito:groups": None})
    assert _refused(ic.handler(_event(token)))


def test_a_token_for_another_resource_is_refused(ic):
    other = "https://agent.gateway.example/mcp"
    assert _refused(ic.handler(_event(_token(aud=other))))
    assert _refused(ic.handler(_event(_token(aud=None, scope=f"{other}/people"))))


def test_a_token_naming_no_resource_is_refused(ic):
    assert _refused(ic.handler(_event(_token(aud=None))))


def test_a_token_without_a_subject_is_refused(ic):
    assert _refused(ic.handler(_event(_token(sub=""))))


def test_mismatched_client_lists_admit_no_one(ic, monkeypatch):
    monkeypatch.setenv("CLIENT_NAMES", "quick")
    assert _refused(ic.handler(_event(_token())))


def test_an_unreadable_resource_admits_no_one(ic, monkeypatch):
    def unreadable():
        raise ic.Refused("resource unreadable")

    monkeypatch.setattr(ic, "resource", unreadable)
    assert _refused(ic.handler(_event(_token())))


def test_the_resource_parameter_must_be_https(ic, monkeypatch):
    import boto3

    class _Ssm:
        def get_parameter(self, Name):
            return {"Parameter": {"Value": "http://plain"}}

    spec = importlib.util.spec_from_file_location("people_interceptor_res", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(boto3, "client", lambda *_a, **_k: _Ssm())
    with pytest.raises(mod.Refused):
        mod.resource()


# ── refused: the request ──


def test_arguments_that_are_not_an_object_are_refused(ic):
    assert _refused(ic.handler(_event(_token(), arguments=["x"])))


def test_a_batch_is_refused(ic):
    event = _event(_token())
    event["mcp"]["gatewayRequest"]["body"] = [{"method": "tools/call"}]
    assert _refused(ic.handler(event))


def test_an_event_without_a_request_is_refused(ic):
    assert _refused(ic.handler({"mcp": {}}))
    assert _refused(ic.handler({}))


# ── claims from the Gateway's input (open question 1, the other answer) ──


def test_claims_in_the_input_are_used_without_headers(ic, monkeypatch):
    monkeypatch.setenv("CLAIMS_PATH", "mcp.gatewayRequest.context.claims")
    event = _event(headers={}, context={"claims": _claims()})
    caller = _forwarded(ic.handler(event))["params"]["arguments"]["caller"]
    assert caller["sub"] == "6f1c-sub"


def test_claims_in_the_input_are_checked_like_a_tokens(ic, monkeypatch):
    monkeypatch.setenv("CLAIMS_PATH", "mcp.gatewayRequest.context.claims")
    for bad in (
        {"token_use": "id"},
        {"client_id": "other"},
        {"aud": "https://other"},
        {"sub": ""},
    ):
        event = _event(headers={}, context={"claims": _claims(**bad)})
        assert _refused(ic.handler(event)), bad


def test_missing_claims_in_the_input_are_refused(ic, monkeypatch):
    monkeypatch.setenv("CLAIMS_PATH", "mcp.gatewayRequest.context.claims")
    # A valid token in the headers does not stand in for them.
    assert _refused(ic.handler(_event(_token(), context=None)))


@pytest.mark.parametrize(
    "path", ["mcp.gatewayRequest.headers", "mcp.gatewayRequest.body.params.arguments"]
)
def test_claims_are_never_read_from_what_the_client_writes(ic, monkeypatch, path):
    monkeypatch.setenv("CLAIMS_PATH", path)
    event = _event(_token(), arguments={"pack_id": "p", **_claims()})
    event["mcp"]["gatewayRequest"]["headers"] = _claims()
    assert _refused(ic.handler(event))


# ── logging ──


def test_the_log_never_holds_the_token_or_the_arguments(ic, caplog):
    caplog.set_level(logging.INFO)
    token = _token()
    ic.handler(
        _event(token, arguments={"pack_id": "secret-pack", "content": "SOP text"})
    )
    ic.handler(_event(_token(key=_OTHER)))
    text = caplog.text
    assert token not in text and "secret-pack" not in text and "SOP text" not in text
    assert '"verdict": "allowed"' in text and '"verdict": "refused"' in text
    assert '"sub": "6f1c-sub"' in text
