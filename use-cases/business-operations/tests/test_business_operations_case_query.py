# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""business-operations API: GET /cases as a server-side query, and GET /cases/counts.

The fake index below orders entries the way DynamoDB does (by `updated_at` only, ties
in no particular order: here, adversarially, newest case id first) and pages by
`Limit`, so a test that walks every page proves no case is repeated or skipped.
"""

from __future__ import annotations

import base64
import json

import pytest
from test_business_operations_api import USERS, FakeTable, _cases, _event

FIELDS = {
    "supplier_number": {"label": "Supplier", "type": "string"},
    "amount": {"label": "Amount", "type": "amount", "currency_field": "currency"},
    "currency": {"label": "Currency", "type": "string"},
    "sap_block_reason": {"label": "Reason", "type": "string", "role": "reason"},
}


#: Each index the list reads, by its sort key.
INDEXES = {
    "pack-status-updated-index": "updated_at",
    "pack-status-amount-index": "amount_sort",
}


class IndexTable(FakeTable):
    """The cases table with its two list indexes behaving as DynamoDB's do."""

    def query(self, **kw):
        self.queries.append(kw)
        sort = INDEXES[kw["IndexName"]]
        values = kw["ExpressionAttributeValues"]
        condition = kw["KeyConditionExpression"]
        if "#k" in condition:
            assert kw["ExpressionAttributeNames"]["#k"] == sort
        rows = [
            i
            for i in self.items.values()
            if i["pack_id"] == values[":p"]
            and i["status"] == values[":s"]
            and sort in i  # a sparse index holds only what carries its key
        ]
        # `:k` the list's cursor, `:u` the summary's `since` (both on the sort key).
        bound = values.get(":k", values.get(":u"))
        if bound is not None:
            if ">=" in condition:
                rows = [r for r in rows if r[sort] >= bound]
            else:
                rows = [r for r in rows if r[sort] <= bound]
        # Ties in an order unrelated to case_id, as the index gives no promise.
        rows.sort(key=lambda r: r["case_id"], reverse=True)
        rows.sort(key=lambda r: r[sort], reverse=not kw.get("ScanIndexForward", True))
        start = kw.get("ExclusiveStartKey")
        if start:
            ids = [r["case_id"] for r in rows]
            rows = rows[ids.index(start["case_id"]) + 1 :]
        limit = kw.get("Limit") or len(rows)
        chunk, rest = rows[:limit], rows[limit:]
        response = {"Count": len(chunk)}
        if kw.get("Select") != "COUNT":
            projected = [
                "created_at",
                "document_number",
                "exception_type",
                "process_type",
                "attributes",
                "disposition",
                "cost_summary",
                "case_id",
                "pack_id",
                "status",
                sort,
            ]
            response["Items"] = [
                {k: v for k, v in r.items() if k in projected} for r in chunk
            ]
        if rest:
            last = chunk[-1]
            response["LastEvaluatedKey"] = {
                k: last[k] for k in ("case_id", "pack_id", "status", sort)
            }
        return response


class Resource:
    def __init__(self, table: IndexTable):
        self.table = table
        self.calls = 0

    def batch_get_item(self, RequestItems):
        self.calls += 1
        ((name, request),) = RequestItems.items()
        keys = [k["case_id"] for k in request["Keys"]]
        # Hand back the last key unprocessed once, as DynamoDB may.
        held = keys[-1:] if self.calls == 1 and len(keys) > 1 else []
        found = [
            self.table.items[k] for k in keys if k in self.table.items and k not in held
        ]
        response = {"Responses": {name: found}}
        if held:
            response["UnprocessedKeys"] = {
                name: {"Keys": [{"case_id": k} for k in held]}
            }
        return response


def _case(n, pack="ap", status="detected", day=None, **extra):
    return {
        "case_id": f"{pack}-{n:026d}",
        "pack_id": pack,
        "status": status,
        # Several cases share a timestamp, so ties are exercised.
        "updated_at": f"2026-11-{(day if day is not None else n // 3) + 1:02d}T00:00:00",
        "process_type": "invoice_matching",
        "agent_traces": [{"timestamp": f"t{i}"} for i in range(3)],
        "action_log": [{"action": "x"}],
        **extra,
    }


@pytest.fixture
def api(monkeypatch):
    def make(cases, entitlement=None, groups=(USERS,)):
        mod, _table, _sqs = _cases(monkeypatch, entitlement or {"ap": {}, "tax": {}})
        import case_query

        # As every writer stores a case (`case_store.put_case`).
        written = [
            {**c, "amount_sort": case_query.amount_sort(c.get("attributes"), FIELDS)}
            for c in cases
        ]
        table = IndexTable({c["case_id"]: c for c in written})
        monkeypatch.setattr(mod, "table", table)
        resource = Resource(table)
        monkeypatch.setattr(mod, "dynamodb", resource)
        monkeypatch.setattr(
            mod.registry,
            "packs",
            lambda: {"ap": {"fields": FIELDS}, "tax": {"fields": FIELDS}},
        )

        def get(resource_path="/cases", **qs):
            multi = {k: v for k, v in qs.items() if isinstance(v, list)}
            single = {k: v for k, v in qs.items() if not isinstance(v, list)}
            event = _event(
                resource=resource_path,
                groups=groups,
                queryStringParameters={**single, **{k: v[-1] for k, v in multi.items()}}
                or None,
                multiValueQueryStringParameters={
                    **{k: [v] for k, v in single.items()},
                    **multi,
                }
                or None,
            )
            response = mod.handler(event, None)
            return response["statusCode"], json.loads(response["body"])

        get.table = table
        get.resource = resource
        get.mod = mod
        return get

    return make


def _walk(get, **qs):
    """Every page of a query, as (case ids in order, pages)."""
    ids, pages, token = [], 0, None
    while True:
        status, body = get(**qs, **({"next_token": token} if token else {}))
        assert status == 200, body
        pages += 1
        ids += [i["case_id"] for i in body["items"]]
        token = body["next_token"]
        if not token:
            return ids, pages
        assert pages < 200, "the walk never ended"


def test_every_status_is_in_exactly_one_view_besides_all_and_team():
    from case_status import DECIDER_VIEWS, VIEWS
    from contracts import CASE_STATUSES

    seen = [
        s for name, (st, _) in VIEWS.items() if name not in ("all", "team") for s in st
    ]
    assert sorted(seen) == sorted(CASE_STATUSES)
    assert VIEWS["all"][0] is None
    # The team view is needs_you's statuses, over every pack the caller sees.
    assert VIEWS["team"] == VIEWS["needs_you"] and DECIDER_VIEWS == {"needs_you"}


CASES = [
    *(_case(n) for n in range(10)),
    *(_case(n, status="awaiting_human_input") for n in range(10, 16)),
    *(_case(n, status="complete") for n in range(16, 24)),
    *(_case(n, "tax", "detected") for n in range(24, 30)),
    *(_case(n, "tax", "manual_review_required") for n in range(30, 33)),
]


def _expected(view_statuses, packs=("ap", "tax"), asc=True, cases=CASES):
    rows = [c for c in cases if c["status"] in view_statuses and c["pack_id"] in packs]
    rows.sort(key=lambda c: (c["updated_at"], c["case_id"]), reverse=not asc)
    return [c["case_id"] for c in rows]


@pytest.mark.parametrize("limit", ["1", "4", "50"])
@pytest.mark.parametrize("order", ["asc", "desc"])
def test_paging_a_view_returns_every_case_once_in_order(api, limit, order):
    get = api(CASES)
    ids, _ = _walk(get, view="needs_you", order=order, limit=limit)
    assert ids == _expected(
        {"awaiting_human_input", "manual_review_required"}, asc=order == "asc"
    )


def test_the_default_view_is_all_newest_first(api):
    get = api(CASES)
    ids, _ = _walk(get, limit="7")
    assert ids == _expected({c["status"] for c in CASES}, asc=False)


def test_each_item_is_cut_to_what_a_list_shows_and_read_whole_by_id(api):
    get = api(CASES)
    status, body = get(view="new", limit="3")
    assert status == 200
    assert (
        body["view"] == "new"
        and body["sort"] == "updated_at"
        and body["order"] == "asc"
    )
    item = body["items"][0]
    assert item["agent_traces"] == [{"timestamp": "t2"}] and item["traces_count"] == 3
    assert "action_log" not in item
    # One BatchGetItem, and its unprocessed key read again.
    assert get.resource.calls == 2


SEARCH = [
    _case(1, document_number="5100001976", attributes={"supplier_number": "SUP-ACME"}),
    _case(2, document_number="5100002000", attributes={"supplier_number": "Globex"}),
    _case(3, document_number="1976", attributes={"supplier_number": "acme-two"}),
]


@pytest.mark.parametrize(
    "q,found",
    [
        ("51000019", [1]),  # a document number's prefix
        ("5100", [1, 2]),
        ("sup-a", [1]),  # the party value, ignoring case
        ("ACME", [3]),  # a prefix, not a substring: SUP-ACME does not match
        ("ap-0000000000000000000000000", [1, 2, 3]),  # the case id
        ("1976", [3]),
        ("nothing", []),
    ],
)
def test_q_matches_a_prefix_of_the_document_the_party_or_the_case_id(api, q, found):
    get = api(SEARCH)
    ids, _ = _walk(get, q=q, order="asc")
    assert ids == [f"ap-{n:026d}" for n in found]


def test_the_party_field_is_the_pack_field_the_table_shows():
    import case_query

    assert case_query.list_fields(FIELDS) == ("amount", "supplier_number")
    hidden = {"x": {"type": "string", "panel": False}, "y": {"type": "string"}}
    assert case_query.list_fields(hidden) == (None, "y")
    assert case_query.list_fields(None) == (None, None)


def test_exception_type_is_repeatable_and_falls_back_to_the_process_type(api):
    cases = [
        _case(1, exception_type="price_variance"),
        _case(2, exception_type="duplicate_invoice"),
        _case(3),  # not identified yet: its process type
    ]
    get = api(cases)
    ids, _ = _walk(
        get, exception_type=["price_variance", "invoice_matching"], order="asc"
    )
    assert ids == [cases[0]["case_id"], cases[2]["case_id"]]


def test_a_search_that_reads_its_budget_hands_back_a_cursor_and_misses_nothing(
    api, monkeypatch
):
    import case_query

    monkeypatch.setattr(case_query, "READ_BUDGET", 2)
    cases = [
        _case(n, day=n, document_number="9" if n % 7 else "5100") for n in range(40)
    ]
    get = api(cases)
    ids, pages = _walk(get, q="5100", order="asc", limit="2")
    assert ids == [c["case_id"] for c in cases if c["document_number"] == "5100"]
    assert pages > len(ids) // 2  # short pages, each with a cursor


def test_many_cases_sharing_a_timestamp_are_neither_repeated_nor_skipped(
    api, monkeypatch
):
    import case_query

    monkeypatch.setattr(case_query, "READ_BUDGET", 3)
    cases = [_case(n, day=n // 12) for n in range(36)]
    get = api(cases)
    for order in ("asc", "desc"):
        ids, _ = _walk(get, q="ap-", order=order, limit="5")
        assert ids == _expected({"detected"}, asc=order == "asc", cases=cases)


def test_sort_by_amount_orders_numerically_with_no_amount_lowest(api):
    amounts = ["900", "1000.50", None, "25", "1000.50", "-5", "n/a"]
    cases = [
        _case(n, attributes={"amount": a} if a is not None else {})
        for n, a in enumerate(amounts)
    ]
    get = api(cases)
    asc, _ = _walk(get, sort="amount", order="asc", pack_id="ap", limit="2")
    desc, _ = _walk(get, sort="amount", order="desc", pack_id="ap", limit="2")
    by = {c["case_id"]: c for c in cases}
    amount = [by[i]["attributes"].get("amount") for i in asc]
    # No amount, or one that is not a number, sorts below every amount, ties by id.
    assert amount[:2] in ([None, "n/a"], ["n/a", None])
    assert amount[2:] == ["-5", "25", "900", "1000.50", "1000.50"]
    assert asc == list(reversed(desc))
    # Every page read the amount index, from the cursor on, never the time index.
    assert {q["IndexName"] for q in get.table.queries} == {"pack-status-amount-index"}


def test_sort_by_amount_reads_a_page_not_the_view(api):
    cases = [_case(n, attributes={"amount": str(n)}) for n in range(600)]
    get = api(cases)
    status, body = get(sort="amount", order="desc", pack_id="ap", limit="5", view="new")
    assert status == 200
    amounts = [i["attributes"]["amount"] for i in body["items"]]
    assert amounts == ["599", "598", "597", "596", "595"]
    # One read of at most a page's budget, not all 600 entries.
    (query,) = get.table.queries
    assert query["Limit"] <= 100


def test_sort_by_amount_needs_one_pack(api):
    get = api(CASES)
    status, body = get(sort="amount")
    assert (status, body["field"]) == (400, "sort")
    # No ceiling on the cases a pack may hold any more.
    assert get(sort="amount", pack_id="ap")[0] == 200


def test_only_a_pack_a_single_pack_caller_sees_sorts_by_amount_without_pack_id(api):
    get = api([_case(1, attributes={"amount": "1"})], entitlement={"ap": {}})
    assert get(sort="amount")[0] == 200


@pytest.mark.parametrize(
    "qs,field",
    [
        ({"view": "needs-you"}, "view"),
        ({"view": ["all", "done"]}, "view"),
        ({"sort": "title"}, "sort"),
        ({"order": "up"}, "order"),
        ({"limit": "0"}, "limit"),
        ({"limit": "201"}, "limit"),
        ({"limit": "x"}, "limit"),
        ({"q": "x" * 65}, "q"),
        ({"q": "  "}, "q"),
        ({"q": "a\x00b"}, "q"),
        ({"exception_type": "Price Variance"}, "exception_type"),
        ({"exception_type": [f"t{i}" for i in range(21)]}, "exception_type"),
        ({"pack_id": "AP"}, "pack_id"),
        ({"status": "detected"}, "status"),
        ({"pack": "ap"}, "pack"),
        ({"next_token": "%%%"}, "next_token"),
    ],
)
def test_every_parameter_is_checked_before_a_read(api, qs, field):
    get = api(CASES)
    status, body = get(**qs)
    assert (status, body["field"]) == (400, field), body
    assert not get.table.queries


def test_a_token_from_another_query_or_not_ours_is_refused(api):
    get = api(CASES)
    status, body = get(view="new", limit="2")
    token = body["next_token"]
    assert get(view="new", limit="2", next_token=token)[0] == 200
    for other in (
        {"view": "done"},
        {"view": "new", "limit": "3"},
        {"view": "new", "q": "a"},
    ):
        status, body = get(**other, next_token=token)
        assert (status, body["field"]) == (400, "next_token")
    state = json.loads(base64.urlsafe_b64decode(token))
    for forged in (
        {**state, "after": [1, 2]},
        {**state, "after": ["x"]},
        {**state, "pack": "hr"},
        {**state, "v": 2},
    ):
        raw = base64.urlsafe_b64encode(json.dumps(forged).encode()).decode()
        assert get(view="new", limit="2", next_token=raw)[0] == 400


def test_a_token_carries_no_pack_so_it_never_reaches_another_packs_rows(api):
    hr = _case(1, "hr", "detected")
    get = api(
        [*CASES, hr],
        entitlement={"ap": {}, "hr": {"groups": ["business-operations-hr"]}},
    )
    ids, _ = _walk(get, view="new", limit="2")
    assert hr["case_id"] not in ids
    assert {q["ExpressionAttributeValues"][":p"] for q in get.table.queries} == {"ap"}
    status, body = get(view="new", pack_id="hr")
    assert (status, body) == (403, {"error": "Not entitled to this pack"})


def test_pack_id_narrows_and_repeats(api):
    get = api(CASES)
    ids, _ = _walk(get, view="new", pack_id="tax", order="asc")
    assert ids == _expected({"detected"}, packs=("tax",))
    ids, _ = _walk(get, view="new", pack_id=["tax", "ap"], order="asc")
    assert ids == _expected({"detected"})


def test_a_key_lookup_takes_no_other_parameter(api):
    get = api(CASES)
    status, body = get(key='ap:{"a":"1"}', view="new")
    assert (status, body["field"]) == (400, "key")


# ── GET /cases/counts ──


def test_counts_count_every_view_with_one_count_query_per_pack_and_status(api):
    from contracts import CASE_STATUSES

    get = api(CASES)
    status, body = get("/cases/counts")
    assert status == 200
    assert body["views"] == {
        "needs_you": 9,
        "team": 9,
        "new": 16,
        "running": 0,
        "failed": 0,
        "done": 8,
        "all": 33,
    }
    assert body["counts"]["tax"] == {"detected": 6, "manual_review_required": 3}
    assert all(q["Select"] == "COUNT" for q in get.table.queries)
    assert len(get.table.queries) == 2 * len(CASE_STATUSES)


def test_counts_take_the_lists_filters(api):
    get = api(SEARCH)
    status, body = get("/cases/counts", q="5100")
    assert status == 200 and body["views"]["new"] == 2 and body["views"]["all"] == 2
    assert not any(q.get("Select") == "COUNT" for q in get.table.queries)
    status, body = get("/cases/counts", view="new")
    assert (status, body["field"]) == (400, "view")


def test_counts_are_floored_like_the_list(api):
    get = api(CASES, entitlement={"ap": {"groups": ["ap-clerk"]}})
    status, _ = get("/cases/counts", pack_id="ap")
    assert status == 403 and not get.table.queries


# ── GET /cases/summary ──


def _closed(
    n, hours_ago, disposition=None, amount=None, currency="USD", cost=None, **kw
):
    from datetime import UTC, datetime, timedelta

    when = (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()
    attributes = {"currency": currency} if currency else {}
    if amount is not None:
        attributes["amount"] = amount
    return _case(
        n,
        status=kw.pop("status", "complete"),
        updated_at=when,
        attributes=attributes,
        **({"disposition": disposition} if disposition else {}),
        **({"cost_summary": {"total_cost_usd": cost}} if cost is not None else {}),
        **kw,
    )


def test_the_summary_counts_outcomes_value_per_currency_and_cost_per_day(api):
    from decimal import Decimal

    cases = [
        _closed(1, 1, "released", "100.50", cost=Decimal("0.25")),
        _closed(2, 2, "released", "200", status="sap_updated", cost=Decimal("0.5")),
        _closed(3, 3, "released", "50", currency="EUR"),
        _closed(4, 4, None, None),
        _closed(5, 30, "released", "999"),  # before since
        _closed(6, 1, "x", "5", status="awaiting_human_input"),  # not closed
    ]
    get = api(cases)
    status, body = get("/cases/summary")
    assert status == 200, body
    totals = body["totals"]
    assert totals["closed"] == 4
    assert totals["by_outcome"]["released"] == {
        "cases": 3,
        "value": {"USD": 300.5, "EUR": 50},
    }
    assert totals["by_outcome"]["none"] == {"cases": 1, "value": {}}
    assert totals["value"] == {"USD": 300.5, "EUR": 50}
    assert totals["agent_cost_usd"] == 0.75
    assert sum(d["closed"] for d in body["days"]) == 4
    assert body["days"][0]["date"] < body["days"][-1]["date"]
    assert len(body["days"]) in (
        2,
        3,
    )  # since is 24 hours back, so two UTC dates or three
    # Only the done statuses, from since, and no case read whole.
    queried = {q["ExpressionAttributeValues"][":s"] for q in get.table.queries}
    assert queried == {"complete", "sap_updated"}
    assert get.resource.calls == 0


def test_a_day_with_nothing_closed_is_a_zero_row(api):
    from datetime import UTC, datetime, timedelta

    since = (datetime.now(UTC) - timedelta(days=5)).isoformat()
    get = api([_closed(1, 1, "released", "1")])
    status, body = get("/cases/summary", since=since)
    assert status == 200 and len(body["days"]) == 6
    assert body["days"][0] == {
        "date": body["days"][0]["date"],
        "closed": 0,
        "by_outcome": {},
        "value": {},
        "agent_cost_usd": 0,
    }


@pytest.mark.parametrize(
    "qs,field",
    [
        ({"since": "yesterday"}, "since"),
        ({"since": "2026-11-19T09:00:00"}, "since"),  # no zone
        ({"since": "2020-01-01T00:00:00Z"}, "since"),  # more than 30 days
        ({"since": "2999-01-01T00:00:00Z"}, "since"),  # the future
        ({"view": "done"}, "view"),
        ({"pack_id": "AP"}, "pack_id"),
    ],
)
def test_the_summary_checks_its_parameters_before_a_read(api, qs, field):
    get = api(CASES)
    status, body = get("/cases/summary", **qs)
    assert (status, body["field"]) == (400, field)
    assert not get.table.queries


def test_the_summary_is_floored_like_the_list(api):
    get = api(CASES, entitlement={"ap": {"groups": ["ap-clerk"]}})
    assert get("/cases/summary")[0] == 403
    get = api(CASES, entitlement={"ap": {}, "hr": {"groups": ["hr"]}})
    assert get("/cases/summary", pack_id="hr")[0] == 403 and not get.table.queries


# ── counts by exception type ──

FACETS = [
    _case(1, exception_type="price_variance", document_number="5100001"),
    _case(2, exception_type="price_variance", document_number="5100002"),
    _case(3, document_number="5100003"),  # not identified: its process type
    _case(
        4, status="complete", exception_type="duplicate_invoice", document_number="9"
    ),
    _case(5, "tax", "awaiting_human_input", exception_type="price_variance"),
]


def test_counts_by_exception_type_per_view(api):
    get = api(FACETS)
    status, body = get("/cases/counts", by="exception_type")
    assert status == 200, body
    types = body["exception_types"]
    assert types["new"] == {"invoice_matching": 1, "price_variance": 2}
    assert types["done"] == {"duplicate_invoice": 1}
    assert types["needs_you"] == {"price_variance": 1}
    assert types["running"] == {} and types["failed"] == {}
    assert sum(types["all"].values()) == body["views"]["all"] == 5
    # The entries come back projected to what the breakdown reads.
    assert all("ProjectionExpression" in q for q in get.table.queries)
    assert not any(q.get("Select") == "COUNT" for q in get.table.queries)


def test_the_facet_takes_q_and_pack_id_but_not_its_own_filter(api):
    get = api(FACETS)
    status, body = get(
        "/cases/counts", by="exception_type", q="5100", exception_type="price_variance"
    )
    assert status == 200
    # The views narrow to the chosen type; the facet still counts every type under q.
    assert body["views"]["new"] == 2
    assert body["exception_types"]["new"] == {
        "invoice_matching": 1,
        "price_variance": 2,
    }
    _, body = get("/cases/counts", by="exception_type", pack_id="tax")
    assert body["exception_types"]["all"] == {"price_variance": 1}


def test_without_by_the_counts_stay_a_count_and_carry_no_breakdown(api):
    get = api(FACETS)
    _, body = get("/cases/counts")
    assert "exception_types" not in body
    assert all(q.get("Select") == "COUNT" for q in get.table.queries)


@pytest.mark.parametrize(
    "by", ["pack_id", "status", ["exception_type", "exception_type"]]
)
def test_by_takes_only_exception_type_once(api, by):
    get = api(FACETS)
    status, body = get("/cases/counts", by=by)
    assert (status, body["field"]) == (400, "by")
    assert not get.table.queries


# ── needs_you and team ──

APPROVERS = "business-operations-ap-manager"
SPLIT = {"ap": {"approve_groups": [APPROVERS]}, "tax": {}}


def test_needs_you_is_what_the_caller_may_decide_and_team_is_every_pack(api):
    get = api(CASES, entitlement=SPLIT)
    waiting = {"awaiting_human_input", "manual_review_required"}
    ids, _ = _walk(get, view="team", order="asc")
    assert ids == _expected(waiting)
    # Not an ap approver: only tax's waiting cases are theirs to decide.
    ids, _ = _walk(get, view="needs_you", order="asc")
    assert ids == _expected(waiting, packs=("tax",))
    approver = api(CASES, entitlement=SPLIT, groups=(USERS, APPROVERS))
    ids, _ = _walk(approver, view="needs_you", order="asc")
    assert ids == _expected(waiting)


def test_a_pack_seen_but_not_decided_is_an_empty_needs_you_not_a_403(api):
    get = api(CASES, entitlement=SPLIT)
    status, body = get(view="needs_you", pack_id="ap")
    assert (status, body["items"], body["next_token"]) == (200, [], None)
    status, body = get(view="needs_you", pack_id="ap", sort="amount")
    assert (status, body["items"]) == (200, [])
    status, body = get(view="team", pack_id="ap")
    assert status == 200 and len(body["items"]) == 6


def test_counts_sum_needs_you_over_the_decider_packs_only(api):
    get = api(CASES, entitlement=SPLIT)
    status, body = get("/cases/counts")
    assert status == 200
    assert (body["views"]["needs_you"], body["views"]["team"]) == (3, 9)
    # Per pack and status is still every pack the caller sees.
    assert body["counts"]["ap"]["awaiting_human_input"] == 6
    status, body = get("/cases/counts", by="exception_type")
    types = body["exception_types"]
    assert sum(types["needs_you"].values()) == 3
    assert sum(types["team"].values()) == 9
