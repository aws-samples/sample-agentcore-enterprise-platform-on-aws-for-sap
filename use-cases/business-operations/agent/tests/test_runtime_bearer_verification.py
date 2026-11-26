# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The Runtime's inbound bearer is verified before its `sub` is trusted, and the
delegated identity header is believed only on a verified delegating bearer.

Real RS256 keys and a real JWKS document; only the two network fetches are replaced.
"""

import base64
import json
import time

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from utils import auth

_ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TEST"
_DISCOVERY = f"{_ISSUER}/.well-known/openid-configuration"
_JWKS_URI = f"{_ISSUER}/.well-known/jwks.json"


def _keypair():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


_KEY = _keypair()
_OTHER = _keypair()


def _jwk(key, kid):
    jwk = json.loads(pyjwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return {**jwk, "kid": kid, "alg": "RS256", "use": "sig"}


def _sign(claims=None, key=_KEY, kid="k1", alg="RS256", headers=None):
    base = {
        "sub": "user-1",
        "iss": _ISSUER,
        "exp": int(time.time()) + 300,
        "client_id": "c",
    }
    return pyjwt.encode(
        {**base, **(claims or {})},
        key,
        algorithm=alg,
        headers={"kid": kid, **(headers or {})},
    )


@pytest.fixture(autouse=True)
def _issuer(monkeypatch):
    monkeypatch.setenv(auth.RUNTIME_DISCOVERY_ENV, _DISCOVERY)
    monkeypatch.setenv(auth.DELEGATING_CLIENTS_ENV, "machine")
    auth._oidc_metadata.cache_clear()
    auth._jwk_client.cache_clear()
    monkeypatch.setattr(
        auth, "_oidc_metadata", lambda url: {"issuer": _ISSUER, "jwks_uri": _JWKS_URI}
    )

    def fetch(self, *a, **k):
        return {"keys": [_jwk(_KEY, "k1")]}

    monkeypatch.setattr(pyjwt.PyJWKClient, "fetch_data", fetch)
    yield
    auth._jwk_client.cache_clear()


def _headers(token):
    return {"Authorization": f"Bearer {token}"}


def test_a_validly_signed_bearer_resolves_its_subject():
    assert auth.extract_user_id_from_headers(_headers(_sign())) == "user-1"


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(lambda: _sign(key=_OTHER), id="wrong-key"),
        pytest.param(
            lambda: _sign({"iss": "https://evil.example.com"}), id="wrong-issuer"
        ),
        pytest.param(lambda: _sign({"exp": int(time.time()) - 60}), id="expired"),
        pytest.param(lambda: _sign(kid="unknown"), id="unknown-kid"),
    ],
)
def test_a_bearer_that_fails_verification_is_refused(token):
    with pytest.raises(pyjwt.PyJWTError):
        auth.extract_user_id_from_headers(_headers(token()))


def _seg(d):
    return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()


def test_an_unsigned_alg_none_token_is_refused():
    body = {"sub": "attacker", "iss": _ISSUER, "exp": int(time.time()) + 300}
    token = f"{_seg({'alg': 'none', 'kid': 'k1'})}.{_seg(body)}."
    with pytest.raises(pyjwt.PyJWTError):
        auth.extract_user_id_from_headers(_headers(token))


def test_hs256_signed_with_the_public_key_is_refused():
    """The HS/RS key-confusion class GHSA-ffc3-869f-jxw9 reopened in pyjwt 2.13."""
    pem = _KEY.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    body = {"sub": "attacker", "iss": _ISSUER, "exp": int(time.time()) + 300}
    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "HS256", "kid": "k1"}).encode()
    ).rstrip(b"=")
    payload = base64.urlsafe_b64encode(json.dumps(body).encode()).rstrip(b"=")
    import hashlib
    import hmac

    sig = base64.urlsafe_b64encode(
        hmac.new(pem, header + b"." + payload, hashlib.sha256).digest()
    ).rstrip(b"=")
    with pytest.raises(pyjwt.PyJWTError):
        auth.extract_user_id_from_headers(
            _headers((header + b"." + payload + b"." + sig).decode())
        )


def test_a_bearer_with_no_sub_is_refused():
    with pytest.raises(pyjwt.PyJWTError):
        auth.extract_user_id_from_headers(_headers(_sign({"sub": None})))


def test_no_discovery_url_refuses(monkeypatch):
    monkeypatch.delenv(auth.RUNTIME_DISCOVERY_ENV)
    with pytest.raises(ValueError, match="cannot be verified"):
        auth.extract_user_id_from_headers(_headers(_sign()))


def test_an_unreachable_issuer_fails_closed(monkeypatch):
    def down(url):
        raise OSError("no route to issuer")

    monkeypatch.setattr(auth, "_oidc_metadata", down)
    with pytest.raises(OSError):
        auth.extract_user_id_from_headers(_headers(_sign()))


def test_a_delegated_subject_is_read_once_the_delegating_bearer_verifies(monkeypatch):
    monkeypatch.setenv(auth.DELEGATING_CLIENTS_ENV, "machine")
    headers = {
        **_headers(_sign({"client_id": "machine"})),
        auth._TRUSTED_IDENTITY_HEADER: "human-1",
    }
    assert auth.extract_user_id_from_headers(headers) == "human-1"


def test_a_delegated_subject_on_a_forged_bearer_is_refused(monkeypatch):
    monkeypatch.setenv(auth.DELEGATING_CLIENTS_ENV, "machine")
    headers = {
        **_headers(_sign({"client_id": "machine"}, key=_OTHER)),
        auth._TRUSTED_IDENTITY_HEADER: "human-1",
    }
    with pytest.raises(pyjwt.PyJWTError):
        auth.extract_user_id_from_headers(headers)


def test_a_delegated_subject_from_a_client_not_allowed_to_delegate_is_refused(
    monkeypatch,
):
    monkeypatch.setenv(auth.DELEGATING_CLIENTS_ENV, "machine")
    headers = {
        **_headers(_sign({"client_id": "browser"})),
        auth._TRUSTED_IDENTITY_HEADER: "human-1",
    }
    with pytest.raises(ValueError, match="not allowed to assert"):
        auth.extract_user_id_from_headers(headers)


def test_a_delegated_subject_with_no_delegating_clients_configured_is_refused(
    monkeypatch,
):
    monkeypatch.delenv(auth.DELEGATING_CLIENTS_ENV, raising=False)
    headers = {
        **_headers(_sign({"client_id": "machine"})),
        auth._TRUSTED_IDENTITY_HEADER: "human-1",
    }
    with pytest.raises(ValueError, match="no client is allowed"):
        auth.extract_user_id_from_headers(headers)


_TID = "11111111-2222-3333-4444-5555aaaa5555"
_ENTRA_V2 = f"https://login.microsoftonline.com/{_TID}/v2.0"
_ENTRA_V1 = f"https://sts.windows.net/{_TID}/"


def _entra(monkeypatch, discovered):
    monkeypatch.setattr(
        auth,
        "_oidc_metadata",
        lambda url: {"issuer": discovered, "jwks_uri": _JWKS_URI},
    )


@pytest.mark.parametrize("discovered", [_ENTRA_V2, _ENTRA_V1])
@pytest.mark.parametrize("token_iss", [_ENTRA_V2, _ENTRA_V1])
def test_an_entra_token_of_either_version_verifies_for_its_own_tenant(
    monkeypatch, discovered, token_iss
):
    """v1 `sts.windows.net` and v2 `login.microsoftonline.com/.../v2.0`, same tenant."""
    _entra(monkeypatch, discovered)
    token = _sign({"iss": token_iss, "aud": "api://erp", "azp": "spa", "tid": _TID})
    assert auth.extract_user_id_from_headers(_headers(token)) == "user-1"


@pytest.mark.parametrize(
    "token_iss",
    [
        "https://sts.windows.net/99999999-2222-3333-4444-5555aaaa5555/",
        "https://login.microsoftonline.com/99999999-2222-3333-4444-5555aaaa5555/v2.0",
        "https://login.microsoftonline.com/common/v2.0",
    ],
)
def test_an_entra_token_from_another_tenant_is_refused(monkeypatch, token_iss):
    _entra(monkeypatch, _ENTRA_V2)
    with pytest.raises(pyjwt.PyJWTError):
        auth.extract_user_id_from_headers(_headers(_sign({"iss": token_iss})))


def test_only_entra_gets_a_second_issuer():
    assert auth.accepted_issuers(_ISSUER) == [_ISSUER]
    okta = "https://example.okta.com/oauth2/default"
    assert auth.accepted_issuers(okta) == [okta]
    assert auth.accepted_issuers(_ENTRA_V2) == [_ENTRA_V2, _ENTRA_V1]


def test_multi_tenant_discovery_fails_closed(monkeypatch):
    _entra(monkeypatch, "https://login.microsoftonline.com/{tenantid}/v2.0")
    with pytest.raises(pyjwt.PyJWTError):
        auth.extract_user_id_from_headers(_headers(_sign({"iss": _ENTRA_V2})))


@pytest.mark.parametrize(
    "url", ["http://issuer.example.com/", "file:///etc/passwd", ""]
)
def test_signing_keys_are_fetched_only_over_https(url):
    # Over plain http anyone on the path can substitute the JWKS, and every token signed
    # with their key then verifies. Both fetches route through this one guard.
    with pytest.raises(ValueError, match="must be https"):
        auth._require_https(url, "x")
    assert auth._require_https(_DISCOVERY, "x") == _DISCOVERY


# ── Which subject a run acts as ──

_MACHINE = {"sub": "machine", "client_id": "machine"}


def test_missing_authorization_raises():
    with pytest.raises(ValueError, match="Authorization"):
        auth.extract_user_id_from_headers({})


def test_a_queued_run_and_an_interactive_turn_share_one_actor_id():
    # Pinned to the value, not merely to agreement: resolution that returned any
    # constant, or preferred a `username` claim, would agree and still split Memory.
    sub = "8a7b6c5d-1234-5678-9abc-def012345678"
    queued = auth.extract_user_id_from_headers(
        {**_headers(_sign(_MACHINE)), auth._TRUSTED_IDENTITY_HEADER: sub}
    )
    interactive = auth.extract_user_id_from_headers(
        _headers(_sign({"sub": sub, "cognito:username": "alice"}))
    )
    assert auth.to_memory_actor_id(queued) == auth.to_memory_actor_id(interactive)
    assert queued == sub


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace"),
        pytest.param("a" * 257, id="over-256-characters"),
        pytest.param("alice\nX-Injected: 1", id="control-character"),
    ],
)
def test_a_malformed_delegated_subject_is_refused(value):
    # A control character matters most: this value is logged and persisted.
    headers = {**_headers(_sign(_MACHINE)), auth._TRUSTED_IDENTITY_HEADER: value}
    with pytest.raises(ValueError, match="identity header is invalid"):
        auth.extract_user_id_from_headers(headers)


@pytest.mark.parametrize(
    "key",
    [
        pytest.param(auth._TRUSTED_IDENTITY_HEADER.lower(), id="lowercase"),
        pytest.param(auth._TRUSTED_IDENTITY_HEADER.title(), id="title-case"),
        pytest.param(auth._TRUSTED_IDENTITY_HEADER.upper(), id="uppercase"),
    ],
)
def test_the_delegated_header_is_read_whatever_case_it_arrives_in(key):
    # ASGI lowercases header names, so the canonical spelling is the one production
    # never delivers; a case-sensitive lookup would resolve every queued run to the
    # machine client.
    headers = {**_headers(_sign(_MACHINE)), key: "alice-subject"}
    assert auth.extract_user_id_from_headers(headers) == "alice-subject"


def test_actor_ids_are_memory_safe():
    assert auth.to_memory_actor_id("user@example.com") == "user_example_com"
    uuid = "8a7b6c5d-1234-5678-9abc-def012345678"
    assert auth.to_memory_actor_id(uuid) == uuid


# ── Direct mode (Entra): `azp` names the client, and a person's token rides along ──

_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Identity"


def test_an_entra_delegating_bearer_is_named_by_azp():
    token = _sign({"client_id": None, "azp": "machine"})
    headers = {**_headers(token), _HEADER: "person-1"}
    assert auth.extract_user_id_from_headers(headers) == "person-1"


def test_delegating_only_refuses_any_other_app_even_without_the_header(monkeypatch):
    monkeypatch.setenv(auth.DELEGATING_ONLY_ENV, "true")
    other = _sign({"client_id": None, "azp": "some-tenant-app"})
    with pytest.raises(ValueError, match="delegated"):
        auth.extract_user_id_from_headers(_headers(other))
    mine = _sign({"client_id": None, "azp": "machine"})
    assert auth.extract_user_id_from_headers(_headers(mine)) == "user-1"


def _forwarded(claims=None, **kw):
    return {
        auth.USER_TOKEN_HEADER: _sign(
            {
                "client_id": None,
                "azp": "console",
                "aud": "platform-app",
                **(claims or {}),
            },
            **kw,
        )
    }


def test_no_forwarded_token_means_the_vault_token():
    assert auth.forwarded_user_token({}, "user-1") is None


def test_a_forwarded_token_is_accepted_only_when_configured(monkeypatch):
    with pytest.raises(ValueError, match="not set"):
        auth.forwarded_user_token(_forwarded(), "user-1")
    monkeypatch.setenv(auth.USER_TOKEN_AUDIENCE_ENV, "platform-app")
    headers = _forwarded()
    assert (
        auth.forwarded_user_token(headers, "user-1") == headers[auth.USER_TOKEN_HEADER]
    )


@pytest.mark.parametrize(
    "headers,subject",
    [
        pytest.param(
            lambda: _forwarded({"aud": "other-api"}), "user-1", id="wrong-aud"
        ),
        pytest.param(lambda: _forwarded(key=_OTHER), "user-1", id="wrong-key"),
        pytest.param(
            lambda: _forwarded({"exp": int(time.time()) - 60}), "user-1", id="expired"
        ),
        pytest.param(lambda: _forwarded(), "someone-else", id="not-the-subject"),
    ],
)
def test_a_forwarded_token_that_fails_is_refused(monkeypatch, headers, subject):
    monkeypatch.setenv(auth.USER_TOKEN_AUDIENCE_ENV, "platform-app")
    with pytest.raises(Exception):  # noqa: B017 - PyJWT's or ours, both refuse
        auth.forwarded_user_token(headers(), subject)


def test_a_forwarded_token_must_be_the_acting_subjects(monkeypatch):
    # A valid token for another person, paired with this run's subject.
    monkeypatch.setenv(auth.USER_TOKEN_AUDIENCE_ENV, "platform-app")
    with pytest.raises(ValueError, match="not the acting subject's"):
        auth.forwarded_user_token(_forwarded({"sub": "user-2"}), "user-1")
