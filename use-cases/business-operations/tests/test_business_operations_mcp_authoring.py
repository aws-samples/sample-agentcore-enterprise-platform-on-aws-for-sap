# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Contract tests for the stdio -> REST authoring MCP server.

Four things break this server and none of them is visible from reading it: the
one-input discovery (which key names the issuer), PKCE (a wrong challenge fails at
the token endpoint, not before), the token cache (a refresh token in a
world-readable file, or a refresh reply that drops the old one), and the tier split
in the two write bodies. All four are pinned here. The HTTP seam is faked, so no
network, no browser and no AWS.
"""

import base64
import hashlib
import io
import json
import stat
import sys
import urllib.parse
from pathlib import Path

import pytest

_UC = Path(__file__).resolve().parents[3] / "use-cases" / "business-operations"
sys.path.insert(0, str(_UC / "api" / "layer" / "python"))
sys.path.insert(0, str(_UC / "tools" / "mcp-authoring"))

import mcp_authoring as authoring
import pack_tiers
from mcp_authoring import (
    REDIRECT_URI,
    TOOLS,
    Api,
    AuthoringError,
    Deployment,
    Session,
    _content_body,
    _create_body,
    check_callback,
    checked_url,
    discover,
    handle,
    pkce_pair,
    serve,
)

_EXPORTS = {
    "authority": "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_abc/",
    "client_id": "cid123",
    "apiUrl": "https://api.example.com/prod/",
}
_OIDC = {
    "authorization_endpoint": "https://login.example.com/oauth2/authorize",
    "token_endpoint": "https://login.example.com/oauth2/token",
}


class FakeHttp:
    """Canned replies keyed by URL prefix, recording every call in order."""

    def __init__(self, replies):
        self.replies = dict(replies)
        self.calls = []

    def __call__(self, method, url, *, data=None, headers=None):
        self.calls.append((method, url, data, headers or {}))
        for prefix, reply in self.replies.items():
            if url.startswith(prefix):
                return reply
        return 404, b'{"error":"no canned reply"}'


def _json(payload, status=200):
    return status, json.dumps(payload).encode()


def _deployment():
    return Deployment(
        api_url="https://api.example.com/prod",
        client_id="cid123",
        authorization_endpoint=_OIDC["authorization_endpoint"],
        token_endpoint=_OIDC["token_endpoint"],
        scope="email openid profile",
        label="app.example.com",
    )


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """No test may read or write the real cache, or open a real browser tab."""
    monkeypatch.setattr(authoring, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(authoring.webbrowser, "open", lambda url: None)


# --- discovery -----------------------------------------------------------------


def test_discovery_reads_both_documents_from_one_url():
    http = FakeHttp(
        {
            "https://app.example.com/aws-exports.json": _json(_EXPORTS),
            _EXPORTS["authority"]: _json(_OIDC),
        }
    )
    deployment = discover("https://app.example.com/", request=http)

    assert deployment.api_url == "https://api.example.com/prod"  # trailing / stripped
    assert deployment.client_id == "cid123"
    assert deployment.token_endpoint == _OIDC["token_endpoint"]
    assert deployment.scope == authoring.DEFAULT_SCOPE  # exports named none
    assert deployment.label == "app.example.com"
    # The authority's trailing slash must not double up in the discovery path.
    assert http.calls[1][1] == (
        "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_abc"
        "/.well-known/openid-configuration"
    )


@pytest.mark.parametrize("key", ["metadata_url", "metadataUrl"])
def test_metadata_url_wins_over_authority(key):
    """An external-IdP profile names its issuer here, and `lib/config.ts` prefers it.

    Both spellings, because that file honours both — a server that only read one
    would fall back to the Cognito authority for half the auth profiles.
    """
    exports = {
        **_EXPORTS,
        key: "https://idp.example.com/v2/.well-known/openid-configuration",
    }
    http = FakeHttp(
        {
            "https://app.example.com/aws-exports.json": _json(exports),
            "https://idp.example.com/v2/": _json(_OIDC),
        }
    )
    deployment = discover("https://app.example.com", request=http)

    assert deployment.token_endpoint == _OIDC["token_endpoint"]
    # Fetched as given: the key already names a discovery document, so appending
    # `/.well-known/openid-configuration` to it would 404.
    assert http.calls[1][1] == exports[key]


def test_discovery_names_the_document_that_is_missing_a_key():
    http = FakeHttp(
        {
            "https://app.example.com/aws-exports.json": _json(
                {k: v for k, v in _EXPORTS.items() if k != "client_id"}
            ),
            _EXPORTS["authority"]: _json(_OIDC),
        }
    )
    with pytest.raises(AuthoringError, match="aws-exports.json carries no usable"):
        discover("https://app.example.com", request=http)


def test_a_scope_in_exports_is_honoured():
    http = FakeHttp(
        {
            "https://app.example.com/aws-exports.json": _json(
                {**_EXPORTS, "scope": " openid profile "}
            ),
            _EXPORTS["authority"]: _json(_OIDC),
        }
    )
    assert discover("https://app.example.com", request=http).scope == "openid profile"


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "data:text/json,{}", "/no-scheme"]
)
def test_only_http_schemes_are_fetched(url):
    """The app URL is operator input and aws-exports.json is fetched content that
    then names the API — urllib would honour `file:` from either."""
    with pytest.raises(AuthoringError, match="refusing to fetch"):
        checked_url(url)


# --- PKCE ----------------------------------------------------------------------


def test_pkce_challenge_is_the_unpadded_s256_of_the_verifier():
    verifier, challenge = pkce_pair()
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    assert challenge == expected
    assert "=" not in verifier and "=" not in challenge  # RFC 7636 forbids padding
    assert pkce_pair()[0] != verifier  # a fresh verifier per sign-in


def test_the_default_browser_is_resolved_at_the_call_site(monkeypatch):
    """A dataclass freezes a default at class creation, so `webbrowser.open` as the
    default is unreachable to `_isolated_cache` and every test that reaches
    `login()` without injecting one opens a real sign-in tab. Keep it late-bound.
    """
    opened = []
    monkeypatch.setattr(authoring.webbrowser, "open", opened.append)
    http = FakeHttp(
        {_OIDC["token_endpoint"]: _json({"id_token": "t", "expires_in": 60})}
    )
    monkeypatch.setattr(
        authoring, "wait_for_callback", lambda state, **_: {"code": "c", "state": state}
    )

    Session(_deployment(), request=http, log=lambda _: None).login()

    assert opened and opened[0].startswith(_OIDC["authorization_endpoint"])


def test_login_sends_s256_and_the_registered_redirect(monkeypatch):
    opened = []
    http = FakeHttp(
        {
            _OIDC["token_endpoint"]: _json(
                {"id_token": "idtok", "refresh_token": "rt", "expires_in": 3600}
            )
        }
    )
    session = Session(
        _deployment(), request=http, open_browser=opened.append, log=lambda _: None
    )
    monkeypatch.setattr(
        authoring,
        "wait_for_callback",
        lambda state, **_: {"code": "abc", "state": state},
    )

    assert session.login() == "idtok"

    query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(opened[0]).query))
    assert query["code_challenge_method"] == "S256"
    assert query["redirect_uri"] == REDIRECT_URI
    assert query["client_id"] == "cid123"
    assert query["response_type"] == "code"
    # The verifier is sent to the token endpoint, never to the browser.
    exchanged = dict(urllib.parse.parse_qsl(http.calls[0][2].decode()))
    assert exchanged["code_verifier"] not in opened[0]
    assert exchanged["redirect_uri"] == REDIRECT_URI


def test_a_callback_whose_state_does_not_match_is_discarded():
    with pytest.raises(AuthoringError, match="`state` does not match"):
        check_callback({"code": "abc", "state": "somebody-elses"}, "mine")


def test_the_state_check_runs_before_the_error_is_read():
    """Any page that can reach this port can hit /callback. If `error` were read
    first, one of them could end the sign-in with a message of its choosing."""
    with pytest.raises(AuthoringError, match="`state` does not match"):
        check_callback({"error": "access_denied", "state": "wrong"}, "mine")
    # With the right state, the provider's own refusal is what surfaces.
    with pytest.raises(AuthoringError, match="access_denied"):
        check_callback({"error": "access_denied", "state": "mine"}, "mine")


def test_a_matching_redirect_carrying_neither_code_nor_error_is_refused():
    with pytest.raises(AuthoringError, match="neither a code nor an error"):
        check_callback({"state": "mine"}, "mine")


# --- the token cache -----------------------------------------------------------


def test_the_cache_is_owner_only(monkeypatch):
    http = FakeHttp(
        {_OIDC["token_endpoint"]: _json({"id_token": "t", "expires_in": 60})}
    )
    session = Session(_deployment(), request=http, log=lambda _: None)
    monkeypatch.setattr(
        authoring, "wait_for_callback", lambda state, **_: {"code": "c", "state": state}
    )
    session.login()

    path = authoring.cache_path("app.example.com")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # The directory is created with mode 0o700, which umask can only narrow — so
    # assert what matters: nothing outside the owner can traverse it.
    assert not stat.S_IMODE(path.parent.stat().st_mode) & 0o077


def test_a_refresh_reply_without_a_refresh_token_keeps_the_cached_one():
    """Cognito omits it on a refresh. Overwriting with None would force a browser
    sign-in on every single tool call."""
    http = FakeHttp(
        {_OIDC["token_endpoint"]: _json({"id_token": "fresh", "expires_in": 3600})}
    )
    authoring.write_cache(
        "app.example.com",
        {"refresh_token": "keepme", "id_token": "stale", "expires_at": 0},
    )
    session = Session(_deployment(), request=http, log=lambda _: None)

    assert session.id_token() == "fresh"
    assert authoring.read_cache("app.example.com")["refresh_token"] == "keepme"
    assert dict(urllib.parse.parse_qsl(http.calls[0][2].decode()))["grant_type"] == (
        "refresh_token"
    )


def test_a_dead_refresh_token_falls_through_to_a_fresh_sign_in(monkeypatch):
    replies = iter(
        [
            (400, b'{"error":"invalid_grant"}'),
            _json({"id_token": "new", "refresh_token": "rt2", "expires_in": 3600}),
        ]
    )

    def http(method, url, *, data=None, headers=None):
        return next(replies)

    authoring.write_cache("app.example.com", {"refresh_token": "revoked"})
    session = Session(_deployment(), request=http, log=lambda _: None)
    monkeypatch.setattr(
        authoring, "wait_for_callback", lambda state, **_: {"code": "c", "state": state}
    )

    assert session.id_token() == "new"


def test_a_corrupt_cache_means_sign_in_again():
    authoring.CACHE_DIR.mkdir(parents=True)
    authoring.cache_path("app.example.com").write_text("{not json", encoding="utf-8")
    session = Session(_deployment(), request=FakeHttp({}), log=lambda _: None)
    assert session._refresh_token is None


def test_an_unexpired_cached_token_costs_no_request():
    authoring.write_cache(
        "app.example.com",
        {"refresh_token": "rt", "id_token": "cached", "expires_at": 2**40},
    )
    http = FakeHttp({})
    assert (
        Session(_deployment(), request=http, log=lambda _: None).id_token() == "cached"
    )
    assert http.calls == []


def test_the_token_endpoint_must_return_an_id_token(monkeypatch):
    """The REST API takes the ID token — only it carries `cognito:groups`, which
    every authorization gate reads. An access token would 401 on every call."""
    http = FakeHttp(
        {_OIDC["token_endpoint"]: _json({"access_token": "at", "expires_in": 3600})}
    )
    session = Session(_deployment(), request=http, log=lambda _: None)
    monkeypatch.setattr(
        authoring, "wait_for_callback", lambda state, **_: {"code": "c", "state": state}
    )
    with pytest.raises(AuthoringError, match="no id_token"):
        session.login()


# --- the tools -----------------------------------------------------------------


def _api(reply):
    http = FakeHttp({"https://api.example.com": reply})
    authoring.write_cache(
        "app.example.com",
        {"refresh_token": "rt", "id_token": "idtok", "expires_at": 2**40},
    )
    deployment = _deployment()
    session = Session(deployment, request=http, log=lambda _: None)
    return Api(deployment, session, request=http), http


def _call(name, arguments, reply):
    api, http = _api(reply)
    response = handle(
        api,
        TOOLS,
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        lambda _: None,
    )
    return response, http


def test_the_tool_set_is_exactly_the_two_open_tiers_plus_create_plus_reads():
    assert [tool.name for tool in TOOLS] == [
        "list_packs",
        "read_rules",
        "read_controls",
        "read_deployment_settings",
        "read_base_prompt",
        "list_sops",
        "get_sop",
        "create_pack",
        "publish_pack_content",
        "lower_pack_limits",
        "put_sop",
        "create_sop",
        "validate_sop",
        "sync_sops",
    ]


def test_rehearse_is_absent():
    """`POST /runs` would start one, but the transcript reaches only the AppSync Events
    channel and nothing persists it — a rehearsal writes no case row by design. A tool
    that started a run it could not report would send the analyst to the browser anyway,
    which is where the control already is."""
    assert not [tool for tool in TOOLS if "rehears" in tool.name]


def test_no_tool_offers_a_privileged_key():
    """Read against `pack_tiers` itself, so a key promoted into the privileged tier
    later is caught here too. `pack_id` is excluded because these tools name it as the
    *target* of a patch, never as a value to write.

    The composer re-derives the tier and would refuse anyway — this is the filter, not
    the boundary. But a schema that advertises `gateway_tools` invites a model to try,
    and the refusal it earns teaches the wrong lesson.
    """
    # `constants` is a content key, which publish_pack_content sends as a pack edit.
    privileged = pack_tiers.STRUCTURE_KEYS - {"pack_id", "constants"}
    for tool in TOOLS:
        assert not privileged & set(tool.schema.get("properties", {})), tool.name


def test_every_tool_schema_is_closed():
    for tool in TOOLS:
        assert tool.schema["additionalProperties"] is False, tool.name


def test_delete_sop_is_absent():
    """Deleting a SOP removes its process types from the pack, so every case routed
    to them loses its procedure. It stays on the console's pack pages and in a
    checkout."""
    assert not [tool for tool in TOOLS if "delete" in tool.name]


def _tool(name):
    (tool,) = [tool for tool in TOOLS if tool.name == name]
    return tool


def test_a_content_patch_omits_what_was_not_named():
    # The pack is the path's (`PATCH /packs/{pack_id}`), never the body's.
    assert _content_body({"pack_id": "finance_ops", "display_name": "AP"}) == {
        "pack": {"display_name": "AP"},
    }
    # base_prompt travels at the top level, never inside `pack`: it is its own
    # document, and `pack_tiers` would refuse it as a pack key.
    assert _content_body({"pack_id": "p", "base_prompt": "x"}) == {"base_prompt": "x"}
    # A pack_id alone sends no `pack` key at all, rather than an empty object.
    assert _content_body({"pack_id": "p"}) == {}
    # The version read travels beside the patch, so a lost update is a 409.
    assert _content_body({"pack_id": "p", "expected_version": 3}) == {
        "expected_version": 3,
    }
    publish = _tool("publish_pack_content")
    assert (publish.method, publish.route({"pack_id": "p"})) == ("PATCH", "/packs/p")
    put_sop = _tool("put_sop")
    sent = {"pack_id": "p", "process_type": "a", "content": "x", "expected_version": 3}
    assert put_sop.route(sent) == "/packs/p/sops/a"
    assert put_sop.body(sent) == {"content": "x", "expected_version": 3}
    validate_sop = _tool("validate_sop")
    assert validate_sop.method == "POST"
    assert validate_sop.route(sent) == "/packs/p/sops/a/check"
    assert validate_sop.body(sent) == {"content": "x"}
    create_sop = _tool("create_sop")
    assert (create_sop.method, create_sop.route(sent)) == ("POST", "/packs/p/sops")
    assert create_sop.body(sent) == {
        "process_type": "a",
        "content": "x",
        "expected_version": 3,
    }


def test_a_create_nests_the_spec_and_keeps_the_pack_id_outside_it():
    """`pack_id` addresses the pack; `create` is the spec. A `pack_id` left inside the
    spec would be an unknown key and refuse the whole request."""
    assert _create_body(
        {
            "pack_id": "outbound_delivery_block",
            "display_name": "Outbound Delivery Block",
            "short_id": "odb",
            "kind": "aging",
            "older_than_days": 5,
        }
    ) == {
        "pack_id": "outbound_delivery_block",
        "create": {
            "display_name": "Outbound Delivery Block",
            "short_id": "odb",
            "kind": "aging",
            "older_than_days": 5,
        },
    }


def test_a_create_forwards_a_key_it_may_not_set_rather_than_dropping_it():
    """The schema is closed, so a host that validates refuses this first — but a host that
    does not must reach the API's refusal, which names the tier `gateway_tools` belongs to.
    Dropping it here would compose a pack that silently lacks what was asked for."""
    body = _create_body({"pack_id": "p", "gateway_tools": ["odata_update"]})
    assert body["create"]["gateway_tools"] == ["odata_update"]


def test_create_pack_posts_the_spec_to_the_packs_route():
    _, http = _call(
        "create_pack",
        {
            "pack_id": "outbound_delivery_block",
            "display_name": "Outbound Delivery Block",
            "description": "Deliveries past their planned goods issue date.",
            "short_id": "odb",
            "kind": "aging",
            "definition": "A delivery whose planned GI date has passed.",
            "process_types": ["delivery_block"],
            "older_than_days": 5,
        },
        _json({"pack_id": "outbound_delivery_block", "written": []}, 201),
    )
    method, url, data, _ = http.calls[0]
    assert (method, url) == ("POST", "https://api.example.com/prod/packs")
    sent = json.loads(data)
    assert sent["pack_id"] == "outbound_delivery_block"
    assert sent["create"]["short_id"] == "odb"
    # A create is not a patch, and the route refuses a body carrying both.
    assert "pack" not in sent and "base_prompt" not in sent


def test_a_taken_pack_id_is_returned_as_the_api_wrote_it():
    """The 409 says the pack exists and nothing was written. Rewording it would delete
    the only explanation the caller gets."""
    response, _ = _call(
        "create_pack",
        {
            "pack_id": "finance_ops",
            "display_name": "AP",
            "description": "d",
            "short_id": "ap",
            "kind": "filter",
            "definition": "x",
            "process_types": ["invoice_matching"],
        },
        _json({"error": "finance_ops is already published"}, 409),
    )
    assert response["result"]["isError"] is True
    assert "already published" in response["result"]["content"][0]["text"]


def test_the_two_write_tools_send_one_tier_each():
    _, http = _call(
        "publish_pack_content",
        {"pack_id": "p", "display_name": "AP", "base_prompt": "b"},
        _json({"written": []}, 201),
    )
    content = json.loads(http.calls[0][2])
    assert set(content["pack"]) == {"display_name"}
    assert "constants" not in json.dumps(content)

    _, http = _call(
        "lower_pack_limits",
        {"pack_id": "p", "limits": {"AUTO_WRITE_LIMIT_USD": 7}},
        _json({"updated": 1}, 200),
    )
    # A lowered write limit is this stack's setting, on the pack's controls.
    method, url, data, _ = http.calls[0]
    assert (method, url) == ("PUT", "https://api.example.com/prod/packs/p/controls")
    assert json.loads(data) == {"limits": {"AUTO_WRITE_LIMIT_USD": 7}}


def test_a_read_carries_the_id_token_and_no_body():
    response, http = _call("list_packs", {}, _json({"packs": []}))
    method, url, data, headers = http.calls[0]
    assert (method, url) == ("GET", "https://api.example.com/prod/registry")
    assert data is None
    assert headers["Authorization"] == "Bearer idtok"
    assert response["result"]["isError"] is False


def test_read_base_prompt_puts_the_pack_id_in_the_path():
    """Encoded with `safe=''`, so a `pack_id` carrying a separator cannot walk out of
    `/packs/` into another route — the API refuses it either way, but as a 400 on a path
    it owns rather than as whatever the traversed route happens to answer."""
    _, http = _call(
        "read_base_prompt", {"pack_id": "finance_ops"}, _json({"base_prompt": "x"})
    )
    assert http.calls[0][1] == (
        "https://api.example.com/prod/packs/finance_ops/base_prompt"
    )

    _, http = _call(
        "read_base_prompt", {"pack_id": "../sops"}, _json({"error": "nope"}, 400)
    )
    assert http.calls[0][1] == (
        "https://api.example.com/prod/packs/..%2Fsops/base_prompt"
    )


def test_get_sop_puts_the_pack_and_process_type_in_the_path():
    _, http = _call(
        "get_sop",
        {"pack_id": "finance_ops", "process_type": "../config"},
        _json({"content": "x"}),
    )
    assert http.calls[0][1] == (
        "https://api.example.com/prod/packs/finance_ops/sops/..%2Fconfig"
    )


def test_a_refusal_reaches_the_caller_verbatim():
    """A 400/403 body names the tier a key belongs to and where to take it instead.
    That text is the whole teaching mechanism, so it is passed through as written."""
    refusal = {
        "error": "The content tier may not write gateway_tools",
        "detail": "Publish it from a checkout with a deploy.",
    }
    response, _ = _call(
        "publish_pack_content",
        {"pack_id": "p", "display_name": "x"},
        _json(refusal, 403),
    )
    assert response["result"]["isError"] is True
    assert json.loads(response["result"]["content"][0]["text"]) == refusal


def test_a_missing_route_is_named_rather_than_left_as_a_sigv4_error():
    """A stack deployed before these routes existed answers the write with API
    Gateway's SigV4 parser error, which mentions only the `Authorization` header —
    the first live test of this transport lost real time to it. The upstream body is
    still passed through; the cause and the remedy are appended to it."""
    sigv4 = {
        "message": (
            "Invalid key=value pair (missing equal-sign) in Authorization header "
            "(hashed with SHA-256 and encoded with Base64): 'c8aZP/cDTpeAk785'."
        )
    }
    response, _ = _call(
        "publish_pack_content", {"pack_id": "p", "display_name": "x"}, _json(sigv4, 403)
    )
    text = response["result"]["content"][0]["text"]
    assert response["result"]["isError"] is True
    assert sigv4["message"] in text
    assert "has no /packs/p route" in text
    assert "Redeploy the use case" in text


def test_an_empty_error_body_still_says_what_happened():
    response, _ = _call("sync_sops", {}, (409, b""))
    assert response["result"]["isError"] is True
    assert response["result"]["content"][0]["text"] == "HTTP 409, empty body"


# --- protocol ------------------------------------------------------------------


def test_initialize_answers_before_any_sign_in():
    """A host initializes on startup. Reaching for a token there would open a browser
    the moment the client launched, whether or not anyone was authoring."""
    api, http = _api(_json({}))
    response = handle(
        api, TOOLS, {"jsonrpc": "2.0", "id": 1, "method": "initialize"}, lambda _: None
    )
    assert response["result"]["protocolVersion"] == authoring.PROTOCOL_VERSION
    assert response["result"]["serverInfo"]["name"] == "business-operations-authoring"
    assert http.calls == []


def test_tools_list_reports_every_tool_with_a_schema():
    api, _ = _api(_json({}))
    response = handle(
        api, TOOLS, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, lambda _: None
    )
    listed = response["result"]["tools"]
    assert len(listed) == len(TOOLS)
    assert all(entry["inputSchema"]["type"] == "object" for entry in listed)
    assert all(entry["description"] for entry in listed)


def test_a_notification_gets_no_answer():
    api, _ = _api(_json({}))
    assert (
        handle(
            api,
            TOOLS,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            lambda _: None,
        )
        is None
    )


def test_an_unknown_tool_is_an_error_not_a_crash():
    api, _ = _api(_json({}))
    response = handle(
        api,
        TOOLS,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "rm_rf"},
        },
        lambda _: None,
    )
    assert response["error"]["code"] == authoring._METHOD_NOT_FOUND


def test_a_failing_sign_in_is_one_failed_call_not_a_dead_server(monkeypatch):
    """An exception escaping `handle` ends the process, which an MCP host reads as the
    server dying mid-session rather than as one tool failing."""
    deployment = _deployment()
    http = FakeHttp({_OIDC["token_endpoint"]: (400, b'{"error":"invalid_grant"}')})
    session = Session(
        deployment, request=http, log=lambda _: None, open_browser=lambda _: None
    )
    session._refresh_token = "revoked"
    monkeypatch.setattr(
        authoring,
        "wait_for_callback",
        lambda state, **_: (_ for _ in ()).throw(
            AuthoringError("no redirect arrived within 300s")
        ),
    )

    response = handle(
        Api(deployment, session, request=http),
        TOOLS,
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "list_packs"},
        },
        lambda _: None,
    )
    assert response["error"]["code"] == authoring._INTERNAL_ERROR
    assert "no redirect arrived" in response["error"]["message"]


def test_serve_drops_junk_lines_and_answers_the_rest():
    api, _ = _api(_json({}))
    logged = []
    stdout = io.StringIO()
    serve(
        api,
        TOOLS,
        io.StringIO('not json\n\n{"jsonrpc":"2.0","id":9,"method":"ping"}\n'),
        stdout,
        logged.append,
    )
    answers = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert answers == [{"jsonrpc": "2.0", "id": 9, "result": {}}]
    assert any("unparseable" in entry for entry in logged)
