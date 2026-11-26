# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tell the platform ops topic that someone other than CloudFormation wrote under layers/sap/.

Invoked by the layer's EventBridge rule on the CloudTrail event. The message names the
call, the parameter and the principal, never a value.
"""

import os

import boto3

_sns = boto3.client("sns")


def message(detail: dict) -> str:
    req = detail.get("requestParameters") or {}
    # CloudTrail may omit requestParameters; say so rather than print "None".
    names = req.get("names") or [
        req.get("name") or "a parameter CloudTrail did not name"
    ]
    who = (detail.get("userIdentity") or {}).get("arn", "unknown")
    return "\n".join(
        [
            f"{detail.get('eventName')} on {', '.join(str(n) for n in names)}",
            f"by {who} at {detail.get('eventTime')}",
            "The SAP layer's check trusts these parameters only from CloudFormation, so it",
            "now refuses this one. Restore it with: deploy.sh layer run sap rewrite",
            "Then find out who wrote it, and why.",
        ]
    )


def handler(event, context):
    _sns.publish(
        TopicArn=os.environ["TOPIC_ARN"],
        Subject="SAP layer: parameter written outside CloudFormation",
        Message=message(event.get("detail") or {}),
    )
