# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""business-operations: who may subscribe to which run channel.

The channel's Cognito mode admits any pool user who signs in through the console
client, so this is the only group and pack check on it. Refused unless the caller is
a users-group member on a pack they may see, or on a session channel.
"""

from __future__ import annotations

import importlib.util
import json

import pytest
from test_business_operations_api import CONSOLE, UC, USERS

SESSION = "0b6f3c1e-2a4d-4e8f-9c71-5d2e8a9b0c13"
CASE = "ap-k7m2q9x4rt6bw8ncf3jhd5gzpv"


@pytest.fixture
def auth(monkeypatch):
    monkeypatch.setenv("API_ALLOWED_CLIENTS", CONSOLE)
    monkeypatch.setenv("USERS_GROUP", USERS)
    # `ap` is open to every user; `tax` only to its own group.
    monkeypatch.setenv(
        "ENTITLEMENT_MAP", json.dumps({"ap": {}, "tax": {"groups": ["bo-tax"]}})
    )
    spec = importlib.util.spec_from_file_location(
        "bo_run_events_auth", UC / "run_events_auth" / "index.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    packs = {
        "ap": {"source": {"short_id": "ap"}},
        "tax": {"source": {"short_id": "tx"}},
    }
    monkeypatch.setattr(mod.registry, "packs", lambda: packs)
    monkeypatch.setattr(mod.registry, "refresh_for_request", lambda: None)
    return mod


def _sub(path, groups=(USERS,), aud=CONSOLE):
    return {
        "identity": {"sub": "u-1", "groups": list(groups), "claims": {"aud": aud}},
        "info": {"channel": {"path": path}, "operation": "SUBSCRIBE"},
    }


@pytest.mark.parametrize("path", ["/runs/ap/*", f"/runs/ap/{CASE}", f"/runs/{SESSION}"])
def test_a_user_may_follow_a_visible_pack_and_their_session(auth, path):
    assert auth.handler(_sub(path), None) is None


@pytest.mark.parametrize("path", ["/runs/ap/*", f"/runs/{SESSION}"])
def test_a_pool_user_outside_the_users_group_is_refused(auth, path):
    assert auth.handler(_sub(path, groups=()), None) == auth.REFUSED
    assert auth.handler(_sub(path, groups=("other-tenant",)), None) == auth.REFUSED


def test_another_clients_token_is_refused(auth):
    assert auth.handler(_sub("/runs/ap/*", aud="other-client"), None) == auth.REFUSED


def test_a_pack_the_caller_may_not_see_is_refused(auth):
    assert auth.verdict(_sub("/runs/tx/*")) == auth.REFUSED
    assert auth.verdict(_sub("/runs/tx/*", groups=(USERS, "bo-tax"))) is None


@pytest.mark.parametrize(
    "path",
    [
        "/runs/*",  # every run, every pack: what the console used to ask for
        "/runs/zz/*",  # a short id no published pack has
        f"/runs/tx/{CASE}",  # a case filed under a pack it does not belong to
        "/runs/ap/*/x",
        f"/runs/{CASE}",  # a case id is guessable, so it is never a session key
        "/runs/not-a-session",
        "",
    ],
)
def test_everything_else_is_refused(auth, path):
    groups = (USERS, "bo-tax")
    assert auth.verdict(_sub(path, groups=groups)) == auth.REFUSED


def test_an_unreadable_registry_refuses_rather_than_opens(auth, monkeypatch):
    def unavailable():
        raise auth.registry.RegistryUnavailable("down")

    monkeypatch.setattr(auth.registry, "packs", unavailable)
    with pytest.raises(auth.registry.RegistryUnavailable):
        auth.handler(_sub("/runs/ap/*"), None)
