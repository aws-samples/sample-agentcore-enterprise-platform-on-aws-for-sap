# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The `load_sop` Gateway tool: the whole focused SOP for a classified process type.

It reads the SOP bucket and the published registry, never a knowledge base, so it is
served whether or not SOP search is deployed. Which process types a call may name is
the interceptor check's decision (the run grant's packs), made before this runs.

`registry.py` and `config_overrides.py` here are symlinks to the agent's copies: the
prompt and a mid-run reload resolve the same mapping and state the same tolerances.

The reply's first line names the document and its S3 VersionId, so the run's evidence
records exactly which bytes it was given (the bucket denies version deletes).
"""

import logging
import os

import boto3
import config_overrides
import registry

logger = logging.getLogger()
logger.setLevel(logging.INFO)

BUCKET = os.environ.get("SOPS_BUCKET", "")
_TEXT = (".txt", ".md")
_s3 = None


def _client():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


def header(key: str, version_id: str | None) -> str:
    """The first line of a reply; agent/utils/evidence.py parses it."""
    return f"[sop {key} version {version_id or 'none'}]"


def candidates(key: str) -> list[str]:
    """The mapped key, then the other text extension, as the agent tries them."""
    stem, ext = os.path.splitext(key)
    if ext not in _TEXT:
        return []
    return [key, *(stem + alt for alt in _TEXT if alt != ext)]


def load_sop(process_type: str) -> dict:
    # Per call: an analyst's publish is honoured at once, and load_sop is rare.
    registry.refresh_for_request()
    index = registry.sop_index()
    pack_id, entry = next(
        ((p, e) for p, e in index.items() if process_type in e["sops"]), (None, None)
    )
    if entry is None:
        known = sorted({pt for e in index.values() for pt in e["sops"]})
        return {"error": f"Unknown process_type {process_type!r}. Known: {known}"}
    key = entry["sops"][process_type]
    s3 = _client()
    for candidate in candidates(key):
        try:
            obj = s3.get_object(Bucket=BUCKET, Key=candidate)
        except s3.exceptions.NoSuchKey:
            continue
        try:
            text = obj["Body"].read().decode("utf-8")
        except UnicodeDecodeError:
            return {"error": f"SOP {candidate} is not UTF-8 text"}
        text = config_overrides.substitute(
            text, config_overrides.deployed_contacts(), entry["constants"], pack_id
        )
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"{header(candidate, obj.get('VersionId'))}\n{text}",
                }
            ]
        }
    # Never an empty success: that reads as permission to improvise.
    return {"error": f"SOP for process_type {process_type!r} not found ({key})"}


def handler(event, context):
    # The Gateway sets the tool name; a plain invoke has none. This turns away a
    # mistaken direct call only: the Gateway role's invoke grant is the control.
    try:
        full = context.client_context.custom["bedrockAgentCoreToolName"]
    except (AttributeError, KeyError, TypeError):
        return {"error": "No tool name: call this through the AgentCore Gateway"}
    tool = full.split("___", 1)[-1]
    if tool != "load_sop":
        return {"error": f"Unknown tool: {tool}"}
    process_type = str(event.get("process_type") or "").strip()
    if not process_type:
        return {"error": "process_type is required"}
    try:
        return load_sop(process_type)
    except registry.RegistryUnavailable:
        logger.exception("registry unreadable")
        return {"error": "The published packs could not be read; try again shortly"}
    except Exception as e:  # a tool error, not a Lambda failure
        logger.exception("load_sop failed")
        return {"error": f"load_sop failed: {type(e).__name__}"}
