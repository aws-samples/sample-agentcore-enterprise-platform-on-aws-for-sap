# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# This is sample code. Not for use in production.
# See NOTICE and LICENSE for more information.
"""Authentication utilities for AgentCore Runtime requests."""

import json
import logging
import os
import re
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache

import jwt
from jwt import PyJWKClient

logger = logging.getLogger(__name__)

# AgentCore Memory actorId only accepts [a-zA-Z0-9-_/] (with optional `:` segments).
# Okta/Entra subjects are emails (user@example.com) whose `@`/`.` violate it, so any
# non-conforming char is collapsed to `_`. Cognito UUID subjects pass through unchanged.
_ACTOR_ID_DISALLOWED = re.compile(r"[^a-zA-Z0-9\-_/:]")
_TRUSTED_IDENTITY_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Identity"
_MAX_IDENTITY_LENGTH = 256

# The Runtime authorizer's own discovery URL, set by the deploy from the same issuer it
# hands the authorizer, so the two cannot name different issuers.
RUNTIME_DISCOVERY_ENV = "RUNTIME_JWT_DISCOVERY_URL"
# The client ids allowed to assert a delegated subject: the use case's delegating client,
# which is also the only client the Runtime's authorizer admits.
DELEGATING_CLIENTS_ENV = "RUNTIME_DELEGATING_CLIENTS"
# "true" under the platform's direct mode: the Runtime's authorizer pins only an
# audience, which any app in the tenant can obtain a token for, so every call (not only
# a delegated one) must come from a delegating client.
DELEGATING_ONLY_ENV = "RUNTIME_DELEGATING_ONLY"
# A person's own token, forwarded for an obo SAP system, and the audience it must name
# (the platform app). Unset: no token is ever accepted from the header.
USER_TOKEN_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token"
USER_TOKEN_AUDIENCE_ENV = "RUNTIME_USER_TOKEN_AUDIENCE"


def to_memory_actor_id(subject: str) -> str:
    """Map an inbound subject to a deterministic Memory-safe actorId.

    Prefixed with the runtime's MEMORY_ACTOR_PREFIX: the Memory is the platform's, and
    the runtime role may touch only actor ids under its own prefix, while every other
    runtime's role is refused them. Without it every Memory call is denied.
    """
    return os.environ.get("MEMORY_ACTOR_PREFIX", "") + _ACTOR_ID_DISALLOWED.sub(
        "_", subject
    )


@dataclass
class InboundIdentity:
    """Normalized identity extracted from trusted Runtime request metadata."""

    subject: str


def _get_header(headers: Mapping[str, str], name: str) -> str | None:
    """Read a header from plain dictionaries or Starlette's case-insensitive Headers."""
    direct = headers.get(name) or headers.get(name.lower()) or headers.get(name.title())
    if direct is not None:
        return direct
    lowered = name.lower()
    return next(
        (value for key, value in headers.items() if key.lower() == lowered), None
    )


def _decode_claims_from_headers(headers: Mapping[str, str]) -> dict:
    auth_header = _get_header(headers, "Authorization")
    if not auth_header:
        raise ValueError(
            "No Authorization header found in request context. Ensure the AgentCore "
            "Runtime uses JWT inbound auth and allows the Authorization header."
        )

    token = (
        auth_header[7:] if auth_header.lower().startswith("bearer ") else auth_header
    )
    return verify_runtime_bearer(token)


def _require_https(url: str, what: str) -> str:
    """Refuse anything but https for the two URLs signing keys come from. Over plain
    http, anyone on the path can hand this verifier their own keys, and every token
    they sign with them verifies."""
    if not url.startswith("https://"):
        raise ValueError(f"{what} must be https, got {url!r}")
    return url


@lru_cache(maxsize=4)
def _oidc_metadata(discovery_url: str) -> dict:
    """Issuer metadata (``issuer``, ``jwks_uri``), cached per container.

    A failed fetch raises and is not cached, so the next request retries.
    """
    url = _require_https(discovery_url, RUNTIME_DISCOVERY_ENV)
    with urllib.request.urlopen(url, timeout=5) as resp:  # nosec B310 - https asserted above  # nosemgrep: dynamic-urllib-use-detected
        return json.loads(resp.read())


_ENTRA_V2_ISSUER = re.compile(r"^https://login\.microsoftonline\.com/([^/]+)/v2\.0$")
_ENTRA_V1_ISSUER = re.compile(r"^https://sts\.windows\.net/([^/]+)/$")


def accepted_issuers(discovered: str) -> list[str]:
    """The ``iss`` values a bearer from this issuer may carry.

    Exactly the discovered issuer, except for Entra: the ``iss`` on an Entra *access*
    token follows the resource app's ``accessTokenAcceptedVersion``, not the endpoint
    that minted it. An app left at the default emits ``https://sts.windows.net/{tid}/``
    even from the v2.0 endpoint whose discovery document names
    ``https://login.microsoftonline.com/{tid}/v2.0``, and the Entra runbook supports both.
    So the other form is accepted too — for the **same tenant** only, which signs both
    with the same keys. Multi-tenant (``common``/``organizations``) discovery names a
    ``{tenantid}`` placeholder that matches no real ``iss``, so it fails closed; no
    profile here uses it.
    """
    issuers = [discovered]
    v2 = _ENTRA_V2_ISSUER.match(discovered)
    v1 = _ENTRA_V1_ISSUER.match(discovered)
    if v2:
        issuers.append(f"https://sts.windows.net/{v2.group(1)}/")
    elif v1:
        issuers.append(f"https://login.microsoftonline.com/{v1.group(1)}/v2.0")
    return issuers


@lru_cache(maxsize=4)
def _jwk_client(jwks_uri: str) -> PyJWKClient:
    # PyJWKClient refetches on an unknown `kid`, which is what absorbs key rotation.
    return PyJWKClient(_require_https(jwks_uri, "jwks_uri"))


def verify_runtime_bearer(token: str) -> dict:
    """Verify the Runtime's inbound bearer and return its claims.

    AgentCore Runtime's JWT authorizer validates this token before the container sees it,
    and checks the client or audience. This re-checks signature, ``iss`` and ``exp``
    against the same issuer so the acting subject does not rest on that authorizer alone:
    a Runtime redeployed without it, or reached on another invocation path, can no longer
    hand the agent a forged ``sub``. Client and audience are left to the authorizer, which
    already chooses between ``client_id`` (Cognito) and ``aud`` (Okta, Entra) per issuer.

    Fails closed: with no discovery URL configured it refuses.
    """
    discovery_url = os.environ.get(RUNTIME_DISCOVERY_ENV, "").strip()
    if not discovery_url:
        raise ValueError(
            f"{RUNTIME_DISCOVERY_ENV} is not set, so the inbound bearer cannot be verified."
        )
    meta = _oidc_metadata(discovery_url)
    signing_key = _jwk_client(meta["jwks_uri"]).get_signing_key_from_jwt(token)
    return jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        issuer=accepted_issuers(meta["issuer"]),
        # A fresh dict per call: PyJWT has mutated a reused options dict before
        # (GHSA-gvp8-978c-rx2q).
        options={"verify_aud": False, "require": ["exp", "iss", "sub"]},
    )


def _require_delegating_bearer(claims: Mapping[str, object]) -> None:
    """Refuse a delegated subject unless the verified bearer's client may delegate."""
    allowed = {c for c in os.environ.get(DELEGATING_CLIENTS_ENV, "").split(",") if c}
    if not allowed:
        raise ValueError(
            f"A delegated identity arrived but {DELEGATING_CLIENTS_ENV} is not set, so no "
            "client is allowed to assert one."
        )
    # Cognito names the client in `client_id`, Entra in `azp` (`appid` on a v1 token).
    client = claims.get("client_id") or claims.get("azp") or claims.get("appid")
    if not isinstance(client, str) or client not in allowed:
        raise ValueError(
            "The bearer's client is not allowed to assert a delegated identity."
        )


def _identity_from_claims(claims: dict) -> InboundIdentity:
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise ValueError(
            "JWT token does not contain a non-empty 'sub' claim. Cannot determine identity."
        )
    return InboundIdentity(subject=subject)


def get_inbound_identity_from_headers(
    headers: Mapping[str, str],
) -> InboundIdentity:
    """Resolve the acting identity from Runtime-validated request headers.

    The delegated identity header is an unauthenticated assertion of the acting
    subject, trusted here because only a caller holding machine credentials can set
    it. Two things have to hold together for that to be true, and both live in
    ``business_operations/runtime.py``: the header is on the Runtime's allowlist, so
    AgentCore forwards it instead of stripping it, and that Runtime's authorizer accepts
    the delegating client alone. Without the header every caller resolves to the subject
    of its own Runtime-validated JWT, which is what the fallback below does.

    Do not add a browser client to that authorizer while this header is allowlisted. Doing so would let any authenticated browser caller assert an
    arbitrary subject, spoofing the audit initiator, the persisted trace, and the
    Memory ``actorId`` partition that separates users. The CDK derives the allowlist
    from the resolved client list precisely so the two cannot drift apart.
    """
    delegated_subject = _get_header(headers, _TRUSTED_IDENTITY_HEADER)
    if delegated_subject is not None:
        delegated_subject = delegated_subject.strip()
        has_control_character = any(
            ord(character) < 32 or ord(character) == 127
            for character in delegated_subject
        )
        if (
            not delegated_subject
            or len(delegated_subject) > _MAX_IDENTITY_LENGTH
            or has_control_character
        ):
            raise ValueError("The trusted user identity header is invalid.")
        # The header is a bare string, so it is only as good as the bearer it rides with:
        # that bearer must verify and must belong to a client allowed to delegate. Without
        # this the container trusted the header on the Runtime authorizer's word alone.
        _require_delegating_bearer(_decode_claims_from_headers(headers))
        identity = InboundIdentity(subject=delegated_subject)
    else:
        claims = _decode_claims_from_headers(headers)
        if os.environ.get(DELEGATING_ONLY_ENV) == "true":
            _require_delegating_bearer(claims)
        identity = _identity_from_claims(claims)

    logger.info("Inbound identity: subject=%s", identity.subject)
    return identity


def forwarded_user_token(headers: Mapping[str, str], subject: str) -> str | None:
    """The person's own token for this run's Gateway calls, or None when none came.

    Only ``POST /runs`` sends one, for an obo SAP system. It is a credential, not an
    assertion, so it is verified here whatever rode in with it: signature, issuer and
    expiry as for the bearer, ``aud`` the platform app, and ``sub`` the acting subject
    already resolved, so a delegating caller cannot pair one person's identity with
    another's token. Raises on any failure: a run that cannot act as its person must
    not fall back to the agent's own client.
    """
    token = _get_header(headers, USER_TOKEN_HEADER)
    if token is None:
        return None
    audience = os.environ.get(USER_TOKEN_AUDIENCE_ENV, "").strip()
    if not audience:
        raise ValueError(
            f"A user token arrived but {USER_TOKEN_AUDIENCE_ENV} is not set, so none is accepted."
        )
    token = token.strip()
    discovery_url = os.environ.get(RUNTIME_DISCOVERY_ENV, "").strip()
    if not discovery_url:
        raise ValueError(f"{RUNTIME_DISCOVERY_ENV} is not set.")
    meta = _oidc_metadata(discovery_url)
    signing_key = _jwk_client(meta["jwks_uri"]).get_signing_key_from_jwt(token)
    claims = jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        issuer=accepted_issuers(meta["issuer"]),
        audience=audience,
        options={"require": ["exp", "iss", "sub", "aud"]},
    )
    if claims.get("sub") != subject:
        raise ValueError("The forwarded user token is not the acting subject's.")
    return token


def extract_user_id_from_headers(headers: Mapping[str, str]) -> str:
    """Return the acting subject from FastAPI/ASGI request headers."""
    return get_inbound_identity_from_headers(headers).subject


if __name__ == "__main__":
    assert to_memory_actor_id("user@example.com") == "user_example_com"
    assert to_memory_actor_id("11c8...-uuid-4XIj") == "11c8___-uuid-4XIj"
    _uuid = "8a7b6c5d-1234-5678-9abc-def012345678"
    assert to_memory_actor_id(_uuid) == _uuid
    assert not re.search(
        r"[^a-zA-Z0-9\-_/:]", to_memory_actor_id("user+tag@corp.example.com")
    )
    print("auth actorId self-check OK")
