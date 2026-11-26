# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Automatic containment: an anomaly alarm went to ALARM, so set the trigger mode to `manual`.

Invoked by the containment rule (business_operations/containment.py) on the alarm's
state change. It only ever writes `manual`, so it can narrow what the agent does and
never widen it. It does not fight a person: a switch someone wrote after the alarm
fired is left alone, and an operator who sets `auto` again keeps it until the next
alarm, because a rule fires on a state change and an alarm that stays in ALARM sends
no second one. A redelivered event finds `manual` and does nothing.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import boto3

TOPIC_ARN = os.environ.get("TOPIC_ARN", "")
PARAMETER = os.environ.get("TRIGGER_MODE_PARAMETER", "")
#: The record `trigger-mode get` and GET /deployment/trigger-mode read beside the mode (ops.py, the route).
ACTOR_SUFFIX = "-last-changed-by"
ACTOR = "automatic containment"

_ssm = boto3.client("ssm")
_sns = boto3.client("sns")


def contain(event: dict) -> dict:
    """What containment did for one alarm event: `{"contained": bool, "why": str}`."""
    detail = event.get("detail") or {}
    if (detail.get("state") or {}).get("value") != "ALARM":
        return {"contained": False, "why": "not an ALARM transition"}
    alarm = detail.get("alarmName") or "an alarm"
    # The alarm's own state timestamp carries milliseconds; the event's `time` is cut
    # to the second, so an operator change in that same second would read as later.
    fired = datetime.fromisoformat(
        (detail.get("state") or {}).get("timestamp") or event["time"]
    )
    try:
        current = _ssm.get_parameter(Name=PARAMETER)["Parameter"]
    except _ssm.exceptions.ParameterNotFound:
        current = None
    # Absent is manual: every reader fails closed to it (pipeline/case_store.py).
    if current is None or current["Value"] == "manual":
        return {"contained": False, "why": "the trigger mode is already manual"}
    if current["LastModifiedDate"] > fired:
        return {
            "contained": False,
            "why": f"the trigger mode was set to {current['Value']} after {alarm} fired",
        }
    _ssm.put_parameter(
        Name=PARAMETER,
        Value="manual",
        Type="String",
        Overwrite=True,
        Description=f"{ACTOR}: {alarm}"[:1024],
    )
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        _ssm.put_parameter(
            Name=PARAMETER + ACTOR_SUFFIX,
            Value=f"manual set by {ACTOR} ({alarm}) at {stamp}",
            Type="String",
            Overwrite=True,
        )
    except Exception as e:  # noqa: BLE001 - the flip happened; only its record did not
        print(f"[CONTAINMENT] could not record the change: {e}")
    reason = (detail.get("state") or {}).get("reason") or ""
    _sns.publish(
        TopicArn=TOPIC_ARN,
        Subject="business-operations: contained, trigger-mode set to manual",
        Message="\n".join(
            [
                f"{alarm} went to ALARM at {event['time']}. {reason}".strip(),
                f"{PARAMETER} was {current['Value']}; containment set it to manual.",
                "New SAP exceptions now wait in `detected` for a person. Runs already",
                "queued or under way, and runs a person starts, are not stopped: for",
                "those, follow docs/operating/break-glass.md.",
                "To resume once you have looked: deploy.sh usecase run",
                "business-operations trigger-mode set auto. Containment acts again only",
                "on the next alarm.",
            ]
        ),
    )
    return {"contained": True, "why": f"{alarm}: {current['Value']} -> manual"}


def handler(event, _context):
    result = contain(event)
    print(f"[CONTAINMENT] {result}")
    return result
