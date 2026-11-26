# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The people Gateway's REQUEST interceptor: the verified person in, the token out.

The people Gateway's authorizer has already refused a request without a valid token
from a remote client (gate 1). This function is gate 2, and the only place the token
is read after that:

* **Where the claims come from.** With `CLAIMS_PATH` unset (the default), the
  `Authorization` header, whose JWT is verified here again: RS256 with a `kid` from the
  pool's JWKS (https only, keys under 2048 bits refused), `iss` the pool's issuer,
  `exp` with a minute of skew. With `CLAIMS_PATH` set, the claims object the Gateway
  put at that dotted path of the event, never under `body` or `headers`, which the
  client writes.
* **What they must say.** `token_use` is `access`; `client_id` is one of the remote
  clients (`ALLOWED_CLIENTS`, named by `CLIENT_NAMES`); the token was issued for this Gateway: `aud` names its
  URL, or `scope` holds `<url>/people` (the URL is read from `RESOURCE_PARAMETER` in
  SSM, since the Gateway's URL is not known when this function is created); `sub` is
  set. Anything else refuses the request with a tool error, whatever its method.
* **What a target gets.** On `tools/call`, `arguments.caller` is replaced by
  `{sub, username, client, client_id, groups}` from those claims, so a client cannot
  name someone else. The transformed request is the body alone: the token, and every
  other header, go no further (the MCP authorization specification's rule against
  token passthrough).

One log line per request: method, tool, `sub`, client, verdict, reason. Never a header,
an argument or a token.

**With an issuer** (`DISCOVERY_URL` set: `gateway.people.issuer`), the token is the
organisation's own identity provider's, and the checks are the issuer's: an algorithm the
discovery document lists, among RSA, RSA-PSS and EC ones (never `none` or HMAC), with a
`kid` from its `jwks_uri`; the document's issuer is the one its URL names, and `iss` is
that issuer; `exp` with a minute of skew; `aud` holds `AUDIENCE` (`gateway`: this
Gateway's URL); `azp`, `client_id`, `act.client_id` (an RFC 8693 on-behalf-of
  token) or `aud` names one of `ALLOWED_CLIENTS`; the subject
claim is set. There is no `token_use`. Groups come from a claim, through `GROUP_MAP`
when it has entries, or (`GROUPS_FROM=pool`) from the one enabled pool user whose
verified email is the token's email claim, cached for a minute.

Knows no use case and no identity provider: every people target gets the same `caller`.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import time
import urllib.request
from functools import lru_cache

import boto3
import jwt

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_OUTPUT_VERSION = "1.0"
SKEW = 60
#: Keys are refetched at least this often, so one the pool withdraws stops verifying.
KEYS_TTL = 3600
#: An unknown kid refetches the JWKS at most this often, so forged kids cannot turn
#: every request into a fetch.
REFETCH_EVERY = 60
SCOPE = "people"
_DECODE_OPTIONS = {
    "require": ["exp", "iss", "sub"],
    # The audience is checked below: a Cognito access token may carry no `aud`, and
    # then the scope binds it to this Gateway instead.
    "verify_aud": False,
    "enforce_minimum_key_length": True,
}
#: The parts of the event the client writes: claims are never read from them.
_CLIENT_WRITTEN = ("body", "headers")
#: The asymmetric algorithms an issuer's token may use. Never `none` or HMAC: a shared
#: secret would let anyone who holds it mint a token.
ISSUER_ALGORITHMS = frozenset(
    {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"}
)
#: How long a pool user's groups are reused for one email.
POOL_TTL = 60
#: The caller's `client` for every token from the issuer.
ISSUER_CLIENT = "issuer"
_WELL_KNOWN = re.compile(
    r"^(https://[^/?#]+)(/[^?#]*)?/\.well-known/"
    r"(openid-configuration|oauth-authorization-server)(/[^?#]*)?$"
)


class Refused(Exception):
    """The request is not from a verified person of this Gateway."""


def _clients() -> dict[str, str]:
    """client id -> the remote client's name (`quick`, `copilot-studio`):
    `ALLOWED_CLIENTS` and `CLIENT_NAMES`, comma-separated in the same order. A
    mismatch admits no one."""
    ids = [v for v in os.environ.get("ALLOWED_CLIENTS", "").split(",") if v]
    names = [v for v in os.environ.get("CLIENT_NAMES", "").split(",") if v]
    return dict(zip(ids, names, strict=True)) if len(ids) == len(names) else {}


def issuer_mode() -> bool:
    return bool(os.environ.get("DISCOVERY_URL"))


def expected_issuer() -> str:
    """The issuer this Gateway trusts: the pool's, or the one the issuer's discovery
    URL names (OpenID Connect Discovery: the URL before the well-known path; RFC 8414:
    the host, then the path after it)."""
    if not issuer_mode():
        return os.environ.get("ISSUER", "").rstrip("/")
    m = _WELL_KNOWN.match(os.environ["DISCOVERY_URL"])
    if not m:
        raise Refused("the discovery URL is not a well-known URL")
    return f"{m.group(1)}{m.group(2) or ''}{m.group(4) or ''}".rstrip("/")


def _fetch(url: str) -> dict:
    if not url.startswith("https://"):
        raise Refused(f"refusing a non-https key source: {url!r}")
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:  # nosec B310 - https asserted above  # nosemgrep: dynamic-urllib-use-detected
            return json.loads(resp.read())
    except (OSError, ValueError) as e:
        raise Refused(f"key source unreadable: {type(e).__name__}") from e


@lru_cache(maxsize=1)
def _metadata() -> dict:
    if issuer_mode():
        return _fetch(os.environ["DISCOVERY_URL"])
    issuer = os.environ.get("ISSUER", "").rstrip("/")
    if not issuer:
        raise Refused("no issuer configured")
    return _fetch(f"{issuer}/.well-known/openid-configuration")


def algorithms() -> frozenset[str]:
    """The algorithms a token may be signed with here."""
    if not issuer_mode():
        return frozenset({"RS256"})
    listed = _metadata().get("id_token_signing_alg_values_supported")
    if not isinstance(listed, list) or not listed:
        return frozenset({"RS256"})
    return frozenset(str(a) for a in listed) & ISSUER_ALGORITHMS


@lru_cache(maxsize=1)
def _jwks() -> jwt.PyJWKClient:
    uri = str(_metadata().get("jwks_uri") or "")
    if not uri.startswith("https://"):
        raise Refused(f"refusing a non-https key source: {uri!r}")
    return jwt.PyJWKClient(
        uri, lifespan=KEYS_TTL, cooldown_duration=REFETCH_EVERY, timeout=5
    )


@lru_cache(maxsize=1)
def resource() -> str:
    """This Gateway's URL: the resource its tokens are issued for."""
    name = os.environ.get("RESOURCE_PARAMETER", "")
    if not name:
        raise Refused("no resource configured")
    try:
        value = boto3.client("ssm").get_parameter(Name=name)["Parameter"]["Value"]
    except Exception as e:  # an unreadable audience admits no one
        raise Refused(f"resource unreadable: {type(e).__name__}") from e
    if not str(value).startswith("https://"):
        raise Refused("resource is not an https URL")
    return str(value).rstrip("/")


def _bearer(headers: object) -> str:
    if not isinstance(headers, dict):
        raise Refused("no token")
    raw = next(
        (str(v) for k, v in headers.items() if str(k).lower() == "authorization"), ""
    )
    if not raw.lower().startswith("bearer "):
        raise Refused("no token")
    return raw[7:].strip()


def verify_token(token: str) -> dict:
    """The token's claims after its signature, issuer and expiry check out."""
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as e:
        raise Refused("malformed token") from e
    # Pinned before any key is looked up: the token only picks among the allowed.
    alg = header.get("alg")
    if alg not in algorithms() or not isinstance(header.get("kid"), str):
        raise Refused("the algorithm or key id is not accepted")
    issuer = str(_metadata().get("issuer") or "")
    if not issuer or issuer.rstrip("/") != expected_issuer():
        raise Refused("the discovery document names another issuer")
    try:
        key = _jwks().get_signing_key(header["kid"])
    except jwt.PyJWKClientConnectionError as e:
        raise Refused(f"key source unreadable: {type(e).__name__}") from e
    except jwt.PyJWTError as e:
        raise Refused("unknown signing key") from e
    # PyJWT verifies with the algorithm a key is bound to, and binds a key that
    # declares none to its type's default; bind it to the token's allowed algorithm
    # instead, and never use a key published for another.
    data = getattr(key, "_jwk_data", None)
    if not isinstance(data, dict):
        raise Refused("unknown signing key")
    if data.get("alg") not in (None, alg):
        raise Refused("the key is published for another algorithm")
    try:
        key = jwt.PyJWK(data, algorithm=alg)
    except jwt.PyJWTError as e:
        raise Refused("the key does not fit the algorithm") from e
    try:
        return jwt.decode(
            token,
            key,
            algorithms=[alg],
            issuer=issuer,
            leeway=SKEW,
            options=_DECODE_OPTIONS,
        )
    except jwt.ExpiredSignatureError as e:
        raise Refused("expired") from e
    except jwt.InvalidIssuerError as e:
        raise Refused("wrong issuer") from e
    except jwt.InvalidSignatureError as e:
        raise Refused("bad signature") from e
    except jwt.InvalidKeyError as e:
        raise Refused("signing key too short") from e
    except jwt.InvalidTokenError as e:
        raise Refused(f"invalid token: {type(e).__name__}") from e


def _at(event: dict, path: str) -> object:
    node: object = event
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _claims_from_input(event: dict, path: str) -> dict:
    parts = path.split(".")
    if not path or any(p in _CLIENT_WRITTEN for p in parts):
        raise Refused("claims path names a part the client writes")
    claims = _at(event, path)
    if not isinstance(claims, dict):
        raise Refused("no claims in the Gateway's input")
    return claims


def _as_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value if str(v)]
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.startswith("["):
            try:
                decoded = json.loads(text)
            except ValueError:
                decoded = text[1:-1].split()
            return _as_list(decoded) if isinstance(decoded, list) else []
        return text.split()
    return []


def checked(claims: dict) -> dict:
    """The caller these claims name, or Refused."""
    if issuer_mode():
        return checked_issuer(claims)
    if claims.get("token_use") != "access":
        raise Refused("not an access token")
    clients = _clients()
    client_id = claims.get("client_id")
    if not isinstance(client_id, str) or client_id not in clients:
        raise Refused("wrong client")
    url = resource()
    audiences = {a.rstrip("/") for a in _as_list(claims.get("aud"))}
    scopes = set(_as_list(claims.get("scope")))
    if url not in audiences and f"{url}/{SCOPE}" not in scopes:
        raise Refused("not issued for this Gateway")
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub.strip():
        raise Refused("no subject")
    username = claims.get("username") or claims.get("cognito:username") or sub
    return {
        "sub": sub,
        "username": str(username)[:256],
        "client": clients[client_id],
        "client_id": client_id,
        "groups": _as_list(claims.get("cognito:groups")),
    }


def _audience() -> str:
    # Unset is "", which no token's `aud` holds: it admits no one.
    audience = os.environ.get("AUDIENCE", "")
    return resource() if audience == "gateway" else audience.rstrip("/")


def checked_issuer(claims: dict) -> dict:
    """The caller an issuer's claims name, or Refused. No `token_use`: only Cognito
    marks one."""
    if str(claims.get("iss") or "").rstrip("/") != expected_issuer():
        raise Refused("wrong issuer")
    audiences = {a.rstrip("/") for a in _as_list(claims.get("aud"))}
    if _audience() not in audiences:
        raise Refused("not issued for this Gateway")
    allowed = {v for v in os.environ.get("ALLOWED_CLIENTS", "").split(",") if v}
    # An on-behalf-of token (RFC 8693) names the acting client in `act.client_id`; its
    # `aud` is this Gateway, not the client.
    act = claims.get("act")
    presented = [
        claims.get("azp"),
        claims.get("client_id"),
        act.get("client_id") if isinstance(act, dict) else None,
        *_as_list(claims.get("aud")),
    ]
    client_id = next(
        (c for c in presented if isinstance(c, str) and c in allowed), None
    )
    if client_id is None:
        raise Refused("wrong client")
    sub = claims.get(os.environ.get("SUBJECT_CLAIM") or "sub")
    if not isinstance(sub, str) or not sub.strip():
        raise Refused("no subject")
    display = claims.get(os.environ.get("DISPLAY_CLAIM") or "email")
    return {
        "sub": sub,
        "username": str(display if isinstance(display, str) and display else sub)[:256],
        "client": ISSUER_CLIENT,
        "client_id": client_id,
        "groups": issuer_groups(claims),
    }


def issuer_groups(claims: dict) -> list[str]:
    if os.environ.get("GROUPS_FROM") == "pool":
        return pool_groups(claims)
    values = _as_list(claims.get(os.environ.get("GROUPS_CLAIM") or "groups"))
    mapping = json.loads(os.environ.get("GROUP_MAP") or "{}")
    if not mapping:
        return values
    # Only mapped groups, as the pool's group hook adds them to a console token.
    return list(dict.fromkeys(g for v in values for g in mapping.get(v, [])))


_pool_cache: dict[str, tuple[float, list[str]]] = {}


def _cognito():
    return boto3.client("cognito-idp")


def pool_groups(claims: dict) -> list[str]:
    """The groups of the one enabled pool user whose verified email is the token's."""
    raw = claims.get(os.environ.get("EMAIL_CLAIM") or "email")
    if not isinstance(raw, str) or not raw.strip() or '"' in raw or "\\" in raw:
        raise Refused("no usable email claim")
    # Directories differ in the case they emit; an address is matched without it.
    email = raw.strip().lower()
    if claims.get("email_verified") is False or claims.get("email_verified") == "false":
        raise Refused("the issuer says the email is not verified")
    now = time.monotonic()
    hit = _pool_cache.get(email)
    if hit and now - hit[0] < POOL_TTL:
        return list(hit[1])
    pool = os.environ.get("USER_POOL_ID", "")
    if not pool:
        raise Refused("no user pool configured")
    idp = _cognito()
    try:
        users = (
            idp.list_users(UserPoolId=pool, Filter=f'email = "{email}"', Limit=2).get(
                "Users"
            )
            or []
        )
    except Exception as e:  # an unreadable pool admits no one
        raise Refused(f"user pool unreadable: {type(e).__name__}") from e
    if len(users) != 1:
        raise Refused(
            f"{len(users)} pool users have that email; exactly one may ({_shape(raw)})"
        )
    user = users[0]
    attrs = {a.get("Name"): a.get("Value") for a in user.get("Attributes") or []}
    if not user.get("Enabled", False):
        raise Refused("the pool user is disabled")
    if (attrs.get("email") or "").lower() != email or attrs.get(
        "email_verified"
    ) != "true":
        raise Refused("the pool user's email is not that verified email")
    groups: list[str] = []
    token = None
    try:
        while True:
            page = idp.admin_list_groups_for_user(
                UserPoolId=pool,
                Username=user["Username"],
                **({"NextToken": token} if token else {}),
            )
            groups += [g["GroupName"] for g in page.get("Groups") or []]
            token = page.get("NextToken")
            if not token:
                break
    except Exception as e:  # unreadable groups admit no one
        raise Refused(f"user pool unreadable: {type(e).__name__}") from e
    _pool_cache[email] = (now, groups)
    return list(groups)


def _shape(value: str) -> str:
    """What a refused email claim looked like, without the address: enough to tell an
    alias from an address, and a domain or case mismatch from a missing user."""
    if "@" not in value:
        return f"no @, {len(value)} characters"
    domain = value.rsplit("@", 1)[1]
    case = "with capitals" if value != value.lower() else "lower case"
    return f"an address at {domain}, {case}"


def caller_of(event: dict, request: dict) -> dict:
    path = os.environ.get("CLAIMS_PATH", "")
    if path:
        claims = _claims_from_input(event, path)
    else:
        claims = verify_token(_bearer(request.get("headers")))
    return checked(claims)


def _log(body: object, verdict: str, caller: dict | None, reason: str | None) -> None:
    body = body if isinstance(body, dict) else {}
    params = body.get("params") if isinstance(body.get("params"), dict) else {}
    line = {
        "gate": 2,
        "method": body.get("method"),
        "tool": params.get("name"),
        "request_id": body.get("id"),
        "sub": (caller or {}).get("sub"),
        "client": (caller or {}).get("client"),
        "verdict": verdict,
        "reason": reason,
    }
    level = logging.INFO if verdict == "allowed" else logging.WARNING
    logger.log(level, "people_interceptor %s", json.dumps(line, default=str))


def _refusal(request_id, text: str) -> dict:
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
                                "text": f"Refused by the people Gateway: {text}",
                            }
                        ],
                    },
                },
            }
        },
    }


def handler(event, _context=None):
    mcp = (event or {}).get("mcp") if isinstance(event, dict) else None
    request = mcp.get("gatewayRequest") if isinstance(mcp, dict) else None
    if not isinstance(request, dict):
        _log(None, "refused", None, "no request in the event")
        return _refusal(None, "the request could not be read")
    body = request.get("body")
    request_id = body.get("id") if isinstance(body, dict) else None
    if not isinstance(body, dict):
        _log(body, "refused", None, "not a single JSON-RPC call")
        return _refusal(None, "the request is not a single JSON-RPC call")
    try:
        caller = caller_of(event, request)
    except Refused as why:
        _log(body, "refused", None, str(why))
        return _refusal(request_id, "the token could not be verified")
    out = copy.deepcopy(body)
    if out.get("method") == "tools/call":
        params = out.get("params")
        arguments = params.get("arguments", {}) if isinstance(params, dict) else None
        if not isinstance(arguments, dict):
            _log(body, "refused", caller, "arguments are not an object")
            return _refusal(request_id, "the call's arguments are not an object")
        params["arguments"] = {**arguments, "caller": caller}
    _log(body, "allowed", caller, None)
    # The body alone: echoing the headers would carry the token on, and the Gateway
    # refuses a transformed request that repeats them.
    return {
        "interceptorOutputVersion": _OUTPUT_VERSION,
        "mcp": {"transformedGatewayRequest": {"body": out}},
    }
