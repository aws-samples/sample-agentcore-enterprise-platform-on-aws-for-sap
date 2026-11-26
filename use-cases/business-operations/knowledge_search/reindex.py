# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The SOP search's follow-up ingestion, run every five minutes.

Amazon Bedrock runs one ingestion job per data source at a time, so a SOP write that finds one
running cannot start its own; it sets the needs-reindex flag, an SSM parameter, instead
(`sop_store.start_ingestion`). Amazon Bedrock emits no EventBridge event when an ingestion job
ends, so this polls. With the flag clear it reads one parameter and stops.

With the flag set it clears it, then starts a job. Clearing first is what loses no
write: a write that sets the flag again after the clear is either seen by this job or
flagged for the next. A job still running refuses the start, and the flag goes back
for the next tick, so a finished job gets at most one follow-up.
"""

from __future__ import annotations

import os

import boto3
from botocore.exceptions import ClientError

FLAG = os.environ.get("REINDEX_FLAG_PARAM", "")
KB_ID = os.environ.get("SOPS_KB_ID", "")
DATA_SOURCE_ID = os.environ.get("SOPS_DATA_SOURCE_ID", "")

ssm = boto3.client("ssm")
bedrock = boto3.client("bedrock-agent")


def _code(exc: ClientError) -> str:
    return exc.response.get("Error", {}).get("Code", "")


def _flag(value: str) -> None:
    ssm.put_parameter(Name=FLAG, Value=value, Type="String", Overwrite=True)


def handler(event=None, context=None) -> dict:
    try:
        since = ssm.get_parameter(Name=FLAG)["Parameter"]["Value"]
    except ClientError as exc:
        if _code(exc) == "ParameterNotFound":
            return {"status": "IDLE"}
        raise
    try:
        ssm.delete_parameter(Name=FLAG)
    except ClientError as exc:
        if _code(exc) != "ParameterNotFound":
            raise
    try:
        job = bedrock.start_ingestion_job(
            knowledgeBaseId=KB_ID,
            dataSourceId=DATA_SOURCE_ID,
            description=f"follow-up for SOP writes since {since}"[:200],
        )["ingestionJob"]
    except ClientError as exc:
        _flag(since)
        if _code(exc) == "ConflictException":
            return {"status": "WAITING"}
        raise
    return {"status": job["status"], "job_id": job["ingestionJobId"]}
