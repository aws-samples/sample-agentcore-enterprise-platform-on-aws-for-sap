# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Knowledge search Gateway tool: search_sap_sops and search_sap_api_docs.

`load_sop` is not here: it
reads the SOP bucket, not a knowledge base, so it is the `business-operations-sops`
target's (sop_tools/) and stays available when the knowledge bases are off.

search_sap_sops is scoped to one pack in the query itself: every SOP in a pack
folder has a `<key>.metadata.json` sidecar naming its `pack_id`, which ingestion
indexes as filterable metadata. The interceptor check refuses a `pack_id` outside
the run's grant before this runs.

A SOP hit comes back with its `{{CONTACT_*}}` and `{{SYMBOL}}` values filled exactly
as `load_sop` fills them (`config_overrides.substitute`, from the API layer): a raw
placeholder is what the model copies into a ticket's assignee.
"""

import json
import logging
import os

import boto3
import config_overrides
import registry

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Set only for the knowledge bases this deployment turned on.
KB_IDS = {
    "search_sap_sops": os.environ.get("SOPS_KB_ID", ""),
    "search_sap_api_docs": os.environ.get("API_DOCS_KB_ID", ""),
}
# The SOPs knowledge base holds one vector per SOP (no chunking), so a hit is a
# whole SOP: two is already more text than the injected one. API docs are
# chunked normally and keep a wider window.
MAX_RESULTS = {"search_sap_sops": 2, "search_sap_api_docs": 5}

_client = None


def _bedrock():
    global _client
    if _client is None:
        _client = boto3.client("bedrock-agent-runtime")
    return _client


def shape(results: list[dict], max_results: int) -> str:
    """Retrieved chunks to tool text, each headed by its source URI."""
    kept = []
    for r in results[:max_results]:
        text = (r.get("content") or {}).get("text")
        if not text:
            continue
        uri = ((r.get("location") or {}).get("s3Location") or {}).get("uri") or ""
        # The URI lets a quoted clause be traced back to the document.
        kept.append(f"[{uri}]\n{text}" if uri else text)
    return "\n\n".join(kept) if kept else "No relevant results found."


def resolve(text: str, pack_id: str) -> str:
    """The SOP text as `load_sop` serves it: contacts and the pack's constants filled,
    operator overrides winning."""
    registry.refresh_for_request()
    constants = registry.constants().get(pack_id) or {}
    return config_overrides.substitute(
        text, config_overrides.deployed_contacts(), constants, pack_id
    )


def _search(tool: str, event: dict) -> dict:
    kb_id = KB_IDS.get(tool)
    if not kb_id:
        return {"error": f"Unknown tool: {tool}"}
    query = (event.get("query") or "").strip()
    if not query:
        return {"error": "query is required"}
    wanted = MAX_RESULTS[tool]
    search = {"numberOfResults": wanted}
    if tool == "search_sap_sops":
        pack_id = event.get("pack_id")
        if not isinstance(pack_id, str) or not pack_id or "/" in pack_id:
            return {"error": "pack_id is required: the pack whose SOPs to search"}
        search["filter"] = {"equals": {"key": "pack_id", "value": pack_id}}
    resp = _bedrock().retrieve(
        knowledgeBaseId=kb_id,
        retrievalQuery={"text": query},
        retrievalConfiguration={"vectorSearchConfiguration": search},
    )
    text = shape(resp.get("retrievalResults", []), wanted)
    if tool == "search_sap_sops":
        text = resolve(text, event["pack_id"])
    return {"content": [{"type": "text", "text": text}]}


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
        return _search(tool, event)
    except registry.RegistryUnavailable:
        # Never the raw text: an unfilled placeholder reads as a real assignee.
        logger.exception("registry unreadable")
        return {"error": "The published packs could not be read; try again shortly"}
    except Exception as e:  # a tool error, not a Lambda failure
        logger.exception("knowledge search failed")
        return {"error": f"Knowledge search failed: {type(e).__name__}"}
