# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Cases API: the case list and detail, a rating, an operator's reply, an enqueue.

  GET  /cases                      one page of a view of the cases the caller may see
                                   (?view= ?q= ?exception_type= ?pack_id= ?sort= ?order=
                                   ?limit= ?next_token=; `case_query.py`)
  GET  /cases?key=<pack_id>:<json> the case with this natural key
  GET  /cases/counts               how many cases per view, pack and status
                                   (?q= ?exception_type= ?pack_id=), and with
                                   ?by=exception_type each view by exception type
  GET  /cases/summary              what closed since ?since=, per outcome, currency
                                   and day (?pack_id=)
  GET  /cases/{case_id}            one case, whole
  PUT  /cases/{case_id}/rating     rate the resolution (one rating per rater)
  POST /cases/{case_id}/reply      an operator's reply or decision, for the agent
  POST /cases/enqueue              queue a case for the agent

Every route is floored by the caller's pack entitlement (`entitlement.py`). `?pack_id=`,
`?view=` and `?key=` narrow a result inside that floor; they never widen it.

The list is a page, not the table, and each item is cut to what a list shows: the
latest trace (the briefing reads its proposed write and citation) and the live run's
header. The view, search, filter and order are applied here, over the
`pack-status-updated-index`, which projects only what they read, so the console never
loads every case to sort it. `GET /cases/{id}` still returns a case whole.
"""

import json
import logging
import os
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import boto3
import case_query
import decision_options
import emergency_stop
import registry
from api_http import HttpError, field, json_body, respond, route
from case_key import CaseKeyError, derive_case_id, to_case_key
from case_status import DECIDER_VIEWS, PARKED_ON_PERSON_STATUSES, VIEWS
from contracts import CASE_STATUSES
from decisions import (
    ALREADY_APPROVED,
    NOT_AN_APPROVER,
    AlreadyApproved,
    Settled,
    authorize,
    awaiting_second_approver,
    option_decision,
    record_decision,
)
from entitlement import (
    CASE_FORBIDDEN,
    CASE_MISSING,
    CASE_NOT_A_CASE_WORKER,
    CallerEntitlement,
    caller_identity,
    case_pack_verdict,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")
TABLE_NAME = os.environ["TABLE_NAME"]
table = dynamodb.Table(TABLE_NAME)
sqs = boto3.client("sqs")
#: Empty until the agent queue is deployed; the reply and enqueue routes then 503.
AGENT_QUEUE_URL = os.environ.get("AGENT_QUEUE_URL", "")

#: Rejected with 413 rather than truncated: a rationale cut mid-sentence reaches the
#: agent while the operator believes it was sent whole.
_MAX_REPLY_CHARS = 5000
_MAX_KEY_FIELDS = 16
#: The live run's header, without its segments and prompt, which can be large.
_LIVE_RUN_FIELDS = ("run_id", "trigger", "started_at", "updated_at")

_FORBIDDEN = {"error": "Not entitled to this pack"}
_NOT_AN_APPROVER = {"error": "Not entitled to decide on this case"}
_NOT_A_CASE_WORKER = {
    "error": "Only this pack's case workers may start the agent on its cases"
}
_VALID_DECISIONS = ("approved", "denied", "replied")


def _summary(item: dict) -> dict:
    """A case as a list shows it: the latest trace only, the live run's header only."""
    item = {k: v for k, v in item.items() if k not in ("action_log",)}
    traces = item.get("agent_traces")
    if isinstance(traces, list) and traces:
        item["agent_traces"] = [
            max(traces, key=lambda t: str((t or {}).get("timestamp") or ""))
        ]
        item["traces_count"] = len(traces)
    live = item.get("live_run")
    if isinstance(live, dict):
        item["live_run"] = {k: live[k] for k in _LIVE_RUN_FIELDS if k in live}
    return item


def _read_partition(
    pack_id: str,
    status: str,
    query: case_query.Query,
    party: str | None,
) -> case_query.Partition:
    """One pack and status from the query's index, from the cursor on.

    Reads stop at `limit` matches or the read budget; the frontier is then the sort
    value of the last entry read, so entries sharing it are read again rather than
    skipped (the index orders them only by that value). The time order reads
    `pack-status-updated-index`, the amount order `pack-status-amount-index`.
    """
    index, attribute = case_query.index_for(query)
    names = {"#p": "pack_id", "#s": "status"}
    values: dict = {":p": pack_id, ":s": status}
    condition = "#p = :p AND #s = :s"
    if query.after:
        condition += " AND #k >= :k" if query.asc else " AND #k <= :k"
        names["#k"] = attribute
        values[":k"] = (
            Decimal(query.after[0]) if query.sort == "amount" else query.after[0]
        )
    entries: list[tuple[tuple, dict]] = []
    read, start = 0, None
    while True:
        response = table.query(
            IndexName=index,
            KeyConditionExpression=condition,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ScanIndexForward=query.asc,
            Limit=min(max(query.limit, 100), case_query.READ_BUDGET),
            **({"ExclusiveStartKey": start} if start else {}),
        )
        last = None
        for item in response.get("Items", []):
            read += 1
            key = case_query.sort_key(item, query)
            last = key
            if case_query.beyond(key, query.after, query) and case_query.matches(
                item, query, party
            ):
                entries.append((key, item))
        start = response.get("LastEvaluatedKey")
        if not start:
            return case_query.Partition(entries, None, read)
        if last is None:
            continue
        frontier = (last[0], "") if query.asc else (last[0], "~")
        # Never stop at the cursor's own value: the next page would start here again.
        progressed = case_query.beyond(frontier, query.after, query)
        if progressed and (
            len(entries) >= query.limit or read >= case_query.READ_BUDGET
        ):
            return case_query.Partition(entries, frontier, read)


def _fetch(case_ids: list[str]) -> dict[str, dict]:
    """The whole cases for one page, by id; at most 100 keys per call."""
    found: dict[str, dict] = {}
    for i in range(0, len(case_ids), 100):
        request = {
            TABLE_NAME: {"Keys": [{"case_id": c} for c in case_ids[i : i + 100]]}
        }
        while request:
            response = dynamodb.batch_get_item(RequestItems=request)
            for item in response.get("Responses", {}).get(TABLE_NAME, []):
                found[item["case_id"]] = item
            request = response.get("UnprocessedKeys") or None
    return found


def _pack_fields(pack_id: str) -> tuple[str | None, str | None]:
    return case_query.list_fields((registry.packs().get(pack_id) or {}).get("fields"))


def _list(packs: list[str], query: case_query.Query, event: dict) -> dict:
    """One page of the view across `packs`, in the query's order, through the query
    index, then the page's cases read whole and cut to what a list shows."""
    # Never none but for a view in DECIDER_VIEWS, which may narrow to no pack.
    if query.sort == "amount" and len(packs) > 1:
        raise HttpError(400, "sort=amount needs exactly one pack_id", field="sort")
    partitions = []
    for pack_id in packs:
        party = _pack_fields(pack_id)[1]
        for status in case_query.view_statuses(query.view):
            partitions.append(_read_partition(pack_id, status, query, party))
    entries, cursor = case_query.page(partitions, query)
    whole = _fetch([e["case_id"] for e in entries])
    items = [
        _summary(whole[e["case_id"]])
        for e in entries
        if e["case_id"] in whole and whole[e["case_id"]].get("pack_id") in packs
    ]
    return respond(
        200,
        {
            "items": items,
            "next_token": case_query.encode_token(query, cursor) if cursor else None,
            "view": query.view,
            "sort": query.sort,
            "order": query.order,
        },
        event,
    )


def _counts(
    packs: list[str], query: case_query.Query, event: dict, *, deciding: list[str]
) -> dict:
    """`{views, counts}` under the query's filters, and with `by=exception_type`
    `exception_types`, each view by exception type under every filter but
    `exception_type` (a facet counts what each choice would show).

    One query per pack and status on the query index. With no filter and no breakdown,
    DynamoDB counts (`Select=COUNT`); otherwise the entries come back, projected to what
    the match and the breakdown read, and are counted here. The read units are the same
    either way. A status with none is omitted from `counts`, a type with none from
    `exception_types`. A view in `DECIDER_VIEWS` sums only the `deciding` packs.
    """
    read = query.filtered or query.by_exception
    names = {"#p": "pack_id", "#s": "status"}
    projection: dict = {}
    if read:
        wanted = ["exception_type", "process_type"]
        if query.q:
            wanted += ["case_id", "document_number", "attributes"]
        names |= {f"#a{i}": a for i, a in enumerate(wanted)}
        projection = {
            "ProjectionExpression": ", ".join(f"#a{i}" for i in range(len(wanted)))
        }
    facet_query = case_query.Query(q=query.q, pack_ids=query.pack_ids)
    # ponytail: sequential, packs x statuses round trips; fan out if a deployment's
    # pack count makes the heartbeat slow.
    counts: dict[str, dict[str, int]] = {}
    by_status: dict[tuple[str, str], dict[str, int]] = {}
    for pack_id in packs:
        party = _pack_fields(pack_id)[1]
        per: dict[str, int] = {}
        for status in CASE_STATUSES:
            n, start = 0, None
            types = by_status.setdefault((pack_id, status), {})
            while True:
                page = table.query(
                    IndexName=case_query.INDEX,
                    KeyConditionExpression="#p = :p AND #s = :s",
                    ExpressionAttributeNames=names,
                    ExpressionAttributeValues={":p": pack_id, ":s": status},
                    **(projection if read else {"Select": "COUNT"}),
                    **({"ExclusiveStartKey": start} if start else {}),
                )
                if read:
                    for item in page.get("Items", []):
                        n += case_query.matches(item, query, party)
                        if query.by_exception and case_query.matches(
                            item, facet_query, party
                        ):
                            kind = case_query.exception_of(item) or "unknown"
                            types[kind] = types.get(kind, 0) + 1
                else:
                    n += page.get("Count", 0)
                start = page.get("LastEvaluatedKey")
                if not start:
                    break
            if n:
                per[status] = n
        counts[pack_id] = per

    def scope(view: str) -> list[str]:
        return deciding if view in DECIDER_VIEWS else packs

    views = {
        view: sum(
            counts[p].get(s, 0)
            for p in scope(view)
            for s in case_query.view_statuses(view)
        )
        for view in VIEWS
    }
    body: dict = {"views": views, "counts": counts}
    if query.by_exception:
        body["exception_types"] = {}
        for view in VIEWS:
            merged: dict[str, int] = {}
            for p in scope(view):
                for status in case_query.view_statuses(view):
                    for kind, n in by_status.get((p, status), {}).items():
                        merged[kind] = merged.get(kind, 0) + n
            body["exception_types"][view] = dict(sorted(merged.items()))
    return respond(200, body, event)


def _lookup_by_key(raw: str, event: dict) -> dict:
    """`?key=<pack_id>:<json>`: the case whose natural key is exactly this one.

    The id is a digest of the pack and the key, so it is derived, not searched for.
    A list, empty when there is no such case, so the shape a search returned stays.
    """
    pack_id, _, raw_key = raw.partition(":")
    if not pack_id or not raw_key:
        raise HttpError(400, "key must be <pack_id>:<json object of key fields>")
    try:
        key = json.loads(raw_key)
    except json.JSONDecodeError:
        raise HttpError(400, "key is not valid JSON") from None
    if (
        not isinstance(key, dict)
        or not key
        or len(key) > _MAX_KEY_FIELDS
        or not all(isinstance(v, str) for v in key.values())
    ):
        raise HttpError(
            400, f"key must be 1-{_MAX_KEY_FIELDS} field names to string values"
        )
    short_id = ((registry.packs().get(pack_id) or {}).get("source") or {}).get(
        "short_id"
    )
    if not short_id:
        return respond(200, [], event)  # a pack with no source mints no case
    try:
        case_id = derive_case_id(pack_id, short_id, key)
    except CaseKeyError as error:
        raise HttpError(400, str(error)) from None
    item = table.get_item(Key={"case_id": case_id}).get("Item")
    found = item and item.get("pack_id") == pack_id
    return respond(200, [_summary(item)] if found else [], event)


def _case_pack_gate(
    case_key: dict, caller: CallerEntitlement, event: dict, *, drive: bool = False
):
    verdict = case_pack_verdict(table, case_key, caller, drive=drive)
    if verdict == CASE_MISSING:
        return respond(404, {"error": "Case not found"}, event)
    if verdict == CASE_FORBIDDEN:
        return respond(403, _FORBIDDEN, event)
    if verdict == CASE_NOT_A_CASE_WORKER:
        return respond(403, _NOT_A_CASE_WORKER, event)
    return None


def _dedup_id(event: dict) -> str:
    """One HTTP request is one intended submission. The queue dedupes on content
    otherwise, which would drop a second identical reply inside five minutes."""
    return (event.get("requestContext") or {}).get("requestId") or str(uuid.uuid4())


def _enqueue(event: dict, caller: CallerEntitlement) -> dict:
    """Put a case on the agent queue for an operator-initiated run."""
    if not AGENT_QUEUE_URL:
        return respond(503, {"error": "Agent queue not configured"}, event)
    try:
        case_key = to_case_key(field(json_body(event), "case_id"))
    except CaseKeyError:
        raise HttpError(400, "Invalid case_id") from None
    gate = _case_pack_gate(case_key, caller, event, drive=True)
    if gate:
        return gate
    if stop := emergency_stop.standing():
        return respond(409, emergency_stop.refusal_body(stop), event)
    case_id = case_key["case_id"]
    sqs.send_message(
        QueueUrl=AGENT_QUEUE_URL,
        MessageBody=json.dumps(
            {
                "case_id": case_id,
                "trigger": "manual",
                "subject": caller_identity(event).subject,
            }
        ),
        MessageGroupId=case_id,
        MessageDeduplicationId=_dedup_id(event),
    )
    logger.info("Enqueued manual run for case=%s", case_id)
    return respond(200, {"enqueued": True, "case_id": case_id}, event)


@route
def handler(event: dict) -> dict:
    method = event.get("httpMethod", "")
    path = event.get("path", "")
    params = event.get("pathParameters") or {}
    logger.info("Request: %s %s", method, path)

    case_key = None
    if params.get("case_id"):
        try:
            case_key = to_case_key(params["case_id"])
        except CaseKeyError:
            raise HttpError(400, "Invalid case_id") from None

    caller = CallerEntitlement.resolve(event)

    if method == "POST" and path.endswith("/enqueue"):
        return _enqueue(event, caller)
    if method == "PUT" and case_key and path.endswith("/rating"):
        return _case_pack_gate(case_key, caller, event) or _save_rating(
            case_key, event, caller
        )
    if method == "POST" and case_key and path.endswith("/reply"):
        return _reply(case_key, event, caller)

    if case_key:
        item = table.get_item(Key=case_key, ConsistentRead=True).get("Item")
        if not item:
            return respond(404, {"error": "Case not found"}, event)
        if not caller.may_see(item.get("pack_id")):
            return respond(403, _FORBIDDEN, event)
        return respond(200, item, event)

    qs = event.get("queryStringParameters") or {}
    if qs.get("key"):
        if set(case_query.params(event)) != {"key"}:
            raise HttpError(400, "key takes no other parameter", field="key")
        pack_id = qs["key"].partition(":")[0]
        if pack_id and not caller.may_see(pack_id):
            return respond(403, _FORBIDDEN, event)
        return _lookup_by_key(qs["key"], event)
    if path.endswith("/summary"):
        return _summary_route(caller, event)
    counting = path.endswith("/counts")
    try:
        raw = case_query.params(event)
        query = (case_query.parse_counts if counting else case_query.parse_list)(raw)
    except case_query.QueryError as error:
        raise HttpError(400, str(error), field=error.param) from None
    # An unentitled pack_id is refused: [] would claim the pack is empty.
    if any(not caller.may_see(p) for p in query.pack_ids):
        return respond(403, _FORBIDDEN, event)
    packs = list(query.pack_ids) or sorted(caller.visible_packs)
    if not packs:
        return respond(403, _FORBIDDEN, event)
    # The packs whose cases this caller may decide, by the gate the decision routes use.
    deciding = [p for p in packs if authorize(caller, p, "replied") is None]
    if counting:
        return _counts(packs, query, event, deciding=deciding)
    if query.view in DECIDER_VIEWS:
        packs = deciding
    return _list(packs, query, event)


def _save_rating(case_key: dict, event: dict, caller: CallerEntitlement) -> dict:
    """One rater's rating of a case. Ratings rank which cases become precedent.

    Kept per rater, under `ratings.<sub>`, so a second reviewer adds a vote rather than
    overwriting the first; the same rater rating again replaces their own. The rater
    comes from the authorizer's claims, never from the body.

    `author` marks a rater who may author the case's pack. The exemplar builder turns
    only such a rater's comment on a thumbs-down into a lesson `get_precedent` returns:
    a lesson is text every later run for that supplier reads, so it takes the same
    groups as writing a SOP, not just the right to see the case.
    """
    body = json_body(event)
    rating = field(body, "rating")
    if rating not in ("positive", "negative"):
        raise HttpError(400, "rating must be 'positive' or 'negative'")
    name, sub = caller_identity(event)
    if not sub:
        return respond(401, {"error": "Authenticated operator is required"}, event)
    entry: dict = {
        "rating": rating,
        "name": name or sub,
        "at": datetime.now(UTC).isoformat(),
    }
    comment = field(body, "comment")
    if comment:
        entry["comment"] = comment[:5000]
        pack_id = (
            table.get_item(Key=case_key, ProjectionExpression="pack_id").get("Item")
            or {}
        ).get("pack_id")
        if caller.clears_author_floor() and caller.may_author(pack_id):
            entry["author"] = True
    try:
        # A nested SET needs the map to exist, and the two cannot share one expression.
        table.update_item(
            Key=case_key,
            UpdateExpression="SET ratings = if_not_exists(ratings, :empty)",
            ExpressionAttributeValues={":empty": {}},
            ConditionExpression="attribute_exists(case_id)",
        )
        table.update_item(
            Key=case_key,
            UpdateExpression="SET ratings.#rater = :entry",
            ExpressionAttributeNames={"#rater": sub},
            ExpressionAttributeValues={":entry": entry},
            ConditionExpression="attribute_exists(case_id)",
        )
    except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
        return respond(404, {"error": "Case not found"}, event)
    return respond(
        200, {"saved": True, "rating": rating, "rater": {"sub": sub, **entry}}, event
    )


def _summary_route(caller: CallerEntitlement, event: dict) -> dict:
    """`GET /cases/summary`: what closed since `since`, per outcome, currency and day.

    One query per pack and done status on the query index from `since`; it reads each
    closed case's index entry in the window and no case whole.
    """
    now = datetime.now(UTC)
    try:
        query = case_query.parse_summary(case_query.params(event), now)
    except case_query.QueryError as error:
        raise HttpError(400, str(error), field=error.param) from None
    if any(not caller.may_see(p) for p in query.pack_ids):
        return respond(403, _FORBIDDEN, event)
    packs = list(query.pack_ids) or sorted(caller.visible_packs)
    if not packs:
        return respond(403, _FORBIDDEN, event)
    entries: list[tuple[dict, dict | None]] = []
    for pack_id in packs:
        fields = (registry.packs().get(pack_id) or {}).get("fields")
        for status in case_query.view_statuses("done"):
            start = None
            while True:
                page = table.query(
                    IndexName=case_query.INDEX,
                    KeyConditionExpression="#p = :p AND #s = :s AND #u >= :u",
                    ExpressionAttributeNames={
                        "#p": "pack_id",
                        "#s": "status",
                        "#u": "updated_at",
                    },
                    ExpressionAttributeValues={
                        ":p": pack_id,
                        ":s": status,
                        ":u": query.since.isoformat(),
                    },
                    **({"ExclusiveStartKey": start} if start else {}),
                )
                entries += [(item, fields) for item in page.get("Items", [])]
                start = page.get("LastEvaluatedKey")
                if not start:
                    break
    return respond(
        200,
        {
            "since": query.since.isoformat(),
            "until": now.isoformat(),
            "packs": packs,
            **case_query.summarize(entries, query.since, now),
        },
        event,
    )


def _chosen_option(
    case: dict, option_id: str | None, decision: str
) -> tuple[dict | None, dict | None]:
    """`(option, set)`: the option a decision chose out of the set the case offers.

    A case with options is decided only by one of them (a reply still authorises
    nothing, so it stays); a case without keeps approve, deny and reply.
    """
    offered = decision_options.offered(case)
    if option_id is None:
        if offered and decision != "replied":
            raise HttpError(400, "Choose one of the case's options", field="option")
        return None, offered
    if not offered:
        raise HttpError(409, "This case offers no options; approve, deny or reply")
    option = decision_options.choose(offered, option_id)
    if option is None or case.get("status") not in PARKED_ON_PERSON_STATUSES:
        raise HttpError(
            409,
            "That option is not one the case offers now",
            options=[o.get("id") for o in offered["options"]],
        )
    return option, offered


def _reply(case_key: dict, event: dict, caller: CallerEntitlement) -> dict:
    """Record an operator's reply or decision on the case, then queue it for the agent.

    Every decision takes `may_approve` (`decisions.authorize`): each resumes a run the
    run checks let write under the pack's limit. The decision is written to the case's action log
    before it is queued (`decisions.py`), conditioned on the case not having settled.
    """
    if not AGENT_QUEUE_URL:
        return respond(503, {"error": "Agent queue not configured"}, event)
    # Before anything is recorded: a decision resumes a run, and none starts while an
    # emergency stop stands. The person decides again once it is cleared.
    if stop := emergency_stop.standing():
        return respond(409, emergency_stop.refusal_body(stop), event)
    body = json_body(event)
    message = field(body, "message")
    if not message or not message.strip():
        raise HttpError(400, "message is required")
    if len(message) > _MAX_REPLY_CHARS:
        raise HttpError(413, f"message must be {_MAX_REPLY_CHARS} characters or fewer")
    option_id = field(body, "option")
    if option_id is not None and (
        not decision_options.valid_id(option_id) or "decision" in body
    ):
        raise HttpError(
            400, "option must be an option id, and replaces decision", field="option"
        )
    decision = field(body, "decision") or "replied"
    if decision not in _VALID_DECISIONS:
        raise HttpError(400, "decision must be 'approved', 'denied', or 'replied'")

    item = table.get_item(
        Key=case_key,
        ProjectionExpression=(
            "pack_id, agent_traces, cost_summary, #a, action_log, #s, decision_options"
        ),
        ExpressionAttributeNames={"#a": "attributes", "#s": "status"},
        ConsistentRead=True,
    ).get("Item")
    if not item:
        return respond(404, {"error": "Case not found"}, event)
    pack_id = item.get("pack_id")
    # Before the options are read: a refusal must not name what the case offers.
    refusal = authorize(caller, pack_id, decision)
    if refusal == NOT_AN_APPROVER:
        logger.warning("refused approval on %s (pack %s)", case_key, pack_id)
        return respond(403, _NOT_AN_APPROVER, event)
    if refusal:
        return respond(403, _FORBIDDEN, event)
    option, offered = _chosen_option(item, option_id, decision)
    if option is not None:
        decision = option_decision(option)

    sender, subject = caller_identity(event)
    if not sender or not subject:
        return respond(401, {"error": "Authenticated operator is required"}, event)

    case_id = case_key["case_id"]
    try:
        entry = record_decision(
            table,
            case_id,
            decision=decision,
            actor=subject,
            actor_display=sender,
            source="console",
            case=item,
            option=option,
            option_set=offered["set_id"] if offered else None,
        )
    except AlreadyApproved:
        raise HttpError(409, ALREADY_APPROVED, awaiting_second_approver=True) from None
    except Settled:
        raise HttpError(
            409, "The case is already decided, or changed since it was read"
        ) from None
    if awaiting_second_approver(item, entry, pack_id):
        # Recorded, not queued: the run resumes when a different approver agrees.
        logger.info("First of two approvals recorded for case=%s", case_id)
        return respond(
            202,
            {"enqueued": False, "case_id": case_id, "awaiting_second_approver": True},
            event,
        )

    payload = {
        "source": "console",
        "case_id": case_id,
        "sender": sender,
        # Verbatim: the entitled operator is the principal, as on their own `/runs`
        # prompt. The regex filter stays on SAP push, where the text
        # is someone else's; the guardrail's prompt-attack filter covers both.
        "message": message,
        "decision": decision,
        **(
            {"option": {"id": option["id"], "label": option["label"]}} if option else {}
        ),
        **(
            {"approved_write": entry["approved_write"]}
            if "approved_write" in entry
            else {}
        ),
    }
    sqs.send_message(
        QueueUrl=AGENT_QUEUE_URL,
        MessageBody=json.dumps(
            {
                "case_id": case_id,
                "trigger": "console-action",
                "payload": payload,
                "subject": subject,
            }
        ),
        MessageGroupId=case_id,
        MessageDeduplicationId=_dedup_id(event),
    )
    logger.info("Enqueued console %s for case=%s", decision, case_id)
    return respond(200, {"enqueued": True, "case_id": case_id}, event)
