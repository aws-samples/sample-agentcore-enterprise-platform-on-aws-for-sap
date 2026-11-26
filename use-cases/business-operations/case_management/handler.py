# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Case management Gateway tool: read and record state on a case.

The two tools the platform seam needs. A case is one DynamoDB item keyed by its opaque
case_id.
"""

import json
import logging
import os
from datetime import UTC, datetime
from decimal import Decimal

import boto3
import decision_options
import registry
from botocore.exceptions import ClientError
from case_key import try_normalize_case_id
from case_status import SETTLED_STATUSES
from contracts import CASE_STATUSES, PLATFORM_CASE_STATUSES
from persist_mask import mask

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_table = boto3.resource("dynamodb").Table(os.environ["CASES_TABLE"])

# The only fields the model may write, as the tool description promises. An
# allowlist, so a field added to the case later is closed until named here:
# `ttl` would let the model have DynamoDB delete the record of a SAP write,
# `ratings` would promote its own case as precedent. `updated_at`, `action_log`
# and `inquiry_sent_at` are written below, never taken from the caller.
WRITABLE = frozenset({"status", "title", "description", "priority", "disposition"})
#: Written beside `status: awaiting_human_input` only, checked by `decision_options.py`.
OPTIONS = "decision_options"

# The schema's CaseStatus enum minus the ones the platform sets. The tool spec
# advertises the same set (stack.py), so the model is told before it is refused.
AGENT_STATUSES = frozenset(CASE_STATUSES) - frozenset(PLATFORM_CASE_STATUSES)
_AWAITING = "awaiting_human_input"


def _action(action: object) -> str:
    """The model's label for the entry. `human_` is reserved: the approval routes write
    those entries, and the invoker reads the latest one as the case's decision."""
    action = str(action or "update")
    return f"agent_{action}" if action.startswith("human_") else action


def _text(item: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(item, default=str)}]}


def _get_case(args: dict) -> dict:
    case_id = try_normalize_case_id(args.get("case_id"))
    if not case_id:
        return {"error": "case_id is not a case id"}
    item = _table.get_item(Key={"case_id": case_id}, ConsistentRead=True).get("Item")
    if not item:
        return {"content": [{"type": "text", "text": f"No case found for {case_id}"}]}
    return _text(item)


def _disposition_refusal(case_id: str, disposition: object) -> str | None:
    """A pack that declares `dispositions` accepts only those; one that declares
    none takes any label. A blank one sets nothing, so it is never refused."""
    if disposition in (None, ""):
        return None
    item = _table.get_item(
        Key={"case_id": case_id}, ProjectionExpression="pack_id", ConsistentRead=True
    ).get("Item")
    pack_id = (item or {}).get("pack_id")
    if not pack_id:
        return None  # the conditional update below refuses a missing case
    registry.refresh_for_request()
    declared = ((registry.packs().get(str(pack_id)) or {}).get("pack") or {}).get(
        "dispositions"
    )
    if isinstance(declared, list) and disposition not in declared:
        return (
            f"disposition {disposition!r} is not one pack {pack_id} declares: "
            f"{', '.join(map(str, declared))}"
        )
    return None


def _update_case(args: dict) -> dict:
    case_id = try_normalize_case_id(args.get("case_id"))
    if not case_id:
        return {"error": "case_id is not a case id"}
    try:
        # DynamoDB refuses Python floats; numbers go in as Decimal.
        updates = json.loads(args.get("updates") or "{}", parse_float=Decimal)
    except json.JSONDecodeError:
        return {"error": "Invalid JSON in updates field"}
    if not isinstance(updates, dict):
        return {"error": "updates must be a JSON object"}
    # A field passed beside `updates` rather than in it is what the model meant: a
    # `status` there used to be dropped without a word, and a case the agent had
    # closed was parked for manual review. `updates` wins where both name a field.
    for key, value in args.items():
        if key not in ("case_id", "updates", "action"):
            updates.setdefault(key, value)
    options = updates.pop(OPTIONS, None)
    # Dropped rather than rejected, so a bundled status write still lands, and
    # named in the reply so the model can correct course.
    refused = sorted(k for k in updates if k not in WRITABLE)
    for key in refused:
        del updates[key]
    # A wrong status is refused whole: the case's state machine is the point.
    status = updates.get("status")
    if "status" in updates and status not in AGENT_STATUSES:
        return {
            "error": f"status {status!r} is not one this tool sets. Use one of: "
            f"{', '.join(sorted(AGENT_STATUSES))}, or leave status out to record "
            "a note without moving the case"
        }
    if "disposition" in updates:
        refusal = _disposition_refusal(case_id, updates["disposition"])
        if refusal:
            return {"error": refusal}

    # The outcomes a person chooses between: offered only as the case is parked on
    # them, and refused whole, since a set cut short would offer a different choice.
    if options is not None:
        if status != _AWAITING:
            return {
                "error": f"{OPTIONS} are recorded only in the update that sets status "
                f"{_AWAITING}, the moment a person is asked to choose"
            }
        try:
            options = decision_options.narrow(options)
        except decision_options.OptionsError as error:
            return {"error": str(error)}

    free = [k for k in ("title", "description") if isinstance(updates.get(k), str)]
    for key, value in zip(free, mask([updates[k] for k in free])):
        updates[key] = value

    ts = datetime.now(UTC).isoformat()
    if options is not None:
        texts = mask([t for o in options for t in (o["label"], o["effect"])])
        for i, option in enumerate(options):
            option["label"], option["effect"] = texts[2 * i], texts[2 * i + 1]
        updates[OPTIONS] = {
            "set_id": decision_options.set_id(options),
            "recorded_at": ts,
            "options": options,
        }
    names = {f"#f{i}": k for i, k in enumerate(updates)}
    values = {f":f{i}": v for i, v in enumerate(updates.values())}
    sets = [f"#f{i} = :f{i}" for i in range(len(updates))]
    sets += [
        "updated_at = :ts",
        "action_log = list_append(if_not_exists(action_log, :empty), :entry)",
    ]
    # Reaching awaiting_human_input is the moment a person was asked, so the
    # handover's "waiting 6d" is stamped here, not asked of the model. The first
    # inquiry is the one the age is about, so a re-write keeps it; any other
    # status clears it. An update with no status touches neither. A status this
    # tool writes carries no `status_reason`, so it clears the one an earlier
    # status was parked with.
    # A status written without options removes the last set, so a case parked again
    # never offers the choices an earlier stop did.
    remove = ""
    if status == _AWAITING:
        sets.append("inquiry_sent_at = if_not_exists(inquiry_sent_at, :ts)")
        remove = " REMOVE status_reason"
    elif status is not None:
        remove = " REMOVE inquiry_sent_at, status_reason"
    if status is not None and options is None:
        remove += f", {OPTIONS}"
    settled = {f":settled{i}": s for i, s in enumerate(sorted(SETTLED_STATUSES))}
    values |= {
        ":ts": ts,
        ":empty": [],
        ":entry": [{"action": _action(args.get("action")), "timestamp": ts}],
        **settled,
    }
    try:
        resp = _table.update_item(
            Key={"case_id": case_id},
            UpdateExpression="SET " + ", ".join(sets) + remove,
            # An update, never an upsert: an unknown id would be a case with no pack.
            # And never on a settled case, whose decision is recorded and whose SAP
            # write may be posted: a run must not move it on again.
            ConditionExpression="attribute_exists(case_id) AND NOT "
            f"(#st IN ({', '.join(settled)}))",
            ExpressionAttributeValues=values,
            ExpressionAttributeNames=names | {"#st": "status"},
            ReturnValues="ALL_NEW",
            ReturnValuesOnConditionCheckFailure="ALL_OLD",
        )
    except ClientError as error:
        if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        # Returned in AttributeValue form even through the resource API.
        status = (error.response.get("Item") or {}).get("status", {}).get("S")
        if status in SETTLED_STATUSES:
            return {
                "error": f"Not applied: case {case_id} is settled as {status!r}, "
                "and a settled case takes no further update. Put the disposition "
                "and the note in the update that settles a case."
            }
        return {"error": f"No case found for {case_id}"}
    reply = _text(resp["Attributes"])
    if refused:
        reply["content"].insert(
            0,
            {
                "type": "text",
                "text": f"Not applied: {', '.join(refused)}. Only "
                f"{', '.join(sorted(WRITABLE))} are writable through this tool; "
                "the rest of this update was applied.",
            },
        )
    return reply


def handler(event, context):
    # The Gateway sets the tool name; a plain invoke has none. This only turns
    # away a mistaken direct call: anyone allowed to invoke can set it, so the
    # Gateway role's invoke grant is the access control, not this.
    try:
        full = context.client_context.custom["bedrockAgentCoreToolName"]
    except (AttributeError, KeyError, TypeError):
        return {"error": "No tool name: call this through the AgentCore Gateway"}
    tool = full.split("___", 1)[-1]
    try:
        if tool == "get_case_state":
            return _get_case(event)
        if tool == "update_case_state":
            return _update_case(event)
        return {"error": f"Unknown tool: {tool}"}
    except Exception as e:  # a tool error, not a Lambda failure
        logger.exception("case management failed")
        return {"error": f"Case management error: {type(e).__name__}"}
