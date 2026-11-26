# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Exemplar Builder — generates condensed resolution examples from successful cases.

Runs daily via EventBridge. For each process_type with completed cases:
  1. Queries DDB status-index for status=complete cases from last 30 days
  2. Ranks them (`_pick_best`): rated-down cases out, then net rating, trace length, recency
  3. Picks the top N
  4. Uses Amazon Bedrock to condense agent traces into clean step sequences
  5. Writes {pack_id}/{process_type}_exemplars.md to EXEMPLAR_BUCKET, which no
     knowledge base ingests — see exemplar_s3_key

Ratings are per rater (`ratings.<sub>` on the case, written by `PUT /cases/{id}/rating`);
a case's score is its positive raters minus its negative ones. A case scored below zero
is never an exemplar, and its precedent row carries the negative score so `get_precedent`
drops it too.

The skill router loads these at agent start time — zero runtime cost.

With agent knowledge on, the same pass writes the Aurora rows `get_precedent` reads: a
precedent per completed case, and a lesson per comment a pack author left on a
thumbs-down (`lessons_from_ratings`).

Everything above is pack-agnostic: a trace is a trace. The one AP-specific part
is the precedent table, which keys on a supplier — see _has_precedent_key.
"""

import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import boto3
import registry
from boto3.dynamodb.conditions import Key
from ddb_paginate import paginate

# The precedent reader (`get_precedent`) keys on this same function; a copy here that
# drifted would silently return no precedents. Bundled beside this file at synth.
from queries import (
    CASE_LESSON_INSERT_SQL,
    CASE_LESSONS_DELETE_SQL,
    amount_band,
    execute,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")
s3 = boto3.client("s3")
bedrock = boto3.client(
    "bedrock-runtime", region_name=os.environ.get("AWS_REGION", "us-east-1")
)

TABLE_NAME = os.environ["CASES_TABLE"]
# Not the SOP bucket: everything there is ingested by the SOPs knowledge base, and
# these condensed traces must never come back as `search_sap_sops` results.
EXEMPLAR_BUCKET = os.environ["EXEMPLAR_BUCKET"]
MODEL_ID = os.environ["MODEL_ID"]
MAX_EXEMPLARS = 3
LOOKBACK_DAYS = 30

# The knowledge stack publishes the Aurora cluster here when agent_knowledge is on,
# and grants this function's role the precedent write. Absent means off.
AGENT_KNOWLEDGE_PARAMETER_PREFIX = os.environ.get(
    "AGENT_KNOWLEDGE_PARAMETER_PREFIX", ""
)

PRECEDENT_UPSERT = """
INSERT INTO agent_knowledge.precedent
  (case_id, process_type, supplier_number, amount_band, disposition,
   tool_sequence, sop_version, user_rating, resolved_at)
VALUES
  (:case_id, :process_type, :supplier_number, :amount_band, :disposition,
   CAST(:tool_sequence AS jsonb), :sop_version, :user_rating,
   CAST(:resolved_at AS timestamptz))
ON CONFLICT (case_id) DO UPDATE SET
  disposition   = EXCLUDED.disposition,
  tool_sequence = EXCLUDED.tool_sequence,
  sop_version   = EXCLUDED.sop_version,
  user_rating   = EXCLUDED.user_rating,
  resolved_at   = EXCLUDED.resolved_at
"""

CONDENSE_PROMPT = """Condense this agent trace into a numbered step sequence.
Each step should be ONE line: the tool/action name and what it accomplished.
Strip PO numbers, dollar amounts, and timestamps — keep only the pattern.
Output ONLY the numbered steps, nothing else.

Agent trace:
{history}"""


def _process_type_pack_map() -> dict[str, str]:
    """process_type → pack_id, from the published registry."""
    return registry.process_type_pack_map()


def exemplar_s3_key(process_type: str) -> str | None:
    """Must match skill_router.exemplar_s3_key — see test_exemplar_key_parity.

    None when no pack claims the process_type: the reader derives the key from
    the owning pack's id, so a guessed one could never be read back.
    """
    pack_id = _process_type_pack_map().get(process_type)
    return f"{pack_id}/{process_type}_exemplars.md" if pack_id else None


def _query_successful_cases() -> list[dict]:
    """Query all completed cases from the last N days."""
    table = dynamodb.Table(TABLE_NAME)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).isoformat()

    # Every page: a filtered query pages on what it *scanned*, so a lookback window
    # with more than 1 MB of completed cases behind it returns a short first page and
    # the corpus is built from a fraction of what completed.
    pages = paginate(
        table.query,
        IndexName="status-index",
        KeyConditionExpression=Key("status").eq("complete"),
        FilterExpression="updated_at >= :cutoff",
        ExpressionAttributeValues={":cutoff": cutoff},
    )
    return list(pages)


def _group_by_process_type(cases: list[dict]) -> dict[str, list[dict]]:
    """Group cases by process_type, filtering to those with agent traces."""
    grouped = defaultdict(list)
    for case in cases:
        pt = case.get("process_type")
        if pt and case.get("agent_traces"):
            grouped[pt].append(case)
    return grouped


def rating_score(case: dict) -> int:
    """Positive raters minus negative raters; 0 for an unrated case."""
    ratings = case.get("ratings") or {}
    return sum(
        {"positive": 1, "negative": -1}.get((r or {}).get("rating"), 0)
        for r in ratings.values()
    )


def _segments(case: dict) -> int:
    return sum(len(t.get("segments", [])) for t in case.get("agent_traces", []))


def _pick_best(cases: list[dict], n: int) -> list[dict]:
    """The N best exemplar candidates: higher net rating, then the shorter trace, then
    the newer case. A case its raters scored below zero is never one."""
    newest_first = sorted(
        (c for c in cases if rating_score(c) >= 0),
        key=lambda c: str(c.get("updated_at") or ""),
        reverse=True,
    )
    # Stable, so recency survives as the tie-break.
    return sorted(newest_first, key=lambda c: (-rating_score(c), _segments(c)))[:n]


def tool_sequence_from_traces(traces: list[dict]) -> list[str]:
    """Ordered tool names from a case's traces.

    Mechanical, not model-derived: the same case must always yield the same
    precedent row, which is what makes a precedent citation defensible.
    """
    return [
        seg["tool_name"]
        for trace in traces
        for seg in trace.get("segments", [])
        if seg.get("type") == "tool" and seg.get("tool_name")
    ]


def sop_version_from_traces(traces: list[dict]) -> str:
    """The SOP version the case's last invocation followed.

    Read off the trace, not the SOP as it stands now: revising a SOP must not
    restate what an already-resolved case was decided under. The last trace wins
    because that is the run that reached the disposition being recorded.

    "unversioned" where no trace carries one — traces stored before this field
    existed, the discovery path, and SOPs with no Version header all land here.
    The column is NOT NULL because a precedent citation without a SOP version is
    not defensible.
    """
    for trace in reversed(traces or []):
        version = (trace or {}).get("sop_version")
        if version:
            return str(version)
    return "unversioned"


def disposition_from_traces(traces: list[dict]) -> str:
    """A stand-in disposition, read off the case's last SAP write.

    Only a SOP that asks for one produces a real disposition — the agent records it
    on the case and `_write_precedent` prefers it. Where no SOP asked, the fallback
    used to be the case's own `status`, which `_query_successful_cases` filters to
    "complete": every row stated the one thing the query had already guaranteed.
    What the run actually did to SAP is at least a fact about the case, and reading
    it off the trace keeps the row reproducible, like the two functions above.

    The last write wins, for the same reason the last trace's SOP version does. A
    write SAP refused is not a disposition: the MCP wrapper reports a rejection as a
    successful tool call carrying `success: false`, so `sap_result.accepted` is the
    only thing that separates it from a write that landed.

    "no_sap_write" where the run wrote nothing at all — true of a case whose
    disposition was to reject, and distinguishable from one whose SOP simply named
    no disposition.
    """
    for trace in reversed(traces or []):
        for seg in reversed((trace or {}).get("segments") or []):
            evidence = seg.get("evidence") or {}
            op = evidence.get("op")  # sap_write only — no other kind carries one
            if not op or seg.get("status") == "error":
                continue
            if (evidence.get("sap_result") or {}).get("accepted") is False:
                continue
            entity = (evidence.get("source") or {}).get("entity")
            return f"{op}:{entity}" if entity else str(op)
    return "no_sap_write"


def _attribute(case: dict, name: str) -> object:
    """One of the case's own attributes, by the name its `field_map` chose."""
    return (case.get("attributes") or {}).get(name)


_agent_knowledge: dict | None = None


def _agent_knowledge_target() -> dict:
    """`{cluster, secret, database}` from SSM, or {} when agent knowledge is off."""
    global _agent_knowledge
    if _agent_knowledge is None:
        _agent_knowledge = {}
        if AGENT_KNOWLEDGE_PARAMETER_PREFIX:
            ssm = boto3.client("ssm")
            try:
                _agent_knowledge = {
                    k: ssm.get_parameter(
                        Name=f"{AGENT_KNOWLEDGE_PARAMETER_PREFIX}agent-knowledge-{p}"
                    )["Parameter"]["Value"]
                    for k, p in (
                        ("cluster", "cluster-arn"),
                        ("secret", "secret-arn"),
                        ("database", "database"),
                    )
                }
            except ssm.exceptions.ParameterNotFound:
                pass
    return _agent_knowledge


def _agent_knowledge_enabled() -> bool:
    return bool(_agent_knowledge_target())


def _has_precedent_key(case: dict) -> bool:
    """Whether this case can be recorded as a precedent at all.

    The precedent table is AP's, not the platform's: both of its read paths
    (`get_precedent` and `check_vendor_risk` in agent_knowledge/queries.py) key on
    `supplier_number`, and only a pack whose `field_map` produces one can fill it.
    A row written with an empty supplier would be matched by every later lookup for
    an empty supplier, so a pack that has no supplier records nothing here — it
    simply does not list those tools in its `gateway_tools`. Generalising the table
    would mean inventing a supplier for packs that have no such concept.
    """
    return bool(str(_attribute(case, "supplier_number") or "").strip())


def _write_precedent(case: dict, process_type: str) -> None:
    """Upsert one precedent row. Raises — the caller decides whether to continue."""
    rds_data = boto3.client("rds-data")
    traces = case.get("agent_traces", [])
    # Lowercased and joined because the column is shown and compared verbatim, so
    # "Rejected Duplicate" would read as a second outcome from rejected_duplicate.
    recorded = "_".join(str(case.get("disposition") or "").lower().split())
    params = {
        "case_id": str(case.get("case_id") or ""),
        "process_type": process_type,
        "supplier_number": str(_attribute(case, "supplier_number") or ""),
        "amount_band": amount_band(_attribute(case, "amount")),
        "disposition": recorded or disposition_from_traces(traces),
        "tool_sequence": json.dumps(tool_sequence_from_traces(traces)),
        "sop_version": sop_version_from_traces(traces),
        # The case's own time, not the build's: `get_precedent` breaks rating ties on
        # it, and a rating never moves `updated_at`.
        "resolved_at": str(case.get("updated_at") or ""),
    }
    target = _agent_knowledge_target()
    execute(
        rds_data,
        resourceArn=target["cluster"],
        secretArn=target["secret"],
        database=target["database"],
        sql=PRECEDENT_UPSERT,
        parameters=[
            *({"name": k, "value": {"stringValue": v}} for k, v in params.items()),
            {"name": "user_rating", "value": {"longValue": rating_score(case)}},
        ],
    )


def lessons_from_ratings(case: dict) -> list[dict[str, str]]:
    """The lessons one case teaches: each pack author's comment on their thumbs-down.

    Only a rater the rating route marked `author` counts. A lesson is text every later
    run for this supplier reads, so it takes the groups that may write a SOP, not just
    the right to see the case. A positive rating's comment is praise, not a lesson.
    """
    return [
        {"lesson": r["comment"].strip(), "authored_by": str(r.get("name") or sub)}
        for sub, r in sorted((case.get("ratings") or {}).items())
        if (r or {}).get("rating") == "negative"
        and r.get("author") is True
        and isinstance(r.get("comment"), str)
        and r["comment"].strip()
    ]


def _write_lessons(cases: list[dict], process_type: str) -> int:
    """Replace these cases' lessons with what their ratings say now. Raises.

    Delete first, so a thumbs-down withdrawn, or its comment cleared, stops teaching
    at the next build, the way a rated-down exemplar is removed.
    """
    rds_data = boto3.client("rds-data")
    target = _agent_knowledge_target()

    def run(sql: str, params: dict[str, str]) -> None:
        execute(
            rds_data,
            resourceArn=target["cluster"],
            secretArn=target["secret"],
            database=target["database"],
            sql=sql,
            parameters=[
                {"name": k, "value": {"stringValue": v}} for k, v in params.items()
            ],
        )

    run(
        CASE_LESSONS_DELETE_SQL,
        {"case_ids": json.dumps([str(c.get("case_id") or "") for c in cases])},
    )
    written = 0
    for case in cases:
        for lesson in lessons_from_ratings(case):
            run(
                CASE_LESSON_INSERT_SQL,
                {
                    "process_type": process_type,
                    # Scoped to the supplier: one reviewer's note on one vendor's case
                    # is not a rule for every case of the process type.
                    "scope_vendor": str(_attribute(case, "supplier_number")).strip(),
                    "case_id": str(case.get("case_id") or ""),
                    **lesson,
                },
            )
            written += 1
    return written


def _condense_trace(traces: list[dict]) -> str:
    """Use Amazon Bedrock to condense agent traces into clean steps."""
    lines = []
    for trace in traces:
        for seg in trace.get("segments", []):
            if seg.get("type") == "tool" and seg.get("tool_name"):
                result_preview = (seg.get("tool_result") or "")[:200]
                lines.append(f"- {seg['tool_name']}: {result_preview}")
            elif seg.get("type") == "text" and seg.get("content"):
                lines.append(f"- thought: {seg['content'][:200]}")

    history_text = "\n".join(lines)

    resp = bedrock.invoke_model(
        modelId=MODEL_ID,
        contentType="application/json",
        accept="application/json",
        body=json.dumps(
            {
                "anthropic_version": "bedrock-2023-05-31",
                # No `temperature` (Sonnet 5.5 rejects it) and no `thinking`:
                # Sonnet 5.5 returns a 400 on `{"type": "disabled"}`, so the call
                # runs the model's default adaptive thinking. Thinking tokens count
                # against max_tokens, so the ceiling is set well above the short
                # step list wanted; at 500, thinking could crowd the list out.
                "max_tokens": 4000,
                "messages": [
                    {
                        "role": "user",
                        "content": CONDENSE_PROMPT.format(history=history_text),
                    }
                ],
            }
        ),
    )
    result = json.loads(resp["body"].read())
    # A thinking block can precede the text, so content[0] is not always it.
    return next(b["text"] for b in result["content"] if b.get("type") == "text").strip()


def _build_exemplar_doc(process_type: str, condensed: list[str]) -> str:
    """Build the exemplar markdown document."""
    lines = [f"## Recent Successful Resolutions: {process_type}\n"]
    for i, steps in enumerate(condensed, 1):
        lines.append(f"### Example {i}\n{steps}\n")
    lines.append(
        f"_Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d')} "
        f"from {len(condensed)} successful case(s)._\n"
    )
    return "\n".join(lines)


def handler(event: dict, context: object) -> dict:
    """Lambda handler — generates exemplar documents from successful cases."""
    logger.info("Exemplar builder started")

    cases = _query_successful_cases()
    logger.info(f"Found {len(cases)} completed cases in last {LOOKBACK_DAYS} days")

    if not cases:
        logger.info("No completed cases — nothing to generate")
        return {"status": "no_cases"}

    grouped = _group_by_process_type(cases)
    generated = []
    failures: list[str] = []
    picked: dict[str, list[str]] = {}

    for process_type, pt_cases in grouped.items():
        best = _pick_best(pt_cases, MAX_EXEMPLARS)
        picked[process_type] = [str(c.get("case_id")) for c in best]
        scores = [(c.get("case_id"), rating_score(c), _segments(c)) for c in best]
        logger.info(
            f"{process_type}: {len(pt_cases)} candidates, picked {len(best)} "
            f"(case, rating, segments) — {scores}"
        )

        # The precedent store supersedes the condensed exemplar where it applies,
        # which is why this branch ends in `continue`. It only applies to a pack that
        # has a supplier — see _has_precedent_key — so a pack with none falls through
        # to the exemplar path rather than producing nothing at all.
        if _agent_knowledge_enabled():
            # Every candidate, not just `best`: a row is the only place a later
            # thumbs-down reaches `get_precedent`, which ranks and filters by rating.
            # ponytail: one Data API call per case in the window; batch_execute_statement
            # if the window grows past a few hundred cases.
            eligible = [c for c in pt_cases if _has_precedent_key(c)]
            if eligible:
                written = 0
                for case in eligible:
                    try:
                        _write_precedent(case, process_type)
                        written += 1
                    except Exception as e:  # noqa: BLE001 - one case's attrition, counted below
                        logger.warning(
                            f"Precedent write failed for {case.get('case_id')}: {e}"
                        )
                        failures.append(
                            f"{process_type} precedent: {type(e).__name__}: {e}"
                        )
                logger.info(
                    f"{process_type}: wrote {written} of {len(eligible)} precedent row(s)"
                )
                try:
                    lessons = _write_lessons(eligible, process_type)
                    logger.info(f"{process_type}: wrote {lessons} lesson(s)")
                except Exception as e:  # noqa: BLE001 - the precedents above still stand
                    logger.warning(f"Lesson write failed for {process_type}: {e}")
                    failures.append(f"{process_type} lessons: {type(e).__name__}: {e}")
                if written:
                    generated.append(process_type)
                continue
            logger.info(
                f"{process_type}: no case carries a supplier, so the precedent table "
                f"has no key for this pack — condensing an exemplar instead"
            )

        if not best:
            # Every candidate was rated down: yesterday's document may still teach one.
            key = exemplar_s3_key(process_type)
            if key:
                s3.delete_object(Bucket=EXEMPLAR_BUCKET, Key=key)
                logger.info(f"{process_type}: every case rated down; removed {key}")
            continue

        condensed = []
        for case in best:
            try:
                steps = _condense_trace(case["agent_traces"])
                condensed.append(steps)
            except Exception as e:  # noqa: BLE001 - one case's attrition, counted below
                logger.warning(f"Failed to condense case {case.get('case_id')}: {e}")
                failures.append(f"{process_type} condense: {type(e).__name__}: {e}")

        if condensed:
            key = exemplar_s3_key(process_type)
            if not key:
                logger.warning(
                    f"No skill owns process_type {process_type!r} — skipping "
                    f"exemplar write; nothing would ever read it back"
                )
                continue
            doc = _build_exemplar_doc(process_type, condensed)
            s3.put_object(Bucket=EXEMPLAR_BUCKET, Key=key, Body=doc.encode())
            logger.info(f"Wrote {key} to {EXEMPLAR_BUCKET}")
            generated.append(process_type)

    # A run that found candidates and produced nothing is a failure, and until now it
    # returned `{"status": "ok", "generated": []}` — a scheduled invocation, so nobody
    # reads that value and every per-case `except` had already downgraded the cause to a
    # warning. The builder was denied `bedrock:InvokeModel` for months and reported
    # success on every run. Raising is the whole signal: `exemplar-builder` Errors is on
    # the dashboard's Lambda Errors graph and inside the `-lambda-errors` alarm summed
    # across the four pipeline functions, so this needs no alarm of its own. The schedule
    # retries twice before giving up — which is also that alarm's threshold arithmetic:
    # one failed run here is three Errors datapoints, not one.
    #
    # Guarded on `not generated` rather than on `failures` being non-empty: losing one
    # case out of five is attrition worth a warning and no more, and only a run that
    # accomplished nothing at all is worth waking someone for. And only when something
    # was picked: a process type whose every case was rated down has nothing to teach.
    if not generated and any(picked.values()):
        raise RuntimeError(
            f"exemplar builder produced nothing from {len(cases)} case(s) across "
            f"{len(grouped)} process type(s): "
            + ("; ".join(failures) if failures else "no case yielded a writable output")
        )

    if failures:
        logger.warning(
            f"Produced {len(generated)} output(s) with {len(failures)} failure(s): "
            f"{'; '.join(failures)}"
        )

    return {"status": "ok", "generated": generated, "picked": picked}
