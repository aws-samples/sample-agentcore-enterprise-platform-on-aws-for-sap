# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""business-operations API: the envelope every handler shares (`api_http`)."""

from __future__ import annotations

import json
import logging

import api_http
import pytest
import registry

ORIGIN = "https://console.test"


@pytest.fixture
def admitted(monkeypatch):
    monkeypatch.setattr(api_http, "admit", lambda event: None)
    monkeypatch.setattr(registry, "refresh_for_request", lambda: None)
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", ORIGIN)


def _event(body=None, origin=ORIGIN):
    return {"headers": {"Origin": origin}, "body": body, "httpMethod": "POST"}


def _call(fn, event):
    response = api_http.route(fn)(event, None)
    return response["statusCode"], json.loads(response["body"]), response["headers"]


@pytest.mark.parametrize("allowlist", ["", ",", " , "])
def test_an_empty_allowlist_echoes_no_origin_not_even_an_absent_one(
    monkeypatch, allowlist
):
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", allowlist)
    assert api_http.cors(_event(origin="")) == {}
    assert api_http.cors(_event()) == {}
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS")
    assert api_http.cors(_event()) == {}


def test_only_a_listed_origin_is_echoed(monkeypatch):
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", f"https://other.test, {ORIGIN}")
    assert api_http.cors(_event())["Access-Control-Allow-Origin"] == ORIGIN
    assert api_http.cors(_event(origin="https://evil.test")) == {}


def test_a_refusal_from_admit_comes_first_and_carries_cors(monkeypatch, admitted):
    monkeypatch.setattr(api_http, "admit", lambda event: {"error": "no"})
    called = []
    status, body, headers = _call(lambda e: called.append(e), _event())
    assert (status, body, called) == (403, {"error": "no"}, [])
    assert headers["Access-Control-Allow-Origin"] == ORIGIN


@pytest.mark.parametrize("raw", ["not json", "[1]", '"x"'])
def test_a_body_that_is_not_an_object_is_400(admitted, raw):
    status, body, _ = _call(lambda e: api_http.json_body(e), _event(raw))
    assert status == 400 and "Body" in body["error"]


@pytest.mark.parametrize(
    "value,kind,ok",
    [("x", str, True), (5, str, False), (["auto"], str, False), (True, bool, True),
     ("yes", bool, False), ({}, dict, True), ("{}", dict, False)],
)  # fmt: skip
def test_a_field_of_the_wrong_type_is_400_not_500(admitted, value, kind, ok):
    def fn(event):
        api_http.field(api_http.json_body(event), "f", kind)
        return api_http.respond(200, {}, event)

    status, body, _ = _call(fn, _event(json.dumps({"f": value})))
    assert status == (200 if ok else 400), body


def test_a_required_or_too_long_field_is_400(admitted):
    with pytest.raises(api_http.HttpError) as missing:
        api_http.field({}, "prompt", required=True)
    assert missing.value.status == 400
    with pytest.raises(api_http.HttpError):
        api_http.field({"p": "abc"}, "p", max_len=2)
    assert api_http.field({}, "p") is None


def test_an_unreadable_registry_is_a_503_with_no_detail(admitted, capsys):
    def fn(event):
        raise registry.RegistryUnavailable("s3://bucket boom")

    status, body, headers = _call(fn, _event())
    assert (status, body) == (503, api_http.REGISTRY_UNAVAILABLE_BODY)
    assert headers["Access-Control-Allow-Origin"] == ORIGIN
    assert "RegistryUnavailable" in capsys.readouterr().out


def test_the_503_survives_a_metric_failure(admitted, monkeypatch):
    def broken():
        raise OSError("stdout unavailable")

    monkeypatch.setattr(api_http, "_registry_unavailable_metric", broken)

    def fn(event):
        raise registry.RegistryUnavailable("x")

    assert _call(fn, _event())[0] == 503


def test_a_crash_is_a_logged_500_with_cors_and_no_message(admitted, caplog):
    token = "-".join(["secret", "token"])  # noqa: FLY002 — a runtime value, not a source literal

    def fn(event):
        raise ValueError(f"Bearer {token}")

    with caplog.at_level(logging.ERROR):
        status, body, headers = _call(fn, _event())
    assert (status, body) == (500, api_http.INTERNAL_ERROR_BODY)
    assert headers["Access-Control-Allow-Origin"] == ORIGIN
    assert "ValueError" in caplog.text and "secret-token" not in caplog.text


def test_an_http_error_answers_its_own_status_and_body(admitted):
    def fn(event):
        raise api_http.HttpError(409, "taken", detail="try again")

    assert _call(fn, _event())[:2] == (409, {"error": "taken", "detail": "try again"})


def test_decimals_render_as_numbers(admitted):
    from decimal import Decimal

    response = api_http.respond(200, {"a": Decimal(2), "b": Decimal("1.5")}, _event())
    assert json.loads(response["body"]) == {"a": 2, "b": 1.5}
