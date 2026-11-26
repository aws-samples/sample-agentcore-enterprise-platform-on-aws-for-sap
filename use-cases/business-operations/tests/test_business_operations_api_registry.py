# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""business-operations API: a valid empty registry is not an error; an unreadable one
is, and never degrades to an empty corpus.

Under declare-to-allow an empty map refuses every pack, so degrading would fail
closed here rather than open as in the earlier system; it would still hide an outage behind
a 403, so the reader raises and every handler answers a CORS-bearing 503.
"""

from __future__ import annotations

import json
from unittest import mock

import entitlement
import pytest
import registry
from test_business_operations_api import _cases, _event


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    registry.reset()
    monkeypatch.setenv("REGISTRY_BUCKET", "bucket")
    yield
    registry.reset()


def _s3(*bodies):
    """An S3 whose successive GetObjects return these bodies (an Exception raises)."""
    s3 = mock.MagicMock()

    def get_object(**_):
        body = bodies[min(s3.get_object.call_count - 1, len(bodies) - 1)]
        if isinstance(body, Exception):
            raise body
        return {"Body": mock.Mock(read=lambda: body)}

    s3.get_object.side_effect = get_object
    return s3


def _doc(packs: dict, schema=registry.SCHEMA) -> bytes:
    return json.dumps(
        {"schema": schema, "platform_prompt": "", "packs": packs}
    ).encode()


def _use(monkeypatch, s3):
    monkeypatch.setattr(registry, "_get_s3", lambda: s3)
    return s3


def test_a_valid_empty_registry_is_not_an_error(monkeypatch):
    _use(monkeypatch, _s3(_doc({})))
    assert registry.load()["packs"] == {}


@pytest.mark.parametrize(
    "body,match",
    [
        (RuntimeError("simulated S3 failure"), "simulated S3 failure"),
        (b"{not json", "not valid JSON"),
        (_doc({}, schema="usecase-registry/99"), "registry/99"),
        (json.dumps({"schema": registry.SCHEMA}).encode(), "no packs object"),
        (b"[]", "not an object"),
        (b'"hello"', "not an object"),
        (b"42", "not an object"),
    ],
)
def test_an_unreadable_registry_raises(monkeypatch, body, match):
    _use(monkeypatch, _s3(body))
    with pytest.raises(registry.RegistryUnavailable, match=match):
        registry.load()


def test_neither_an_outage_nor_an_empty_corpus_is_cached(monkeypatch):
    full = _doc({"ap": {"pack": {"pack_id": "ap"}}})
    _use(monkeypatch, _s3(RuntimeError("down"), _doc({}), full))
    with pytest.raises(registry.RegistryUnavailable):
        registry.load()
    assert registry.load()["packs"] == {}
    assert "ap" in registry.load()["packs"]


def test_a_populated_registry_is_cached(monkeypatch):
    s3 = _use(monkeypatch, _s3(_doc({"ap": {"pack": {"pack_id": "ap"}}})))
    assert registry.load() is registry.load()
    assert s3.get_object.call_count == 1


def test_a_pack_declaring_no_entitlement_is_open_to_a_base_user(monkeypatch):
    _use(monkeypatch, _s3(_doc({"ap": {"pack": {"pack_id": "ap"}}})))
    caller = entitlement.CallerEntitlement(frozenset(), registry.entitlement_map())
    assert caller.may_see("ap") and not caller.may_see("unpublished")


def test_the_handler_answers_503_with_cors_and_no_detail(monkeypatch):
    mod, table, _sqs = _cases(monkeypatch, {})
    monkeypatch.delenv("ENTITLEMENT_MAP")
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://app.test")
    _use(monkeypatch, _s3(RuntimeError("S3 boom")))
    response = mod.handler(_event(headers={"origin": "https://app.test"}), None)
    assert response["statusCode"] == 503
    assert json.loads(response["body"]) == {"error": "Service temporarily unavailable"}
    assert "S3 boom" not in response["body"]
    assert response["headers"]["Access-Control-Allow-Origin"] == "https://app.test"
    assert not table.queries


def test_the_metric_carries_no_exception_detail(capsys, monkeypatch):
    import api_http

    monkeypatch.setenv("STACK_NAME", "test-stack")
    api_http._registry_unavailable_metric()
    payload = json.loads(capsys.readouterr().out.strip())
    assert set(payload) == {"_aws", "Stack", "RegistryUnavailable"}
    assert payload["Stack"] == "test-stack" and payload["RegistryUnavailable"] == 1


# ── a warm reader re-validates on the ETag, once per request ──


class _Versioned:
    """An S3 holding one registry.json whose ETag changes on publish."""

    def __init__(self, packs):
        self.publish(packs)
        self.calls: list[dict] = []
        self.fail = False

    def publish(self, packs):
        self.body, self.etag = _doc(packs), f'"{len(json.dumps(packs))}-{id(packs)}"'

    def get_object(self, **kw):
        from botocore.exceptions import ClientError

        self.calls.append(kw)
        if self.fail:
            raise ClientError({"Error": {"Code": "SlowDown"}}, "GetObject")
        if kw.get("IfNoneMatch") == self.etag:
            raise ClientError(
                {"Error": {"Code": "304"}, "ResponseMetadata": {"HTTPStatusCode": 304}},
                "GetObject",
            )
        return {"Body": mock.Mock(read=lambda: self.body), "ETag": self.etag}


def test_an_unchanged_registry_costs_a_304_and_keeps_the_cache(monkeypatch):
    s3 = _use(monkeypatch, _Versioned({"ap": {"pack": {"pack_id": "ap"}}}))
    first = registry.load()
    registry.refresh_for_request()
    assert registry.load() is first
    assert s3.calls[-1]["IfNoneMatch"] == s3.etag and len(s3.calls) == 2


def test_a_publish_reaches_a_warm_reader_on_its_next_request(monkeypatch):
    s3 = _use(monkeypatch, _Versioned({"ap": {"pack": {"pack_id": "ap"}}}))
    assert set(registry.packs()) == {"ap"}
    s3.publish({"ap": {"pack": {"pack_id": "ap"}}, "tax": {"pack": {"pack_id": "tax"}}})
    assert set(registry.packs()) == {"ap"}, "never mid-request"
    registry.refresh_for_request()
    assert set(registry.packs()) == {"ap", "tax"}
    assert len(s3.calls) == 2, "the changed body is used, not read twice"


def test_a_failed_revalidation_drops_the_cache_and_the_next_read_raises(monkeypatch):
    s3 = _use(monkeypatch, _Versioned({"ap": {"pack": {"pack_id": "ap"}}}))
    registry.load()
    s3.fail = True
    registry.refresh_for_request()  # never raises itself
    with pytest.raises(registry.RegistryUnavailable):
        registry.load()
