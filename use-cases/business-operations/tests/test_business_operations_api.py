# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""business-operations REST API: who gets in, the ticket approval gate, the case list.

The handlers run in-process against fake DynamoDB and SQS clients; the stack is
synthesized in-process from the contract fixtures.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from unittest import mock

import aws_cdk as cdk
import bo_sap_helper
import boto3
import pytest
from aws_cdk.assertions import Template
from botocore.exceptions import ClientError

from infra_utils.platform_config import load_platform_config
from infra_utils.use_case_context import UseCaseContext

REPO = Path(__file__).resolve().parents[3]
UC = REPO / "use-cases" / "business-operations"
LAYER = UC / "api" / "layer" / "python"
FIXTURES = REPO / "use-cases" / "business-operations" / "fixtures" / "contract"
sys.path.insert(0, str(LAYER))

CONSOLE = "console-client"
USERS, ADMINS = "business-operations-users", "business-operations-admins"
CASE = "ap-" + "a" * 26
SAP_SYSTEM = {
    "system_role": "sandbox",
    "base_url": "https://sap.example.com/sap/opu/odata/sap/",
    "secret_name": "business-operations/sap/s4",
}


def _event(method="GET", resource="/cases", groups=(USERS,), aud=CONSOLE, **extra):
    claims = {"aud": aud, "sub": "sub-1", "email": "op@example.com"}
    claims["cognito:groups"] = "[" + " ".join(groups) + "]"
    return {
        "httpMethod": method,
        "resource": resource,
        "path": resource,
        "headers": {},
        "requestContext": {"authorizer": {"claims": claims}, "requestId": "r1"},
        **extra,
    }


# ── entitlement.admit and the pack gates ──


@pytest.fixture
def gate_env(monkeypatch):
    monkeypatch.setenv("API_ALLOWED_CLIENTS", CONSOLE)
    monkeypatch.setenv("USERS_GROUP", USERS)
    monkeypatch.setenv("ADMINS_GROUP", ADMINS)
    import entitlement

    return entitlement


def test_admit_refuses_another_client_on_the_shared_pool(gate_env):
    assert (
        gate_env.admit(_event(aud="platform-web-client")) == gate_env.FORBIDDEN_CLIENT
    )
    # No client claim at all is a refusal too, not a pass.
    event = _event()
    del event["requestContext"]["authorizer"]["claims"]["aud"]
    assert gate_env.admit(event) == gate_env.FORBIDDEN_CLIENT


def test_admit_needs_the_base_group(gate_env):
    assert gate_env.admit(_event(groups=())) == gate_env.NOT_A_USER
    assert gate_env.admit(_event(groups=("other-tenant-users",))) == gate_env.NOT_A_USER
    assert gate_env.admit(_event()) is None


def test_admit_refuses_everyone_when_the_group_is_unset(gate_env, monkeypatch):
    monkeypatch.delenv("USERS_GROUP")
    assert gate_env.admit(_event()) == gate_env.NOT_A_USER


@pytest.mark.parametrize(
    "method,resource,needs_admin",
    [
        ("PUT", "/deployment/trigger-mode", True),
        ("GET", "/deployment/trigger-mode", False),
        ("PUT", "/packs/{pack_id}/trigger-mode", True),
        ("GET", "/packs/{pack_id}/trigger-mode", False),
        ("GET", "/cases", False),
        ("POST", "/cases/{case_id}/reply", False),
        ("PUT", "/config", False),
    ],
)
def test_admit_gates_admin_routes(gate_env, method, resource, needs_admin):
    user = gate_env.admit(_event(method, resource))
    admin = gate_env.admit(_event(method, resource, groups=(USERS, ADMINS)))
    assert admin is None
    assert user == (gate_env.NOT_AN_ADMIN if needs_admin else None)


def test_every_pack_gate_is_declare_to_allow(gate_env):
    caller = gate_env.CallerEntitlement(
        frozenset({USERS}),
        {"open": {}, "ap": {"groups": ["business-operations-ap"]}},
    )
    assert caller.may_see("open") and caller.may_approve("open")
    assert not caller.may_see("ap")
    # A pack the registry does not know is refused, whatever else is declared.
    assert not caller.may_see("unpublished")
    assert caller.visible_packs == {"open"}
    # Each write gate keys on its own entry, with no deployment-wide switch.
    only_approve = gate_env.CallerEntitlement(
        frozenset({USERS}), {"ap": {"approve_groups": ["business-operations-ap-mgr"]}}
    )
    assert only_approve.may_see("ap") and not only_approve.may_approve("ap")


def test_one_approval_gate_serves_both_decision_routes(gate_env):
    import decisions

    clerk = gate_env.CallerEntitlement(
        frozenset({USERS}), {"ap": {"approve_groups": ["business-operations-ap-mgr"]}}
    )
    # Every decision resumes a run the run checks let write under the limit, so a reader
    # records none of them.
    for decision in ("approved", "denied", "replied"):
        assert decisions.authorize(clerk, "ap", decision) == decisions.NOT_AN_APPROVER
    assert decisions.authorize(clerk, "other", "replied") == decisions.NOT_ENTITLED
    for route in ("api/cases/index.py", "api/tickets/index.py"):
        source = (UC / route).read_text()
        assert "authorize(" in source and "may_approve(" not in source, route


# ── handlers against fake AWS ──


class _Conditional(Exception):
    pass


class FakeTable:
    def __init__(self, items=None, pages=None):
        self.items = dict(items or {})
        self.pages = list(pages or [])
        self.queries: list[dict] = []
        self.updates: list[dict] = []
        self.refuse_updates = False
        self.meta = type(
            "M",
            (),
            {
                "client": type(
                    "C",
                    (),
                    {
                        "exceptions": type(
                            "E", (), {"ConditionalCheckFailedException": _Conditional}
                        )
                    },
                )
            },
        )

    def get_item(self, Key, **_):
        item = self.items.get(next(iter(Key.values())))
        return {"Item": dict(item)} if item else {}

    def query(self, **kw):
        self.queries.append(kw)
        return self.pages.pop(0) if self.pages else {"Items": []}

    def scan(self, **kw):
        raise AssertionError("no route scans the cases table")

    def update_item(self, **kw):
        self.updates.append(kw)
        if self.refuse_updates:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
            )
        return {"Attributes": {**self.items.get(next(iter(kw["Key"].values())), {})}}

    def put_item(self, **kw):
        self.items[kw["Item"]["ticket_id"]] = kw["Item"]


class FakeSqs:
    def __init__(self):
        self.sent: list[dict] = []

    def send_message(self, **kw):
        self.sent.append(kw)


def _load(name: str, monkeypatch, tables: dict, sqs: FakeSqs, env: dict):
    monkeypatch.setenv("API_ALLOWED_CLIENTS", CONSOLE)
    monkeypatch.setenv("USERS_GROUP", USERS)
    monkeypatch.setenv("ADMINS_GROUP", ADMINS)
    monkeypatch.setenv("AGENT_QUEUE_URL", "https://sqs.example/q.fifo")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    resource = type(
        "R",
        (),
        {
            "Table": lambda self, n: tables[n],
            "batch_get_item": lambda self, RequestItems: {"Responses": {}},
            "meta": FakeTable().meta,
        },
    )()
    monkeypatch.setattr(boto3, "resource", lambda *a, **k: resource)
    monkeypatch.setattr(boto3, "client", lambda *a, **k: sqs)
    spec = importlib.util.spec_from_file_location(
        f"bo_api_{name}", UC / "api" / name / "index.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _proposal_trace(ts: str, field: str) -> dict:
    return {
        "trace_id": ts,
        "timestamp": ts,
        "segments": [
            {
                "evidence": {
                    "kind": "notification",
                    "proposed_write": {"fields": [{"name": field, "proposed": "1"}]},
                }
            }
        ],
    }


def _tickets(monkeypatch, entitlement_map, ticket_status="open"):
    cases = FakeTable({CASE: {"case_id": CASE, "pack_id": "ap", "agent_traces": []}})
    tickets = FakeTable(
        {
            "TKT-1": {
                "ticket_id": "TKT-1",
                "case_id": CASE,
                "status": ticket_status,
                "response_type": "approval",
            }
        }
    )
    sqs = FakeSqs()
    mod = _load(
        "tickets",
        monkeypatch,
        {"tickets": tickets, "cases": cases},
        sqs,
        {
            "TICKETS_TABLE_NAME": "tickets",
            "CASES_TABLE_NAME": "cases",
            "ENTITLEMENT_MAP": json.dumps(entitlement_map),
        },
    )
    return mod, cases, tickets, sqs


def _action(mod, action, groups=(USERS,)):
    event = _event(
        "POST",
        "/tickets/{id}/action",
        groups=groups,
        pathParameters={"id": "TKT-1"},
        body=json.dumps({"action": action}),
    )
    event["path"] = "/tickets/TKT-1/action"
    return mod.handler(event, None)


# The earlier system skipped may_approve when no pack declared `groups`; this map declares
# only approve_groups, which is exactly that case.
_APPROVERS_ONLY = {"ap": {"approve_groups": ["business-operations-ap-approvers"]}}


def test_ticket_approval_checks_the_approver_when_no_pack_declares_groups(
    monkeypatch,
):
    mod, cases, tickets, sqs = _tickets(monkeypatch, _APPROVERS_ONLY)
    assert _action(mod, "approved")["statusCode"] == 403
    assert not sqs.sent and not cases.updates and not tickets.updates


def test_ticket_approval_by_an_approver_is_recorded_before_it_is_queued(monkeypatch):
    mod, cases, _tickets_t, sqs = _tickets(monkeypatch, _APPROVERS_ONLY)
    response = _action(
        mod, "approved", groups=(USERS, "business-operations-ap-approvers")
    )
    assert response["statusCode"] == 200, response["body"]
    entry = cases.updates[0]["ExpressionAttributeValues"][":entry"][0]
    assert entry["actor"] == "sub-1" and entry["source"] == "ticket"
    assert entry["action"] == "human_approved"
    assert len(sqs.sent) == 1


def test_ticket_put_of_an_approval_takes_the_same_gate(monkeypatch):
    mod, _cases, tickets, _sqs = _tickets(monkeypatch, _APPROVERS_ONLY)
    event = _event(
        "PUT",
        "/tickets/{id}",
        pathParameters={"id": "TKT-1"},
        body=json.dumps({"status": "approved"}),
    )
    assert mod.handler(event, None)["statusCode"] == 403
    assert not tickets.updates


def test_a_denial_takes_the_approvers_too(monkeypatch):
    mod, _cases, _tickets_t, sqs = _tickets(monkeypatch, _APPROVERS_ONLY)
    assert _action(mod, "denied")["statusCode"] == 403
    assert not sqs.sent


def _cases(monkeypatch, entitlement_map, items=None, pages=None):
    table = FakeTable(items, pages)
    sqs = FakeSqs()
    mod = _load(
        "cases",
        monkeypatch,
        {"cases": table},
        sqs,
        {"TABLE_NAME": "cases", "ENTITLEMENT_MAP": json.dumps(entitlement_map)},
    )
    return mod, table, sqs


def _reply(mod, decision="approved", groups=(USERS,)):
    event = _event(
        "POST",
        "/cases/{case_id}/reply",
        groups=groups,
        pathParameters={"case_id": CASE},
        body=json.dumps({"message": "ok", "decision": decision}),
    )
    event["path"] = f"/cases/{CASE}/reply"
    return mod.handler(event, None)


def test_console_approval_records_the_write_it_saw(monkeypatch):
    traces = [_proposal_trace("t1", "Old"), _proposal_trace("t2", "Amount")]
    mod, table, sqs = _cases(
        monkeypatch,
        {"ap": {}},
        items={
            CASE: {
                "case_id": CASE,
                "pack_id": "ap",
                "agent_traces": traces,
                "cost_summary": {"invocation_count": 25},
            }
        },
    )
    response = _reply(mod)
    assert response["statusCode"] == 200, response["body"]
    update = table.updates[0]
    entry = update["ExpressionAttributeValues"][":entry"][0]
    import decisions

    assert entry["approved_write"] == decisions.write_digest(
        {"fields": [{"name": "Amount", "proposed": "1"}]}
    )
    # The run count, not the trace list's length, which stops growing at the cap.
    assert "cost_summary.invocation_count = :n" in update["ConditionExpression"]
    assert update["ExpressionAttributeValues"][":n"] == 25
    assert (
        json.loads(sqs.sent[0]["MessageBody"])["payload"]["approved_write"]
        == (entry["approved_write"])
    )


def test_a_decision_on_a_case_that_never_ran_requires_it_still_never_ran(monkeypatch):
    mod, table, _sqs = _cases(
        monkeypatch, {"ap": {}}, items={CASE: {"case_id": CASE, "pack_id": "ap"}}
    )
    assert _reply(mod, "replied")["statusCode"] == 200
    condition = table.updates[0]["ConditionExpression"]
    assert "attribute_not_exists(cost_summary.invocation_count)" in condition


def test_a_decision_on_a_settled_or_changed_case_is_refused_and_not_queued(
    monkeypatch,
):
    mod, table, sqs = _cases(
        monkeypatch, {"ap": {}}, items={CASE: {"case_id": CASE, "pack_id": "ap"}}
    )
    table.refuse_updates = True
    assert _reply(mod, "replied")["statusCode"] == 409
    assert not sqs.sent


def test_a_rating_records_the_rater(monkeypatch):
    mod, table, _sqs = _cases(
        monkeypatch, {"ap": {}}, items={CASE: {"case_id": CASE, "pack_id": "ap"}}
    )
    event = _event(
        "PUT",
        "/cases/{case_id}/rating",
        pathParameters={"case_id": CASE},
        body=json.dumps({"rating": "positive"}),
    )
    event["path"] = f"/cases/{CASE}/rating"
    # A body naming another rater is not believed: the rater is the token's.
    event["body"] = json.dumps({"rating": "positive", "sub": "sub-2", "name": "x"})
    response = mod.handler(event, None)
    assert response["statusCode"] == 200
    # The map first, then this rater's own key in it, so a second rater adds a vote
    # and the same rater rating again replaces theirs.
    create, vote = table.updates
    assert create["UpdateExpression"] == "SET ratings = if_not_exists(ratings, :empty)"
    assert vote["UpdateExpression"] == "SET ratings.#rater = :entry"
    assert vote["ExpressionAttributeNames"] == {"#rater": "sub-1"}
    entry = vote["ExpressionAttributeValues"][":entry"]
    assert entry["rating"] == "positive" and entry["name"] == "op@example.com"
    assert json.loads(response["body"])["rater"]["sub"] == "sub-1"


@pytest.mark.parametrize(
    "groups,pack,comment,author",
    [
        # The floor and the pack's own author_groups: what writing a SOP takes.
        ((USERS, "authors", "ap-author"), {"author_groups": ["ap-author"]}, "x", True),
        ((USERS, "ap-author"), {"author_groups": ["ap-author"]}, "x", False),
        ((USERS, "authors"), {"author_groups": ["ap-author"]}, "x", False),
        # No comment, no lesson to mark.
        (
            (USERS, "authors", "ap-author"),
            {"author_groups": ["ap-author"]},
            None,
            False,
        ),
    ],
)
def test_a_rating_marks_a_pack_author_so_only_their_comment_becomes_a_lesson(
    monkeypatch, groups, pack, comment, author
):
    mod, table, _sqs = _cases(
        monkeypatch, {"ap": pack}, items={CASE: {"case_id": CASE, "pack_id": "ap"}}
    )
    monkeypatch.setenv("SOP_AUTHOR_GROUPS", "authors")
    body = {"rating": "negative", **({"comment": comment} if comment else {})}
    event = _event(
        "PUT",
        "/cases/{case_id}/rating",
        groups=groups,
        pathParameters={"case_id": CASE},
        body=json.dumps({**body, "author": True}),  # the body is never believed
    )
    event["path"] = f"/cases/{CASE}/rating"
    assert mod.handler(event, None)["statusCode"] == 200
    entry = table.updates[-1]["ExpressionAttributeValues"][":entry"]
    assert entry.get("author", False) is author


def test_a_rating_on_a_missing_case_creates_nothing(monkeypatch):
    mod, table, _sqs = _cases(
        monkeypatch, {"ap": {}}, items={CASE: {"case_id": CASE, "pack_id": "ap"}}
    )

    def deleted_meanwhile(**kw):
        table.updates.append(kw)
        raise _Conditional

    table.update_item = deleted_meanwhile
    event = _event(
        "PUT",
        "/cases/{case_id}/rating",
        pathParameters={"case_id": CASE},
        body=json.dumps({"rating": "negative"}),
    )
    event["path"] = f"/cases/{CASE}/rating"
    assert mod.handler(event, None)["statusCode"] == 404
    assert all(
        u["ConditionExpression"] == "attribute_exists(case_id)" for u in table.updates
    )


def test_every_handler_goes_through_the_shared_route():
    # `api_http.route` admits first and answers every failure with CORS; its own
    # behaviour is tested in test_business_operations_api_http.py.
    for handler in (UC / "api").glob("*/index.py"):
        text = handler.read_text()
        if handler.parent.name == "people":
            # Not an HTTP handler: the people Gateway's target, which answers each
            # tool only by calling one of the routed handlers below.
            assert "def handler(event, context):" in text
            assert "_route(tool.handler).handler(" in text
            continue
        assert re.search(r"^@route(\(.*\))?\ndef handler\(", text, re.MULTILINE), (
            handler
        )


# ── the stack ──


def _stack(fixture: str, settings: dict | None = None) -> dict:
    spec = importlib.util.spec_from_file_location("bo_stack_api", UC / "stack.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    platform = load_platform_config(FIXTURES / fixture)
    production = platform.deployment.mode == "production"
    ctx = UseCaseContext(
        project="p", environment="dev", platform=platform, production=production
    )

    ctx, config = bo_sap_helper.load(ctx, settings)
    stack = mod.BusinessOperationsStack(
        cdk.App(),
        "p-dev-uc-business-operations",
        ctx=ctx,
        config=config,
        env=cdk.Environment(account="111111111111", region="us-east-1"),
    )
    return Template.from_stack(stack).to_json()


def _of(template: dict, kind: str) -> list[dict]:
    return [r for r in template["Resources"].values() if r["Type"] == kind]


def test_no_api_claims_the_account_cloudwatch_role_unless_told_to():
    assert not _of(_stack("business-operations.yaml"), "AWS::ApiGateway::Account")
    claimed = _stack(
        "business-operations.yaml", {"api": {"set_account_cloudwatch_role": True}}
    )
    assert len(_of(claimed, "AWS::ApiGateway::Account")) == 1


def test_waf_follows_production_and_fronts_every_api():
    assert not _of(_stack("business-operations.yaml"), "AWS::WAFv2::WebACL")
    prod = _stack(
        "business-operations-production.yaml", {"api": {"erp_events_endpoint": True}}
    )
    assert len(_of(prod, "AWS::WAFv2::WebACL")) == 1
    # The console API (with /tickets on it) and /erp-events.
    assert len(_of(prod, "AWS::WAFv2::WebACLAssociation")) == 2


def test_console_client_lists_only_the_brokered_idp():
    prod = _stack("business-operations-production.yaml")
    console = next(
        c["Properties"]
        for c in _of(prod, "AWS::Cognito::UserPoolClient")
        if c["Properties"]["ClientName"].endswith("-console")
    )
    assert console["SupportedIdentityProviders"] != ["COGNITO"]
    assert console["ExplicitAuthFlows"] == ["ALLOW_REFRESH_TOKEN_AUTH"]


def test_every_console_route_has_the_cognito_authorizer_and_ingest_has_none():
    t = _stack("business-operations.yaml", {"api": {"erp_events_endpoint": True}})
    methods = [m["Properties"] for m in _of(t, "AWS::ApiGateway::Method")]
    open_methods = [
        m for m in methods if m["AuthorizationType"] != "COGNITO_USER_POOLS"
    ]
    # Besides /erp-events, only the CORS preflights, which never reach a Lambda.
    assert [m["HttpMethod"] for m in open_methods if m["HttpMethod"] != "OPTIONS"] == [
        "POST"
    ]
    preflights = [m for m in open_methods if m["HttpMethod"] == "OPTIONS"]
    assert preflights and all(m["Integration"]["Type"] == "MOCK" for m in preflights)
    # One per authorized resource, so a browser can preflight every path.
    authed = {json.dumps(m["ResourceId"]) for m in methods if m not in open_methods}
    assert authed <= {json.dumps(m["ResourceId"]) for m in preflights}


def _paths(template: dict) -> set[str]:
    return {
        r["Properties"]["PathPart"] for r in _of(template, "AWS::ApiGateway::Resource")
    }


def test_sample_ticketing_is_on_the_console_api_and_can_be_turned_off():
    on = _stack("business-operations.yaml", {})
    # One RestApi: /tickets is mounted on the console API, behind its authorizer.
    assert len(_of(on, "AWS::ApiGateway::RestApi")) == 1
    assert "tickets" in _paths(on) and "SampleTicketing" in on["Outputs"]
    assert "TicketsApiUrl" not in on["Outputs"]
    off = _stack("business-operations.yaml", {"api": {"sample_ticketing": False}})
    assert len(_of(off, "AWS::ApiGateway::RestApi")) == 1
    assert "tickets" not in _paths(off)
    assert "SampleTicketing" not in off.get("Outputs", {})
    assert not [
        f
        for f in _of(off, "AWS::Lambda::Function")
        if "TICKETS_TABLE_NAME"
        in f["Properties"].get("Environment", {}).get("Variables", {})
    ]


def test_add_user_grants_the_base_group_and_only_known_extras():
    sys.path.insert(0, str(UC))
    from ops import groups_for

    assert groups_for([]) == [USERS]
    # An admin is in no pack's groups otherwise, and /cases is a 403 for them.
    from business_operations import packs

    assert groups_for(["--admin", "--author", "--admin"]) == [
        USERS,
        ADMINS,
        *packs.checkout_groups(),
        "business-operations-authors",
    ]
    assert groups_for(["--limit-owner"]) == [USERS, "business-operations-limit-owners"]
    assert groups_for(["--case-worker"]) == [USERS, "business-operations-case-workers"]
    assert groups_for(["--root"]) is None
    # A pack's own groups, and only this use case's: the pool is shared.
    assert groups_for(["--groups", "business-operations-ap-manager, "]) == [
        USERS,
        "business-operations-ap-manager",
    ]
    for bad in (["--groups"], ["--groups", "admins"], ["--groups", ""]):
        assert groups_for(bad) is None, bad


def test_add_user_create_makes_the_user_it_cannot_find(monkeypatch):
    sys.path.insert(0, str(UC))
    import ops

    idp, ssm = mock.MagicMock(), mock.MagicMock()
    ssm.get_parameter.side_effect = lambda Name: {
        "Parameter": {"Value": "cognito" if Name.endswith("/mode") else "pool-1"}
    }
    idp.list_users.return_value = {"Users": []}
    idp.admin_create_user.return_value = {"User": {"Username": "u-1"}}
    monkeypatch.setattr(
        "boto3.client", lambda name, **_: {"ssm": ssm, "cognito-idp": idp}[name]
    )
    assert ops.add_user(["a@example.com"]) == 1  # no --create: never made up
    idp.admin_create_user.assert_not_called()
    assert ops.add_user(["a@example.com", "--create", "--admin"]) == 0
    assert idp.admin_create_user.call_args.kwargs["Username"] == "a@example.com"
    added = [c.kwargs["GroupName"] for c in idp.admin_add_user_to_group.call_args_list]
    from business_operations import packs

    assert added == [USERS, ADMINS, *packs.checkout_groups()]


def _mounted_routes(template: dict) -> set[tuple[str, str]]:
    resources = {
        k: r["Properties"]
        for k, r in template["Resources"].items()
        if r["Type"] == "AWS::ApiGateway::Resource"
    }

    def path(ref: dict) -> str:
        if "Ref" not in ref:  # Fn::GetAtt RootResourceId
            return ""
        props = resources[ref["Ref"]]
        return f"{path(props['ParentId'])}/{props['PathPart']}"

    return {
        (m["HttpMethod"].lower(), path(m["ResourceId"]) or "/")
        for m in (r["Properties"] for r in _of(template, "AWS::ApiGateway::Method"))
        if m["HttpMethod"] != "OPTIONS"
    }


def test_openapi_documents_exactly_the_mounted_routes():
    import yaml

    spec = yaml.safe_load((UC / "openapi.yaml").read_text())
    documented = {
        (method, p)
        for p, ops in spec["paths"].items()
        for method in ops
        if method in {"get", "put", "post", "patch", "delete"}
    }
    # Every optional route on, so each documented one is mounted somewhere.
    mounted = _mounted_routes(
        _stack(
            "business-operations.yaml",
            {
                "api": {"erp_events_endpoint": True},
                "test_data": {"enabled": True},
                "sap": {"systems": {"s4": SAP_SYSTEM}},
            },
        )
    )
    assert documented == mounted


def _openapi():
    import yaml

    return yaml.safe_load((UC / "openapi.yaml").read_text())


def test_every_request_side_array_in_the_contract_is_capped():
    """The premise of the contract's CKV_OPENAPI_21 suppression in `.ash.yaml`: an
    uncapped array a client can post is the rule's threat; a response array is not."""
    spec = _openapi()
    schemas = spec["components"]["schemas"]
    uncapped = []

    def walk(node, where, seen):
        if isinstance(node, dict):
            ref = node.get("$ref", "")
            if ref:
                name = ref.rsplit("/", 1)[-1]
                if name not in seen:
                    walk(schemas[name], f"{where}/{name}", seen | {name})
                return
            if node.get("type") == "array" and "maxItems" not in node:
                uncapped.append(where)
            for key, value in node.items():
                walk(value, f"{where}/{key}", seen)
        elif isinstance(node, list):
            for value in node:
                walk(value, where, seen)

    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            if isinstance(op, dict):
                for part in ("requestBody", "parameters"):
                    walk(op.get(part), f"{method} {path} {part}", frozenset())
    assert uncapped == []


def test_only_the_push_route_opts_out_of_the_authorizer():
    """The premise of the contract's CKV_OPENAPI_5 suppression in `.ash.yaml`."""
    public = {
        (method, path)
        for path, ops in _openapi()["paths"].items()
        for method, op in ops.items()
        if isinstance(op, dict) and op.get("security") == []
    }
    assert public == {("post", "/erp-events")}


def test_the_seeder_is_refused_under_production():
    with pytest.raises(ValueError, match="test_data.enabled is refused"):
        _stack("business-operations-production.yaml", {"test_data": {"enabled": True}})


def test_admin_email_is_refused_under_production():
    with pytest.raises(ValueError, match="admin_email is refused"):
        _stack(
            "business-operations-production.yaml", {"admin_email": "admin@example.com"}
        )


@pytest.mark.parametrize(
    "old, new",
    [
        ("push_ingest", "erp_events_endpoint"),
        ("cloudwatch_role", "set_account_cloudwatch_role"),
    ],
)
def test_api_keys_have_no_old_name(old, new):
    from business_operations import config as uc_config

    assert getattr(uc_config.load({"api": {new: True}}).api, new) is True
    with pytest.raises(ValueError, match=old):
        uc_config.load({"api": {old: True}})


# ── a second approver above the pack's dual-approval limit ──

_DUAL_TRACE = [_proposal_trace("t2", "Amount")]


def _dual_case(prior_actor=None):
    import decisions

    digest = decisions.write_digest({"fields": [{"name": "Amount", "proposed": "1"}]})
    log = (
        [{"action": "human_approved", "actor": prior_actor, "approved_write": digest}]
        if prior_actor
        else []
    )
    return {
        "case_id": CASE,
        "pack_id": "ap",
        "agent_traces": _DUAL_TRACE,
        "cost_summary": {"invocation_count": 3},
        "attributes": {"amount": "1000", "currency": "USD"},
        "action_log": log,
    }


@pytest.mark.parametrize(
    ("prior", "status", "queued"),
    [(None, 202, False), ("sub-2", 200, True)],
)
def test_an_approval_over_the_dual_limit_waits_for_a_different_approver(
    monkeypatch, prior, status, queued
):
    mod, table, sqs = _cases(monkeypatch, {"ap": {}}, items={CASE: _dual_case(prior)})
    import decisions

    monkeypatch.setattr(decisions, "dual_limit", lambda pack: 500)
    response = _reply(mod)
    assert response["statusCode"] == status, response["body"]
    assert bool(sqs.sent) is queued
    # Recorded either way: the first approval is what the second one completes.
    update = table.updates[0]
    assert update["ExpressionAttributeValues"][":entry"][0]["actor"] == "sub-1"
    # Against the log as read, so a racing approval cannot slip in beside it.
    assert "size(action_log) = :len" in update["ConditionExpression"]
    if not queued:
        assert json.loads(response["body"])["awaiting_second_approver"] is True


def test_a_second_approval_by_the_same_person_is_refused_and_records_nothing(
    monkeypatch,
):
    mod, table, sqs = _cases(monkeypatch, {"ap": {}}, items={CASE: _dual_case("sub-1")})
    import decisions

    monkeypatch.setattr(decisions, "dual_limit", lambda pack: 500)
    response = _reply(mod)
    assert response["statusCode"] == 409
    body = json.loads(response["body"])
    assert "different approver" in body["error"]
    assert body["awaiting_second_approver"] is True
    assert not table.updates and not sqs.sent


def test_the_same_person_may_approve_again_after_a_denial_between(monkeypatch):
    item = _dual_case("sub-1")
    item["action_log"].append(
        {"action": "human_denied", "timestamp": "t", "actor": "sub-3"}
    )
    mod, table, _sqs = _cases(monkeypatch, {"ap": {}}, items={CASE: item})
    import decisions

    monkeypatch.setattr(decisions, "dual_limit", lambda pack: 500)
    assert _reply(mod)["statusCode"] == 202
    assert table.updates


def test_a_repeat_ticket_approval_is_refused_and_records_nothing(monkeypatch):
    mod, cases, tickets, sqs = _tickets(monkeypatch, {"ap": {}})
    cases.items[CASE] = _dual_case("sub-1")
    import decisions

    monkeypatch.setattr(decisions, "dual_limit", lambda pack: 500)
    response = _action(mod, "approved")
    assert response["statusCode"] == 409
    assert "different approver" in json.loads(response["body"])["error"]
    assert not cases.updates and not tickets.updates and not sqs.sent


def test_a_pushed_case_under_the_dual_limit_still_waits_for_a_second_approver(
    monkeypatch,
):
    item = {
        **_dual_case(),
        "origin": "sap-push",
        "attributes": {"amount": "1", "currency": "USD"},
    }
    mod, _table, sqs = _cases(monkeypatch, {"ap": {}}, items={CASE: item})
    import decisions

    monkeypatch.setattr(decisions, "dual_limit", lambda pack: 500)
    response = _reply(mod)
    assert response["statusCode"] == 202 and not sqs.sent
    assert json.loads(response["body"])["awaiting_second_approver"] is True


def test_a_write_under_the_dual_limit_takes_one_approval(monkeypatch):
    mod, _table, sqs = _cases(monkeypatch, {"ap": {}}, items={CASE: _dual_case()})
    import decisions

    monkeypatch.setattr(decisions, "dual_limit", lambda pack: 5000)
    assert _reply(mod)["statusCode"] == 200 and len(sqs.sent) == 1


def test_a_ticket_approval_over_the_dual_limit_keeps_the_ticket_open(monkeypatch):
    mod, cases, tickets, sqs = _tickets(monkeypatch, _APPROVERS_ONLY)
    cases.items[CASE] = _dual_case()
    import decisions

    monkeypatch.setattr(decisions, "dual_limit", lambda pack: 500)
    response = _action(
        mod, "approved", groups=(USERS, "business-operations-ap-approvers")
    )
    assert response["statusCode"] == 202, response["body"]
    assert not sqs.sent and len(cases.updates) == 1
    update = tickets.updates[0]
    assert "#s" not in update["UpdateExpression"]  # the status stays open
    assert "1 of 2" in update["ExpressionAttributeValues"][":comment"][0]["text"]


def test_a_dual_limit_below_the_auto_limit_is_refused():
    import pack_tiers

    errors = pack_tiers.constant_errors(
        {"AUTO_WRITE_LIMIT_USD": 1000, "DUAL_APPROVAL_LIMIT_USD": 500}
    )
    assert any("DUAL_APPROVAL_LIMIT_USD" in e for e in errors)
    assert not pack_tiers.constant_errors(
        {"AUTO_WRITE_LIMIT_USD": 1000, "DUAL_APPROVAL_LIMIT_USD": 5000}
    )


def test_a_dual_limit_without_an_auto_limit_is_refused():
    # The run checks gate unapproved writes on the auto limit: without one, a dual limit
    # would read as a control while letting any unapproved write through.
    import pack_tiers

    errors = pack_tiers.constant_errors({"DUAL_APPROVAL_LIMIT_USD": 5000})
    assert any("needs AUTO_WRITE_LIMIT_USD" in e for e in errors)
    assert not pack_tiers.constant_errors({"AUTO_WRITE_LIMIT_USD": 1000})
