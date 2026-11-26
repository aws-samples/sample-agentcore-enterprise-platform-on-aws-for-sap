# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tell the platform ops topic about an audit-relevant change: a trigger-mode change (the
switch or a pack's mode), a `registry.json` write, a SOP written by anyone but its writer, or a change to the
SOP or registry bucket's policy.

Two sources. The deployment's trigger mode is an SSM parameter and a bucket policy is bucket
configuration; their writes are CloudTrail management events, which reach EventBridge
through the platform's trail or the account's organization trail. The registry and the SOPs are S3 objects, and object
writes are data events that neither trail logs, so those come from the buckets' own
event notifications, which carry the writer's role id and session name.

The message names the call, the resource and the principal, never a value.
"""

from __future__ import annotations

import os
from urllib.parse import unquote_plus

import boto3

TOPIC_ARN = os.environ.get("TOPIC_ARN", "")
SOPS_BUCKET = os.environ.get("SOPS_BUCKET", "")
#: `AWS:<role id>:` for the SOP bucket's own writers (the pack composer and the SOP
#: API, which is `sop_store`). A role id cannot be chosen, unlike a session name.
SOP_WRITERS = tuple(
    f"AWS:{r}:" for r in os.environ.get("SOP_WRITER_ROLE_IDS", "").split(",") if r
)

_sns = boto3.client("sns")


def from_cloudtrail(detail: dict) -> tuple[str, str]:
    req = detail.get("requestParameters") or {}
    if detail.get("eventSource") == "s3.amazonaws.com":
        who = (detail.get("userIdentity") or {}).get("arn", "unknown")
        return "pack bucket policy changed", "\n".join(
            [
                f"{detail.get('eventName')} on s3://{req.get('bucketName')}",
                f"by {who} at {detail.get('eventTime')}",
                "That policy is what limits who may write SOPs and the registry.",
            ]
        )
    names = req.get("names") or [
        req.get("name") or "a parameter CloudTrail did not name"
    ]
    who = (detail.get("userIdentity") or {}).get("arn", "unknown")
    pack = any("/autonomy/packs/" in str(n) for n in names)
    return (
        "pack trigger mode changed" if pack else "deployment trigger mode changed",
        "\n".join(
            [
                f"{detail.get('eventName')} on {', '.join(str(n) for n in names)}",
                f"by {who} at {detail.get('eventTime')}",
                "A flip from the console or ops.py also records the person beside the",
                "parameter: `deploy.sh usecase run business-operations trigger-mode get`.",
            ]
        ),
    )


def from_s3(record: dict) -> tuple[str, str] | None:
    """The alert for one S3 notification record, or None for a SOP writer's own write."""
    bucket = record["s3"]["bucket"]["name"]
    key = unquote_plus(record["s3"]["object"]["key"])
    who = (record.get("userIdentity") or {}).get("principalId", "unknown")
    if bucket == SOPS_BUCKET:
        if who.startswith(SOP_WRITERS):
            return None
        subject = "SOP written outside the pack composer"
    else:
        subject = "pack registry written"
    return subject, "\n".join(
        [
            f"{record.get('eventName')} on s3://{bucket}/{key}",
            f"by {who} at {record.get('eventTime')}",
        ]
    )


def alerts(event: dict) -> list[tuple[str, str]]:
    if "detail" in event:
        return [from_cloudtrail(event["detail"] or {})]
    found = (from_s3(r) for r in event.get("Records") or [] if "s3" in r)
    return [a for a in found if a]


def handler(event, _context):
    for subject, message in alerts(event):
        print(f"[AUDIT] {subject}: {message}")
        _sns.publish(
            TopicArn=TOPIC_ARN,
            Subject=f"business-operations: {subject}",
            Message=message,
        )
