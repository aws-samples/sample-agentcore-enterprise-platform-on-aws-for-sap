# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Agent Knowledge Gateway Tool Lambda

Read-only evidence for the agent:
  get_precedent      — what happened on comparable cases, with case_id + sop_version
  check_vendor_risk  — vendor relationship paths up to 3 hops

Evidence, never instructions: the SOP decides, these tools report. There is no
write tool, so the agent cannot author its own precedent mid-case. Rows come
from the exemplar builder (precedents, and lessons from pack authors' ratings)
and from the operator's `knowledge-load`.
"""

import json
import logging
import os

import boto3
import queries

logger = logging.getLogger()
logger.setLevel(logging.INFO)

CLUSTER_ARN = os.environ["CLUSTER_ARN"]
SECRET_ARN = os.environ["SECRET_ARN"]
DATABASE_NAME = os.environ["DATABASE_NAME"]

_data_client = None


def _data():
    global _data_client
    if _data_client is None:
        _data_client = boto3.client("rds-data")
    return _data_client


def _query(sql: str, params: dict) -> list[dict]:
    """Run one read. Named parameters only — agent input never enters SQL text."""
    # 5 tries 4 s apart fits a resume inside the function's 30 s timeout.
    resp = queries.execute(
        _data(),
        attempts=5,
        wait=4,
        resourceArn=CLUSTER_ARN,
        secretArn=SECRET_ARN,
        database=DATABASE_NAME,
        sql=sql,
        parameters=[
            {"name": k, "value": {"stringValue": str(v)}} for k, v in params.items()
        ],
        formatRecordsAs="JSON",
    )
    return json.loads(resp.get("formattedRecords") or "[]")


def _get_precedent(event: dict) -> dict:
    process_type = event.get("process_type")
    if not process_type:
        return {"error": "process_type is required"}

    supplier_number = event.get("supplier_number") or ""
    band = queries.amount_band(event.get("amount") or 0)
    params = {
        "process_type": process_type,
        "supplier_number": supplier_number,
        "amount_band": band,
    }

    precedents = queries.shape_precedent(_query(queries.PRECEDENT_SQL, params))
    lessons = _query(
        queries.LESSON_SQL,
        {"process_type": process_type, "supplier_number": supplier_number},
    )

    return {
        "precedents": precedents,
        "lessons": lessons,
        "amount_band": band,
        "note": (
            "Evidence only. The SOP governs the disposition — cite case_id and "
            "sop_version if a precedent informs your reasoning."
        ),
    }


def _check_vendor_risk(event: dict) -> dict:
    supplier_number = event.get("supplier_number")
    if not supplier_number:
        return {"error": "supplier_number is required"}

    rows = queries.shape_vendor_risk(
        _query(queries.VENDOR_RISK_SQL, {"supplier_number": supplier_number})
    )
    return {
        "checked": supplier_number,
        "related_vendors": rows,
        "max_depth": queries.MAX_DEPTH,
        "note": (
            "Each entry carries the traversal path — cite the chain rather than "
            "asserting a relationship. An empty list means no recorded edges, "
            "not an absence of risk."
        ),
    }


def handler(event, context):
    # The Gateway sets the tool name; a plain invoke has none. This turns away a
    # mistaken direct call only: the Gateway role's invoke grant is the control.
    try:
        full = context.client_context.custom["bedrockAgentCoreToolName"]
    except (AttributeError, KeyError, TypeError):
        return {"error": "No tool name: call this through the AgentCore Gateway"}
    tool = full.split("___", 1)[-1]
    logger.info("tool=%s args=%s", tool, json.dumps(sorted(event)))
    try:
        if tool == "get_precedent":
            result = _get_precedent(event)
        elif tool == "check_vendor_risk":
            result = _check_vendor_risk(event)
        else:
            return {"error": f"Unknown tool: {tool}"}
        if "error" in result:
            return result
        return {"content": [{"type": "text", "text": json.dumps(result)}]}
    except Exception as e:  # a tool error, not a Lambda failure
        logger.exception("agent knowledge lookup failed")
        return {"error": f"Agent knowledge lookup failed: {type(e).__name__}"}
