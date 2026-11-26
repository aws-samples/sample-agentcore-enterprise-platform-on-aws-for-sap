# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""business-operations API: /deployment/trigger-mode, the CORS allowlist, and the 503 every
registry reader owes the browser.

`trigger-mode: auto` is the switch for unattended SAP action, so who flipped it is
recorded from the authorizer context alone, and its capability flag is parsed as a
string comparison (bool("false") is True).
"""

from __future__ import annotations

import importlib.util
import json

import boto3
import pytest
from test_business_operations_api import ADMINS, CONSOLE, UC, USERS, _event

PREFIX = "/p/dev/use-cases/business-operations/autonomy"
TRUSTED = "https://d1234.cloudfront.net"


class FakeSsm:
    def __init__(self, value="manual"):
        self.value, self.puts = value, []
        self.refuse_actor = False

    def get_parameter(self, Name):
        if self.value is None:
            raise RuntimeError("ParameterNotFound")
        return {"Parameter": {"Value": self.value}}

    def put_parameter(self, **kw):
        if self.refuse_actor and kw["Name"].endswith("-last-changed-by"):
            raise RuntimeError("AccessDeniedException")
        self.puts.append(kw)


@pytest.fixture
def mode_api(monkeypatch):
    def load(capable="true", ssm=None):
        monkeypatch.setenv("API_ALLOWED_CLIENTS", CONSOLE)
        monkeypatch.setenv("USERS_GROUP", USERS)
        monkeypatch.setenv("ADMINS_GROUP", ADMINS)
        monkeypatch.setenv("TRIGGER_MODE_PREFIX", PREFIX)
        monkeypatch.setenv("CORS_ALLOWED_ORIGINS", TRUSTED)
        monkeypatch.setenv("AUTONOMOUS_CAPABLE", capable)
        fake = ssm or FakeSsm()
        monkeypatch.setattr(boto3, "client", lambda *a, **k: fake)
        spec = importlib.util.spec_from_file_location(
            "bo_api_trigger_mode", UC / "api" / "trigger_mode" / "index.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod, fake

    return load


DEPLOYMENT = "/deployment/trigger-mode"
PACK = "/packs/{pack_id}/trigger-mode"


def _get(mod, **extra):
    return mod.handler(_event("GET", DEPLOYMENT, **extra), None)


def _put(mod, body, groups=(USERS, ADMINS)):
    """The deployment's trigger mode, or one pack's mode when the body names `pack_id`: the
    pack goes on the path, as the route takes it."""
    body = dict(body)
    if "pack_id" in body:
        event = _event("PUT", PACK, groups=groups, body=json.dumps(body))
        event["pathParameters"] = {"pack_id": body.pop("pack_id")}
        event["body"] = json.dumps(body)
    else:
        event = _event("PUT", DEPLOYMENT, groups=groups, body=json.dumps(body))
    return mod.handler(event, None)


def _get_pack(mod, pack_id, groups=(USERS,)):
    event = _event("GET", PACK, groups=groups)
    event["pathParameters"] = {"pack_id": pack_id}
    return mod.handler(event, None)


@pytest.mark.parametrize("raw,expected", [("true", True), ("false", False)])
def test_capability_is_a_string_comparison(mode_api, raw, expected):
    mod, _ = mode_api(capable=raw)
    assert json.loads(_get(mod)["body"])["autonomous-capable"] is expected


def test_capability_ships_with_the_mode_even_when_the_mode_is_unreadable(mode_api):
    mod, _ = mode_api(ssm=FakeSsm(value=None))
    body = json.loads(_get(mod)["body"])
    assert body["trigger-mode"] is None
    assert body["autonomous-capable"] is True


def test_a_flip_needs_an_admin(mode_api):
    mod, ssm = mode_api()
    assert _put(mod, {"trigger-mode": "auto"}, groups=(USERS,))["statusCode"] == 403
    assert not ssm.puts


def test_a_flip_records_who_made_it_after_the_mode(mode_api):
    mod, ssm = mode_api()
    response = _put(mod, {"trigger-mode": "auto"})
    assert response["statusCode"] == 200
    mode, mirror = ssm.puts
    assert mode == {
        "Name": f"{PREFIX}/trigger-mode",
        "Value": "auto",
        "Type": "String",
        "Overwrite": True,
        # Both identities: the email to read, the immutable sub to resolve.
        "Description": "op@example.com (sub-1)",
    }
    assert mirror["Name"] == f"{PREFIX}/trigger-mode{mod.ACTOR_SUFFIX}"
    assert mirror["Value"].startswith("auto set by op@example.com (sub-1) at ")


def test_the_actor_comes_from_the_authorizer_not_the_body(mode_api):
    mod, ssm = mode_api()
    _put(mod, {"trigger-mode": "auto", "actor": "someone.else@example.com"})
    assert ssm.puts and not any("someone.else" in json.dumps(p) for p in ssm.puts)


def test_an_unrecordable_actor_does_not_fail_the_flip(mode_api):
    ssm = FakeSsm()
    ssm.refuse_actor = True
    mod, _ = mode_api(ssm=ssm)
    response = _put(mod, {"trigger-mode": "auto"})
    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"trigger-mode": "auto"}


@pytest.mark.parametrize(
    "body",
    [
        {"trigger-mode": "yolo"},
        {"trigger-mode": ["auto"]},
        {"trigger_mode": "auto"},
        {"enqueue_case_id": "5100001922-2026"},
    ],
)
def test_an_invalid_or_retired_field_is_400_not_a_silent_200(mode_api, body):
    mod, ssm = mode_api()
    response = _put(mod, body)
    assert response["statusCode"] == 400
    assert not ssm.puts
    # The enqueue path left this route entirely; POST /cases/enqueue is gated.
    assert not hasattr(mod, "sqs")


# ── CORS: echo a listed origin, else nothing ──


@pytest.mark.parametrize(
    "headers,echoed",
    [
        ({"origin": TRUSTED}, TRUSTED),
        ({"origin": "https://evil.example.com"}, None),
        ({"origin": ""}, None),
        ({}, None),
    ],
)
def test_only_a_listed_origin_is_echoed(mode_api, headers, echoed):
    mod, _ = mode_api()
    response = _get(mod, headers=headers)
    assert response["headers"].get("Access-Control-Allow-Origin") == echoed


# ── Trigger mode per pack ──


class PackSsm(FakeSsm):
    """FakeSsm with named values, and `get_parameters` as SSM answers it."""

    def __init__(self, values):
        super().__init__(values.get(f"{PREFIX}/trigger-mode"))
        self.values = values

    def get_parameter(self, Name):
        if Name not in self.values:
            raise RuntimeError("ParameterNotFound")
        return {"Parameter": {"Value": self.values[Name]}}

    def get_parameters(self, Names):
        assert len(Names) <= 10
        return {
            "Parameters": [
                {"Name": n, "Value": self.values[n]} for n in Names if n in self.values
            ]
        }


def _packs(mod, monkeypatch, *pack_ids):
    monkeypatch.setattr(mod.registry, "refresh_for_request", lambda: None)
    monkeypatch.setattr(
        mod.registry, "packs", lambda: {p: {"pack": {}} for p in pack_ids}
    )


def test_get_lists_each_packs_mode_and_what_is_in_effect(mode_api, monkeypatch):
    ssm = PackSsm(
        {
            f"{PREFIX}/trigger-mode": "auto",
            f"{PREFIX}/packs/finance_ops": "auto",
            f"{PREFIX}/pack-actors/finance_ops": "auto set by a at t",
            f"{PREFIX}/packs/gl": "manual",
        }
    )
    mod, _ = mode_api(ssm=ssm)
    _packs(mod, monkeypatch, "finance_ops", "gl", "new_pack")
    packs = json.loads(_get(mod)["body"])["packs"]
    assert packs == {
        "finance_ops": {
            "trigger-mode": "auto",
            "trigger-mode-last-changed-by": "auto set by a at t",
            "effective": "auto",
        },
        "gl": {
            "trigger-mode": "manual",
            "trigger-mode-last-changed-by": None,
            "effective": "manual",
        },
        # Never set, so manual: a new pack does not run alone.
        "new_pack": {
            "trigger-mode": None,
            "trigger-mode-last-changed-by": None,
            "effective": "manual",
        },
    }


@pytest.mark.parametrize("switch,capable", [("manual", "true"), ("auto", "false")])
def test_no_pack_is_in_effect_auto_under_a_manual_or_inert_switch(
    mode_api, monkeypatch, switch, capable
):
    ssm = PackSsm(
        {f"{PREFIX}/trigger-mode": switch, f"{PREFIX}/packs/finance_ops": "auto"}
    )
    mod, _ = mode_api(capable=capable, ssm=ssm)
    _packs(mod, monkeypatch, "finance_ops")
    pack = json.loads(_get(mod)["body"])["packs"]["finance_ops"]
    assert pack["trigger-mode"] == "auto" and pack["effective"] == "manual"


def test_get_lists_only_the_packs_the_caller_may_see(mode_api, monkeypatch):
    mod, _ = mode_api(ssm=PackSsm({}))
    _packs(mod, monkeypatch, "finance_ops", "hr")
    monkeypatch.setenv(
        "ENTITLEMENT_MAP",
        json.dumps({"finance_ops": {}, "hr": {"groups": ["business-operations-hr"]}}),
    )
    assert list(json.loads(_get(mod)["body"])["packs"]) == ["finance_ops"]


def test_an_unreadable_registry_leaves_the_switch_answering(mode_api, monkeypatch):
    mod, _ = mode_api(ssm=PackSsm({f"{PREFIX}/trigger-mode": "auto"}))

    def boom():
        raise mod.registry.RegistryUnavailable("s3")

    monkeypatch.setattr(mod.registry, "refresh_for_request", boom)
    response = _get(mod)
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["trigger-mode"] == "auto" and body["packs"] is None


def test_a_pack_flip_writes_its_own_mode_then_who_made_it(mode_api, monkeypatch):
    mod, ssm = mode_api(ssm=PackSsm({}))
    _packs(mod, monkeypatch, "finance_ops")
    response = _put(mod, {"pack_id": "finance_ops", "trigger-mode": "auto"})
    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {
        "pack_id": "finance_ops",
        "trigger-mode": "auto",
    }
    mode, actor = ssm.puts
    assert mode == {
        "Name": f"{PREFIX}/packs/finance_ops",
        "Value": "auto",
        "Type": "String",
        "Overwrite": True,
        "Description": "op@example.com (sub-1)",
    }
    assert actor["Name"] == f"{PREFIX}/pack-actors/finance_ops"
    assert actor["Value"].startswith("auto set by op@example.com (sub-1) at ")


@pytest.mark.parametrize(
    "body,status",
    [
        ({"pack_id": "no_such_pack", "trigger-mode": "auto"}, 404),
        ({"pack_id": "../trigger-mode", "trigger-mode": "auto"}, 400),
        ({"pack_id": "Finance", "trigger-mode": "auto"}, 400),
        ({"pack_id": "7", "trigger-mode": "auto"}, 400),
        ({"pack_id": "finance_ops", "trigger-mode": "yolo"}, 400),
    ],
)
def test_a_pack_flip_names_a_published_pack_and_a_mode(
    mode_api, monkeypatch, body, status
):
    mod, ssm = mode_api(ssm=PackSsm({}))
    _packs(mod, monkeypatch, "finance_ops")
    assert _put(mod, body)["statusCode"] == status
    assert not ssm.puts


def test_a_pack_flip_needs_an_admin(mode_api, monkeypatch):
    mod, ssm = mode_api(ssm=PackSsm({}))
    _packs(mod, monkeypatch, "finance_ops")
    body = {"pack_id": "finance_ops", "trigger-mode": "auto"}
    assert _put(mod, body, groups=(USERS,))["statusCode"] == 403
    assert not ssm.puts


def test_one_packs_mode_reads_with_the_switch(mode_api, monkeypatch):
    mod, _ = mode_api(
        ssm=PackSsm(
            {
                f"{PREFIX}/trigger-mode": "auto",
                f"{PREFIX}/packs/finance_ops": "auto",
                f"{PREFIX}/pack-actors/finance_ops": "auto set by a at t",
            }
        )
    )
    monkeypatch.setattr(mod, "AUTONOMOUS_CAPABLE", True)
    _packs(mod, monkeypatch, "finance_ops")
    response = _get_pack(mod, "finance_ops")
    assert json.loads(response["body"]) == {
        "pack_id": "finance_ops",
        "trigger-mode": "auto",
        "trigger-mode-last-changed-by": "auto set by a at t",
        "effective": "auto",
        "deployment": "auto",
    }
    assert _get_pack(mod, "nope")["statusCode"] == 404
    monkeypatch.setenv(
        "ENTITLEMENT_MAP", json.dumps({"finance_ops": {"groups": ["x"]}})
    )
    assert _get_pack(mod, "finance_ops")["statusCode"] == 403


# ── an emergency stop ──


STOP = {
    "stands": True,
    "reason": "SAP looks wrong",
    "stopped_by": "op",
    "stopped_at": "t",
}


@pytest.mark.parametrize("pack", [None, "finance_ops"])
def test_no_mode_turns_to_auto_while_a_stop_stands_but_manual_does(
    mode_api, monkeypatch, pack
):
    import emergency_stop

    mod, fake = mode_api()
    monkeypatch.setattr(mod.registry, "packs", lambda: {"finance_ops": {}})
    monkeypatch.setattr(mod.registry, "refresh_for_request", lambda: None)
    monkeypatch.setattr(emergency_stop, "standing", lambda *a, **k: dict(STOP))
    body = {"trigger-mode": "auto", **({"pack_id": pack} if pack else {})}
    response = _put(mod, body)
    assert response["statusCode"] == 409
    answer = json.loads(response["body"])
    assert answer["error"].startswith("An emergency stop stands: SAP looks wrong")
    assert answer["emergency_stop"]["stands"] is True
    assert not fake.puts
    response = _put(mod, {**body, "trigger-mode": "manual"})
    assert response["statusCode"] == 200 and fake.puts
