# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Everything a run writes back to the case item in DynamoDB.

Three writers touch a case, and they are here together because they have to agree
with each other:

* ``_live_run_writer`` publishes the in-flight snapshot a reloading browser reads.
* ``_save_trace_to_ddb`` appends the finished trace — and, in the same statement,
  REMOVEs that snapshot. It is the only thing that removes it.
* ``_flag_case_for_review`` parks a case a human has to look at.

All three resolve the table through ``_cases_table``, so "is this case writable?"
is answered once rather than three times.
"""

import json
import os
from datetime import UTC

import boto3

from . import persist_mask, registry
from .case_key import CaseKeyError, to_case_key
from .evidence import MAX_TRACES, cap_traces, merge_evidence
from .live_run import LiveRunWriter
from .persist_mask import mask_record


def _cases_table(case_id: str):
    """`(table, case_key)` for the cases table, or None when the case is unaddressable.

    The three writers on a case item — the live snapshot, the trace append, and the
    review flag — have to agree on when a case is writable, because `live_run` is only
    ever removed by the trace append. One resolver makes that agreement structural: a
    guard added here cannot reach one writer and miss another, which hand-copied guard
    sets could not promise.

    None means no CASES_TABLE, or a case_id the key codec rejects — what every caller
    already treats as nothing-to-write. Construction failures propagate instead, so
    each caller keeps reporting them under its own log tag.
    """
    table_name = os.environ.get("CASES_TABLE")
    if not table_name or not case_id:
        return None
    try:
        case_key = to_case_key(case_id)
    except CaseKeyError:
        return None
    region = os.environ.get(
        "AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    )
    return boto3.resource("dynamodb", region_name=region).Table(table_name), case_key


def _live_run_writer(
    case_id: str, run_id: str, trigger: str, prompt: str
) -> LiveRunWriter | None:
    """A LiveRunWriter for this run, or None when there is nowhere safe to write.

    Resolves the table through _cases_table, the same call _save_trace_to_ddb makes, so
    `live_run` can only ever exist on a case whose trace write will later REMOVE it.
    """
    try:
        resolved = _cases_table(case_id)
    except Exception as error:  # noqa: BLE001
        print(f"[LIVE] no live_run writer for {case_id}: {error}")
        return None
    if resolved is None:
        return None
    table, case_key = resolved
    mask = None
    if persist_mask.enabled():
        # One cache per run, so each write sends only text the last ones did not.
        seen: dict = {}

        def mask(snapshot: dict) -> dict:
            return mask_record(snapshot, cache=seen)

    return LiveRunWriter(
        table=table,
        case_key=case_key,
        run_id=run_id,
        trigger=trigger,
        prompt=prompt,
        mask=mask,
    )


def identified_exception_type(pack_id: str, process_types: list[str]) -> str | None:
    """The exception type a run identified, from the SOPs it was given, or None.

    `process_types` are the routed SOP's type, then each one `load_sop` served, in
    order. The last that names an exception SOP of the case's own pack wins
    (`registry.exception_type`): a triage or shared SOP, an alias of triage, and a type
    the pack does not map are never recorded. Never a value the model wrote: the model
    picks which SOP to load, and the pack decides whether that is an exception type.
    """
    try:
        document = registry.authored(registry.packs().get(pack_id) or {})
    except Exception as error:  # noqa: BLE001 - an unread registry records nothing
        print(f"[CASE] exception type not decided for pack {pack_id}: {error}")
        return None
    found = None
    for process_type in process_types:
        found = registry.exception_type(document, process_type) or found
    return found


def trace_append_expression(identified: bool) -> str:
    """The UpdateExpression that appends a run's trace, with the case's exception type
    when the run `identified` one.

    One statement for all of it: the trace, the cost counters, the exception type it
    evidences and the REMOVE of the live copy, so live to stored is one atomic swap with
    no window where a reader sees both copies, or neither.
    """
    return (
        "SET agent_traces = list_append(if_not_exists(agent_traces, :empty), :trace)"
        + (", exception_type = :exception_type" if identified else "")
        + " ADD cost_summary.total_cost_usd :cost,"
        " cost_summary.total_input_tokens :inp,"
        " cost_summary.total_output_tokens :out,"
        " cost_summary.total_cache_read_tokens :cache,"
        " cost_summary.invocation_count :one"
        " REMOVE live_run"
    )


def _save_trace_to_ddb(
    case_id: str,
    trigger: str,
    prompt: str,
    segments: list,
    outcome: str,
    metrics: dict | None = None,
    audit_context: dict | None = None,
    evidence_by_id: dict | None = None,
    sop_version: str | None = None,
    sop_key: str | None = None,
    sop_version_id: str | None = None,
    exception_type: str | None = None,
    intent: str | None = None,
):
    """Save agent trace directly to DynamoDB cases table.

    Args:
        metrics: Optional dict with latency_ms, input_tokens, output_tokens,
                 cache_read_tokens, cache_write_tokens, estimated_cost_usd.
                 Added to the trace record when available.
        audit_context: Optional {initiator, correlation_id} persisted into the
                 trace so a SAP write is user-attributable from the case (T12).
        evidence_by_id: Evidence keyed by tool_call_id, from EvidenceHook. Merged
                 onto matching segments; absent keys leave a segment in its
                 pre-migration shape.
        sop_version: Version that SOP declared, from resolve_skill. Recorded per run
                 so a precedent citing this case names the authority it followed,
                 not whichever revision is current when the precedent is written.
        sop_key: The document that version was read from, also from resolve_skill.
                 A version alone does not say *which* SOP, so a reader of the case
                 could not open the text the run followed. Empty when the SOP did
                 not load, which is why this is not derived from `sop_version`.
        sop_version_id: The S3 VersionId of those bytes. The header version is typed
                 by the author; this one names exactly what the run was given, in a
                 bucket that denies version deletes.
        exception_type: What this run identified (`identified_exception_type`). Set on
                 the trace and, in the same statement, on the case; None leaves the
                 case's value as an earlier run recorded it.
        intent: `ask` on a conversation turn, which could change nothing; None on a run
                 that could write.
    """
    import uuid
    from datetime import datetime
    from decimal import Decimal

    try:
        resolved = _cases_table(case_id)
    except Exception as e:  # noqa: BLE001
        # Same tag and swallow as the write below: resolving the table and writing to it
        # are one failure to a reader of the logs.
        print(f"[TRACE] Failed to save for {case_id}: {e}")
        return
    if resolved is None:
        return
    table, case_key = resolved
    segments = merge_evidence(segments, evidence_by_id)
    trace = {
        "trace_id": str(uuid.uuid4()),
        "timestamp": datetime.now(UTC).isoformat(),
        "trigger": trigger,
        "prompt": prompt,
        "outcome": outcome,
        "segments": segments,
    }
    # The model read these as they were; the record keeps them masked.
    mask_record(trace)
    if sop_version:
        trace["sop_version"] = sop_version
    if sop_key:
        trace["sop_key"] = sop_key
    if sop_version_id:
        trace["sop_version_id"] = sop_version_id
    if exception_type:
        trace["exception_type"] = exception_type
    if intent:
        trace["intent"] = intent
    # T12: persist initiator so writes are user-attributable (additive)
    if audit_context:
        trace["initiator"] = audit_context.get("initiator")
        trace["correlation_id"] = audit_context.get("correlation_id")
    # Enrich with metrics when available (additive — old traces without these fields still work)
    if metrics:
        trace["latency_ms"] = Decimal(str(round(metrics.get("latency_ms", 0), 1)))
        trace["input_tokens"] = metrics.get("input_tokens", 0)
        trace["output_tokens"] = metrics.get("output_tokens", 0)
        trace["cache_read_tokens"] = metrics.get("cache_read_tokens", 0)
        trace["cache_write_tokens"] = metrics.get("cache_write_tokens", 0)
        trace["estimated_cost_usd"] = Decimal(
            str(round(metrics.get("estimated_cost_usd", 0), 6))
        )
        if metrics.get("verifier_calls"):
            # Its own list, not folded into the tokens above: the benchmark prices those
            # at the agent's model, and the verifier's are priced at its own.
            trace["verifier_calls"] = json.loads(
                json.dumps(metrics["verifier_calls"], default=str), parse_float=Decimal
            )
    # Per-case cost accumulator — sums across all invocations (initial + escalation resumptions)
    cost_delta = (
        Decimal(str(round(metrics.get("estimated_cost_usd", 0), 6)))
        if metrics
        else Decimal(0)
    )
    inp_delta = metrics.get("input_tokens", 0) if metrics else 0
    out_delta = metrics.get("output_tokens", 0) if metrics else 0
    cache_delta = metrics.get("cache_read_tokens", 0) if metrics else 0

    try:
        # Ensure cost_summary map exists (no-op if already present)
        try:
            table.update_item(
                Key=case_key,
                UpdateExpression="SET cost_summary = if_not_exists(cost_summary, :init)",
                ExpressionAttributeValues={
                    ":init": {
                        "total_cost_usd": 0,
                        "total_input_tokens": 0,
                        "total_output_tokens": 0,
                        "total_cache_read_tokens": 0,
                        "invocation_count": 0,
                    }
                },
                ConditionExpression="attribute_exists(case_id)",
            )
        except Exception:  # noqa: BLE001, S110
            pass  # nosec B110 — best-effort init, ADD below will fail visibly if needed

        # Append trace + increment cost counters. ReturnValues gives back the new
        # list, which is the only way to trim: list_append has no drop-oldest form.
        # The exception type lands with the trace that evidences it.
        identified = {":exception_type": exception_type} if exception_type else {}
        response = table.update_item(
            Key=case_key,
            UpdateExpression=trace_append_expression(bool(identified)),
            ExpressionAttributeValues={
                ":empty": [],
                ":trace": [trace],
                ":cost": cost_delta,
                ":inp": inp_delta,
                ":out": out_delta,
                ":cache": cache_delta,
                ":one": 1,
                **identified,
            },
            ConditionExpression="attribute_exists(case_id)",
            ReturnValues="UPDATED_NEW",
        )
        print(
            f"[TRACE] Saved {trace['trace_id']} for {case_id} ({len(segments)} segments, outcome={outcome}, cost=${cost_delta:.4f})"
        )

        # Read-modify-write, safe because SQS FIFO serialises invocations
        # per case_id (MessageGroupId is the case id); gate on a version attribute
        # if a second writer is ever added.
        kept, dropped = cap_traces(
            response.get("Attributes", {}).get("agent_traces"), MAX_TRACES
        )
        if dropped:
            table.update_item(
                Key=case_key,
                UpdateExpression="SET agent_traces = :kept ADD traces_dropped :dropped",
                ExpressionAttributeValues={":kept": kept, ":dropped": dropped},
                ConditionExpression="attribute_exists(case_id)",
            )
            print(
                f"[TRACE] Capped {case_id} at {MAX_TRACES} traces ({dropped} dropped)"
            )
    except Exception as e:  # noqa: BLE001
        print(f"[TRACE] Failed to save for {case_id}: {e}")


def _flag_case_for_review(case_id: str, reason: str):
    """Move a case to manual_review_required with a reason. Used when the agent
    cannot safely run (e.g. SOP failed to load) — mirrors the DLQ-exhaustion path
    in agent_invoker so the case doesn't sit in 'processing' and a human is cued.
    `status` is a DynamoDB reserved word, so it's aliased."""
    from datetime import datetime

    try:
        resolved = _cases_table(case_id)
        if resolved is None:
            return
        table, case_key = resolved
        table.update_item(
            Key=case_key,
            UpdateExpression="SET #s = :s, status_reason = :r, updated_at = :t",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": "manual_review_required",
                ":r": reason,
                ":t": datetime.now(UTC).isoformat(),
            },
            ConditionExpression="attribute_exists(case_id)",
        )
        print(f"[CASE] Flagged {case_id} for manual review: {reason}")
    except Exception as e:  # noqa: BLE001
        print(f"[CASE] Failed to flag {case_id} for review: {e}")
