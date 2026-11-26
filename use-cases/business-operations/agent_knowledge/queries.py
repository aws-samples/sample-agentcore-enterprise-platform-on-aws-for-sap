# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
SQL and row shaping for the agent knowledge tools.

Kept separate from the handler so the traversal semantics — cycle guard and
depth bound — are testable without a database. `traverse` is the pure reference
for VENDOR_RISK_SQL; if you change one, change both. The DDL, the reads and the
operator load's writes all live here, so the schema has one source.
"""

import json
import time


def amount_band(amount: object) -> str:
    """Bucket a currency amount so precedents join across differing totals.

    The precedent writer must key on this same function, or the join returns
    nothing. Absolute value: a -5000 credit memo raises the same review
    questions as a 5000 debit. A missing or non-numeric amount is the lowest
    band rather than an error.
    """
    try:
        value = abs(float(amount or 0))
    except (TypeError, ValueError):
        value = 0.0
    for ceiling in (100, 1_000, 10_000, 100_000):
        if value < ceiling:
            return f"lt_{ceiling}"
    return "gte_100000"


def execute(client, *, attempts: int = 6, wait: float = 5, **statement) -> dict:
    """One Data API statement, waiting out a paused cluster's resume.

    With `min_acu: 0` the cluster pauses when idle, and the Data API refuses every
    statement with DatabaseResumingException until it is back (measured: under 25 s).
    Every caller goes through here, so none fails the first call after a pause.
    """
    for attempt in range(attempts):
        try:
            return client.execute_statement(**statement)
        except client.exceptions.DatabaseResumingException:
            if attempt == attempts - 1:
                raise
            time.sleep(wait)
    raise AssertionError("unreachable")


# The SQL below spells these as literals (no string-built SQL); demo() pins them together.
MAX_DEPTH = 3
MAX_PRECEDENTS = 3

SCHEMA_DDL = [
    "CREATE SCHEMA IF NOT EXISTS agent_knowledge",
    """
    CREATE TABLE IF NOT EXISTS agent_knowledge.precedent (
      case_id           text PRIMARY KEY,
      process_type      text NOT NULL,
      supplier_number   text,
      amount_band       text NOT NULL,
      disposition       text NOT NULL,
      tool_sequence     jsonb NOT NULL,
      sop_version       text NOT NULL,
      user_rating       smallint,
      resolved_at       timestamptz NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS precedent_lookup_idx
      ON agent_knowledge.precedent (process_type, supplier_number, resolved_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_knowledge.lesson (
      lesson_id     bigserial PRIMARY KEY,
      process_type  text NOT NULL,
      scope_vendor  text,
      lesson        text NOT NULL,
      authored_by   text NOT NULL,
      created_at    timestamptz NOT NULL DEFAULT now(),
      UNIQUE NULLS NOT DISTINCT (process_type, scope_vendor, lesson)
    )
    """,
    # The case whose thumbs-down a lesson came from; NULL for a loaded lesson.
    "ALTER TABLE agent_knowledge.lesson ADD COLUMN IF NOT EXISTS source_case text",
    """
    CREATE TABLE IF NOT EXISTS agent_knowledge.vendor (
      supplier_number text PRIMARY KEY,
      name            text,
      country         text
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_knowledge.vendor_edge (
      from_vendor  text NOT NULL REFERENCES agent_knowledge.vendor,
      to_vendor    text NOT NULL REFERENCES agent_knowledge.vendor,
      edge_type    text NOT NULL,
      evidence     text NOT NULL,
      PRIMARY KEY (from_vendor, to_vendor, edge_type)
    )
    """,
]

# Keyed, ordered, bounded. `user_rating` is the case's net rating (raters up minus
# raters down, written by the exemplar builder). coalesce(...) >= 0 drops a precedent
# its raters scored below zero: a disposition humans rejected must never come back as
# guidance. Then the higher rating, then the newer case.
PRECEDENT_SQL = """
SELECT case_id, disposition, tool_sequence::text AS tool_sequence,
       sop_version, user_rating
FROM agent_knowledge.precedent
WHERE process_type = :process_type
  AND (:supplier_number = '' OR supplier_number = :supplier_number)
  AND amount_band = :amount_band
  AND coalesce(user_rating, 0) >= 0
ORDER BY user_rating DESC NULLS LAST, resolved_at DESC
LIMIT 3
"""

LESSON_SQL = """
SELECT lesson_id, lesson, authored_by, scope_vendor
FROM agent_knowledge.lesson
WHERE process_type = :process_type
  AND (scope_vendor IS NULL OR scope_vendor = :supplier_number)
ORDER BY created_at DESC
LIMIT 5
"""

# `path` is returned as evidence: the agent cites the chain rather than
# asserting a relationship. The ANY(r.path) test is the cycle guard and must
# stay in the recursive term — as a post-filter it would not terminate.
VENDOR_RISK_SQL = """
WITH RECURSIVE ring(vendor, path, depth, edge_type) AS (
  SELECT :supplier_number, ARRAY[:supplier_number], 0, NULL::text
  UNION ALL
  SELECT e.to_vendor, r.path || e.to_vendor, r.depth + 1, e.edge_type
  FROM ring r
  JOIN agent_knowledge.vendor_edge e ON e.from_vendor = r.vendor
  WHERE r.depth < 3
    AND NOT e.to_vendor = ANY(r.path)
)
SELECT vendor, path, depth, edge_types
FROM (
  SELECT DISTINCT ON (vendor) vendor, path, depth, edge_type AS edge_types
  FROM ring
  WHERE depth > 0
  ORDER BY vendor, depth
) shortest
ORDER BY depth, vendor
"""


# The operator load (ops.py `knowledge-load`). Upserts, so a file loads twice
# without duplicating rows. An edge names vendors that may not be loaded yet, so
# both ends are inserted as bare vendors first (the foreign keys need them).
VENDOR_UPSERT_SQL = """
INSERT INTO agent_knowledge.vendor (supplier_number, name, country)
VALUES (:supplier_number, NULLIF(:name, ''), NULLIF(:country, ''))
ON CONFLICT (supplier_number) DO UPDATE
  SET name = coalesce(EXCLUDED.name, agent_knowledge.vendor.name),
      country = coalesce(EXCLUDED.country, agent_knowledge.vendor.country)
"""

EDGE_UPSERT_SQL = """
INSERT INTO agent_knowledge.vendor_edge (from_vendor, to_vendor, edge_type, evidence)
VALUES (:from_vendor, :to_vendor, :edge_type, :evidence)
ON CONFLICT (from_vendor, to_vendor, edge_type) DO UPDATE SET evidence = EXCLUDED.evidence
"""

LESSON_INSERT_SQL = """
INSERT INTO agent_knowledge.lesson (process_type, scope_vendor, lesson, authored_by)
VALUES (:process_type, NULLIF(:scope_vendor, ''), :lesson, :authored_by)
ON CONFLICT DO NOTHING
"""

# The exemplar builder's lessons: an author's comment on a case they rated down.
# Rewritten per case at every build, so a rating revised or withdrawn takes its
# lesson with it; a loaded lesson (source_case NULL) is never touched.
CASE_LESSONS_DELETE_SQL = """
DELETE FROM agent_knowledge.lesson
WHERE source_case IN (SELECT jsonb_array_elements_text(CAST(:case_ids AS jsonb)))
"""

CASE_LESSON_INSERT_SQL = """
INSERT INTO agent_knowledge.lesson
  (process_type, scope_vendor, lesson, authored_by, source_case)
VALUES (:process_type, :scope_vendor, :lesson, :authored_by, :case_id)
ON CONFLICT DO NOTHING
"""

_REQUIRED = {
    "vendors": ("supplier_number",),
    "vendor_edges": ("from_vendor", "to_vendor", "edge_type", "evidence"),
    "lessons": ("process_type", "lesson", "authored_by"),
}
_OPTIONAL = {
    "vendors": ("name", "country"),
    "vendor_edges": (),
    "lessons": ("scope_vendor",),
}


def load_statements(doc: dict) -> list[tuple[str, dict[str, str]]]:
    """A load file to (sql, params) pairs, in foreign-key order. Raises ValueError.

    The file is `{"vendors": [...], "vendor_edges": [...], "lessons": [...]}`,
    every key optional. Values must be strings; unknown keys are refused, so a
    misspelt column fails the load instead of loading as empty.
    """
    if not isinstance(doc, dict) or set(doc) - set(_REQUIRED):
        raise ValueError(f"expected an object with keys {sorted(_REQUIRED)}")
    rows: dict[str, list[dict[str, str]]] = {}
    for kind, required in _REQUIRED.items():
        allowed = set(required) | set(_OPTIONAL[kind])
        items = doc.get(kind, [])
        if not isinstance(items, list):
            raise ValueError(f"{kind} must be a list")  # noqa: TRY004 - one error type per bad file
        rows[kind] = []
        for i, row in enumerate(items):
            where = f"{kind}[{i}]"
            if not isinstance(row, dict) or set(row) - allowed:
                raise ValueError(f"{where}: allowed keys are {sorted(allowed)}")
            if not all(isinstance(v, str) for v in row.values()):
                raise ValueError(f"{where}: every value must be a string")
            missing = [k for k in required if not row.get(k, "").strip()]
            if missing:
                raise ValueError(f"{where}: missing {missing}")
            rows[kind].append({k: row.get(k, "") for k in sorted(allowed)})
    vendors = {r["supplier_number"]: r for r in rows["vendors"]}
    for e in rows["vendor_edges"]:
        if e["from_vendor"] == e["to_vendor"]:
            raise ValueError(f"edge {e['from_vendor']} -> itself")
        for end in (e["from_vendor"], e["to_vendor"]):
            vendors.setdefault(end, {"supplier_number": end, "name": "", "country": ""})
    return (
        [(VENDOR_UPSERT_SQL, v) for v in vendors.values()]
        + [(EDGE_UPSERT_SQL, e) for e in rows["vendor_edges"]]
        + [(LESSON_INSERT_SQL, x) for x in rows["lessons"]]
    )


def shape_precedent(records: list[dict]) -> list[dict]:
    """Data API rows → precedent evidence. tool_sequence arrives as jsonb::text."""
    shaped = []
    for row in records:
        raw = row.get("tool_sequence")
        try:
            tool_sequence = json.loads(raw) if isinstance(raw, str) else (raw or [])
        except json.JSONDecodeError:
            tool_sequence = []
        shaped.append(
            {
                "case_id": row.get("case_id"),
                "disposition": row.get("disposition"),
                "tool_sequence": tool_sequence,
                "sop_version": row.get("sop_version"),
                "user_rating": row.get("user_rating"),
            }
        )
    return shaped


def shape_vendor_risk(records: list[dict]) -> list[dict]:
    return [
        {
            "vendor": row.get("vendor"),
            "path": row.get("path") or [],
            "depth": row.get("depth"),
            "edge_types": row.get("edge_types"),
        }
        for row in records
    ]


def traverse(
    edges: list[tuple[str, str, str]], start: str, max_depth: int = MAX_DEPTH
) -> list[dict]:
    """Pure reference for VENDOR_RISK_SQL — breadth-first, cycle-guarded.

    Exists so the traversal semantics have a runnable check without a database.
    Keep in step with VENDOR_RISK_SQL.
    """
    adjacency: dict[str, list[tuple[str, str]]] = {}
    for src, dst, edge_type in edges:
        adjacency.setdefault(src, []).append((dst, edge_type))

    out: list[dict] = []
    seen: set[str] = {start}
    frontier = [(start, [start], 0)]
    while frontier:
        vendor, path, depth = frontier.pop(0)
        if depth >= max_depth:
            continue
        for neighbour, edge_type in adjacency.get(vendor, []):
            if neighbour in seen:  # cycle guard
                continue
            seen.add(neighbour)
            next_path = path + [neighbour]
            out.append(
                {
                    "vendor": neighbour,
                    "path": next_path,
                    "depth": depth + 1,
                    "edge_types": edge_type,
                }
            )
            frontier.append((neighbour, next_path, depth + 1))
    return out


def demo() -> None:
    """Self-check for the non-trivial logic: cycle guard, depth bound, banding."""
    assert traverse([("A", "B", "e"), ("B", "A", "e")], "A") == [
        {"vendor": "B", "path": ["A", "B"], "depth": 1, "edge_types": "e"}
    ], "cycle guard failed"

    chain = [("A", "B", "e"), ("B", "C", "e"), ("C", "D", "e"), ("D", "E", "e")]
    assert {r["vendor"] for r in traverse(chain, "A", max_depth=3)} == {
        "B",
        "C",
        "D",
    }, "depth bound failed"

    assert traverse(chain, "Z") == [], "unconnected vendor should return nothing"
    assert amount_band(-5000) == amount_band(5000), "banding must ignore sign"
    assert amount_band(99) != amount_band(101), "banding must separate at 100"

    assert "= ANY(r.path)" in VENDOR_RISK_SQL, "SQL lost its cycle guard"
    assert f"LIMIT {MAX_PRECEDENTS}" in PRECEDENT_SQL, "SQL lost its row bound"
    assert f"r.depth < {MAX_DEPTH}" in VENDOR_RISK_SQL, "SQL lost its depth bound"
    assert "DISTINCT ON (vendor)" in VENDOR_RISK_SQL, "SQL lost its per-vendor dedup"

    print("queries.demo: ok")


if __name__ == "__main__":
    demo()
