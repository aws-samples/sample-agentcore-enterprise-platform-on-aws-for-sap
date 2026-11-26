# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""business-operations case ratings: how a rating changes the precedent the agent is given.

Two readers rank on a case's net rating (positive raters minus negative): the exemplar
builder, which picks the cases condensed into the exemplar document, and `get_precedent`,
whose SQL runs here on SQLite over the rows the builder actually writes.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
UC = REPO / "use-cases" / "business-operations"
for path in (UC / "pipeline", UC / "agent_knowledge", UC / "api" / "layer" / "python"):
    sys.path.insert(0, str(path))


@pytest.fixture
def builder(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    for k in ("CASES_TABLE", "EXEMPLAR_BUCKET", "MODEL_ID"):
        monkeypatch.setenv(k, os.environ.get(k, "x"))
    import exemplar_builder

    return exemplar_builder


def _case(case_id, segments, updated_at="2026-11-01T00:00:00+00:00", **ratings):
    return {
        "case_id": case_id,
        "process_type": "invoice_matching",
        "status": "complete",
        "updated_at": updated_at,
        "attributes": {"supplier_number": "17300001", "amount": "500"},
        "agent_traces": [{"segments": [{"type": "text"}] * segments}],
        "ratings": {
            sub: {"rating": r, "name": sub, "at": "2026-11-02T00:00:00+00:00"}
            for sub, r in ratings.items()
        },
    }


def _ids(cases):
    return [c["case_id"] for c in cases]


def test_rating_score_is_raters_up_minus_raters_down(builder):
    assert builder.rating_score({}) == 0
    assert (
        builder.rating_score(_case("a", 1, x="positive", y="positive", z="negative"))
        == 1
    )


def test_a_rating_changes_which_exemplar_is_picked(builder):
    short, long_ = _case("short", 5), _case("long", 20)
    # Unrated, the shorter trace wins.
    assert _ids(builder._pick_best([long_, short], 1)) == ["short"]
    # One thumbs-up on the longer case outranks any trace length.
    long_["ratings"]["op"] = {"rating": "positive"}
    assert _ids(builder._pick_best([long_, short], 1)) == ["long"]
    # A case scored below zero is never picked, however short.
    short["ratings"]["op"] = {"rating": "negative"}
    assert _ids(builder._pick_best([long_, short], 3)) == ["long"]


def test_recency_breaks_a_tie(builder):
    old = _case("old", 5, "2026-10-01T00:00:00+00:00")
    new = _case("new", 5, "2026-10-20T00:00:00+00:00")
    assert _ids(builder._pick_best([old, new], 2)) == ["new", "old"]


class _FakeDataApi:
    def __init__(self):
        self.calls = []

    def execute_statement(self, **kw):
        self.calls.append(kw)


def _build(builder, monkeypatch, cases):
    """Run the handler with agent knowledge on; return the precedent rows it wrote."""
    api = _FakeDataApi()
    monkeypatch.setattr(builder, "_query_successful_cases", lambda: cases)
    monkeypatch.setattr(
        builder,
        "_agent_knowledge_target",
        lambda: {"cluster": "c", "secret": "s", "database": "d"},
    )
    monkeypatch.setattr(builder.boto3, "client", lambda *a, **k: api)
    builder.handler({}, None)
    return _rows(api, builder.PRECEDENT_UPSERT)


def _rows(api, sql):
    return [
        {p["name"]: next(iter(p["value"].values())) for p in call["parameters"]}
        for call in api.calls
        if call["sql"] == sql
    ]


def _get_precedent(rows: list[dict]) -> list[str]:
    """`queries.PRECEDENT_SQL` on SQLite: same WHERE and ORDER BY, no Postgres casts."""
    import queries

    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE precedent (case_id, process_type, supplier_number, amount_band,"
        " disposition, tool_sequence, sop_version, user_rating, resolved_at)"
    )
    for r in rows:
        db.execute(
            "INSERT INTO precedent VALUES (:case_id, :process_type, :supplier_number,"
            " :amount_band, :disposition, :tool_sequence, :sop_version, :user_rating,"
            " :resolved_at)",
            r,
        )
    sql = queries.PRECEDENT_SQL.replace("agent_knowledge.", "").replace(
        "tool_sequence::text AS tool_sequence", "tool_sequence"
    )
    found = db.execute(
        sql,
        {
            "process_type": "invoice_matching",
            "supplier_number": "17300001",
            "amount_band": "lt_1000",
        },
    )
    return [row[0] for row in found]


def test_a_rating_changes_which_precedent_get_precedent_returns(builder, monkeypatch):
    # Four cases, all newer than the one that will be rated up.
    cases = [
        _case(f"c{i}", 5, f"2026-10-{10 + i}T00:00:00+00:00") for i in range(4)
    ] + [_case("rated", 30, "2026-10-01T00:00:00+00:00")]
    before = _get_precedent(_build(builder, monkeypatch, cases))
    assert before == ["c3", "c2", "c1"]  # newest first; LIMIT 3

    cases[-1]["ratings"]["op"] = {"rating": "positive"}  # the oldest, now rated up
    cases[3]["ratings"]["op"] = {"rating": "negative"}  # the newest, now rated down
    rows = _build(builder, monkeypatch, cases)
    # Every candidate gets a row, so the thumbs-down reaches the table.
    assert {r["case_id"]: r["user_rating"] for r in rows}["c3"] == -1
    # The row's time is the case's own, not the build's.
    assert {r["case_id"]: r["resolved_at"] for r in rows}[
        "c3"
    ] == "2026-10-13T00:00:00+00:00"
    assert _get_precedent(rows) == ["rated", "c2", "c1"]


def test_every_case_rated_down_removes_the_stale_exemplar(builder, monkeypatch):
    deleted = []
    monkeypatch.setattr(
        builder, "_query_successful_cases", lambda: [_case("a", 5, op="negative")]
    )
    monkeypatch.setattr(builder, "_agent_knowledge_target", dict)
    monkeypatch.setattr(builder, "exemplar_s3_key", lambda pt: f"ap/{pt}_exemplars.md")
    monkeypatch.setattr(
        builder,
        "s3",
        type(
            "S3", (), {"delete_object": lambda self, **kw: deleted.append(kw["Key"])}
        )(),
    )
    result = builder.handler({}, None)
    assert deleted == ["ap/invoice_matching_exemplars.md"]
    assert result["picked"] == {"invoice_matching": []}


def _comment(rating, comment, author=True):
    entry = {"rating": rating, "name": "rev@example.com", "comment": comment}
    return {**entry, "author": True} if author else entry


def test_only_a_pack_authors_thumbs_down_comment_becomes_a_lesson(builder):
    case = _case("a", 5)
    case["ratings"] = {
        "s1": _comment("negative", "  Check the remit-to bank first.  "),
        "s2": _comment("negative", "a reader's note", author=False),
        "s3": _comment("positive", "praise, not a lesson"),
        "s4": _comment("negative", "   "),
        "s5": {"rating": "negative", "name": "x", "author": True},
    }
    assert builder.lessons_from_ratings(case) == [
        {"lesson": "Check the remit-to bank first.", "authored_by": "rev@example.com"}
    ]


def test_lessons_are_rewritten_per_case_and_reach_get_precedent_for_that_supplier(
    builder, monkeypatch
):
    import queries

    noted, other = _case("noted", 5), _case("other", 5)
    noted["ratings"]["s1"] = _comment("negative", "Duplicate IBAN on this vendor.")
    api = _FakeDataApi()
    monkeypatch.setattr(builder, "_query_successful_cases", lambda: [noted, other])
    monkeypatch.setattr(
        builder,
        "_agent_knowledge_target",
        lambda: {"cluster": "c", "secret": "s", "database": "d"},
    )
    monkeypatch.setattr(builder.boto3, "client", lambda *a, **k: api)
    builder.handler({}, None)

    lesson_calls = [
        c["sql"]
        for c in api.calls
        if c["sql"] in (queries.CASE_LESSONS_DELETE_SQL, queries.CASE_LESSON_INSERT_SQL)
    ]
    # Delete first, over every candidate, so a withdrawn thumbs-down stops teaching.
    assert lesson_calls == [
        queries.CASE_LESSONS_DELETE_SQL,
        queries.CASE_LESSON_INSERT_SQL,
    ]
    (deleted,) = _rows(api, queries.CASE_LESSONS_DELETE_SQL)
    assert sorted(json.loads(deleted["case_ids"])) == ["noted", "other"]
    (row,) = _rows(api, queries.CASE_LESSON_INSERT_SQL)
    assert row["scope_vendor"] == "17300001" and row["case_id"] == "noted"

    # `queries.LESSON_SQL` on SQLite over that row: the supplier sees it, another does not.
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE lesson (lesson_id INTEGER PRIMARY KEY, process_type,"
        " scope_vendor, lesson, authored_by, source_case,"
        " created_at DEFAULT CURRENT_TIMESTAMP)"
    )
    db.execute(
        "INSERT INTO lesson (process_type, scope_vendor, lesson, authored_by,"
        " source_case) VALUES (:process_type, :scope_vendor, :lesson, :authored_by,"
        " :case_id)",
        row,
    )
    sql = queries.LESSON_SQL.replace("agent_knowledge.", "")

    def lessons(supplier):
        found = db.execute(
            sql, {"process_type": "invoice_matching", "supplier_number": supplier}
        )
        return [r[1] for r in found]

    assert lessons("17300001") == ["Duplicate IBAN on this vendor."]
    assert lessons("99999999") == []


def test_a_failed_lesson_write_leaves_the_precedents_standing(builder, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("Data API down")

    monkeypatch.setattr(builder, "_write_lessons", boom)
    rows = _build(builder, monkeypatch, [_case("a", 5)])
    assert [r["case_id"] for r in rows] == ["a"]
