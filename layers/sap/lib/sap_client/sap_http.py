# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""A direct caller's way into SAP: GET over https as a system's service account.

For a use case's own function that `layers.sap.access.<use case>.bypass_gateway` lets
call SAP outside the Gateway (a poller); copy this file into its asset, flat. Stdlib
only. The credential is read per system from Secrets Manager, the same secret that
system's MCP server signs in with, and the system's `flow` says what it is:

- `basic` (the default): `{"username", "password"}`, sent as Basic auth.
- `m2m-sap` / `m2m-oidc`: `{"clientId", "clientSecret"}`, exchanged for a
  client-credentials token at `token_url` (SAP's SOAUTH2) or at the `token_endpoint`
  `discovery_url` names (an IdP), and sent as Bearer.

It is cached for `CREDENTIAL_TTL_SECONDS` (a token, at most until
`TOKEN_MARGIN_SECONDS` before it expires), and read again at once when SAP refuses it,
so a rotated secret (`deploy.sh layer run sap secrets <system>`) is used without
waiting for a cold start.
"""

from __future__ import annotations

import base64
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

import boto3

_secrets = boto3.client("secretsmanager")

CREDENTIAL_TTL_SECONDS = 15 * 60
#: A token is replaced this long before the `expires_in` it came with.
TOKEN_MARGIN_SECONDS = 60

# Config writes base_url in the OData-root form; callers append the service path.
_ODATA_ROOT_RE = re.compile(r"/sap/opu/odata/sap/?$", re.IGNORECASE)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib carries the Authorization header across a redirect to any host."""

    def redirect_request(self, *args, **kwargs):
        return None  # a 3xx then raises HTTPError


_opener = urllib.request.build_opener(_NoRedirect)


class SapError(Exception):
    """A SAP read failed. The message never carries a credential or a response body."""


def normalize_base_url(url: str) -> str:
    """The host root, refusing anything but https: this sends credentials unattended."""
    normalized = _ODATA_ROOT_RE.sub("", url.strip()).rstrip("/")
    if not normalized.lower().startswith("https://"):
        raise ValueError(f"SAP base_url must use https, got {normalized!r}")
    return normalized


@dataclass
class SapSystem:
    name: str
    base_url: str
    authorization: str
    client: str | None = None
    #: The Secrets Manager id the authorization came from, re-read on a refusal.
    secret: str | None = None
    #: A token flow's `{flow, token_url, discovery_url, scopes}`; None is Basic.
    oauth: dict | None = None
    #: `time.monotonic()` from which `authorization` (a token) is fetched again first.
    expires: float = math.inf

    def header(self) -> str:
        """The Authorization value to send, a token fetched again shortly before expiry."""
        if time.monotonic() >= self.expires:
            self.authorization, self.expires = _authorization(self.secret, self.oauth)
        return self.authorization

    def get_json(self, url: str, params: dict | None = None) -> dict:
        """GET `url`, which must sit under this system's base URL. Raises SapError."""
        # The Authorization header goes to whatever URL this is handed, and paging
        # follows a URL SAP's own response names, so pin it to the system's origin.
        if not same_origin(url, self.base_url):
            raise SapError(f"{self.name}: refusing a URL outside {self.base_url}")
        query = dict(params or {})
        if self.client and "sap-client" not in url:
            query["sap-client"] = self.client
        if query:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{urllib.parse.urlencode(query)}"
        for attempt in (1, 2):
            req = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "Authorization": self.header(),
                },
            )
            try:
                with _opener.open(req, timeout=30) as resp:  # nosec B310 - https only, origin-pinned, no redirects
                    return json.loads(resp.read())
            except urllib.error.HTTPError as e:
                # Basic is retried only when the secret has changed since it was read:
                # a genuine authority failure is not sent twice, and a wrong password
                # is never replayed toward a lockout. A token refused as 401 (revoked,
                # or expired early) is fetched again once; a 403 is SAP's authority
                # check, which a new token would not change.
                refused = e.code == 401 if self.oauth else e.code in (401, 403)
                if refused and attempt == 1 and self.secret:
                    fresh, expires = _authorization(self.secret, self.oauth)
                    if self.oauth or fresh != self.authorization:
                        self.authorization, self.expires = fresh, expires
                        continue
                raise SapError(f"{self.name}: HTTP {e.code}") from None
            except (urllib.error.URLError, TimeoutError, ValueError) as e:
                raise SapError(f"{self.name}: {type(e).__name__}") from None
        raise AssertionError("unreachable")  # pragma: no cover


def same_origin(url: str, base_url: str) -> bool:
    a, b = urllib.parse.urlsplit(url), urllib.parse.urlsplit(base_url)
    return (a.scheme, a.netloc.lower()) == (b.scheme, b.netloc.lower())


_cache: dict[str, tuple[SapSystem, float]] = {}


def _authorization(secret_id: str, oauth: dict | None = None) -> tuple[str, float]:
    """(the Authorization value, the monotonic time to fetch it again from)."""
    creds = json.loads(_secrets.get_secret_value(SecretId=secret_id)["SecretString"])
    if oauth is None:
        token = base64.b64encode(
            f"{creds['username']}:{creds['password']}".encode()
        ).decode()
        return f"Basic {token}", math.inf
    token_url = (
        oauth.get("token_url") or _json(oauth["discovery_url"])["token_endpoint"]
    )
    # RFC 6749 2.3.1 form-encodes the pair before Basic, and an IdP decodes it. SAP's
    # SOAUTH2 does not: it refuses a secret carrying `+` or `/` once encoded (measured),
    # so m2m-sap sends the pair as it is.
    encode = (
        (lambda v: v) if oauth.get("flow") == "m2m-sap" else urllib.parse.quote_plus
    )
    pair = ":".join(encode(creds[k]) for k in ("clientId", "clientSecret"))
    body = _json(
        token_url,
        data=urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "scope": " ".join(oauth.get("scopes") or []),
            }
        ).encode(),
        headers={
            "Authorization": f"Basic {base64.b64encode(pair.encode()).decode()}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    # expires_in is only RECOMMENDED (RFC 6749 5.1); without it, assume five minutes.
    expires = time.monotonic() + int(body.get("expires_in", 300)) - TOKEN_MARGIN_SECONDS
    return f"Bearer {body['access_token']}", expires


def _json(url: str, data: bytes | None = None, headers: dict | None = None) -> dict:
    """JSON from the token endpoint or the discovery document. Raises SapError."""
    if not url.lower().startswith("https://"):
        raise SapError(f"token request: refusing {url!r}, which is not https")
    req = urllib.request.Request(
        url, data=data, headers={"Accept": "application/json", **(headers or {})}
    )
    try:
        with _opener.open(req, timeout=30) as resp:  # nosec B310 - https only, no redirects
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise SapError(f"token request: HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise SapError(f"token request: {type(e).__name__}") from None


def system(name: str, spec: dict) -> SapSystem:
    """The named system, `spec` being its `{base_url, secret, client}` from SAP_SYSTEMS,
    with `{flow, token_url, discovery_url, scopes}` too for a token flow."""
    cached = _cache.get(name)
    if cached is None or time.monotonic() - cached[1] > CREDENTIAL_TTL_SECONDS:
        oauth = spec if spec.get("flow", "basic") != "basic" else None
        authorization, expires = _authorization(spec["secret"], oauth)
        cached = (
            SapSystem(
                name,
                normalize_base_url(spec["base_url"]),
                authorization,
                spec.get("client"),
                spec["secret"],
                oauth,
                expires,
            ),
            time.monotonic(),
        )
        _cache[name] = cached
    return cached[0]
