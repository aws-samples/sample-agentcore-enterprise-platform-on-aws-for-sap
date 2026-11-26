# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""demo_create_ticket and demo_get_ticket: the agent's side of the SAMPLE ticketing.

A stand-in for a real ITSM (ServiceNow, Jira), there to get started: replace it
before production. The tickets
REST API (api/tickets) is the human's side of the same table.
"""

import json
import logging
import os

import boto3
from persist_mask import mask
from ticket_record import new_ticket

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_dynamodb = boto3.resource("dynamodb")


def _text(value) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(value, default=str)}]}


def _create_ticket(event: dict) -> dict:
    title, description = mask([event.get("title"), event.get("description")])
    try:
        ticket = new_ticket(
            title,
            created_by="agent",
            description=description,
            priority=event.get("priority"),
            assigned_to=event.get("assigned_to"),
            case_id=event.get("case_id"),
            category=event.get("category"),
            response_type=event.get("response_type"),
        )
    except ValueError as e:
        return {"error": str(e)}
    case_id = ticket["case_id"]
    _dynamodb.Table(os.environ["TICKETS_TABLE"]).put_item(Item=ticket)

    # The case records its ticket, so a decision on the ticket finds its case, and the
    # ticket's kind, so the case list tells an approval from a question.
    if case_id:
        try:
            _dynamodb.Table(os.environ["CASES_TABLE"]).update_item(
                Key={"case_id": case_id},
                UpdateExpression="SET ticket_id = :tid, ticket_response_type = :rt",
                ExpressionAttributeValues={
                    ":tid": ticket["ticket_id"],
                    ":rt": ticket["response_type"],
                },
                ConditionExpression="attribute_exists(case_id)",
            )
        except Exception:  # the ticket stands; only the back-link is lost
            logger.warning("Could not record ticket on case %s", case_id, exc_info=True)
    return _text(ticket)


def _get_ticket(event: dict) -> dict:
    ticket_id = event.get("ticket_id") or ""
    if not ticket_id:
        return {"error": "ticket_id is required"}
    item = (
        _dynamodb.Table(os.environ["TICKETS_TABLE"])
        .get_item(Key={"ticket_id": ticket_id}, ConsistentRead=True)
        .get("Item")
    )
    if not item:
        return {"content": [{"type": "text", "text": f"No ticket found: {ticket_id}"}]}
    return _text(item)


TOOLS = {"demo_create_ticket": _create_ticket, "demo_get_ticket": _get_ticket}


def handler(event, context):
    # The Gateway sets the tool name; the Gateway role's invoke grant is the
    # access control, not this.
    try:
        full = context.client_context.custom["bedrockAgentCoreToolName"]
    except (AttributeError, KeyError, TypeError):
        return {"error": "No tool name: call this through the AgentCore Gateway"}
    tool = TOOLS.get(full.split("___", 1)[-1])
    if tool is None:
        return {"error": f"Unknown tool: {full}"}
    try:
        return tool(event)
    except Exception as e:
        logger.exception("ticket tool failed")
        return {"error": f"Ticket management error: {type(e).__name__}"}
