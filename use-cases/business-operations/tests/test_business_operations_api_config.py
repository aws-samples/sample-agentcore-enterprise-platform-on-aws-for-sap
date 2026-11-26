# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""business-operations API: /config, the write path for the values SOPs cite.

Every value stored here is substituted into a SOP before the agent reads it, so an
unchecked write is an instruction defect. Writes are allowlisted, range-checked,
attributed from the authorizer, gated (contacts by the author floor, limits by the
limit owners group and the pack's own limit_groups), and applied all or nothing.
"""

from __future__ import annotations

import copy
import importlib.util
import io
import json
from decimal import Decimal
from unittest import mock

import pytest
from test_business_operations_api import CONSOLE, UC, USERS, _event

AUTHORS = "business-operations-authors"
LIMIT_OWNERS = "business-operations-limit-owners"
CONTACTS = {"ap_team": "ap@example.com", "procurement": "buy@example.com"}
CONSTANTS = {
    "finance_ops": {
        "QTY_VARIANCE_PCT": 5,
        "PRICE_VARIANCE_ABS_USD": 0.5,
        "MISSING_GR_ESCALATION_DAYS": 5,
        "AUTO_WRITE_LIMIT_USD": 100000,
        "DUAL_APPROVAL_LIMIT_USD": 50000,
    }
}


@pytest.fixture
def api(monkeypatch):
    for k, v in {
        "API_ALLOWED_CLIENTS": CONSOLE,
        "USERS_GROUP": USERS,
        "CONFIG_TABLE": "cfg",
        "CONTACTS_JSON": json.dumps(CONTACTS),
        "SOP_AUTHOR_GROUPS": AUTHORS,
        "LIMIT_OWNER_GROUPS": LIMIT_OWNERS,
        "ENTITLEMENT_MAP": json.dumps({"finance_ops": {}}),
    }.items():
        monkeypatch.setenv(k, v)
    with mock.patch("boto3.resource"), mock.patch("boto3.client"):
        # pack_access reads PROMOTION_ROLE at import, and another test file may have
        # left it loaded in a different role on this worker.
        import pack_access

        importlib.reload(pack_access)
        spec = importlib.util.spec_from_file_location(
            "bo_api_config", UC / "api" / "config" / "index.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    monkeypatch.setattr(mod.registry, "constants", lambda: CONSTANTS)
    mod.table = mock.MagicMock()
    mod.table.scan.return_value = {"Items": []}
    mod._ddb = mock.MagicMock()
    mod.s3 = mock.MagicMock()
    mod.s3.get_object.side_effect = lambda **_: {
        "Body": io.BytesIO(b'{"version": 3, "sha256": "x"}')
    }
    return mod


SETTINGS = "/deployment/settings"
RULES = "/packs/{pack_id}/rules"
CONTROLS = "/packs/{pack_id}/controls"


def _req(method, resource, body=None, groups=(USERS, AUTHORS), pack="finance_ops"):
    extra = (
        {"body": body if isinstance(body, str) else json.dumps(body)}
        if body is not None
        else {}
    )
    event = _event(method, resource, groups=groups, **extra)
    if "{pack_id}" in resource:
        event["pathParameters"] = {"pack_id": pack}
    return event


def _put(
    api,
    body,
    groups=(USERS, AUTHORS, LIMIT_OWNERS),
    resource=CONTROLS,
    pack="finance_ops",
):
    return api.handler(_req("PUT", resource, body, groups, pack), None)


def _get(api, resource, groups=(USERS,), pack="finance_ops"):
    return api.handler(_req("GET", resource, groups=groups, pack=pack), None)


def _body(resp):
    return json.loads(resp["body"])


def _items(api):
    return api._ddb.transact_write_items.call_args.kwargs["TransactItems"]


OVERRIDES = {
    "Items": [
        {"namespace": "contact", "config_key": "ap_team", "value": "new@x.com"},
        # A rule override left from an older release: ignored, so the pack's value.
        {
            "namespace": "constant#finance_ops",
            "config_key": "QTY_VARIANCE_PCT",
            "value": Decimal(7),
        },
        {
            "namespace": "constant#finance_ops",
            "config_key": "AUTO_WRITE_LIMIT_USD",
            "value": Decimal(25000),
        },
        # Above the pack's ceiling (a later version lowered it): the ceiling holds.
        {
            "namespace": "constant#finance_ops",
            "config_key": "DUAL_APPROVAL_LIMIT_USD",
            "value": Decimal(90000),
        },
    ]
}


def test_settings_carry_the_last_emergency_stop(api):
    api.table.get_item.return_value = {}
    assert _body(_get(api, SETTINGS))["last_stop"] is None
    api.table.get_item.return_value = {
        "Item": {
            "namespace": "deployment",
            "config_key": "last-stop",
            "stopped_by": "op@example.com (sub-1)",
            "reason": "r",
            "runs_total": Decimal(2),
        }
    }
    last = _body(_get(api, SETTINGS))["last_stop"]
    assert last == {
        "stopped_by": "op@example.com (sub-1)",
        "reason": "r",
        "runs_total": 2,
    }
    assert api.table.get_item.call_args.kwargs["Key"] == {
        "namespace": "deployment",
        "config_key": "last-stop",
    }


def test_settings_keep_deployed_contacts_apart_from_overrides(api):
    api.table.scan.return_value = OVERRIDES
    contacts = _body(_get(api, SETTINGS))["contacts"]
    assert contacts["defaults"] == CONTACTS
    assert contacts["overrides"] == {"ap_team": "new@x.com"}
    assert contacts["effective"] == {
        "CONTACT_AP_TEAM": {
            "value": "new@x.com",
            "kind": "contact",
            "source": "stack contact override",
        },
        "CONTACT_PROCUREMENT": {
            "value": "buy@example.com",
            "kind": "contact",
            "source": "platform.yaml",
        },
    }


def test_rules_are_the_packs_values_with_bounds_and_their_guards(api):
    api.table.scan.return_value = OVERRIDES
    body = _body(_get(api, RULES))
    assert set(body["rules"]) == {
        "QTY_VARIANCE_PCT",
        "PRICE_VARIANCE_ABS_USD",
        "MISSING_GR_ESCALATION_DAYS",
    }
    assert body["rules"]["QTY_VARIANCE_PCT"] == {
        "value": 5,
        "kind": "rule",
        "source": "pack v3",
        # The checkout's finance_ops enables the tolerance guard, which reads it.
        "guarded_by": ["within_tolerance_or_approved"],
        # This fake's stored version holds no SOPs, so nothing cites it.
        "cited_by": [],
    }
    # The form gets the bounds a pack edit would be refused by.
    assert body["bounds"]["QTY_VARIANCE_PCT"] == [0, 100]
    assert body["bounds"]["MISSING_GR_ESCALATION_DAYS"] == [0, 365]


def test_controls_are_the_limits_effective_here_and_the_packs_structure(api):
    api.table.scan.return_value = OVERRIDES
    body = _body(_get(api, CONTROLS))
    assert body["limits"]["AUTO_WRITE_LIMIT_USD"] == {
        "value": 25000,
        "kind": "limit",
        "source": "stack override (lowered from 100000)",
        "guarded_by": [],
        "ceiling": 100000,
    }
    assert body["limits"]["DUAL_APPROVAL_LIMIT_USD"]["value"] == 50000
    assert body["limits"]["DUAL_APPROVAL_LIMIT_USD"]["source"] == "pack v3"
    assert set(body["limits"]) == {"AUTO_WRITE_LIMIT_USD", "DUAL_APPROVAL_LIMIT_USD"}
    assert "within_tolerance_or_approved" in body["write_guards"]
    assert "entitlement" in body and body["may_lower"] is False


def test_rules_say_which_enabled_write_guards_read_each(api, monkeypatch):
    monkeypatch.setattr(
        api.registry,
        "packs",
        lambda: {
            "finance_ops": {
                "pack": {
                    "write_guards": ["po_reference", "within_tolerance_or_approved"]
                }
            }
        },
    )
    rules = _body(_get(api, RULES))["rules"]
    assert rules["QTY_VARIANCE_PCT"]["guarded_by"] == ["within_tolerance_or_approved"]
    assert rules["MISSING_GR_ESCALATION_DAYS"]["guarded_by"] == []


def test_a_packs_numbers_go_only_to_who_may_see_it(api, monkeypatch):
    monkeypatch.setenv(
        "ENTITLEMENT_MAP", json.dumps({"finance_ops": {"groups": ["ap-clerk"]}})
    )
    for resource in (RULES, CONTROLS):
        resp = _get(api, resource)
        assert (resp["statusCode"], _body(resp)["error"]) == (
            403,
            "Not entitled to this pack",
        )
        assert _get(api, resource, groups=(USERS, "ap-clerk"))["statusCode"] == 200
    assert _get(api, SETTINGS)["statusCode"] == 403
    assert _get(api, RULES, pack="nope")["statusCode"] == 404


@pytest.mark.parametrize(
    "resource,body",
    [
        (CONTROLS, {"limits": {"MADE_UP_LIMIT": 1}}),
        (CONTROLS, {"limits": {"QTY_VARIANCE_PCT": 4}}),
        (CONTROLS, {"limits": {"AUTO_WRITE_LIMIT_USD": -1}}),
        (CONTROLS, {"limits": {"AUTO_WRITE_LIMIT_USD": "5"}}),
        (CONTROLS, {"limits": {"AUTO_WRITE_LIMIT_USD": True}}),
        (CONTROLS, {"limits": {"AUTO_WRITE_LIMIT_USD": [5]}}),
        (CONTROLS, {}),
        (SETTINGS, {"contacts": {"ap_team": "not-an-email"}}),
        (SETTINGS, {"contacts": {"cfo": "cfo@example.com"}}),
        # One bad field rejects the whole request.
        (SETTINGS, {"contacts": {"ap_team": "good@example.com", "cfo": "x"}}),
        (SETTINGS, {}),
    ],
)
def test_an_invalid_write_is_refused_whole(api, resource, body):
    assert _put(api, body, resource=resource)["statusCode"] == 400
    api._ddb.transact_write_items.assert_not_called()


def test_an_unknown_pack_is_a_404_without_a_write(api):
    resp = _put(api, {"limits": {"AUTO_WRITE_LIMIT_USD": 5}}, pack="x")
    assert resp["statusCode"] == 404
    api._ddb.transact_write_items.assert_not_called()


def test_every_error_comes_back_at_once(api):
    resp = _put(
        api,
        {"contacts": {"ap_team": "bad", "cfo": "cfo@example.com"}},
        resource=SETTINGS,
    )
    assert len(_body(resp)["details"]) == 2
    resp = _put(api, {"limits": {"QTY_VARIANCE_PCT": 1, "AUTO_WRITE_LIMIT_USD": -1}})
    assert len(_body(resp)["details"]) == 2


def test_a_valid_edit_is_one_transaction_attributed_from_the_authorizer(api):
    resp = _put(
        api,
        {"updated_by": "someone-else", "contacts": {"ap_team": "  new@example.com  "}},
        resource=SETTINGS,
    )
    assert resp["statusCode"] == 200
    assert _body(resp)["updated_by"] == "op@example.com"
    (contact,) = (i["Put"]["Item"] for i in _items(api))
    assert contact["value"] == {"S": "new@example.com"}
    assert contact["updated_by"] == {"S": "op@example.com"}
    assert _put(api, {"limits": {"AUTO_WRITE_LIMIT_USD": 0.75}})["statusCode"] == 200
    (limit,) = (i["Put"]["Item"] for i in _items(api))
    # Decimal(str(x)): the float constructor's 0.7500000000000000555 is refused.
    assert limit["value"] == {"N": "0.75"}


@pytest.mark.parametrize(
    "symbol",
    ["QTY_VARIANCE_PCT", "PRICE_VARIANCE_ABS_USD", "MISSING_GR_ESCALATION_DAYS"],
)
def test_a_rule_value_is_refused_as_pack_content(api, symbol):
    resp = _put(api, {"limits": {symbol: 1}})
    assert resp["statusCode"] == 400
    (detail,) = _body(resp)["details"]
    assert f"{symbol} is a rule value of 'finance_ops'" in detail
    assert "PATCH /packs/finance_ops" in detail and "bundle" in detail
    api._ddb.transact_write_items.assert_not_called()


def test_null_deletes_the_override_rather_than_storing_a_blank(api):
    # Also how a rule override left from an older release is removed: clearing one is
    # always taken, setting one never.
    assert _put(api, {"limits": {"QTY_VARIANCE_PCT": None}})["statusCode"] == 200
    assert _items(api) == [
        {
            "Delete": {
                "TableName": "cfg",
                "Key": {
                    "namespace": {"S": "constant#finance_ops"},
                    "config_key": {"S": "QTY_VARIANCE_PCT"},
                },
            }
        }
    ]


def test_malformed_json_and_other_methods_are_refused(api):
    resp = api.handler(_req("PUT", SETTINGS, "{ not json"), None)
    assert resp["statusCode"] == 400
    assert api.handler(_req("DELETE", SETTINGS), None)["statusCode"] == 405
    assert api.handler(_req("PUT", RULES, {}), None)["statusCode"] == 405


def test_the_allowlist_comes_from_the_published_registry():
    assert "registry.constants()" in (UC / "api" / "config" / "index.py").read_text()


def test_a_contact_write_needs_the_author_floor_which_is_closed_when_unset(
    api, monkeypatch
):
    body = {"contacts": {"ap_team": "a@example.com"}}
    groups = (USERS, LIMIT_OWNERS)
    assert _put(api, body, groups=groups, resource=SETTINGS)["statusCode"] == 403
    monkeypatch.setenv("SOP_AUTHOR_GROUPS", "")
    assert _put(api, body, resource=SETTINGS)["statusCode"] == 403
    api._ddb.transact_write_items.assert_not_called()


def test_lowering_a_limit_needs_the_limit_owners_not_the_authors(api, monkeypatch):
    body = {"limits": {"AUTO_WRITE_LIMIT_USD": 3}}
    assert _put(api, body, groups=(USERS, AUTHORS))["statusCode"] == 403
    monkeypatch.setenv("LIMIT_OWNER_GROUPS", "")
    assert _put(api, body)["statusCode"] == 403, "closed when the group is unset"
    api._ddb.transact_write_items.assert_not_called()
    monkeypatch.setenv("LIMIT_OWNER_GROUPS", LIMIT_OWNERS)
    monkeypatch.setenv("SOP_AUTHOR_GROUPS", "")
    assert _put(api, body, groups=(USERS, LIMIT_OWNERS))["statusCode"] == 200


def test_lowering_a_limit_needs_the_packs_limit_groups(api, monkeypatch):
    monkeypatch.setenv(
        "ENTITLEMENT_MAP",
        json.dumps(
            {
                "finance_ops": {
                    "limit_groups": ["ap-controller"],
                    "constant_groups": ["ap-manager"],
                }
            }
        ),
    )
    body = {"limits": {"AUTO_WRITE_LIMIT_USD": 3}}
    assert _put(api, body)["statusCode"] == 403
    # A rule setter is not a limit owner: the two grants are apart.
    assert (
        _put(api, body, groups=(USERS, LIMIT_OWNERS, "ap-manager"))["statusCode"] == 403
    )
    api._ddb.transact_write_items.assert_not_called()
    assert _put(api, body, groups=(USERS, "ap-controller"))["statusCode"] == 403
    resp = _put(api, body, groups=(USERS, LIMIT_OWNERS, "ap-controller"))
    assert resp["statusCode"] == 200


def test_may_lower_is_the_function_the_route_decides_with(api, monkeypatch):
    monkeypatch.setenv(
        "ENTITLEMENT_MAP", json.dumps({"finance_ops": {"limit_groups": ["ap-c"]}})
    )
    monkeypatch.setattr(
        api.registry,
        "packs",
        lambda: {"finance_ops": {"pack": {"constants": CONSTANTS["finance_ops"]}}},
    )
    for groups in ((USERS,), (USERS, LIMIT_OWNERS), (USERS, LIMIT_OWNERS, "ap-c")):
        may = _body(_get(api, CONTROLS, groups=groups))["may_lower"]
        put = _put(api, {"limits": {"AUTO_WRITE_LIMIT_USD": 3}}, groups=groups)
        assert may == (put["statusCode"] == 200), groups


@pytest.mark.parametrize("symbol", ["AUTO_WRITE_LIMIT_USD", "DUAL_APPROVAL_LIMIT_USD"])
@pytest.mark.parametrize("value,status", [(200000, 400), (100001, 400), (40000, 200)])
def test_a_write_limit_may_be_lowered_but_not_raised(
    api, monkeypatch, symbol, value, status
):
    monkeypatch.setattr(
        api.registry, "constants", lambda: {"finance_ops": {symbol: 100000}}
    )
    resp = _put(api, {"limits": {symbol: value}})
    assert resp["statusCode"] == status
    if status == 400:
        assert any("may not be raised above" in d for d in _body(resp)["details"])


def test_a_transaction_over_100_items_is_400_before_dynamodb(api, monkeypatch):
    symbols = {f"SYM_{i}": i for i in range(101)}
    monkeypatch.setattr(api.registry, "constants", lambda: {"finance_ops": symbols})
    with mock.patch.object(api, "_validate_constant", return_value=None):
        resp = _put(api, {"limits": symbols})
    assert resp["statusCode"] == 400 and "100" in _body(resp)["error"]
    api._ddb.transact_write_items.assert_not_called()


def test_an_unreadable_registry_is_a_503_with_no_detail(api, monkeypatch):
    from registry import RegistryUnavailable

    monkeypatch.setattr(
        api.registry, "load", mock.Mock(side_effect=RegistryUnavailable("S3 boom"))
    )
    resp = api.handler(_event("GET", "/registry"), None)
    assert resp["statusCode"] == 503
    assert "temporarily unavailable" in _body(resp)["error"].lower()
    assert "S3 boom" not in resp["body"]


@pytest.fixture(params=["quality", "production"])
def outside_development(request, monkeypatch):
    monkeypatch.setenv("PROMOTION_ROLE", request.param)


def test_contacts_and_limits_stay_writable_outside_development(
    outside_development, api
):
    """Pack and SOP edits are development's alone; contacts and a lowered limit are
    current settings, not edits, so they work in every stack."""
    resp = _put(api, {"contacts": {"ap_team": "new@example.com"}}, resource=SETTINGS)
    assert resp["statusCode"] == 200, resp
    assert _put(api, {"limits": {"AUTO_WRITE_LIMIT_USD": 4}})["statusCode"] == 200


def test_get_registry_reports_what_the_caller_may_do_in_each_pack(api, monkeypatch):
    monkeypatch.setenv("ADMINS_GROUP", "business-operations-admins")
    event = _event("GET", "/registry", groups=(USERS, AUTHORS))
    (pack,) = _body(api.handler(event, None))["packs"]
    access = pack["access"]
    # An author with no pack-admin: content and rule values, no structure.
    assert access["see"] and access["edit"]["procedures_text"]
    assert access["edit"]["rule_values"] and not access["edit"]["controls"]
    assert not access["set_trigger_mode"]
    groups = (USERS, AUTHORS, "business-operations-admins")
    event = _event("GET", "/registry", groups=groups)
    assert _body(api.handler(event, None))["packs"][0]["access"]["set_trigger_mode"]


# ── system roles (where the agent writes, behind sign-in) ──


@pytest.mark.parametrize(
    "systems,named,expected",
    [
        ({"s4": "sandbox"}, None, {"name": "s4", "system_role": "sandbox"}),
        # The pack names its system: that one, whatever else there is.
        (
            {"s4": "sandbox", "prd": "production"},
            "prd",
            {"name": "prd", "system_role": "production"},
        ),
        # Names none, and there are two: the poller cannot say either.
        ({"s4": "sandbox", "prd": "production"}, None, None),
        # Names one this use case does not reach.
        ({"s4": "sandbox"}, "ecc", None),
        ({}, None, None),
    ],
)
def test_get_registry_names_each_packs_system_and_system_role(
    api, monkeypatch, systems, named, expected
):
    monkeypatch.setenv("SAP_SYSTEM_ROLES", json.dumps(systems))
    packs = api.registry.packs()
    entry = copy.deepcopy(packs["finance_ops"])
    if named:
        entry["pack"]["sap_system"] = named
    monkeypatch.setattr(api.registry, "packs", lambda: {"finance_ops": entry})
    (pack,) = _body(api.handler(_event("GET", "/registry"), None))["packs"]
    assert pack["sap_system"] == expected


@pytest.mark.parametrize(
    "systems,non_production",
    [
        ({"s4": "sandbox", "dev": "development", "qas": "quality"}, True),
        ({"s4": "sandbox", "prd": "production"}, False),
        # No system is not "every system is a sandbox".
        ({}, False),
        # A system role it does not know counts as production.
        ({"s4": "sandbx"}, False),
    ],
)
def test_settings_say_the_mode_and_every_systems_system_role(
    api, monkeypatch, systems, non_production
):
    monkeypatch.setenv("SAP_SYSTEM_ROLES", json.dumps(systems))
    monkeypatch.setenv("DEPLOYMENT_MODE", "workshop")
    deployment = _body(_get(api, SETTINGS))["deployment"]
    assert deployment == {
        "mode": "workshop",
        "promotion_role": "development",
        "promotion_only": False,
        "sap_systems": [
            {"name": n, "system_role": system_role}
            for n, system_role in sorted(systems.items())
        ],
        "non_production": non_production,
    }


def test_a_caller_entitled_to_nothing_learns_no_system_role(api, monkeypatch):
    monkeypatch.setenv("SAP_SYSTEM_ROLES", json.dumps({"prd": "production"}))
    monkeypatch.setenv(
        "ENTITLEMENT_MAP", json.dumps({"finance_ops": {"groups": ["x"]}})
    )
    resp = _get(api, SETTINGS)
    assert resp["statusCode"] == 403 and "production" not in resp["body"]
    (body,) = [_body(api.handler(_event("GET", "/registry"), None))]
    assert body == {"packs": []}


def test_the_config_function_is_given_every_systems_role_and_the_mode():
    from test_business_operations_api import SAP_SYSTEM, _of, _stack

    prd = {
        **SAP_SYSTEM,
        "system_role": "production",
        "secret_name": "business-operations/sap/prd",
    }
    template = _stack(
        "business-operations.yaml", {"sap": {"systems": {"s4": SAP_SYSTEM, "prd": prd}}}
    )
    # Only the config function holds it: the console's public aws-exports.json is built
    # by `console.exports`, whose arguments carry no system_role.
    (env,) = [
        variables
        for f in _of(template, "AWS::Lambda::Function")
        if "SAP_SYSTEM_ROLES"
        in (variables := f["Properties"].get("Environment", {}).get("Variables", {}))
    ]
    assert "CONFIG_TABLE" in env and "CONTACTS_JSON" in env
    assert json.loads(env["SAP_SYSTEM_ROLES"]) == {"s4": "sandbox", "prd": "production"}
    assert env["DEPLOYMENT_MODE"] == "workshop"


# ── cited_by: which process types' SOPs name each rule ──


def _stored_finance_ops(api, monkeypatch, sha="sealed-v3"):
    import bundle
    import pack_status

    monkeypatch.setattr(pack_status, "_CITATIONS", {})
    stored = {
        **bundle.from_dir(UC / "packs" / "finance_ops"),
        "version": 3,
        "sha256": sha,
    }
    reads = []

    def get_object(Bucket, Key):
        reads.append(Key)
        body = (
            {"version": 3, "sha256": sha}
            if Key.endswith("active.json")
            else stored
            if Key == "_bundles/finance_ops/3.json"
            else None
        )
        if body is None:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(json.dumps(body).encode())}

    api.s3.get_object.side_effect = get_object
    return reads


def test_each_rule_names_the_process_types_whose_sop_cites_it(api, monkeypatch):
    _stored_finance_ops(api, monkeypatch)
    monkeypatch.setattr(
        api.registry,
        "constants",
        lambda: {
            "finance_ops": {
                "QTY_VARIANCE_PCT": 5,
                "DUPLICATE_MATCH_WINDOW_USD": 100,
                "SHORT_PAY_DISPUTE_WINDOW_DAYS": 30,
            }
        },
    )
    rules = _body(_get(api, RULES))["rules"]
    # invoice_matching.txt cites it, and its two aliases run that file too.
    assert {"invoice_matching", "three_way_match", "ap_exception"} <= set(
        rules["DUPLICATE_MATCH_WINDOW_USD"]["cited_by"]
    )
    assert "duplicate_invoice" in rules["DUPLICATE_MATCH_WINDOW_USD"]["cited_by"]
    assert "short_pay" in rules["SHORT_PAY_DISPUTE_WINDOW_DAYS"]["cited_by"]
    for row in rules.values():
        assert row["cited_by"] == sorted(row["cited_by"])


def test_the_stored_version_is_read_once_per_version_not_per_sop(api, monkeypatch):
    reads = _stored_finance_ops(api, monkeypatch)
    _get(api, RULES)
    _get(api, RULES)
    assert reads.count("_bundles/finance_ops/3.json") == 1
    assert not [k for k in reads if "sops" in k or k.endswith(".txt")]


def test_with_no_stored_version_cited_by_is_unknown_not_empty(api, monkeypatch):
    _stored_finance_ops(api, monkeypatch)
    api.s3.get_object.side_effect = lambda **_: (_ for _ in ()).throw(
        __import__("botocore.exceptions").exceptions.ClientError(
            {"Error": {"Code": "NoSuchKey"}}, "GetObject"
        )
    )
    rules = _body(_get(api, RULES))["rules"]
    assert all(row["cited_by"] is None for row in rules.values())


# ── access: what the caller may do across the deployment ──


@pytest.mark.parametrize(
    "groups,role,create,contacts",
    [
        ((USERS, AUTHORS), "development", True, True),
        ((USERS, AUTHORS), "quality", False, True),
        ((USERS, AUTHORS), "production", False, True),
        ((USERS,), "development", False, False),
    ],
)
def test_settings_say_what_the_caller_may_do_as_the_write_routes_decide(
    api, monkeypatch, groups, role, create, contacts
):
    import pack_access

    monkeypatch.setattr(pack_access, "ROLE", role)
    body = _body(_get(api, SETTINGS, groups=groups))
    assert body["access"] == {"may_create_pack": create, "may_edit_contacts": contacts}
    assert body["deployment"]["promotion_only"] is (role != "development")
    # The contacts gate is the one reported.
    put = _put(api, {"contacts": {"ap_team": "x@example.com"}}, groups, SETTINGS)
    assert (put["statusCode"] != 403) is contacts
