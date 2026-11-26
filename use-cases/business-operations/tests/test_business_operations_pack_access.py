# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The caller's access per pack part, as `GET /registry` reports it, against the routes
that make the decision: each `edit` answer must be what `PATCH /packs/{pack_id}` does with
the same caller, in the same stack."""

from __future__ import annotations

import importlib.util
import json
from unittest import mock

import boto3
import pytest
import test_business_operations_packs as tp
from test_business_operations_packs import _event

USERS = "business-operations-users"
AUTHORS = "business-operations-authors"
ADMINS = "business-operations-admins"
PACK_ADMINS = "business-operations-pack-admins"
CLERKS = "business-operations-ap-clerks"
APPROVERS = "business-operations-ap-approvers"
WRITERS = "business-operations-ap-writers"
SETTERS = "business-operations-ap-setters"
LIMIT_OWNERS = "business-operations-limit-owners"
LIMITERS = "business-operations-ap-limiters"
WORKERS = "business-operations-case-workers"
PROCESSORS = "business-operations-ap-processors"
FINANCE = json.loads((tp.UC / "packs" / "finance_ops" / "pack.json").read_text())
ENTITLEMENT = {
    "groups": [CLERKS],
    "approve_groups": [APPROVERS],
    "author_groups": [WRITERS],
    "constant_groups": [SETTERS],
    "limit_groups": [LIMITERS],
    "process_groups": [PROCESSORS],
}
DOCUMENT = {**FINANCE, "entitlement": ENTITLEMENT}

CALLERS = {
    "clerk": (USERS, CLERKS),
    "approver": (USERS, CLERKS, APPROVERS),
    "writer": (USERS, AUTHORS, CLERKS, WRITERS),
    "setter": (USERS, AUTHORS, CLERKS, WRITERS, SETTERS),
    "writer_without_floor": (USERS, CLERKS, WRITERS),
    "limiter": (USERS, CLERKS, LIMIT_OWNERS, LIMITERS),
    "limiter_without_floor": (USERS, CLERKS, LIMITERS),
    "worker": (USERS, CLERKS, WORKERS, PROCESSORS),
    "worker_outside_process_groups": (USERS, CLERKS, WORKERS),
    "pack_admin": (USERS, AUTHORS, CLERKS, PACK_ADMINS),
    "admin": (USERS, CLERKS, ADMINS),
}
#: One patch per edit part, as `POST /packs/publish` takes it.
RULES = {k: v for k, v in FINANCE["constants"].items() if not k.endswith("_LIMIT_USD")}
PATCHES = {
    "intake": {"pack": {"fields": FINANCE["fields"]}},
    "procedures_text": {"pack": {"display_name": "Renamed"}},
    "rule_values": {"pack": {"constants": RULES}},
    "procedures_structure": {"pack": {"shared_sops": ["short_pay"]}},
    "controls": {"pack": {"write_guards": FINANCE["write_guards"]}},
    "agent_configuration": {"pack": {"max_turns": 12}},
    "evaluations": {"pack": {"seed": FINANCE["seed"]}},
}


@pytest.fixture(params=["development", "quality"])
def stack(request, monkeypatch):
    """The packs route and `pack_access`, in one stack role, with the composer stubbed."""
    for k, v in {
        "API_ALLOWED_CLIENTS": "console-client",
        "USERS_GROUP": USERS,
        "ADMINS_GROUP": ADMINS,
        "SOP_AUTHOR_GROUPS": AUTHORS,
        "LIMIT_OWNER_GROUPS": LIMIT_OWNERS,
        "CASE_WORKER_GROUPS": WORKERS,
        "PROMOTION_ROLE": request.param,
        "ENTITLEMENT_MAP": json.dumps({"finance_ops": ENTITLEMENT}),
        "PACK_COMPOSER_FUNCTION": "composer",
        "REGISTRY_BUCKET": "reg",
    }.items():
        monkeypatch.setenv(k, v)
    with mock.patch.object(boto3, "client"):
        spec = importlib.util.spec_from_file_location(
            f"bo_packs_access_{request.param}", tp.UC / "api" / "packs" / "index.py"
        )
        route = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(route)
        import pack_access

        pack_access = importlib.reload(pack_access)
    monkeypatch.setattr(
        route.registry, "packs", lambda: {"finance_ops": {"pack": DOCUMENT}}
    )
    monkeypatch.setattr(route.registry, "refresh_for_request", lambda: None)
    monkeypatch.setattr(
        route.composer_client, "call", lambda payload: {"pack_id": "finance_ops"}
    )
    return route, pack_access, request.param


def _access(pack_access, groups):
    from entitlement import CallerEntitlement

    caller = CallerEntitlement(frozenset(groups), {"finance_ops": ENTITLEMENT})
    return pack_access.access(groups, caller, "finance_ops", DOCUMENT)


@pytest.mark.parametrize("who", sorted(CALLERS))
def test_each_edit_answer_is_what_the_patch_route_does(stack, who):
    route, pack_access, role = stack
    groups = CALLERS[who]
    edit = _access(pack_access, groups)["edit"]
    for part, patch in PATCHES.items():
        event = _event("PATCH", "/packs/{pack_id}", groups=groups, body=patch)
        event["pathParameters"] = {"pack_id": "finance_ops"}
        response = route.handler(event, None)
        # Authorization is decided before any value is checked, so only a 403 refuses.
        allowed = response["statusCode"] != 403
        if part == "procedures_text":
            # The SOPs go through the SOP routes, which gate on the floor and the pack's
            # author_groups alone and refuse every write outside development. A
            # pack-admin outside author_groups passes the patch route but not this.
            import pack_tiers
            import sop_store

            allowed = (
                allowed
                and sop_store.may_author(
                    frozenset(groups), "finance_ops", "finance_ops/x.txt"
                )
                and not pack_tiers.edit_refusal(role)
            )
        assert edit[part] is allowed, (role, who, part, response["body"])


def test_outside_development_nothing_is_editable_but_the_rest_holds(stack):
    _, pack_access, role = stack
    if role == "development":
        pytest.skip("the development answers are checked against the route above")
    granted = _access(pack_access, CALLERS["setter"])
    assert not any(granted["edit"].values())
    assert granted["rehearse"]
    assert _access(pack_access, CALLERS["limiter"])["lower_limits"]


def test_the_actions_follow_the_packs_groups(stack):
    _, pack_access, _ = stack
    clerk = _access(pack_access, CALLERS["clerk"])
    assert clerk["see"] and not any(clerk["edit"].values())
    assert not (clerk["approve"] or clerk["process"] or clerk["rehearse"])
    assert not (clerk["lower_limits"] or clerk["set_trigger_mode"])
    approver = _access(pack_access, CALLERS["approver"])
    assert approver["approve"] and not approver["rehearse"]
    # Approving and starting a run are apart: a case worker processes, approves nothing.
    assert not approver["process"]
    worker = _access(pack_access, CALLERS["worker"])
    assert worker["process"] and not worker["approve"]
    assert not _access(pack_access, CALLERS["worker_outside_process_groups"])["process"]
    writer = _access(pack_access, CALLERS["writer"])
    assert writer["rehearse"] and not writer["lower_limits"]
    assert not _access(pack_access, CALLERS["writer_without_floor"])["rehearse"]
    # Lowering a limit is the limit owners', apart from setting a rule value.
    assert not _access(pack_access, CALLERS["setter"])["lower_limits"]
    limiter = _access(pack_access, CALLERS["limiter"])
    assert limiter["lower_limits"] and not any(limiter["edit"].values())
    assert not _access(pack_access, CALLERS["limiter_without_floor"])["lower_limits"]
    assert _access(pack_access, CALLERS["admin"])["set_trigger_mode"]
    assert not _access(pack_access, CALLERS["pack_admin"])["set_trigger_mode"]


def test_a_pack_with_no_rule_or_limit_offers_neither(stack):
    _, pack_access, _ = stack
    from entitlement import CallerEntitlement

    groups = (*CALLERS["setter"], LIMIT_OWNERS, LIMITERS)
    caller = CallerEntitlement(frozenset(groups), {"finance_ops": ENTITLEMENT})
    granted = pack_access.access(groups, caller, "finance_ops", {"constants": {}})
    assert not granted["edit"]["rule_values"] and not granted["lower_limits"]


@pytest.mark.parametrize("who", sorted(CALLERS))
def test_the_example_cases_answer_is_what_the_evaluations_route_does(stack, who):
    route, pack_access, _role = stack
    groups = CALLERS[who]
    expected = _access(pack_access, groups)["edit"]["example_cases"]
    event = _event(
        "PUT",
        "/packs/{pack_id}/evaluations/{test_id}",
        groups=groups,
        body={"case": {}},
    )
    event["pathParameters"] = {"pack_id": "finance_ops", "test_id": "a_new_case"}
    # Authorization is decided before the case is checked, so only a 403 refuses.
    assert (route.handler(event, None)["statusCode"] != 403) is expected, who


def test_every_key_is_always_present():
    import pack_access
    from entitlement import CallerEntitlement

    granted = pack_access.access((), CallerEntitlement(frozenset(), {}), "x", {})
    assert set(granted) == {
        "see",
        "edit",
        "lower_limits",
        "set_trigger_mode",
        "approve",
        "process",
        "rehearse",
    }
    assert set(granted["edit"]) == {*PATCHES, "example_cases"}
    assert all(v is False for v in [*granted["edit"].values(), granted["see"]])


def test_every_pack_key_is_in_one_part_and_the_parts_are_the_five():
    import pack_tiers

    keys = pack_tiers.CONTENT_KEYS | pack_tiers.STRUCTURE_KEYS
    assert set(pack_tiers.PARTS) == keys
    assert set(pack_tiers.PARTS.values()) == set(pack_tiers.PART_NAMES)
    # Every key the access answers ask about is the part's own, by the same map.
    import pack_access

    for part, part_keys in pack_access.PART_KEYS.items():
        name = "procedures" if part == "procedures_structure" else part
        assert part_keys <= pack_tiers.part_keys(name), part
