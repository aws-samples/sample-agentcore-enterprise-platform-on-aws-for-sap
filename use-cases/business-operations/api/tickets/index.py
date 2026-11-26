# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
SAMPLE ticketing API: replace it with a real ITSM before production.

Simulates a ServiceNow-like ticketing system with its own DynamoDB table, mounted on
the console API while `api.sample_ticketing` is on; turning it off removes these routes
and this function whole. The agent escalates a ticket, a human approves or denies.

Every decision is checked against the linked case's pack by `decisions.authorize`, the
same gate the console's `/cases/{id}/reply` uses, so the two decision paths cannot
disagree about who may approve.

Routes:
  GET    /tickets              — list tickets (optional ?status=&assigned_to= filters;
                                   ?limit=&next_token= for one page at a time)
  GET    /tickets/{id}         — get single ticket
  POST   /tickets              — create ticket
  PUT    /tickets/{id}         — update ticket (assign, approve, deny, add comment)
  POST   /tickets/{id}/action  — approve/deny/reply: update status + enqueue linked case
"""

import base64
import binascii
import json
import logging
import os
import time
import uuid
from datetime import UTC, datetime

import boto3
import decision_options
import emergency_stop
from api_http import HttpError, field, json_body, respond, route

# Canonical case identity codec, from the API layer. Ticket
# correlation and the agent resume message both key on a case_id, so it is
# imported unconditionally.
from case_key import try_normalize_case_id
from case_status import PARKED_ON_PERSON_STATUSES
from ddb_paginate import paginate
from decisions import (
    ALREADY_APPROVED,
    AlreadyApproved,
    Settled,
    authorize,
    awaiting_second_approver,
    option_decision,
    record_decision,
)

# Pack entitlement — same layer and same decision cases_api floors its reads with.
from entitlement import CallerEntitlement, caller_groups, caller_identity
from ticket_record import VALID_PRIORITIES, VALID_RESPONSE_TYPES, new_ticket

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(os.environ["TICKETS_TABLE_NAME"])
sqs = boto3.client("sqs")
# Optional — only the /action route needs it; absent in deployments that never
# wire the agent queue.
QUEUE_URL = os.environ.get("AGENT_QUEUE_URL", "")


# Read for authorization, and written only to record a decision. `approve_groups` is
# declared per pack, and a ticket does not carry its case's pack, nor could it be
# trusted to: the ticket was written from an agent tool call, and model-supplied input
# must not name the pack an authorization decision is made against.
CASES_TABLE_NAME = os.environ["CASES_TABLE_NAME"]
cases_table = dynamodb.Table(CASES_TABLE_NAME)

VALID_STATUSES = {"open", "assigned", "approved", "denied", "replied", "closed"}
# Keep in sync with types/tickets.schema.json (TicketStatus)
VALID_ACTIONS = {"approved", "denied", "replied"}

#: Names no pack, for the same reason cases_api's refusal does not.
_FORBIDDEN = {"error": "Not entitled to decide on this case"}

#: The tickets table's GSI on `status` (business_operations/data.py).
STATUS_INDEX = "status-index"
DEFAULT_LIMIT = 100
MAX_LIMIT = 500
#: BatchGetItem's per-call key ceiling.
_BATCH_KEYS = 100
_BATCH_ATTEMPTS = 5


# Moved to the shared layer so the console reply route names the operator identically.
# The name stays: this module's callers and its tests both reach for it. Just the
# display field — the two paths below that put a reviewer's name in prose.
def _reviewer_identity(event: dict) -> str | None:
    return caller_identity(event).display


def _case_pack(case_id: str) -> str | None:
    """The linked case's own pack, or None if it cannot be established.

    None is not "no restriction": every caller of this treats it as a refusal.
    """
    item = (
        cases_table.get_item(
            Key={"case_id": case_id},
            ProjectionExpression="pack_id",
            ConsistentRead=True,
        ).get("Item")
        or {}
    )
    return item.get("pack_id") or None


def _case_packs(case_ids: set[str]) -> dict[str, str | None]:
    """`_case_pack` for many cases at once: one BatchGetItem per 100 distinct ids.

    The list route used to call `_case_pack` once per ticket, and ~490 sequential
    consistent GetItems outran the Lambda's 15 s timeout. Same table, same projection,
    same consistency, so the decision is unchanged; an id the table does not return
    stays None, which `_may_read` refuses. Keys still unprocessed after the retries
    raise rather than drop their tickets, since a short list reads as a quiet day.
    """
    packs: dict[str, str | None] = dict.fromkeys(case_ids)
    ids = sorted(case_ids)
    for start in range(0, len(ids), _BATCH_KEYS):
        request = {
            CASES_TABLE_NAME: {
                "Keys": [{"case_id": c} for c in ids[start : start + _BATCH_KEYS]],
                "ProjectionExpression": "case_id, pack_id",
                "ConsistentRead": True,
            }
        }
        for attempt in range(_BATCH_ATTEMPTS):
            resp = dynamodb.batch_get_item(RequestItems=request)
            for item in resp.get("Responses", {}).get(CASES_TABLE_NAME, []):
                packs[item["case_id"]] = item.get("pack_id") or None
            request = resp.get("UnprocessedKeys") or {}
            if not request:
                break
            time.sleep(0.05 * 2**attempt)
        else:
            raise RuntimeError("cases BatchGetItem left keys unprocessed")
    return packs


def _encode_token(last_key: dict | None) -> str | None:
    if not last_key:
        return None
    return base64.urlsafe_b64encode(json.dumps(last_key).encode()).decode()


def _decode_token(token: str, status: str | None) -> dict | None:
    """The ExclusiveStartKey a `next_token` carries, or None if it is not one we issued.

    Client-held, so it is checked for shape before DynamoDB sees it. It cannot widen
    what a caller reads: every item of the page is still floored by entitlement.
    """
    try:
        key = json.loads(base64.urlsafe_b64decode(token.encode()))
    except (binascii.Error, ValueError):
        return None
    expected = {"ticket_id", "status"} if status else {"ticket_id"}
    if not isinstance(key, dict) or set(key) != expected:
        return None
    if not all(isinstance(v, str) for v in key.values()):
        return None
    if status and key["status"] != status:
        return None
    return key


def _may_read(caller: CallerEntitlement, case_id: str | None, packs: dict) -> bool:
    """May this caller read a ticket linked to `case_id`?

    A ticket carries the agent's own title, description and comments about its case,
    so reading one is reading pack-scoped case content — the same content
    `GET /cases/{id}` floors. This is `_decision_gate`'s read half, split out because
    the read routes have no action to weigh and return no 403 body of their own.

    A pack that cannot be established is a refusal, for the reason `_case_pack`
    states. `packs` memoises the lookup for the length of one request, because a list
    of tickets routinely names the same case more than once.
    """
    if case_id not in packs:
        packs[case_id] = _case_pack(case_id) if case_id else None
    pack_id = packs[case_id]
    return pack_id is not None and caller.may_see(pack_id)


def _decision_gate(
    caller: CallerEntitlement, action: str, case_id: str | None, ticket_id: str
) -> None:
    """Pass if this caller may record `action` on the case, else raise the 403."""
    pack_id = _case_pack(case_id) if case_id else None
    if pack_id is not None and authorize(caller, pack_id, action) is None:
        return
    logger.warning(
        "refused %s on ticket %s (case %s, pack %s)",
        action,
        ticket_id,
        case_id,
        pack_id,
    )
    raise HttpError(403, _FORBIDDEN["error"])


_SECOND_APPROVER = (
    "Approval recorded. This write is over the pack's dual-approval limit, so it needs "
    "a second approver: another member of the pack's approve groups must approve it too"
)


def _first_of_two(ticket_id: str, reviewer: str, case_id: str, event: dict) -> dict:
    """The first of two approvals: noted on the ticket, which stays open for the second
    approver. Nothing is queued."""
    now = datetime.now(UTC).isoformat()
    resp = table.update_item(
        Key={"ticket_id": ticket_id},
        UpdateExpression=(
            "SET updated_at = :ts, "
            "comments = list_append(if_not_exists(comments, :empty), :comment)"
        ),
        ExpressionAttributeValues={
            ":ts": now,
            ":empty": [],
            ":comment": [
                {
                    "author": reviewer,
                    "text": "Approved (1 of 2): a second approver is needed",
                    "timestamp": now,
                }
            ],
        },
        ConditionExpression="attribute_exists(ticket_id)",
        ReturnValues="ALL_NEW",
    )
    logger.info("First of two approvals on ticket %s for case=%s", ticket_id, case_id)
    return respond(
        202,
        {
            "ticket": resp.get("Attributes", {}),
            "enqueued": False,
            "case_id": case_id,
            "awaiting_second_approver": True,
        },
        event,
    )


def _record_on_case(
    case_id: str, action: str, event: dict, option_id: str | None = None
) -> tuple[dict, bool, str, dict | None]:
    """The decision entry recorded on the case, whether it still waits for a second
    approver, the ticket status it gives, and the option chosen; or a 409.

    The same durable record the console reply writes (`decisions.py`), before
    anything is queued. With `option_id`, the decision is that option's out of the set
    the case offers; a case that offers options takes no bare approve or deny, on either
    ticket route.
    """
    reviewer, subject = caller_identity(event)
    item = (
        cases_table.get_item(
            Key={"case_id": case_id},
            ProjectionExpression=(
                "pack_id, agent_traces, cost_summary, #a, action_log, #s, "
                "decision_options"
            ),
            ExpressionAttributeNames={"#a": "attributes", "#s": "status"},
            ConsistentRead=True,
        ).get("Item")
        or {}
    )
    offered = decision_options.offered(item)
    option = None
    if option_id is None:
        if offered and action != "replied":
            raise HttpError(400, "Choose one of the case's options", field="option")
    else:
        if not offered:
            raise HttpError(409, "This case offers no options; approve, deny or reply")
        option = decision_options.choose(offered, option_id)
        if option is None or item.get("status") not in PARKED_ON_PERSON_STATUSES:
            raise HttpError(
                409,
                "That option is not one the case offers now",
                options=[o.get("id") for o in offered["options"]],
            )
    if option is not None:
        # The ticket's own vocabulary: an option that approves a write approves the
        # ticket; one that approves none leaves every write unapproved, as a denial.
        action = "approved" if option_decision(option) == "approved" else "denied"
    try:
        entry = record_decision(
            cases_table,
            case_id,
            decision=action,
            actor=subject,
            actor_display=reviewer or "",
            source="ticket",
            case=item,
            option=option,
            option_set=offered["set_id"] if option else None,
        )
    except AlreadyApproved:
        raise HttpError(409, ALREADY_APPROVED, awaiting_second_approver=True) from None
    except Settled:
        raise HttpError(
            409, "The case is already decided, or changed since it was read"
        ) from None
    waiting = awaiting_second_approver(item, entry, item.get("pack_id"))
    return entry, waiting, action, option


@route
def handler(event: dict) -> dict:
    method = event.get("httpMethod", "")
    path = event.get("path", "")
    params = event.get("pathParameters") or {}
    logger.info("Request: %s %s", method, path)
    ticket_id = params.get("id")

    if method == "POST" and ticket_id and path.endswith("/action"):
        return _action_ticket(ticket_id, event)

    if method == "POST" and not ticket_id:
        return _create_ticket(event)

    if method == "PUT" and ticket_id:
        return _update_ticket(ticket_id, event)

    if method == "GET" and ticket_id:
        resp = table.get_item(Key={"ticket_id": ticket_id}, ConsistentRead=True)
        item = resp.get("Item")
        if not item:
            return respond(404, {"error": "Ticket not found"}, event)
        caller = CallerEntitlement.resolve(event)
        if not _may_read(caller, try_normalize_case_id(item.get("case_id")), {}):
            return respond(403, _FORBIDDEN, event)
        return respond(200, item, event)

    if method == "GET":
        return _list_tickets(event)

    return respond(405, {"error": "Method not allowed"}, event)


def _create_ticket(event: dict) -> dict:
    body = json_body(event)
    # A ticket on a case joins that case's pack queue, so filing one takes the same
    # `may_see` as reading it.
    case_id = try_normalize_case_id(body.get("case_id"))
    if body.get("case_id") and not _may_read(
        CallerEntitlement.resolve(event), case_id, {}
    ):
        raise HttpError(403, _FORBIDDEN["error"])
    try:
        ticket = new_ticket(
            body.get("title"),
            # From the token, like `decided_by`, so the write guard's "decided by the
            # principal that raised it" compares the same identity on both sides.
            created_by=caller_identity(event).subject,
            description=body.get("description"),
            priority=body.get("priority"),
            assigned_to=body.get("assigned_to"),
            case_id=body.get("case_id"),
            category=body.get("category"),
            response_type=body.get("response_type"),
        )
    except ValueError as e:
        raise HttpError(400, str(e)) from None

    table.put_item(Item=ticket)
    return respond(201, ticket, event)


def _update_ticket(ticket_id: str, event: dict) -> dict:
    body = json_body(event)
    for name in ("status", "assigned_to", "priority", "resolution", "category"):
        field(body, name)

    # Every field a PUT sets (status, assignee, resolution, category) steers the case's
    # queue, so the caller must see the ticket's pack first, as GET requires. An unknown
    # id is a 404: update_item would otherwise create it.
    caller = CallerEntitlement.resolve(event)
    current = table.get_item(Key={"ticket_id": ticket_id}, ConsistentRead=True).get(
        "Item"
    )
    if not current:
        raise HttpError(404, "Ticket not found")
    case_id = try_normalize_case_id(current.get("case_id"))
    if not _may_read(caller, case_id, {}):
        raise HttpError(403, _FORBIDDEN["error"])

    # PUT sets `status` directly, and the agent reads a ticket's status back through
    # the demo_get_ticket tool — so an unentitled "approved" written here authorizes
    # the same SAP write POST /action would. Same gate, or the gate has a side door.
    new_status = body.get("status")
    # A decision is what the agent acts on, so once made the status stays.
    decided = current.get("status") in _DECIDED
    if "status" in body and decided and new_status != current.get("status"):
        raise HttpError(409, "A decided ticket's status cannot change")
    if new_status in VALID_ACTIONS:
        _check_decidable(current, new_status)
        _decision_gate(caller, new_status, case_id, ticket_id)
        _, waiting, _, _ = _record_on_case(case_id, new_status, event)
        if waiting:
            raise HttpError(409, _SECOND_APPROVER)

    # The agent write gate's duplicate exemption keys on the stored category of an
    # approved ticket (invoice_matching 4.0a), so a decided ticket keeps the category it
    # was decided under — relabelling an approved price ticket `duplicate_review` would
    # repurpose an approval nobody gave.
    if "category" in body and decided:
        raise HttpError(409, "A decided ticket's category cannot change")

    allowed = {"status", "assigned_to", "priority", "resolution", "category"}
    update_parts = ["updated_at = :ts"]
    expr_values: dict = {":ts": datetime.now(UTC).isoformat()}
    expr_names: dict = {}

    if new_status in VALID_ACTIONS:
        # Who decided, for the agent write gate's approval-provenance check — the same
        # two fields `_action_ticket` records, or a PUT approval reads as "no human".
        update_parts.append("decided_by = :by, decided_by_groups = :groups")
        expr_values[":by"] = caller_identity(event).subject
        expr_values[":groups"] = sorted(caller_groups(event))

    for key in allowed:
        if key in body:
            val = body[key]
            if key == "status" and val not in VALID_STATUSES:
                raise HttpError(400, f"Invalid status: {val}")
            if key == "priority" and val not in VALID_PRIORITIES:
                raise HttpError(400, f"Invalid priority: {val}")
            update_parts.append(f"#{key} = :{key}")
            expr_names[f"#{key}"] = key
            expr_values[f":{key}"] = val

    comment_text = (field(body, "comment") or "").strip()
    if comment_text:
        reviewer = _reviewer_identity(event)
        if not reviewer:
            raise HttpError(401, "Authenticated reviewer is required")
        comment = {
            "author": reviewer,
            "text": comment_text,
            "timestamp": expr_values[":ts"],
        }
        update_parts.append(
            "comments = list_append(if_not_exists(comments, :empty), :comment)"
        )
        expr_values[":empty"] = []
        expr_values[":comment"] = [comment]

    try:
        resp = table.update_item(
            Key={"ticket_id": ticket_id},
            UpdateExpression="SET " + ", ".join(update_parts),
            ExpressionAttributeNames=expr_names if expr_names else None,
            ExpressionAttributeValues=expr_values,
            ConditionExpression="attribute_exists(ticket_id)",
            ReturnValues="ALL_NEW",
        )
        return respond(200, resp["Attributes"], event)
    except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
        return respond(404, {"error": "Ticket not found"}, event)


def _list_tickets(event: dict) -> dict:
    """List tickets: a bare array of every match, or one page when asked for one.

    A status filter is a query on the status GSI rather than a scan of the table.
    Passing `limit` or `next_token` switches to `{"items", "next_token"}`; a caller that
    passes neither gets the whole list as before. Order is newest `updated_at` first,
    across the whole list in that mode but only within a page in the paged one — the
    GSI has no sort key to order by.
    """
    qs = event.get("queryStringParameters") or {}
    status = qs.get("status")
    expr_names: dict = {}
    expr_values: dict = {}
    kwargs: dict = {}

    if status:
        op = table.query
        kwargs["IndexName"] = STATUS_INDEX
        kwargs["KeyConditionExpression"] = "#s = :status"
        expr_names["#s"] = "status"
        expr_values[":status"] = status
    else:
        op = table.scan

    if qs.get("assigned_to"):
        kwargs["FilterExpression"] = "assigned_to = :assigned"
        expr_values[":assigned"] = qs["assigned_to"]
    if expr_names:
        kwargs["ExpressionAttributeNames"] = expr_names
    if expr_values:
        kwargs["ExpressionAttributeValues"] = expr_values

    paged = "limit" in qs or "next_token" in qs
    if paged:
        try:
            limit = int(qs.get("limit") or DEFAULT_LIMIT)
        except ValueError:
            limit = 0
        if not 1 <= limit <= MAX_LIMIT:
            raise HttpError(400, f"limit must be an integer 1-{MAX_LIMIT}")
        kwargs["Limit"] = limit
        if qs.get("next_token"):
            start = _decode_token(qs["next_token"], status)
            if start is None:
                raise HttpError(400, "Invalid next_token")
            kwargs["ExclusiveStartKey"] = start
        page = op(**kwargs)
        items = page.get("Items", [])
        next_token = _encode_token(page.get("LastEvaluatedKey"))
    else:
        # ponytail: unbounded on purpose — the briefing counts every ticket. The cost
        # is ⌈table/1 MB⌉ reads plus ⌈cases/100⌉ batch gets; past that, page.
        items = paginate(op, **kwargs)

    # The table carries no pack of its own, so the floor is applied per ticket through
    # the case each one links to. A caller entitled to nothing is refused rather than
    # handed [], for the reason `GET /cases` gives: an empty list would read as a quiet
    # day.
    caller = CallerEntitlement.resolve(event)
    if not caller.visible_packs:
        return respond(403, _FORBIDDEN, event)
    case_ids = [try_normalize_case_id(item.get("case_id")) for item in items]
    packs = _case_packs({c for c in case_ids if c})
    items = [
        item
        for item, case_id in zip(items, case_ids, strict=True)
        if _may_read(caller, case_id, packs)
    ]

    items.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    if paged:
        return respond(200, {"items": items, "next_token": next_token}, event)
    return respond(200, items, event)


_DECIDED = {"approved", "denied", "replied"}


def _check_decidable(current: dict, action: str) -> str:
    """Raise the 409/400 if `action` may not be recorded on this ticket now, else
    return its response type. Both decision routes, so `PUT` is no side door."""
    status = current.get("status", "open")
    if status == "closed":
        raise HttpError(409, "Ticket is already closed")
    if status in _DECIDED and action != status:
        raise HttpError(
            409, f"Ticket already has decision {status}; cannot change it to {action}"
        )
    if status not in {"open", "assigned", *_DECIDED}:
        raise HttpError(409, f"Ticket has unsupported status: {status}")
    response_type = current.get("response_type", "approval")
    if response_type not in VALID_RESPONSE_TYPES:
        raise HttpError(409, f"Ticket has unsupported response_type: {response_type}")
    if response_type == "free_text" and action != "replied":
        raise HttpError(400, "This ticket requires a free-text reply")
    if response_type != "free_text" and action not in {"approved", "denied"}:
        raise HttpError(400, "This ticket requires an approve or deny decision")
    return response_type


def _action_ticket(ticket_id: str, event: dict) -> dict:
    """Validate a human response, persist it, and resume the linked case."""
    body = json_body(event)
    option_id = field(body, "option")
    if option_id is not None:
        if not decision_options.valid_id(option_id) or "action" in body:
            raise HttpError(
                400, "option must be an option id, and replaces action", field="option"
            )
        # The checks and the gate below are the same for every approval-type decision;
        # which one the option makes is known once the case is read.
        action = "approved"
    else:
        action = field(body, "action") or ""
    if action not in VALID_ACTIONS:
        raise HttpError(400, f"action must be one of: {sorted(VALID_ACTIONS)}")
    if not QUEUE_URL:
        return respond(503, {"error": "Agent resume queue is unavailable"}, event)
    # Before anything is recorded: the action resumes a run, and none starts while an
    # emergency stop stands.
    if stop := emergency_stop.standing():
        return respond(409, emergency_stop.refusal_body(stop), event)

    # `reviewer` is a display name for the ticket comment and may be an email. `subject`
    # is the agent's contract, and separate for that reason: it is the OIDC `sub` that
    # becomes the Memory actorId and the audit initiator. Both from the trusted authorizer
    # context, never the body — in the shared layer so the console reply route resolves
    # them identically.
    reviewer, reviewer_subject = caller_identity(event)
    if not reviewer:
        raise HttpError(401, "Authenticated reviewer is required")

    current = table.get_item(Key={"ticket_id": ticket_id}, ConsistentRead=True).get(
        "Item"
    )
    if not current:
        raise HttpError(404, "Ticket not found")

    response_type = _check_decidable(current, action)
    if option_id is not None and current.get("status") in _DECIDED:
        raise HttpError(409, f"Ticket already has decision {current.get('status')}")
    response_text = (field(body, "response_text") or "").strip()
    if response_type == "free_text":
        if not response_text:
            raise HttpError(400, "response_text is required")
        response_text = response_text[:5000]

    # Normalized so the resume message and its FIFO group match every other producer
    # for this case.
    case_id = try_normalize_case_id(current.get("case_id"))
    if not case_id:
        raise HttpError(409, "Ticket is not linked to a case")

    # The entitlement gate on a ProposedWrite. It sits here — after the case is known,
    # before the first mutation — because this decision is what authorizes the SAP
    # write the agent makes on resume; the frontend's WriteDiff only renders it.
    caller = CallerEntitlement.resolve(event)
    _decision_gate(caller, action, case_id, ticket_id)
    entry, waiting, action, option = _record_on_case(case_id, action, event, option_id)
    if waiting:
        return _first_of_two(ticket_id, reviewer, case_id, event)

    default_resolution = (
        response_text
        if action == "replied"
        else f"Chose {option['label']}"
        if option
        else f"{action.title()} by reviewer"
    )
    # Taken beside an option as beside an action: the person's own words, kept. Cut as
    # `response_text` is, so a pasted page cannot grow the ticket without bound.
    resolution = (field(body, "resolution") or "").strip()[:5000] or default_resolution
    comment = (field(body, "comment") or "").strip()[:5000] or default_resolution

    now = datetime.now(UTC).isoformat()
    try:
        resp = table.update_item(
            Key={"ticket_id": ticket_id},
            UpdateExpression=(
                "SET #s = :status, resolution = :res, updated_at = :ts, "
                "decided_by = :by, decided_by_groups = :groups, "
                "comments = list_append(if_not_exists(comments, :empty), :comment)"
                + (", #o = :option" if option else "")
            ),
            ExpressionAttributeNames={
                "#s": "status",
                **({"#o": "option"} if option else {}),
            },
            ExpressionAttributeValues={
                **({":option": option["id"]} if option else {}),
                ":status": action,
                ":res": resolution,
                ":ts": now,
                # Read by the agent write gate (`write_guards.approval_problem`): an
                # approval releases a payment only if a principal in the pack's
                # `approve_groups` recorded it. From the authorizer, never the body.
                ":by": reviewer_subject,
                ":groups": sorted(caller_groups(event)),
                ":empty": [],
                ":comment": [{"author": reviewer, "text": comment, "timestamp": now}],
            },
            ConditionExpression="attribute_exists(ticket_id)",
            ReturnValues="ALL_NEW",
        )
    except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
        raise HttpError(404, "Ticket not found") from None

    ticket = resp["Attributes"]
    message_payload = {
        "source": "ticket-action",
        "ticket_id": ticket_id,
        "ticket_decision": action,
        "resolution": resolution,
        **(
            {"option": {"id": option["id"], "label": option["label"]}} if option else {}
        ),
        **({"response_text": response_text} if response_text else {}),
        # Read off the stored ticket, never the request body: an SOP exemption keys on
        # `category` (invoice_matching 4.0a), so the resumed run must not have to
        # infer it from the title the agent wrote.
        **{k: ticket[k] for k in ("category", "title") if ticket.get(k)},
        **(
            {"approved_write": entry["approved_write"]}
            if "approved_write" in entry
            else {}
        ),
    }
    sqs.send_message(
        QueueUrl=QUEUE_URL,
        MessageBody=json.dumps(
            {
                "case_id": case_id,
                "trigger": "ticket-action",
                "payload": message_payload,
                "subject": reviewer_subject,
            }
        ),
        MessageGroupId=case_id,
        # The queue sets `contentBasedDeduplication: true`, so an identical second reply
        # inside SQS's 5-minute window was discarded while this route still answered
        # `200 {"enqueued": true}`. One HTTP request is one intended decision — same
        # reasoning, and the same key, as the two `/cases` producers.
        MessageDeduplicationId=(event.get("requestContext") or {}).get("requestId")
        or str(uuid.uuid4()),
    )
    logger.info("Enqueued case=%s after ticket %s %s", case_id, ticket_id, action)

    return respond(200, {"ticket": ticket, "enqueued": True, "case_id": case_id}, event)
