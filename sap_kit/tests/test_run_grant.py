# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""sap_kit/run_grant.py: the run-grant mechanics a use case's run checks share.

Each use case's own tests cover how its check and invoker use them; the fixture consumer
is in tests/test_interceptor_extensions.py.
"""

from __future__ import annotations

import base64
import hashlib
import json

import pytest

from sap_kit import run_grant

SECRET = "the-secret"
GRANT = run_grant.Grant(
    run_id="r1",
    client_id="app",
    expires_at=100,
    secret_sha256=hashlib.sha256(SECRET.encode()).hexdigest(),
)


def test_a_grant_is_live_for_its_own_caller_secret_and_time():
    assert run_grant.live(GRANT, "app", SECRET, 99)
    assert not run_grant.live(GRANT, "other", SECRET, 99)
    assert not run_grant.live(GRANT, "app", SECRET, 100)  # expires_at is exclusive
    assert not run_grant.live(GRANT, "app", "wrong", 99)
    assert not run_grant.live(GRANT, "app", None, 99)


def test_a_grant_with_no_hash_matches_no_secret():
    blank = run_grant.Grant(run_id="r", client_id="app", expires_at=100)
    assert not run_grant.holds_secret(blank, "")
    assert not run_grant.holds_secret(blank, SECRET)


def test_a_row_is_a_rehearsal_only_when_it_says_true():
    row = {"run_id": "r", "client_id": "a", "expires_at": 1}
    assert run_grant.Grant.of({**row, "rehearsal": True}).rehearsal is True
    assert run_grant.Grant.of({**row, "rehearsal": "yes"}).rehearsal is False
    assert run_grant.Grant.of(row).secret_sha256 == ""


def test_the_grant_is_read_consistently():
    class Table:
        def get_item(self, Key, ConsistentRead):
            assert Key == {"run_id": "r1"} and ConsistentRead is True
            return {}

    assert run_grant.read(Table(), "r1") is None


def _bearer(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode()
    return f"h.{payload.rstrip('=')}.s"


@pytest.mark.parametrize("prefix", ["Bearer ", "bearer ", ""])
def test_headers_and_claims_are_read_in_any_case(prefix):
    headers = {"AUTHORIZATION": prefix + _bearer({"client_id": "c"})}
    assert run_grant.header(headers, "authorization") == headers["AUTHORIZATION"]
    assert run_grant.header(None, "authorization") is None
    assert run_grant.bearer_claims(headers) == {"client_id": "c"}


@pytest.mark.parametrize(
    ("claims", "key"),
    [
        ({"client_id": "c", "azp": "a"}, "c"),
        ({"azp": "a", "scp": "x", "oid": "o"}, "user:o"),  # a person
        ({"azp": "a"}, "a"),  # an app
    ],
)
def test_the_caller_key(claims, key):
    assert run_grant.caller_key(claims) == key
