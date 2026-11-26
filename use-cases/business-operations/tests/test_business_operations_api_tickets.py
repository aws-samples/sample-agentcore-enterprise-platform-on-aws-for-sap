# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""business-operations API: GET /tickets — the status GSI, batched entitlement reads,
and pagination.

The earlier system's route scanned the table and read each ticket's case with its own
GetItem, and outran its timeout at a few hundred tickets. These pin the shape that
replaced it: O(1) queries plus ceil(cases/100) BatchGetItems.
"""

from __future__ import annotations

import importlib.util
import json
from unittest import mock

import pytest
from case_key import derive_case_id
from test_business_operations_api import CONSOLE, UC, USERS, _event

_MAP = {"ap": {"groups": ["ap-clerk"]}, "other": {"groups": ["x"]}}


class StubTable:
    """A status GSI with no sort key, and 400-row pages."""

    def __init__(self, items, page=400):
        self.items, self.page, self.calls = items, page, []

    def _read(self, rows, kw):
        start = kw.get("ExclusiveStartKey")
        if start:
            idx = next(
                i for i, r in enumerate(rows) if r["ticket_id"] == start["ticket_id"]
            )
            rows = rows[idx + 1 :]
        want = kw.get("ExpressionAttributeValues", {}).get(":assigned")
        n = min(kw.get("Limit", self.page), self.page)
        chunk, rest = rows[:n], rows[n:]
        resp = {
            "Items": [
                dict(r) for r in chunk if want is None or r.get("assigned_to") == want
            ]
        }
        if rest:
            keys = ("ticket_id", "status") if "IndexName" in kw else ("ticket_id",)
            resp["LastEvaluatedKey"] = {k: chunk[-1][k] for k in keys}
        return resp

    def scan(self, **kw):
        self.calls.append(("scan", kw))
        return self._read(self.items, kw)

    def query(self, **kw):
        self.calls.append(("query", kw))
        assert kw["IndexName"] == "status-index"
        status = kw["ExpressionAttributeValues"][":status"]
        return self._read([r for r in self.items if r["status"] == status], kw)


class StubDynamo:
    """batch_get_item over case_id -> pack_id, optionally holding keys back once."""

    def __init__(self, cases, hold_back=0):
        self.cases, self.hold_back, self.calls = cases, hold_back, []

    def batch_get_item(self, RequestItems):
        self.calls.append(RequestItems)
        ((name, req),) = RequestItems.items()
        assert len(req["Keys"]) <= 100 and req["ConsistentRead"] is True
        keys = req["Keys"]
        held, self.hold_back = keys[: self.hold_back], 0
        found = [
            {"case_id": k["case_id"], "pack_id": self.cases[k["case_id"]]}
            for k in keys[len(held) :]
            if k["case_id"] in self.cases
        ]
        resp = {"Responses": {name: found}}
        if held:
            resp["UnprocessedKeys"] = {name: {**req, "Keys": held}}
        return resp


@pytest.fixture
def api(monkeypatch):
    for k, v in {
        "API_ALLOWED_CLIENTS": CONSOLE,
        "USERS_GROUP": USERS,
        "TICKETS_TABLE_NAME": "tickets",
        "CASES_TABLE_NAME": "cases",
        "ENTITLEMENT_MAP": json.dumps(_MAP),
    }.items():
        monkeypatch.setenv(k, v)
    with mock.patch("boto3.resource"), mock.patch("boto3.client"):
        spec = importlib.util.spec_from_file_location(
            "bo_api_tickets_list", UC / "api" / "tickets" / "index.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    mod.time = mock.MagicMock()  # no real backoff sleeps
    return mod


def _case(c: int) -> str:
    return derive_case_id("ap", "ap", {"n": str(c)})


def _seed(api, n_tickets=1000, n_cases=250, hold_back=0, foreign_every=0):
    api.table = StubTable(
        [
            {
                "ticket_id": f"TKT-{i:05d}",
                "status": "open" if i % 2 == 0 else "closed",
                "case_id": _case(i % n_cases),
                "updated_at": f"2026-10-26T00:{i // 60 % 60:02d}:{i % 60:02d}",
                "assigned_to": "u1" if i % 4 == 0 else "",
            }
            for i in range(n_tickets)
        ]
    )
    api.dynamodb = StubDynamo(
        {
            _case(c): "other" if foreign_every and c % foreign_every == 0 else "ap"
            for c in range(n_cases)
        },
        hold_back,
    )


def _get(api, groups=(USERS, "ap-clerk"), **qs):
    response = api.handler(
        _event("GET", "/tickets", groups=groups, queryStringParameters=qs or None), None
    )
    return response["statusCode"], json.loads(response["body"])


def test_a_status_filter_queries_the_gsi_and_batches_the_entitlement_reads(api):
    _seed(api)
    status, body = _get(api, status="open")
    assert status == 200 and isinstance(body, list) and len(body) == 500
    assert {t["status"] for t in body} == {"open"}
    assert [op for op, _ in api.table.calls] == ["query", "query"]
    # 500 open tickets over 125 distinct cases: two batch gets, not 500 GetItems.
    assert len(api.dynamodb.calls) == 2
    assert body == sorted(body, key=lambda t: t["updated_at"], reverse=True)


def test_no_status_filter_scans(api):
    _seed(api)
    status, body = _get(api)
    assert status == 200 and len(body) == 1000
    assert {op for op, _ in api.table.calls} == {"scan"}
    assert len(api.dynamodb.calls) == 3


def test_unprocessed_keys_are_retried_not_dropped(api):
    _seed(api, n_tickets=10, n_cases=5, hold_back=3)
    status, body = _get(api)
    assert status == 200 and len(body) == 10
    assert len(api.dynamodb.calls[1]["cases"]["Keys"]) == 3


def test_keys_that_never_process_fail_loudly(api):
    _seed(api, n_tickets=4, n_cases=2)
    api.dynamodb.batch_get_item = lambda RequestItems: {"UnprocessedKeys": RequestItems}
    assert _get(api)[0] == 500


def test_the_entitlement_floor_filters_and_refuses(api, monkeypatch):
    _seed(api, n_tickets=20, n_cases=10, foreign_every=2)
    status, body = _get(api)
    assert status == 200 and len(body) == 10
    # A case the cases table does not know is a refusal, not an open door.
    api.dynamodb.cases.clear()
    assert _get(api) == (200, [])
    # Entitled to nothing at all: 403, not a quiet empty list.
    _seed(api, n_tickets=4, n_cases=2)
    monkeypatch.setenv("ENTITLEMENT_MAP", json.dumps({"ap": {"groups": ["nobody"]}}))
    assert _get(api)[0] == 403


def test_limit_and_next_token_walk_every_page_once(api):
    _seed(api, n_tickets=300)
    seen, token, pages = [], None, 0
    while True:
        qs = {
            "status": "open",
            "limit": "60",
            **({"next_token": token} if token else {}),
        }
        status, body = _get(api, **qs)
        assert status == 200 and set(body) == {"items", "next_token"}
        assert len(body["items"]) <= 60
        seen += [t["ticket_id"] for t in body["items"]]
        pages += 1
        token = body["next_token"]
        if not token:
            break
    assert pages == 3 and len(seen) == len(set(seen)) == 150
    assert all(kw["Limit"] == 60 for _, kw in api.table.calls)


def test_paged_mode_defaults_its_limit_and_applies_assigned_to(api):
    _seed(api, n_tickets=40)
    status, body = _get(api, status="open", assigned_to="u1", next_token="")
    assert status == 200
    assert {t["assigned_to"] for t in body["items"]} == {"u1"}
    assert api.table.calls[0][1]["Limit"] == 100


@pytest.mark.parametrize("limit", ["0", "501", "ten"])
def test_a_bad_limit_is_a_400(api, limit):
    _seed(api, n_tickets=4)
    assert _get(api, limit=limit)[0] == 400


@pytest.mark.parametrize(
    "key",
    [
        {"ticket_id": "TKT-00002", "status": "closed"},  # another status's cursor
        {"ticket_id": "TKT-00002"},  # a scan cursor on the GSI
        {"ticket_id": 2, "status": "open"},
    ],
)
def test_a_forged_next_token_is_a_400(api, key):
    _seed(api, n_tickets=4)
    assert _get(api, status="open", next_token=api._encode_token(key))[0] == 400
    assert _get(api, status="open", next_token="%%%not-base64")[0] == 400


def test_a_ticket_is_created_by_the_token_not_the_body(api):
    api.table = mock.MagicMock()
    body = json.dumps({"title": "t", "created_by": "agent"})
    response = api.handler(_event("POST", "/tickets", body=body), None)
    assert response["statusCode"] == 201, response["body"]
    assert api.table.put_item.call_args.kwargs["Item"]["created_by"] == "sub-1"
