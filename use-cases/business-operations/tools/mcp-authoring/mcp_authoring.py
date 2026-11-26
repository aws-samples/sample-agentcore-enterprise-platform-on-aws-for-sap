#!/usr/bin/env python3

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""A stdio MCP server that authors packs and SOPs **as the person using it**.

This is the headless twin of the console's `/packs` screen, not of
the shared `sap_kit/mcp_gateway_proxy.py`. It fronts the use case's **REST API**,
and the difference is the whole point: the Gateway proxy authenticates with client
credentials, so its token is a *service* identity carrying no group claim, which
means the audit trail names the service rather than a person and none of the
per-pack authorization can be evaluated at all. Every write here travels on a
user's own ID token from the console client, so the `business-operations-authors`
floor and each pack's `entitlement.author_groups`/`constant_groups` are enforced
server-side, per pack, on every call.

**One input: the application URL.** `{app-url}/aws-exports.json` is public and
unauthenticated, and it carries `authority`, `client_id` and `apiUrl` — everything
needed to run the auth-code + PKCE flow and then reach the API. So this file needs no
AWS credentials, no boto3, no SSM read and no checkout of this repo; copy it to the
analyst's laptop and give them a URL. Honouring `metadata_url` the way the frontend
does also makes it issuer-agnostic for free: an Entra or Okta auth profile works
through the same discovery.

    ./scripts/deploy.sh usecase run business-operations mcp-config   # the host config
    python3 mcp_authoring.py login --app-url https://…   # sign in once, up front

Signing in is also lazy: the first tool call opens a browser if no cached refresh
token works, so a host that never runs `login` still works — it just pays for the
sign-in inside that call's timeout.

**Refusals are the product.** A 400 or a 403 from the API is returned verbatim as the
tool's content with `isError` set, never swallowed or reworded: those bodies say which
tier a key belongs to and where to take it instead, and that text is how an analyst
learns the boundary. Content and constants are separate tools for the same reason the
Gateway proxy's allowlist exists — to keep a model from wandering across a line — and
with the same caveat: it is a filter, not a boundary. Content is a `PATCH /packs/{pack_id}`,
which `pack_composer` re-checks with `pack_tiers`, and a rule constant's value is content
too. What `PUT /packs/{pack_id}/controls` takes is this stack's own: a write limit lowered
below the pack's value.

**`create_pack` is a fourth tier and not the privileged one.** It allowlists no keys at
all: the caller sends a *spec* of what the use case is, and the deployment composes every
privileged value itself — the read-only Gateway tools, the model tier, the SAP source, the
field map, the routing. So the one thing this server cannot do is what the browser cannot
do either. Rehearsal is the exception in the other direction: `POST /runs` starts one, but
a transcript reaches only the AppSync Events channel and nothing persists it, so there is
nothing a stdio server could hand back. That stays on the `/packs` screen.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import IO, Any

#: Cognito matches `redirect_uri` **exactly** and permits plain HTTP only for the
#: literal host `localhost` (not `127.0.0.1`), so neither the port nor the path can be
#: negotiated at runtime: this string and the console client's callback in
#: `business_operations/api.py` (`AUTHORING_REDIRECT_URI`) are one contract, pinned by
#: `tests/test_business_operations_mcp_clients.py`. 12800 sits
#: below every ephemeral range (macOS 49152+, Linux 32768+) and clear of the ports a
#: dev server usually holds — 3000, 4200, 5173, 8000, 8080, 9000.
REDIRECT_PORT = 12800
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"

#: Listen on the loopback address; send the `localhost` spelling Cognito registered.
BIND_HOST = "127.0.0.1"

#: What the console asks for when `aws-exports.json` names no scope (`frontend/src/lib/auth.ts`).
DEFAULT_SCOPE = "email openid profile"

#: One file per deployment, so two stacks do not evict each other's sign-in.
CACHE_DIR = Path.home() / ".business-operations-authoring"

#: How long the browser has to come back before the tool call gives up.
LOGIN_TIMEOUT_SECONDS = 300

#: Refresh this far ahead of expiry, so a token cannot age out mid-request.
REFRESH_SKEW_SECONDS = 120

_HTTP_TIMEOUT_SECONDS = 60

# JSON-RPC codes.
_METHOD_NOT_FOUND = -32601
_INTERNAL_ERROR = -32603

PROTOCOL_VERSION = "2024-11-05"

#: `create_pack`'s two enums, copied rather than imported: this file is stdlib-only so it
#: can be handed to an analyst with no checkout, and `pack_compose` lives in a Lambda layer
#: and imports pydantic. Pinned to `pack_compose.CREATE_KINDS` and
#: `pack_compose.ATTRIBUTE_TYPES` by `tests/test_business_operations_mcp_clients.py`. They are
#: in the schema rather than left to the API's refusal because a model that has to guess
#: `kind` will guess a word that reads right and earn a 400 the analyst then has to read.
_CREATE_KINDS = ("filter", "absence", "aging")
_ATTRIBUTE_TYPES = ("string", "date", "amount", "decimal")

#: (status, body) — the one HTTP seam, so tests replace a function rather than a socket.
Request = Callable[..., tuple[int, bytes]]


class AuthoringError(RuntimeError):
    """Something this server cannot fix by retrying: a missing discovery key, a
    refused sign-in, a port already held. Reported to the caller as written."""


def checked_url(url: str) -> str:
    """Refuse any scheme but HTTP(S) before it reaches `urlopen`.

    The application URL is operator input, and `aws-exports.json` is *fetched
    content* that then names both the authorization server and the API — so a
    `file:` or `data:` URL could arrive from either. urllib would honour it.
    """
    scheme = urllib.parse.urlparse(url).scheme
    if scheme not in ("http", "https"):
        raise AuthoringError(
            f"refusing to fetch a {scheme or 'scheme-less'} URL: {url[:120]}"
        )
    return url


def http_request(
    method: str,
    url: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes]:
    request = urllib.request.Request(
        checked_url(url), data=data, headers=headers or {}, method=method
    )
    try:
        # nosec B310 — checked_url() has already restricted the scheme to http(s).
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:  # nosec B310  # nosemgrep: dynamic-urllib-use-detected
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def get_json(request: Request, url: str) -> dict[str, Any]:
    status, body = request("GET", url)
    text = body.decode("utf-8", "replace")
    if status >= 400:
        raise AuthoringError(f"GET {url} answered {status}: {text[:300]}")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AuthoringError(f"GET {url} did not answer JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise AuthoringError(
            f"GET {url} answered {type(parsed).__name__}, not an object"
        )
    return parsed


def _require(source: dict[str, Any], key: str, where: str) -> str:
    value = source.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AuthoringError(f"{where} carries no usable {key!r}")
    return value.strip()


@dataclass(frozen=True)
class Deployment:
    """Everything the flow needs, all of it discovered from one URL."""

    api_url: str
    client_id: str
    authorization_endpoint: str
    token_endpoint: str
    scope: str
    #: Names the cache file. The app URL's host, so one per deployment.
    label: str


def discover(app_url: str, request: Request = http_request) -> Deployment:
    """`{app-url}/aws-exports.json`, then the IdP's own OIDC discovery document.

    `metadata_url` wins over `authority` when present, exactly as `frontend/src/lib/config.ts`
    resolves it — that key is how a non-Cognito auth profile names its issuer, and
    the two are mutually exclusive.
    """
    base = app_url.rstrip("/")
    exports_url = f"{base}/aws-exports.json"
    exports = get_json(request, exports_url)

    metadata_url = exports.get("metadata_url") or exports.get("metadataUrl")
    if isinstance(metadata_url, str) and metadata_url.strip():
        discovery_url = metadata_url.strip()
    else:
        authority = _require(exports, "authority", exports_url).rstrip("/")
        discovery_url = f"{authority}/.well-known/openid-configuration"
    oidc = get_json(request, discovery_url)

    scope = exports.get("scope")
    return Deployment(
        api_url=_require(exports, "apiUrl", exports_url).rstrip("/"),
        client_id=_require(exports, "client_id", exports_url),
        authorization_endpoint=_require(oidc, "authorization_endpoint", discovery_url),
        token_endpoint=_require(oidc, "token_endpoint", discovery_url),
        scope=scope.strip()
        if isinstance(scope, str) and scope.strip()
        else DEFAULT_SCOPE,
        label=urllib.parse.urlparse(base).netloc or "default",
    )


def cache_path(label: str) -> Path:
    # A netloc may carry a port, and ':' is a path separator on some filesystems.
    return CACHE_DIR / f"{label.replace(':', '_')}.json"


def read_cache(label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(cache_path(label).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # A missing or corrupt cache is not an error: it means "sign in again".
        return {}
    return parsed if isinstance(parsed, dict) else {}


def write_cache(label: str, payload: dict[str, Any]) -> None:
    """0600, created that way rather than corrected afterwards.

    The refresh token in here mints ID tokens for this person until it is revoked,
    so there must be no window in which the file is group- or world-readable —
    `O_CREAT`'s mode only applies to a file this call creates, hence the `chmod`
    for one that already existed.
    """
    CACHE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    # `mode` applies only to a directory this call creates, so a `~/.business-operations-authoring`
    # that already existed at 0755 stayed there — the token file inside is 0600, but
    # the directory listing names every application URL this person has signed in to.
    CACHE_DIR.chmod(0o700)
    path = cache_path(label)
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        json.dump(payload, stream)
    path.chmod(0o600)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def pkce_pair() -> tuple[str, str]:
    """(verifier, S256 challenge). 32 random bytes, base64url, unpadded — RFC 7636."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


class _CallbackHandler(BaseHTTPRequestHandler):
    """The loopback listener the IdP redirects back to.

    Anything that is not the callback gets a 404 rather than ending the wait — a
    browser will ask this port for `/favicon.ico` given the chance, and treating
    that as the answer would drop the real redirect on the floor.
    """

    def __init__(self, *args: Any, sink: dict[str, str], **kwargs: Any) -> None:
        self.sink = sink
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        if parsed.path != "/callback" or not {"code", "error"} & params.keys():
            self.send_error(404)
            return
        self.sink.update(params)
        body = (
            b"<!doctype html><title>Signed in</title>"
            b"<p>Signed in. Close this tab and return to your MCP client."
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        """Silence. This process's stdout is MCP protocol."""


def check_callback(params: dict[str, str], state: str) -> dict[str, str]:
    """Validate one redirect. **State first**, deliberately.

    Everything in `params` arrived from a browser, so a redirect that cannot prove it
    belongs to this request is not evidence of anything — an `error` included. Reading
    the error before the state would let any page that can reach this port end the
    sign-in with a message of its choosing.
    """
    if params.get("state") != state:
        raise AuthoringError("discarding a redirect whose `state` does not match")
    if "error" in params:
        detail = params.get("error_description") or ""
        raise AuthoringError(
            f"the identity provider refused: {params['error']} {detail}"
        )
    if not params.get("code"):
        raise AuthoringError("the redirect carried neither a code nor an error")
    return params


def wait_for_callback(
    state: str, timeout: float = LOGIN_TIMEOUT_SECONDS
) -> dict[str, str]:
    sink: dict[str, str] = {}
    try:
        server = HTTPServer(
            (BIND_HOST, REDIRECT_PORT), partial(_CallbackHandler, sink=sink)
        )
    except OSError as exc:
        raise AuthoringError(
            f"cannot listen on {BIND_HOST}:{REDIRECT_PORT} ({exc}). The port is fixed "
            "because the IdP matches redirect_uri exactly — free it and try again."
        ) from exc

    deadline = time.monotonic() + timeout
    with server:
        server.timeout = 1
        while not sink and time.monotonic() < deadline:
            server.handle_request()

    if not sink:
        raise AuthoringError(
            f"no redirect arrived within {int(timeout)}s — sign-in was not completed"
        )
    return check_callback(sink, state)


@dataclass
class Session:
    """One person's ID token for one deployment, refreshed or re-minted as needed."""

    deployment: Deployment
    request: Request = http_request
    log: Callable[[str], None] = lambda message: None
    #: Resolved at the call site, not here: a dataclass freezes a default at class
    #: creation, so `webbrowser.open` as the default is unreachable to a patch and any
    #: test that reaches `login()` without injecting one opens a real browser tab.
    open_browser: Callable[[str], Any] | None = None
    _id_token: str | None = field(default=None, init=False)
    _refresh_token: str | None = field(default=None, init=False)
    _expires_at: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        cached = read_cache(self.deployment.label)
        self._refresh_token = cached.get("refresh_token")
        self._id_token = cached.get("id_token")
        self._expires_at = float(cached.get("expires_at") or 0.0)

    def id_token(self) -> str:
        """The token to send. Cached, then refreshed, then interactive."""
        if self._id_token and time.time() < self._expires_at - REFRESH_SKEW_SECONDS:
            return self._id_token
        if self._refresh_token:
            try:
                return self._refresh()
            except AuthoringError as exc:
                # A revoked or expired refresh token is the ordinary case here, not a
                # failure to report: fall through to a fresh sign-in.
                self.log(f"refresh failed ({exc}) — signing in again")
        return self.login()

    def login(self) -> str:
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(16)
        url = (
            self.deployment.authorization_endpoint
            + "?"
            + urllib.parse.urlencode(
                {
                    "response_type": "code",
                    "client_id": self.deployment.client_id,
                    "redirect_uri": REDIRECT_URI,
                    "scope": self.deployment.scope,
                    "state": state,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                }
            )
        )
        # Printed as well as opened: on a machine with no browser — a container, an
        # SSH session — stderr is the only way through, and an MCP host shows it.
        self.log(f"sign in to continue: {url}")
        try:
            (self.open_browser or webbrowser.open)(url)
        except Exception as exc:  # noqa: BLE001 - a headless box has no browser; the URL above stands
            self.log(
                f"could not open a browser ({type(exc).__name__}) — use the URL above"
            )
        params = wait_for_callback(state)
        return self._exchange(
            {
                "grant_type": "authorization_code",
                "client_id": self.deployment.client_id,
                "code": params["code"],
                "redirect_uri": REDIRECT_URI,
                "code_verifier": verifier,
            }
        )

    def _refresh(self) -> str:
        return self._exchange(
            {
                "grant_type": "refresh_token",
                "client_id": self.deployment.client_id,
                "refresh_token": self._refresh_token or "",
            }
        )

    def _exchange(self, fields: dict[str, str]) -> str:
        status, body = self.request(
            "POST",
            self.deployment.token_endpoint,
            data=urllib.parse.urlencode(fields).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        text = body.decode("utf-8", "replace")
        if status >= 400:
            raise AuthoringError(f"token endpoint answered {status}: {text[:300]}")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AuthoringError(f"token endpoint did not answer JSON: {exc}") from exc

        id_token = payload.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            # The API takes the **ID** token, not the access token — `useAgentChat.ts`
            # and `PacksPage.tsx` both send `idToken`, and only the ID token carries
            # `cognito:groups`, which is what every authorization gate reads.
            raise AuthoringError("token endpoint returned no id_token")
        self._id_token = id_token
        self._expires_at = time.time() + float(payload.get("expires_in") or 3600)
        # A refresh response carries no new refresh token, so the old one is kept.
        if isinstance(payload.get("refresh_token"), str):
            self._refresh_token = payload["refresh_token"]
        write_cache(
            self.deployment.label,
            {
                "refresh_token": self._refresh_token,
                "id_token": self._id_token,
                "expires_at": self._expires_at,
            },
        )
        return id_token


@dataclass
class Api:
    """The deployment's REST API, called as the signed-in person."""

    deployment: Deployment
    session: Session
    request: Request = http_request

    def call(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> tuple[int, str]:
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "Authorization": f"Bearer {self.session.id_token()}",
            "Accept": "application/json",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        status, raw = self.request(
            method, f"{self.deployment.api_url}{path}", data=data, headers=headers
        )
        return status, raw.decode("utf-8", "replace")


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    method: str
    route: Callable[[dict[str, Any]], str]
    body: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None


def _content_body(arguments: dict[str, Any]) -> dict[str, Any]:
    """A content patch, with only the fields the caller actually named.

    `base_prompt` travels at the top level rather than inside `pack`, because it is its
    own file; every field here is content, so they may share a request. `constants`
    carries values only: the API refuses a name the pack does not declare yet, and either
    write limit, as its structure.
    """
    pack = {
        key: arguments[key]
        for key in ("display_name", "description", "constants")
        if key in arguments
    }
    patch: dict[str, Any] = {}
    if pack:
        patch["pack"] = pack
    for key in ("base_prompt", "expected_version"):
        if key in arguments:
            patch[key] = arguments[key]
    return patch


#: The optional optimistic check both writes take, in development.
_EXPECTED_VERSION = {
    "type": "integer",
    "description": (
        "The pack version you read (read_base_prompt's `version`, get_sop's "
        "`pack_version`). If someone saved the pack since, the write is refused with a "
        "409 naming the version it is at, and nothing is written: read again, reapply "
        "your change, and retry."
    ),
}


def _pack(arguments: dict[str, Any], *rest: str) -> str:
    """`/packs/<pack_id>[/<rest>…]`, each segment quoted: an argument never adds a path."""
    segments = [arguments.get("pack_id", ""), *rest]
    return "/packs/" + "/".join(urllib.parse.quote(str(x), safe="") for x in segments)


def _sop(arguments: dict[str, Any], *rest: str) -> str:
    """`/packs/<pack_id>/sops/<process_type>[/<rest>]`."""
    return _pack(arguments, "sops", str(arguments.get("process_type", "")), *rest)


#: `pack_id`, as every per-pack tool takes it.
_PACK_ID = {"type": "string", "description": "A pack id as list_packs reports it."}
_PROCESS_TYPE = {
    "type": "string",
    "description": (
        "A process type the pack maps, as list_sops reports it. The SOP is the file it "
        "maps to; an alias reads its file and is refused a write."
    ),
}


def _pack_only(extra: dict[str, Any] | None = None, required=("pack_id",)) -> dict:
    return {
        "type": "object",
        "properties": {"pack_id": _PACK_ID, **(extra or {})},
        "required": list(required),
        "additionalProperties": False,
    }


def _create_body(arguments: dict[str, Any]) -> dict[str, Any]:
    """A create request: the `pack_id` addresses it, everything else is the spec.

    Forwarded as sent rather than filtered to `SPEC_KEYS`, because a submitter who typed
    `gateway_tools` needs to read the API's refusal naming the tier it belongs to — a key
    dropped here would compose a pack that silently does not hold what they asked for.
    """
    return {
        "pack_id": arguments.get("pack_id"),
        "create": {key: value for key, value in arguments.items() if key != "pack_id"},
    }


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="list_packs",
        description=(
            "Every use case pack this deployment has published, with its display "
            "name, description, SAP source and process-type routing. Deliberately "
            "omits each pack's agent instructions and its constants — use "
            "read_base_prompt for the instructions, read_rules and read_controls for "
            "the numbers."
        ),
        schema={"type": "object", "properties": {}, "additionalProperties": False},
        method="GET",
        route=lambda _: "/registry",
    ),
    Tool(
        name="read_rules",
        description=(
            "One pack's rule values (tolerances, escalation ages) effective here, each "
            "with its source (`pack v7`), the write guards that read it, and the bounds "
            "a new value is checked against. A rule's value is pack content: change it "
            "with publish_pack_content."
        ),
        schema=_pack_only(),
        method="GET",
        route=lambda a: _pack(a, "rules"),
    ),
    Tool(
        name="read_controls",
        description=(
            "One pack's controls: AUTO_WRITE_LIMIT_USD and DUAL_APPROVAL_LIMIT_USD "
            "effective here with the pack's ceiling (`stack override (lowered from "
            "100000)` when this stack lowered one), who works and approves its cases, "
            "and the checks before every SAP write."
        ),
        schema=_pack_only(),
        method="GET",
        route=lambda a: _pack(a, "controls"),
    ),
    Tool(
        name="read_deployment_settings",
        description=(
            "This stack's contacts as SOPs cite them (`{{CONTACT_<NAME>}}`): the "
            "platform.yaml value, this stack's override and the effective one, and "
            "where the deployment runs: its mode and each SAP system's system_role."
        ),
        schema={"type": "object", "properties": {}, "additionalProperties": False},
        method="GET",
        route=lambda _: "/deployment/settings",
    ),
    Tool(
        name="read_base_prompt",
        description=(
            "One pack's agent instructions as currently published — the text "
            "publish_pack_content replaces. Read this before revising a prompt: the "
            "write is an outright replacement, not a patch, and list_packs omits the "
            "prompt on purpose. Placeholders come back literal ({SOP_CONTENT}, "
            "{PLATFORM_MECHANICS}) and a replacement must keep both. Refused unless you "
            "hold this pack's author_groups. `version` is the pack version you read, "
            "which publish_pack_content takes as `expected_version`."
        ),
        schema=_pack_only(),
        method="GET",
        route=lambda a: _pack(a, "base_prompt"),
    ),
    Tool(
        name="list_sops",
        description=(
            "One pack's SOPs as the deployment holds them — S3, not any checkout — one "
            "row per process type, with the part its SOP plays (triage, exception, "
            "shared, alias), whether you may author them and the search index's state."
        ),
        schema=_pack_only(),
        method="GET",
        route=lambda a: _pack(a, "sops"),
    ),
    Tool(
        name="get_sop",
        description=(
            "One SOP's full text, by a process type that maps it. `pack_version` is its "
            "pack's version as read, which put_sop takes as `expected_version`."
        ),
        schema=_pack_only(
            {"process_type": _PROCESS_TYPE}, required=("pack_id", "process_type")
        ),
        method="GET",
        route=lambda a: _sop(a),
    ),
    Tool(
        name="create_pack",
        description=(
            "Mint a use case pack that does not exist yet. You send a **spec** — what the "
            "exception is, in your own words — and the deployment composes the pack "
            "around it: the read-only Gateway tools, the model tier, the SAP source, the "
            "field map and the process-type-to-SOP routing are all chosen for you, and a "
            "spec naming any of them is refused rather than quietly stripped. "
            "\n\n"
            "The pack is born **incomplete**: every SAP value is an <AWAITING_SURVEY> "
            "marker, so nothing polls it and no case routes to it. Its constants are live "
            "and its content is authorable straight away, and the only way to exercise it "
            "is a rehearsal from the /packs screen. Binding it to a real SAP service is a "
            "survey plus a deploy from a checkout, deliberately. "
            "\n\n"
            "Two refusals are this route's own: a **409** if `pack_id` is already "
            "published, with nothing written — overwriting would replace a live pack's "
            "source and tools with markers — and a **400** if another pack already claims "
            "your `short_id`. Authorized by the `business-operations-authors` group "
            "alone, because a pack that does not exist declares no groups of its own. "
            "\n\n"
            "Follow it with put_sop for each process type it maps, then sync_sops."
        ),
        schema={
            "type": "object",
            "properties": {
                "pack_id": {
                    "type": "string",
                    "description": (
                        "The pack's directory name and its id everywhere — lowercase with "
                        "underscores, e.g. `outbound_delivery_block`. Cannot be changed "
                        "later, and a taken one is a 409."
                    ),
                },
                "display_name": {
                    "type": "string",
                    "description": "What an operator sees, e.g. `Outbound Delivery Block`.",
                },
                "description": {
                    "type": "string",
                    "description": "One or two sentences on what this pack is for.",
                },
                "short_id": {
                    "type": "string",
                    "description": (
                        "2-8 characters, lowercase letters and digits, starting with a "
                        "letter. It is the prefix of every case id this pack mints, so "
                        "another pack claiming it is a 400."
                    ),
                },
                "kind": {
                    "type": "string",
                    "enum": list(_CREATE_KINDS),
                    "description": (
                        "How the exception is found: `filter` matches a condition on a "
                        "record, `absence` catches a record missing an expected "
                        "counterpart, `aging` catches one older than a threshold. Any "
                        "other kind needs a push integration, so it is authored from a "
                        "checkout."
                    ),
                },
                "definition": {
                    "type": "string",
                    "description": (
                        "The exception in business language — what makes a record a case "
                        "this pack should work. This is what the composed prompt is "
                        "written around, so it does the most work of any field here."
                    ),
                },
                "process_types": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string"},
                    "description": (
                        "One name per situation this pack handles, lowercase with "
                        "underscores. Required, not optional: the routing is privileged, "
                        "so a process type omitted now cannot be added over HTTP later. "
                        "Each one is mapped to `<pack_id>/<process_type>.txt`, whose "
                        "text put_sop writes."
                    ),
                },
                "attributes": {
                    "type": "array",
                    "description": (
                        "The case fields this pack carries. Also effectively one-shot — "
                        "`fields` is privileged, so an attribute left out now is a "
                        "checkout change. An empty list composes cases with no attributes."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": (
                                    "Lowercase with underscores. May not shadow a "
                                    "platform property of every case."
                                ),
                            },
                            "label": {"type": "string"},
                            "type": {
                                "type": "string",
                                "enum": list(_ATTRIBUTE_TYPES),
                            },
                            "currency_field": {
                                "type": "string",
                                "description": (
                                    "Required on an `amount`, and must name another "
                                    "attribute declared here — a figure without its "
                                    "currency is a bare number."
                                ),
                            },
                            "panel": {
                                "type": "boolean",
                                "description": "Show it in the case detail panel.",
                            },
                        },
                        "required": ["name", "label", "type"],
                        "additionalProperties": False,
                    },
                },
                "constants": {
                    "type": "object",
                    "description": (
                        "The pack's default thresholds, checked against the ranges "
                        "every write path enforces."
                    ),
                    "additionalProperties": {"type": "number"},
                },
                "older_than_days": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "Only for `kind: aging`, and required there — how old a record "
                        "must be to become a case. Refused on any other kind."
                    ),
                },
            },
            "required": [
                "pack_id",
                "display_name",
                "description",
                "short_id",
                "kind",
                "definition",
                "process_types",
            ],
            "additionalProperties": False,
        },
        method="POST",
        route=lambda _: "/packs",
        body=_create_body,
    ),
    Tool(
        name="publish_pack_content",
        description=(
            "Publish a pack's CONTENT tier — what the agent says, and the values of the "
            "constants it already declares. Any of display_name, description, constants "
            "and base_prompt, together. A replacement "
            "base_prompt must keep {SOP_CONTENT} and {PLATFORM_MECHANICS}. Needs no "
            "deploy, and your read-back is immediate: every API handler reloads the "
            "corpus per request. What lags is the agent — a warm runtime container keeps "
            "the corpus it started with, so a case already in flight finishes on the "
            "previous prompt. Each publish is a new version of the pack, named in the "
            "reply's `version`, and `bundle rollback` returns to an earlier one. Only a "
            "development stack takes it: a quality or production stack refuses every "
            "edit, and gets the pack by bundle import. Refused unless you hold this "
            "pack's author_groups."
        ),
        schema={
            "type": "object",
            "properties": {
                "pack_id": {"type": "string"},
                "display_name": {"type": "string"},
                "description": {"type": "string"},
                "base_prompt": {
                    "type": "string",
                    "description": "Replaces base_prompt.txt outright — not a patch.",
                },
                "constants": {
                    "type": "object",
                    "description": (
                        "New values for constants the pack ALREADY declares — the pack's "
                        "own numbers, which travel with it to every stack. A rule value "
                        "(a tolerance, an escalation age) is set only here: no stack "
                        "overrides one. Naming one "
                        "leaves the others alone, and needs this pack's constant_groups "
                        "as well as its author_groups. Declaring a new name is refused "
                        "as the pack's structure, and so are AUTO_WRITE_LIMIT_USD and "
                        "DUAL_APPROVAL_LIMIT_USD even for a value change: the run checks read "
                        "them on every SAP write, so either needs a pack-admin in a "
                        "development stack."
                    ),
                    "additionalProperties": {"type": "number"},
                },
                "expected_version": _EXPECTED_VERSION,
            },
            "required": ["pack_id"],
            "additionalProperties": False,
        },
        method="PATCH",
        route=lambda a: _pack(a),
        body=_content_body,
    ),
    Tool(
        name="lower_pack_limits",
        description=(
            "Lower this stack's AUTO_WRITE_LIMIT_USD or DUAL_APPROVAL_LIMIT_USD for a "
            "pack. The pack's value is the ceiling: an override may lower it, never "
            "raise it, and stays in this stack. Null removes the override. A rule value "
            "is refused here: send it as `constants` to publish_pack_content in a "
            "development stack, and promote that version by bundle. Needs "
            "business-operations-limit-owners and this pack's limit_groups. The next "
            "case reads the new value."
        ),
        schema=_pack_only(
            {
                "limits": {
                    "type": "object",
                    "description": (
                        "AUTO_WRITE_LIMIT_USD and/or DUAL_APPROVAL_LIMIT_USD to a "
                        "non-negative number no higher than the pack's value, or null "
                        "to remove the override. read_controls reports both."
                    ),
                    "additionalProperties": {"type": ["number", "null"]},
                }
            },
            required=("pack_id", "limits"),
        ),
        method="PUT",
        route=lambda a: _pack(a, "controls"),
        body=lambda a: {"limits": a.get("limits")},
    ),
    Tool(
        name="put_sop",
        description=(
            "Replace the text of the SOP a process type maps. The next run reads it at "
            "once; search_sap_sops serves it once the ingestion job the write starts "
            "finishes. When a job was already running the reply's `ingestion.status` "
            "is `QUEUED`, and another job starts on its own after it. The write is a "
            "new version of the pack, named in the reply's `version`. Only a "
            "development stack takes it: a quality or production stack refuses it, and "
            "gets SOPs by bundle import."
        ),
        schema=_pack_only(
            {
                "process_type": _PROCESS_TYPE,
                "content": {"type": "string"},
                "expected_version": _EXPECTED_VERSION,
            },
            required=("pack_id", "process_type", "content"),
        ),
        method="PUT",
        route=lambda a: _sop(a),
        body=lambda a: {k: a[k] for k in ("content", "expected_version") if k in a},
    ),
    Tool(
        name="create_sop",
        description=(
            "Add a process type to a pack with its SOP, in one version: a SOP file is "
            "always mapped, so the file and its `process_type_to_sop` entry are written "
            "together. The mapping is the pack's structure, so this needs "
            "business-operations-pack-admins as well as the pack's author_groups, in a "
            "development stack. A process type already mapped is a 409: use put_sop."
        ),
        schema=_pack_only(
            {
                "process_type": {
                    "type": "string",
                    "description": "The new process type, lowercase with underscores.",
                },
                "name": {
                    "type": "string",
                    "description": "The file name; default `<process_type>.txt`.",
                },
                "content": {"type": "string"},
                "expected_version": _EXPECTED_VERSION,
            },
            required=("pack_id", "process_type", "content"),
        ),
        method="POST",
        route=lambda a: _pack(a, "sops"),
        body=lambda a: {
            k: a[k]
            for k in ("process_type", "name", "content", "expected_version")
            if k in a
        },
    ),
    Tool(
        name="validate_sop",
        description=(
            "Check a SOP's text without writing it: the same refusals put_sop would "
            "give, then the SOP lint's findings (sections in order, the Version line, "
            "RFC 2119 language, symbols the pack declares, a process type that maps to "
            "it). `ok` is true when there are none. Run it before put_sop or create_sop "
            "and fix what it names; findings do not stop a write, refusals do."
        ),
        schema=_pack_only(
            {"process_type": _PROCESS_TYPE, "content": {"type": "string"}},
            required=("pack_id", "process_type", "content"),
        ),
        method="POST",
        route=lambda a: _sop(a, "check"),
        body=lambda a: {"content": a.get("content")},
    ),
    Tool(
        name="sync_sops",
        description=(
            "Re-index the SOP corpus. Rarely needed: every write starts or queues "
            "its own job. One job runs at a time, so this is refused while one is "
            "in flight."
        ),
        schema={"type": "object", "properties": {}, "additionalProperties": False},
        method="POST",
        route=lambda _: "/sops/sync",
        body=lambda _: {},
    ),
)

# `delete_sop` is deliberately absent. Deleting a SOP removes its process types from the
# pack, so every case routed to them stops having a procedure. It is a destructive
# corpus change with a blast radius no analyst can see from here; it stays on the
# console's pack pages and in a checkout.


def result(request_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def error_response(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def call_tool(api: Api, tool: Tool, arguments: dict[str, Any]) -> dict[str, Any]:
    """Whatever the API said, passed through as written.

    A 400 or 403 body names the tier a key belongs to, or the grant that is missing,
    and where to take the change instead. Rewording it would delete the only
    explanation the caller gets.

    One shape is the exception, because it explains nothing: API Gateway answers a
    path it has no route for by trying to read the bearer token as a SigV4 signature,
    and returns the parser's complaint about the `Authorization` header. Nothing in it
    says the route is absent, and the remedy is a redeploy rather than anything the
    caller can do differently — so that one gets a line naming the cause.
    """
    route = tool.route(arguments)
    body = tool.body(arguments) if tool.body else None
    status, text = api.call(tool.method, route, body)
    if status == 403 and "Invalid key=value pair" in text:
        text += (
            f"\n\nThis deployment has no {route} route — its API predates this tool. "
            "Redeploy the use case."
        )
    return {
        "content": [{"type": "text", "text": text or f"HTTP {status}, empty body"}],
        "isError": status >= 400,
    }


def handle(
    api: Api,
    tools: tuple[Tool, ...],
    message: dict[str, Any],
    log: Callable[[str], None],
) -> dict[str, Any] | None:
    """One request in, at most one response out."""
    method = message.get("method")
    request_id = message.get("id")

    if method == "initialize":
        return result(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "business-operations-authoring", "version": "1"},
            },
        )
    if request_id is None:
        return None  # a notification: nothing to answer
    if method == "ping":
        return result(request_id, {})
    if method == "tools/list":
        return result(
            request_id,
            {
                "tools": [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "inputSchema": tool.schema,
                    }
                    for tool in tools
                ]
            },
        )
    if method != "tools/call":
        return error_response(
            request_id, _METHOD_NOT_FOUND, f"unsupported method {method!r}"
        )

    params = message.get("params") or {}
    name = str(params.get("name", ""))
    tool = next((candidate for candidate in tools if candidate.name == name), None)
    if tool is None:
        return error_response(request_id, _METHOD_NOT_FOUND, f"no such tool: {name!r}")
    try:
        return result(request_id, call_tool(api, tool, params.get("arguments") or {}))
    except Exception as exc:  # noqa: BLE001
        # Deliberately broad, same reason as the Gateway proxy: everything below this
        # line is remote — a sign-in, a token exchange, a socket. An exception
        # escaping here ends the process, and an MCP host reads that as the server
        # dying mid-session rather than as one tool failing.
        detail = (
            str(exc)
            if isinstance(exc, AuthoringError)
            else f"{type(exc).__name__}: {exc}"
        )
        log(detail)
        return error_response(request_id, _INTERNAL_ERROR, detail)


def serve(
    api: Api,
    tools: tuple[Tool, ...],
    stdin: IO[str],
    stdout: IO[str],
    log: Callable[[str], None],
) -> None:
    """Newline-delimited JSON-RPC in, answers out. stdout carries protocol only."""
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            log(f"dropping unparseable line: {line[:200]}")
            continue
        if not isinstance(message, dict):
            log("dropping non-object JSON-RPC message")
            continue
        response = handle(api, tools, message, log)
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp_authoring.py",
        description=(
            "Author use case packs and SOPs over MCP, as yourself. Needs one value: "
            "the deployed application's URL."
        ),
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="serve",
        choices=("serve", "login"),
        help="serve (default) speaks MCP on stdio; login signs in and exits.",
    )
    parser.add_argument(
        "--app-url",
        help="The application URL, e.g. https://dxxxx.cloudfront.net. "
        "Defaults to $BUSINESS_OPERATIONS_APP_URL.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    app_url = args.app_url or os.environ.get("BUSINESS_OPERATIONS_APP_URL")
    if not app_url:
        parser.error(
            "no application URL: pass --app-url or set BUSINESS_OPERATIONS_APP_URL"
        )

    def log(message: str) -> None:
        print(f"[mcp-authoring] {message}", file=sys.stderr, flush=True)

    try:
        deployment = discover(app_url)
    except AuthoringError as exc:
        log(str(exc))
        log("check the URL opens in a browser — /aws-exports.json must be reachable")
        return 1

    session = Session(deployment, log=log)
    if args.command == "login":
        try:
            session.id_token()
        except AuthoringError as exc:
            log(str(exc))
            return 1
        log(f"signed in — cached at {cache_path(deployment.label)}")
        return 0

    log(f"api={deployment.api_url} tools={len(TOOLS)}")
    log("writes are authorized per pack, as you — not as a service identity")
    serve(Api(deployment, session), TOOLS, sys.stdin, sys.stdout, log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
