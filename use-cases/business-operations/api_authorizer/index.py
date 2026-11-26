# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The REST API's authorizer under the platform's direct Entra mode (a TOKEN authorizer).

A REST API takes a Cognito authorizer or a Lambda one, and in direct mode there is no
pool, so this verifies the console's Entra access token itself:

* RS256 only, with a key from the tenant's JWKS, fetched over https from the issuer's
  discovery document.
* `iss` is the tenant's v2 issuer, or its v1 form (`https://sts.windows.net/<tid>/`),
  which an app left at the default token version emits from the same keys.
* `aud` is the platform app (`AUDIENCE`): an access token for another API in the same
  tenant, or an id token (whose `aud` is the console app), is refused.
* `azp` (`appid` on a v1 token) is the console app (`ALLOWED_CLIENTS`). `entitlement`
  checks it again in every handler.
* `exp` and `nbf`, with a minute of skew.

The verified claims reach the handlers as authorizer context, the only place they can
read them from: values there are strings, so the list-valued `roles` travels as JSON
(`entitlement._parse_groups` reads it back).

Signature and claim checks are PyJWT's (`requirements.txt`, bundled for arm64 at synth by
`business_operations/api.py`). The algorithm is pinned before the key is looked up, and a
key shorter than 2048 bits is refused.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from functools import lru_cache

import jwt

DISCOVERY_URL = os.environ.get("DISCOVERY_URL", "")
AUDIENCE = os.environ.get("AUDIENCE", "")
ALLOWED_CLIENTS = frozenset(
    c for c in os.environ.get("ALLOWED_CLIENTS", "").split(",") if c
)
SKEW = 60
#: PyJWKClient refetches the JWKS for an unknown kid at most this often, so forged
#: kids cannot turn every request into a fetch.
REFETCH_EVERY = 60
#: Keys are refetched at least this often, so one the tenant withdraws stops verifying.
KEYS_TTL = 3600
#: Passed to the handlers. `roles` is JSON; the rest are strings.
CONTEXT_CLAIMS = (
    "sub",
    "oid",
    "tid",
    "email",
    "preferred_username",
    "name",
    "azp",
    "aud",
    "scp",
)
_V2 = re.compile(r"^https://login\.microsoftonline\.com/([^/]+)/v2\.0$")
_DECODE_OPTIONS = {
    "require": ["exp", "iss", "aud"],
    "strict_aud": True,
    "enforce_minimum_key_length": True,
}


class Refused(Exception):
    pass


def _fetch(url: str) -> dict:
    if not url.startswith("https://"):
        raise Refused(f"refusing a non-https key source: {url!r}")
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:  # nosec B310 - https asserted above  # nosemgrep: dynamic-urllib-use-detected
            return json.loads(resp.read())
    except (OSError, ValueError) as e:
        # A key source that cannot be read verifies nothing: a 401, not a 500.
        raise Refused(f"key source unreadable: {type(e).__name__}") from e


@lru_cache(maxsize=1)
def _metadata() -> dict:
    return _fetch(DISCOVERY_URL)


@lru_cache(maxsize=1)
def _jwks() -> jwt.PyJWKClient:
    """The tenant's signing keys: cached, and refetched for an unknown kid at most once
    a cooldown (rotation or a forgery: refetch, then refuse)."""
    uri = _metadata()["jwks_uri"]
    if not str(uri).startswith("https://"):
        raise Refused(f"refusing a non-https key source: {uri!r}")
    return jwt.PyJWKClient(
        uri, lifespan=KEYS_TTL, cooldown_duration=REFETCH_EVERY, timeout=5
    )


def _key(kid: str) -> jwt.PyJWK:
    try:
        return _jwks().get_signing_key(kid)
    except jwt.PyJWKClientConnectionError as e:
        # A key source that cannot be read verifies nothing: a 401, not a 500.
        raise Refused(f"key source unreadable: {type(e).__name__}") from e
    except jwt.PyJWTError as e:
        raise Refused("unknown signing key") from e


def issuers(discovered: str) -> set[str]:
    m = _V2.match(discovered)
    return {discovered, f"https://sts.windows.net/{m.group(1)}/"} if m else {discovered}


def verify(token: str) -> dict:
    """The token's claims, or Refused."""
    # Only the header, to pin the algorithm and pick the key; the claims are read
    # once, by jwt.decode, after the signature checks out.
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as e:
        raise Refused("malformed token") from e
    if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
        raise Refused("only RS256 with a kid is accepted")
    if not AUDIENCE:
        raise Refused("wrong audience")
    key = _key(header["kid"])
    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=AUDIENCE,
            issuer=issuers(_metadata()["issuer"]),
            leeway=SKEW,
            options=_DECODE_OPTIONS,
        )
    except jwt.InvalidSignatureError as e:
        raise Refused("bad signature") from e
    except jwt.ExpiredSignatureError as e:
        raise Refused("expired") from e
    except jwt.ImmatureSignatureError as e:
        raise Refused("not yet valid") from e
    except jwt.InvalidIssuerError as e:
        raise Refused("wrong issuer") from e
    except jwt.InvalidAudienceError as e:
        raise Refused("wrong audience") from e
    except jwt.InvalidKeyError as e:
        raise Refused("signing key too short") from e
    except jwt.InvalidTokenError as e:
        raise Refused(f"invalid token: {type(e).__name__}") from e
    client = claims.get("azp") or claims.get("appid")
    if not ALLOWED_CLIENTS or client not in ALLOWED_CLIENTS:
        raise Refused("wrong client")
    if not isinstance(claims.get("sub"), str) or not claims["sub"]:
        raise Refused("no subject")
    return claims


def context(claims: dict) -> dict[str, str]:
    out = {k: str(claims[k]) for k in CONTEXT_CLAIMS if isinstance(claims.get(k), str)}
    out["azp"] = str(claims.get("azp") or claims.get("appid") or "")
    roles = claims.get("roles")
    out["roles"] = json.dumps(
        [str(r) for r in roles] if isinstance(roles, list) else []
    )
    return out


def handler(event, _context):
    raw = str(event.get("authorizationToken") or "")
    token = raw[7:] if raw.lower().startswith("bearer ") else raw
    try:
        claims = verify(token)
    except Refused as why:
        print(f"[AUTHORIZER] refused: {why}")
        # The literal "Unauthorized" is what API Gateway turns into a 401.
        raise Exception("Unauthorized") from None  # noqa: TRY002
    # The whole API, not this method: API Gateway caches the policy by token.
    arn = str(event["methodArn"]).split("/", 2)
    return {
        "principalId": claims["sub"],
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": "Allow",
                    "Resource": f"{arn[0]}/{arn[1]}/*",
                }
            ],
        },
        "context": context(claims),
    }
