# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The people interceptor with an organisation's own issuer (`gateway.people.issuer`).

Tokens are signed here with test keys (RSA and EC), so every check runs as deployed:
the algorithm against the discovery document's list, the key, the issuer the discovery
URL names, expiry, audience, client, subject, and the groups from a claim or from the
one pool user with the verified email. Each refusal has its own test, which passes the
request through, and so fails, if the check is removed.
"""

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa

SRC = (
    Path(__file__).resolve().parents[1] / "tools" / "people_interceptor" / "handler.py"
)
DISCOVERY = "https://idp.example.com/tenant/.well-known/openid-configuration"
ISSUER = "https://idp.example.com/tenant"
URL = "https://people.gateway.example.com/mcp"
CLIENT = "example-client"

_RSA = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_EC = ec.generate_private_key(ec.SECP256R1())
_OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk(private, kid, alg=None) -> dict:
    algo = (
        jwt.algorithms.RSAAlgorithm
        if isinstance(private, rsa.RSAPrivateKey)
        else jwt.algorithms.ECAlgorithm
    )
    data = json.loads(algo.to_jwk(private.public_key()))
    data["kid"] = kid
    if alg:
        data["alg"] = alg
    return data


class _Keys:
    def __init__(self, keys: dict):
        self.keys = keys

    def get_signing_key(self, kid):
        if kid not in self.keys:
            raise jwt.PyJWKClientError("unknown kid")
        return jwt.PyJWK.from_dict(self.keys[kid])


KEYS = {
    "rsa": _jwk(_RSA, "rsa"),
    "ec": _jwk(_EC, "ec"),
    "rs512": _jwk(_RSA, "rs512", alg="RS512"),
}
META = {
    "issuer": ISSUER,
    "jwks_uri": "https://idp.example.com/keys",
    "id_token_signing_alg_values_supported": ["RS256", "ES256", "HS256", "none"],
}


class _Pool:
    """ListUsers and AdminListGroupsForUser over a list of users."""

    def __init__(self, users, groups):
        self.users = users
        self.groups = groups
        self.calls = 0

    def list_users(self, UserPoolId, Filter, Limit):
        self.calls += 1
        email = Filter.split('"')[1]
        found = [
            u
            for u in self.users
            if {"Name": "email", "Value": email} in u["Attributes"]
        ]
        return {"Users": found[:Limit]}

    def admin_list_groups_for_user(self, UserPoolId, Username, NextToken=None):
        names = self.groups.get(Username, [])
        if NextToken is None and len(names) > 1:
            return {"Groups": [{"GroupName": names[0]}], "NextToken": "2"}
        rest = names[1:] if NextToken else names
        return {"Groups": [{"GroupName": g} for g in rest]}


def _user(name, email="pat@example.com", enabled=True, verified="true"):
    return {
        "Username": name,
        "Enabled": enabled,
        "Attributes": [
            {"Name": "email", "Value": email},
            {"Name": "email_verified", "Value": verified},
        ],
    }


@pytest.fixture
def ic(monkeypatch):
    spec = importlib.util.spec_from_file_location("people_interceptor_issuer", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for k, v in {
        "DISCOVERY_URL": DISCOVERY,
        "ALLOWED_CLIENTS": f"{CLIENT},second-client",
        "AUDIENCE": "gateway",
        "SUBJECT_CLAIM": "sub",
        "DISPLAY_CLAIM": "email",
        "EMAIL_CLAIM": "email",
        "GROUPS_FROM": "claim",
        "GROUPS_CLAIM": "groups",
        "GROUP_MAP": "{}",
        "RESOURCE_PARAMETER": "/p/e/people-gateway/url",
    }.items():
        monkeypatch.setenv(k, v)
    for k in ("ISSUER", "CLIENT_NAMES", "CLAIMS_PATH", "USER_POOL_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(mod, "_metadata", lambda: META)
    monkeypatch.setattr(mod, "_jwks", lambda: _Keys(KEYS))
    monkeypatch.setattr(mod, "resource", lambda: URL)
    return mod


def _claims(**over):
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "sub": "person-1",
        "aud": URL,
        "azp": CLIENT,
        "email": "pat@example.com",
        "exp": now + 600,
        "iat": now,
        "groups": ["business-operations-users", "business-operations-remote-authors"],
    }
    claims.update(over)
    return {k: v for k, v in claims.items() if v is not None}


def _token(key=_RSA, kid="rsa", alg="RS256", **over):
    return jwt.encode(_claims(**over), key, algorithm=alg, headers={"kid": kid})


def _event(token):
    body = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "uc-people___get_pack", "arguments": {"pack_id": "p"}},
    }
    return {
        "mcp": {
            "gatewayRequest": {
                "body": body,
                "headers": {"Authorization": f"Bearer {token}"},
            }
        }
    }


def _refused(result) -> bool:
    body = result["mcp"].get("transformedGatewayResponse", {}).get("body") or {}
    return bool((body.get("result") or {}).get("isError"))


def _caller(result) -> dict:
    return result["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"][
        "caller"
    ]


# ── allowed ──


def test_an_issuers_token_reaches_the_target_as_its_person(ic):
    token = _token()
    out = ic.handler(_event(token))
    assert _caller(out) == {
        "sub": "person-1",
        "username": "pat@example.com",
        "client": "issuer",
        "client_id": CLIENT,
        "groups": ["business-operations-users", "business-operations-remote-authors"],
    }
    assert token not in json.dumps(out)


def test_no_token_use_is_needed(ic):
    assert not _refused(ic.handler(_event(_token(token_use=None))))


def test_an_ec_key_the_document_lists_is_accepted(ic):
    assert not _refused(ic.handler(_event(_token(key=_EC, kid="ec", alg="ES256"))))


@pytest.mark.parametrize("claim", ["azp", "client_id", "act", "aud"])
def test_the_client_may_be_named_in_azp_client_id_act_or_aud(ic, claim):
    over = {"azp": None}
    if claim == "aud":
        over["aud"] = [URL, CLIENT]
    elif claim == "act":
        # An on-behalf-of token (RFC 8693): the acting client, aud is this Gateway.
        over["act"] = {"client_id": CLIENT}
    else:
        over[claim] = CLIENT
    assert _caller(ic.handler(_event(_token(**over))))["client_id"] == CLIENT


def test_another_acting_client_is_refused(ic):
    assert _refused(ic.handler(_event(_token(azp=None, act={"client_id": "other"}))))


def test_an_actor_that_is_not_an_object_is_refused(ic):
    assert _refused(ic.handler(_event(_token(azp=None, act=CLIENT))))


def test_a_literal_audience_is_checked_as_written(ic, monkeypatch):
    monkeypatch.setenv("AUDIENCE", "api://people")
    assert not _refused(ic.handler(_event(_token(aud="api://people"))))
    assert _refused(ic.handler(_event(_token(aud=URL))))


def test_the_display_falls_back_to_the_subject(ic):
    assert _caller(ic.handler(_event(_token(email=None))))["username"] == "person-1"


def test_another_subject_claim_is_read(ic, monkeypatch):
    monkeypatch.setenv("SUBJECT_CLAIM", "oid")
    out = ic.handler(_event(_token(oid="object-9")))
    assert _caller(out)["sub"] == "object-9"


def test_group_claim_values_go_through_the_group_map(ic, monkeypatch):
    monkeypatch.setenv(
        "GROUP_MAP", json.dumps({"idp-authors": ["business-operations-authors"]})
    )
    out = ic.handler(_event(_token(groups=["idp-authors", "unmapped"])))
    assert _caller(out)["groups"] == ["business-operations-authors"]


@pytest.mark.parametrize(
    ("discovery", "issuer"),
    [
        (
            "https://idp.example.com/.well-known/openid-configuration",
            "https://idp.example.com",
        ),
        (
            "https://idp.example.com/.well-known/oauth-authorization-server/tenant",
            "https://idp.example.com/tenant",
        ),
        (DISCOVERY, ISSUER),
    ],
)
def test_the_issuer_is_the_one_the_discovery_url_names(
    ic, monkeypatch, discovery, issuer
):
    monkeypatch.setenv("DISCOVERY_URL", discovery)
    assert ic.expected_issuer() == issuer


# ── refused: the signature and the algorithm ──


def test_another_keys_signature_is_refused(ic):
    assert _refused(ic.handler(_event(_token(key=_OTHER))))


def test_an_hmac_token_is_refused_even_when_the_document_lists_it(ic):
    token = jwt.encode(_claims(), "s" * 64, algorithm="HS256", headers={"kid": "rsa"})
    assert _refused(ic.handler(_event(token)))


def test_an_unsigned_token_is_refused_even_when_the_document_lists_it(ic):
    token = jwt.encode(_claims(), None, algorithm="none", headers={"kid": "rsa"})
    assert _refused(ic.handler(_event(token)))


def test_an_algorithm_the_document_does_not_list_is_refused(ic):
    assert _refused(ic.handler(_event(_token(alg="RS384"))))


def test_rs256_alone_when_the_document_lists_none(ic, monkeypatch):
    meta = {
        k: v for k, v in META.items() if k != "id_token_signing_alg_values_supported"
    }
    monkeypatch.setattr(ic, "_metadata", lambda: meta)
    assert not _refused(ic.handler(_event(_token())))
    assert _refused(ic.handler(_event(_token(key=_EC, kid="ec", alg="ES256"))))


def test_a_key_published_for_another_algorithm_is_refused(ic, monkeypatch):
    monkeypatch.setattr(
        ic,
        "_metadata",
        lambda: {**META, "id_token_signing_alg_values_supported": ["RS256", "RS512"]},
    )
    assert not _refused(ic.handler(_event(_token(kid="rs512", alg="RS512"))))
    assert _refused(ic.handler(_event(_token(kid="rs512", alg="RS256"))))


def test_a_key_of_the_wrong_type_is_refused(ic):
    # An RSA key named for an EC algorithm.
    assert _refused(ic.handler(_event(_token(key=_EC, kid="rsa", alg="ES256"))))


def test_an_unknown_kid_is_refused(ic):
    assert _refused(ic.handler(_event(_token(kid="nope"))))


# ── refused: the claims ──


def test_another_issuer_is_refused(ic):
    assert _refused(ic.handler(_event(_token(iss="https://other.example.com"))))


def test_a_discovery_document_naming_another_issuer_is_refused(ic, monkeypatch):
    other = "https://other.example.com"
    monkeypatch.setattr(ic, "_metadata", lambda: {**META, "issuer": other})
    assert _refused(ic.handler(_event(_token(iss=other))))


def test_an_expired_token_is_refused(ic):
    assert _refused(ic.handler(_event(_token(exp=int(time.time()) - 120))))


def test_another_audience_is_refused(ic):
    assert _refused(
        ic.handler(_event(_token(aud="https://agent.gateway.example.com/mcp")))
    )


def test_no_audience_is_refused(ic):
    assert _refused(ic.handler(_event(_token(aud=None))))


def test_another_client_is_refused(ic):
    assert _refused(ic.handler(_event(_token(azp="console-app"))))


def test_no_client_is_refused(ic):
    assert _refused(ic.handler(_event(_token(azp=None))))


@pytest.mark.parametrize("oid", ["", "   ", None])
def test_no_subject_is_refused(ic, monkeypatch, oid):
    monkeypatch.setenv("SUBJECT_CLAIM", "oid")
    assert _refused(ic.handler(_event(_token(oid=oid))))


def test_no_audience_configured_admits_no_one(ic, monkeypatch):
    monkeypatch.setenv("AUDIENCE", "")
    assert _refused(ic.handler(_event(_token())))


def test_claims_from_the_input_are_held_to_the_issuer(ic, monkeypatch):
    monkeypatch.setenv("CLAIMS_PATH", "mcp.gatewayRequest.context.claims")
    event = _event("x")
    event["mcp"]["gatewayRequest"]["context"] = {"claims": _claims(iss="https://other")}
    assert _refused(ic.handler(event))
    event["mcp"]["gatewayRequest"]["context"] = {"claims": _claims()}
    assert not _refused(ic.handler(event))


# ── groups from the pool ──


@pytest.fixture
def pool(ic, monkeypatch):
    monkeypatch.setenv("GROUPS_FROM", "pool")
    monkeypatch.setenv("USER_POOL_ID", "us-east-1_pool")
    fake = _Pool(
        [_user("pat")],
        {"pat": ["business-operations-users", "business-operations-remote-authors"]},
    )
    monkeypatch.setattr(ic, "_cognito", lambda: fake)
    return fake


def test_pool_groups_are_the_one_users_paged_and_cached(ic, pool):
    out = ic.handler(_event(_token(groups=["ignored"])))
    assert _caller(out)["groups"] == [
        "business-operations-users",
        "business-operations-remote-authors",
    ]
    ic.handler(_event(_token()))
    assert pool.calls == 1


def test_the_cache_lasts_a_minute(ic, pool, monkeypatch):
    ic.handler(_event(_token()))
    ic._pool_cache["pat@example.com"] = (time.monotonic() - ic.POOL_TTL - 1, [])
    ic.handler(_event(_token()))
    assert pool.calls == 2


def test_an_email_no_pool_user_has_is_refused(ic, pool):
    assert _refused(ic.handler(_event(_token(email="nobody@example.com"))))


def test_an_email_in_another_case_finds_its_pool_user(ic, pool):
    out = ic.handler(_event(_token(email="Pat@Example.COM")))
    assert "business-operations-users" in _caller(out)["groups"]


def test_a_refused_email_is_logged_by_its_shape_never_the_address(ic, pool, caplog):
    caplog.set_level("WARNING")
    ic.handler(_event(_token(email="nobody")))
    ic.handler(_event(_token(email="Someone@Other.example")))
    assert "no @, 6 characters" in caplog.text
    assert "an address at Other.example, with capitals" in caplog.text
    assert 'nobody"' not in caplog.text and "Someone@" not in caplog.text


def test_an_email_two_pool_users_have_is_refused(ic, pool):
    pool.users.append(_user("pat2"))
    assert _refused(ic.handler(_event(_token())))


def test_a_disabled_pool_user_is_refused(ic, pool):
    pool.users[0] = _user("pat", enabled=False)
    assert _refused(ic.handler(_event(_token())))


def test_a_pool_user_whose_email_is_unverified_is_refused(ic, pool):
    pool.users[0] = _user("pat", verified="false")
    assert _refused(ic.handler(_event(_token())))


@pytest.mark.parametrize("verified", [False, "false"])
def test_an_email_the_issuer_says_is_unverified_is_refused(ic, pool, verified):
    assert _refused(ic.handler(_event(_token(email_verified=verified))))


@pytest.mark.parametrize(
    "email", [None, "", 'pat"@example.com', "pat\\@example.com", 7]
)
def test_an_unusable_email_claim_is_refused(ic, pool, email):
    assert _refused(ic.handler(_event(_token(email=email))))
    assert pool.calls == 0


def test_an_unreadable_pool_admits_no_one(ic, pool, monkeypatch):
    def broken(**_):
        raise RuntimeError("throttled")

    monkeypatch.setattr(pool, "list_users", broken)
    assert _refused(ic.handler(_event(_token())))


def test_no_pool_configured_admits_no_one(ic, pool, monkeypatch):
    monkeypatch.delenv("USER_POOL_ID")
    assert _refused(ic.handler(_event(_token())))
