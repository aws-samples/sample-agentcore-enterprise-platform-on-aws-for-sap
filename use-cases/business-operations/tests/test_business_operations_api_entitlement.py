# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""business-operations API: the client gate, the caller's groups and identity, and the
pack floor on the cases and tickets routes.

Two ways the floor regresses into a lie are pinned here: an unentitled read answering
`[]` instead of 403, and a refusal that lands after the write it should prevent.
"""

from __future__ import annotations

import json

import entitlement
import pytest
from entitlement import (
    API_CLIENTS_ENV,
    FORBIDDEN_CLIENT,
    CallerEntitlement,
    caller_client_allowed,
    caller_groups,
    caller_identity,
)
from test_business_operations_api import (
    CASE,
    USERS,
    _cases,
    _event,
    _tickets,
)

_ALLOWED = "webclient123,machineclient456"
_IAS = "ias-federation-client"

_MAP = {
    "ap": {"groups": ["ap-clerk", "ap-manager"], "approve_groups": ["ap-manager"]},
    "accruals": {},
}
_MAP_CLOSED = {"ap": _MAP["ap"]}


# ── which OAuth client minted the token ──


def _nested(**claims) -> dict:
    return {"requestContext": {"authorizer": {"claims": dict(claims)}}}


def _flat(**context) -> dict:
    return {"requestContext": {"authorizer": dict(context)}}


@pytest.fixture
def allowlist(monkeypatch):
    monkeypatch.setenv(API_CLIENTS_ENV, _ALLOWED)


@pytest.mark.parametrize("shape", [_nested, _flat])
@pytest.mark.parametrize("claim", ["aud", "client_id"])
def test_another_client_is_refused_in_every_claim_and_both_shapes(
    allowlist, shape, claim
):
    assert not caller_client_allowed(shape(**{claim: _IAS}))
    # Positive control: a gate that refused everything would fail here.
    assert caller_client_allowed(shape(**{claim: "webclient123"}))
    assert caller_client_allowed(shape(**{claim: "machineclient456"}))


def test_the_decision_is_an_intersection_of_presented_and_allowed(allowlist):
    assert caller_client_allowed(_nested(aud="webclient123", client_id=_IAS))
    assert not caller_client_allowed(_nested(aud=_IAS, client_id="another-client"))


def test_a_list_valued_audience_is_read_element_wise(allowlist):
    assert caller_client_allowed(_nested(aud=["webclient123"]))
    assert not caller_client_allowed(_nested(aud=[_IAS, "yet-another"]))


def test_an_undeclared_allowlist_refuses_every_client(monkeypatch):
    monkeypatch.delenv(API_CLIENTS_ENV, raising=False)
    assert not caller_client_allowed(_nested(aud="webclient123"))
    monkeypatch.setenv(API_CLIENTS_ENV, "")
    assert not caller_client_allowed(_nested(client_id="machineclient456"))


def test_a_context_naming_no_client_or_no_mapping_is_refused(allowlist):
    assert not caller_client_allowed(_nested(sub="u", email="u@example.com"))
    assert not caller_client_allowed({})
    assert not caller_client_allowed(
        {"requestContext": {"authorizer": {"claims": "nope"}}}
    )


def test_the_client_is_never_taken_from_the_request_itself(allowlist):
    event = _nested(aud=_IAS)
    event["headers"] = {"x-client-id": "webclient123", "aud": "webclient123"}
    event["body"] = json.dumps({"client_id": "webclient123", "aud": "webclient123"})
    event["queryStringParameters"] = {"client_id": "webclient123"}
    assert not caller_client_allowed(event)


def test_the_refusal_names_neither_the_client_nor_the_allowlist():
    assert FORBIDDEN_CLIENT == {"error": "Token was not issued for this application"}


def test_another_clients_token_is_refused_before_any_read(monkeypatch):
    mod, table, _sqs = _cases(monkeypatch, _MAP)
    response = mod.handler(_event(aud=_IAS), None)
    assert response["statusCode"] == 403
    assert json.loads(response["body"]) == FORBIDDEN_CLIENT
    assert not table.queries


# ── groups and identity ──


@pytest.mark.parametrize(
    ("context", "expected"),
    [
        ({"groups": '["ap-clerk"]'}, {"ap-clerk"}),
        (
            {"claims": {"cognito:groups": "[ap-clerk ap-manager]"}},
            {"ap-clerk", "ap-manager"},
        ),
        ({"claims": {"cognito:groups": ["ap-manager"]}}, {"ap-manager"}),
        ({"groups": "ap-clerk,ap-manager"}, {"ap-clerk", "ap-manager"}),
        ({"groups": '["with,comma"]'}, {"with,comma"}),
        ({"groups": "[]"}, set()),
        ({}, set()),
    ],
)
def test_caller_groups_reads_both_authorizer_shapes(context, expected):
    assert caller_groups({"requestContext": {"authorizer": context}}) == expected


@pytest.mark.parametrize("claims", ["a string", ["a", "list"], 7])
def test_a_non_dict_context_is_an_unattributed_caller(claims):
    event = {"requestContext": {"authorizer": {"claims": claims}}}
    assert caller_groups(event) == set()
    assert caller_identity(event) == (None, "")


def test_groups_and_identity_are_never_taken_from_the_request_itself():
    event = {
        "headers": {"x-groups": "ap-manager", "x-user-identity": "ap-manager"},
        "body": json.dumps({"groups": ["ap-manager"], "subject": "sub-x"}),
        "queryStringParameters": {"groups": "ap-manager"},
    }
    assert caller_groups(event) == set()
    assert caller_identity(event) == (None, "")


@pytest.mark.parametrize(
    ("context", "expected"),
    [
        (
            {
                "claims": {
                    "email": "c@example.com",
                    "cognito:username": "c",
                    "sub": "s1",
                }
            },
            ("c@example.com", "s1"),
        ),
        ({"preferred_username": "c@example.com", "sub": "s2"}, ("c@example.com", "s2")),
        ({"claims": {"sub": "s3"}}, ("s3", "s3")),
        ({}, (None, "")),
    ],
)
def test_caller_identity_keeps_display_and_subject_apart(context, expected):
    assert caller_identity({"requestContext": {"authorizer": context}}) == expected


# ── CallerEntitlement: each gate on its own key, visibility first ──


def test_the_floor_keys_each_pack_on_its_own_entry():
    clerk = CallerEntitlement(frozenset({"ap-clerk"}), _MAP)
    manager = CallerEntitlement(frozenset({"ap-manager"}), _MAP)
    stranger = CallerEntitlement(frozenset(), _MAP)
    assert clerk.may_see("ap") and not clerk.may_approve("ap")
    assert manager.may_approve("ap")
    assert not stranger.may_see("ap")
    assert stranger.may_see("accruals")
    assert not stranger.may_see("retired_pack")
    assert stranger.visible_packs == {"accruals"}


def test_every_write_gate_requires_visibility_first():
    odd = CallerEntitlement(
        frozenset({"auditor"}),
        {
            "ap": {
                "groups": ["ap-clerk"],
                "approve_groups": ["auditor"],
                "author_groups": ["auditor"],
                "constant_groups": ["auditor"],
            }
        },
    )
    assert not odd.may_see("ap")
    assert not (
        odd.may_approve("ap") or odd.may_author("ap") or odd.may_set_constants("ap")
    )


def test_author_and_constant_groups_engage_alone_and_stay_separate():
    only = {"ap": {"author_groups": ["ap-lead"], "constant_groups": ["ap-manager"]}}
    clerk = CallerEntitlement(frozenset({"ap-clerk"}), only)
    assert clerk.may_see("ap")
    assert not clerk.may_author("ap") and not clerk.may_set_constants("ap")
    assert clerk.may_approve("ap")  # approve_groups is declared nowhere
    lead = CallerEntitlement(frozenset({"ap-lead"}), only)
    assert lead.may_author("ap") and not lead.may_set_constants("ap")
    manager = CallerEntitlement(frozenset({"ap-manager"}), only)
    assert manager.may_set_constants("ap") and not manager.may_author("ap")


def test_a_pack_declared_nowhere_is_refused_for_every_gate():
    lead = CallerEntitlement(frozenset({"ap-lead"}), {"ap": {}})
    for gate in ("may_see", "may_approve", "may_author", "may_set_constants"):
        assert not getattr(lead, gate)("no_such_pack"), gate


def test_the_author_floor_is_closed_when_unset(monkeypatch):
    caller = CallerEntitlement(frozenset({"business-operations-authors"}), {})
    monkeypatch.delenv(entitlement.AUTHOR_FLOOR_ENV, raising=False)
    assert not caller.clears_author_floor()
    monkeypatch.setenv(entitlement.AUTHOR_FLOOR_ENV, "business-operations-authors")
    assert caller.clears_author_floor()


@pytest.mark.parametrize("raw", ["not json", "[]", '{"ap": "open"}'])
def test_a_malformed_explicit_map_fails_closed(raw):
    from registry import RegistryUnavailable

    with pytest.raises(RegistryUnavailable):
        entitlement.load_map(raw)


# ── the floor on /cases ──


def _call(mod, event):
    response = mod.handler(event, None)
    return response["statusCode"], json.loads(response["body"])


def _case_event(method, resource, path, groups=(USERS,), **extra):
    event = _event(method, resource, groups=groups, **extra)
    event["path"] = path
    return event


def test_an_unentitled_pack_filter_is_403_and_reads_nothing(monkeypatch):
    mod, table, _sqs = _cases(monkeypatch, _MAP)
    status, body = _call(mod, _event(queryStringParameters={"pack_id": "ap"}))
    assert status == 403 and "pack" in body["error"]
    assert not table.queries


def test_a_caller_entitled_to_nothing_is_403_not_an_empty_list(monkeypatch):
    mod, table, _sqs = _cases(monkeypatch, _MAP_CLOSED)
    assert _call(mod, _event())[0] == 403
    assert not table.queries


def test_an_entitled_caller_queries_only_the_packs_they_may_see(monkeypatch):
    mod, table, _sqs = _cases(monkeypatch, _MAP)
    status, _ = _call(mod, _event(groups=(USERS, "ap-clerk")))
    assert status == 200
    assert {q["ExpressionAttributeValues"][":p"] for q in table.queries} == {
        "ap",
        "accruals",
    }


def test_the_view_narrows_within_the_floor(monkeypatch):
    mod, table, _sqs = _cases(monkeypatch, _MAP_CLOSED)
    event = _event(groups=(USERS, "ap-manager"), queryStringParameters={"view": "new"})
    assert _call(mod, event)[0] == 200
    assert [q["ExpressionAttributeValues"] for q in table.queries] == [
        {":p": "ap", ":s": "detected"}
    ]


@pytest.mark.parametrize("pack", ["ap", "unpublished"])
def test_case_detail_in_an_unentitled_pack_is_403(monkeypatch, pack):
    mod, _table, _sqs = _cases(
        monkeypatch, _MAP, items={CASE: {"case_id": CASE, "pack_id": pack}}
    )
    event = _case_event(
        "GET", "/cases/{case_id}", f"/cases/{CASE}", pathParameters={"case_id": CASE}
    )
    assert _call(mod, event)[0] == 403


@pytest.mark.parametrize(
    "groups,status,writes", [((USERS,), 403, 0), ((USERS, "ap-clerk"), 200, 2)]
)
def test_a_rating_is_floored_before_the_write(monkeypatch, groups, status, writes):
    mod, table, _sqs = _cases(
        monkeypatch, _MAP, items={CASE: {"case_id": CASE, "pack_id": "ap"}}
    )
    event = _case_event(
        "PUT",
        "/cases/{case_id}/rating",
        f"/cases/{CASE}/rating",
        groups=groups,
        pathParameters={"case_id": CASE},
        body=json.dumps({"rating": "positive"}),
    )
    assert _call(mod, event)[0] == status
    assert len(table.updates) == writes


def _enqueue(mod, body, groups=(USERS,)):
    return _call(
        mod,
        _case_event(
            "POST",
            "/cases/enqueue",
            "/cases/enqueue",
            groups=groups,
            body=json.dumps(body),
        ),
    )


def test_enqueueing_a_case_in_an_unentitled_pack_is_403_before_the_send(monkeypatch):
    mod, _table, sqs = _cases(
        monkeypatch, _MAP, items={CASE: {"case_id": CASE, "pack_id": "ap"}}
    )
    status, body = _enqueue(mod, {"case_id": CASE})
    assert status == 403 and "pack" in body["error"]
    assert not sqs.sent


def test_an_entitled_enqueue_names_the_acting_human_and_groups_on_the_case(monkeypatch):
    mod, _table, sqs = _cases(
        monkeypatch, _MAP, items={CASE: {"case_id": CASE, "pack_id": "ap"}}
    )
    # A reader, or an approver, may not start the agent on a case: the run checks pass its
    # writes under the limit for whoever started it, so it takes a case worker.
    monkeypatch.setenv("CASE_WORKER_GROUPS", WORKERS)
    for groups in ((USERS, "ap-clerk"), (USERS, "ap-manager")):
        status, body = _enqueue(mod, {"case_id": CASE}, groups=groups)
        assert status == 403 and "case workers" in body["error"]
    assert not sqs.sent
    status, body = _enqueue(mod, {"case_id": CASE}, groups=(USERS, "ap-clerk", WORKERS))
    assert (status, body) == (200, {"enqueued": True, "case_id": CASE})
    sent = sqs.sent[0]
    assert json.loads(sent["MessageBody"]) == {
        "case_id": CASE,
        "trigger": "manual",
        "subject": "sub-1",
    }
    # One case is one FIFO group across every producer.
    assert sent["MessageGroupId"] == CASE
    # The queue dedupes on content otherwise, and a repeat enqueue would vanish.
    assert sent["MessageDeduplicationId"]


WORKERS = "business-operations-case-workers"


def test_an_enqueue_needs_the_packs_process_groups_where_it_sets_them(monkeypatch):
    narrowed = {"ap": {"process_groups": ["ap-lead"]}}
    mod, _table, _sqs = _cases(
        monkeypatch, narrowed, items={CASE: {"case_id": CASE, "pack_id": "ap"}}
    )
    monkeypatch.setenv("CASE_WORKER_GROUPS", WORKERS)
    assert _enqueue(mod, {"case_id": CASE}, groups=(USERS, WORKERS))[0] == 403
    assert _enqueue(mod, {"case_id": CASE}, groups=(USERS, "ap-lead"))[0] == 403
    assert (
        _enqueue(mod, {"case_id": CASE}, groups=(USERS, WORKERS, "ap-lead"))[0] == 200
    )
    monkeypatch.setenv("CASE_WORKER_GROUPS", "")
    assert (
        _enqueue(mod, {"case_id": CASE}, groups=(USERS, WORKERS, "ap-lead"))[0] == 403
    )


def test_an_enqueue_of_a_missing_case_is_404(monkeypatch):
    mod, _table, sqs = _cases(monkeypatch, _MAP)
    assert _enqueue(mod, {"case_id": CASE}, groups=(USERS, "ap-clerk"))[0] == 404
    assert not sqs.sent


@pytest.mark.parametrize("bad", [{"case_id": "no-separator-here"}, {"case_id": ""}, {}])
def test_a_malformed_case_id_is_400_before_the_queue(monkeypatch, bad):
    mod, _table, sqs = _cases(monkeypatch, {"ap": {}})
    assert _enqueue(mod, bad)[0] == 400
    assert not sqs.sent


# ── the floor on ticket decisions ──


def _decide(mod, action, groups=(USERS,), method="POST"):
    if method == "POST":
        event = _case_event(
            "POST",
            "/tickets/{id}/action",
            "/tickets/TKT-1/action",
            groups=groups,
            pathParameters={"id": "TKT-1"},
            body=json.dumps({"action": action}),
        )
    else:
        event = _case_event(
            "PUT",
            "/tickets/{id}",
            "/tickets/TKT-1",
            groups=groups,
            pathParameters={"id": "TKT-1"},
            body=json.dumps({"status": action}),
        )
    return mod.handler(event, None)["statusCode"]


def test_an_approver_may_approve(monkeypatch):
    mod, _cases_t, _tk, sqs = _tickets(monkeypatch, _MAP)
    assert _decide(mod, "approved", (USERS, "ap-manager")) == 200
    assert len(sqs.sent) == 1


def test_a_reader_may_not_approve_and_the_agent_is_never_resumed(monkeypatch):
    mod, cases_t, tickets_t, sqs = _tickets(monkeypatch, _MAP)
    assert _decide(mod, "approved", (USERS, "ap-clerk")) == 403
    assert not tickets_t.updates and not cases_t.updates and not sqs.sent
    # A denial resumes the run too, so it takes the approvers as well.
    assert _decide(mod, "denied", (USERS, "ap-clerk")) == 403
    assert not sqs.sent
    assert _decide(mod, "denied", (USERS, "ap-manager")) == 200


def test_a_stranger_may_not_decide_at_all(monkeypatch):
    mod, _cases_t, _tk, sqs = _tickets(monkeypatch, _MAP)
    assert _decide(mod, "denied") == 403
    assert _decide(mod, "approved") == 403
    assert not sqs.sent


def test_an_unresolvable_pack_fails_closed(monkeypatch):
    # The pack comes from the case, never from the agent-written ticket.
    mod, cases_t, _tk, sqs = _tickets(monkeypatch, _MAP)
    cases_t.items.clear()
    assert _decide(mod, "approved", (USERS, "ap-manager")) == 403
    assert not sqs.sent


@pytest.mark.parametrize(
    "stored,status,expected",
    [
        ({"status": "closed"}, "approved", 409),
        ({"status": "denied"}, "approved", 409),
        ({"status": "approved"}, "open", 409),
        ({"response_type": "free_text"}, "approved", 400),
        ({"response_type": "approval"}, "replied", 400),
    ],
    ids=["closed", "decided", "frozen", "free-text", "approval"],
)
def test_the_put_route_takes_the_action_routes_state_checks(
    monkeypatch, stored, status, expected
):
    mod, _cases_t, tickets_t, sqs = _tickets(monkeypatch, _MAP)
    tickets_t.items["TKT-1"].update(stored)
    assert _decide(mod, status, (USERS, "ap-manager"), method="PUT") == expected
    assert not tickets_t.updates and not sqs.sent


def test_the_plain_put_route_cannot_approve_around_the_gate(monkeypatch):
    mod, _cases_t, tickets_t, _sqs = _tickets(monkeypatch, _MAP)
    assert _decide(mod, "approved", (USERS, "ap-clerk"), method="PUT") == 403
    assert not tickets_t.updates
    assert _decide(mod, "approved", (USERS, "ap-manager"), method="PUT") == 200
