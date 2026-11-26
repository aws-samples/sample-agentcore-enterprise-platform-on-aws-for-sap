# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""AWS wiring every producer of a case shares: the trigger modes, the case table, the queue.

The SAP poller, SAP push, the batch sweeper and the operator re-run all start work through
this module, so the gate and the message shape have one copy.
"""

from __future__ import annotations

import json
import os
import re

import boto3
import case_query
import emergency_stop
import registry
import test_data_records
from botocore.exceptions import ClientError

_ssm = boto3.client("ssm")
_sqs = boto3.client("sqs")
_dynamodb = boto3.resource("dynamodb")

QUEUE_URL = os.environ.get("AGENT_QUEUE_URL", "")
TABLE_NAME = os.environ.get("CASES_TABLE", "")
TRIGGER_MODE_PARAMETER = os.environ.get("TRIGGER_MODE_PARAMETER", "")
#: `<…>/autonomy/packs`: one pack's mode is `<prefix>/<pack_id>` (names.trigger_mode_parameters).
PACK_MODE_PREFIX = os.environ.get("PACK_MODE_PREFIX", "")
#: The seeder's records, set only with `test_data.enabled`: a case opened for a chain it
#: posted carries that record's mark.
TEST_DATA_TABLE = os.environ.get("TEST_DATA_TABLE", "")
#: `contracts.PACK_ID_PATTERN`: a pack id is safe as one parameter-name segment.
_PACK_ID = re.compile(r"[a-z][a-z0-9_]*")


def get_trigger_mode() -> str:
    """The deployment's trigger mode, read fresh on every call. Fails closed to `manual`.

    Uncached on purpose: setting `manual` has to stop the next enqueue, and a warm
    container that remembered `auto` would keep enqueueing until it was recycled.
    An absent parameter is `manual`, which is the default (the poller has no first-run
    baseline, so `auto` on a first deploy would queue every open exception).
    """
    try:
        return _ssm.get_parameter(Name=TRIGGER_MODE_PARAMETER)["Parameter"]["Value"]
    except Exception:  # noqa: BLE001 - absent or unreadable is manual
        return "manual"


def get_pack_mode(pack_id: str) -> str:
    """One pack's trigger mode, read fresh. Fails closed to `manual`, and an absent
    parameter is `manual`, so a pack nobody has set, a new one included, never runs
    unattended."""
    if not PACK_MODE_PREFIX or not _PACK_ID.fullmatch(str(pack_id or "")):
        return "manual"
    try:
        return _ssm.get_parameter(Name=f"{PACK_MODE_PREFIX}/{pack_id}")["Parameter"][
            "Value"
        ]
    except Exception:  # noqa: BLE001 - absent or unreadable is manual
        return "manual"


class Gate:
    """One invocation's reading of the trigger modes.

    A case is queued unattended only when the deployment's trigger mode and its pack's mode
    both read `auto`. The deployment's trigger mode is read once, when the gate is made; each
    pack's mode once, the first time a case of that pack asks. Containment and break
    glass set the deployment's trigger mode, so either stops every pack at once.
    """

    def __init__(self) -> None:
        self.deployment = get_trigger_mode()
        self._packs: dict[str, str] = {}
        # Read once per invocation, like the switch: while a stop stands nothing queues.
        self.stop = emergency_stop.standing()
        self._said = False

    def pack_mode(self, pack_id: str) -> str:
        if pack_id not in self._packs:
            self._packs[pack_id] = get_pack_mode(pack_id)
        return self._packs[pack_id]

    def allows(self, pack_id: str) -> bool:
        if self.stop:
            if not self._said:
                print(f"[STOP] queueing nothing: {emergency_stop.refusal(self.stop)}")
                self._said = True
            return False
        return self.deployment == "auto" and self.pack_mode(pack_id) == "auto"


def get_cases_table():
    return _dynamodb.Table(TABLE_NAME)


def case_exists(table, case_id: str) -> bool:
    return "Item" in table.get_item(
        Key={"case_id": case_id}, ProjectionExpression="case_id"
    )


def test_data_mark(case_id: str) -> dict | None:
    """The `test_data` mark for a case the seeder posted, or None.

    A read that fails opens the case unmarked rather than not at all: the seeder copies
    the mark on when it settles, and the page lists the post either way.
    """
    if not TEST_DATA_TABLE:
        return None
    try:
        record = test_data_records.for_case(_dynamodb.Table(TEST_DATA_TABLE), case_id)
    except Exception as e:  # noqa: BLE001 - logged; the case still opens
        print(f"[TEST DATA] could not read the seed record for {case_id}: {e}")
        return None
    return test_data_records.mark(record) if record else None


def put_case(table, case_item: dict) -> bool:
    """Conditional put. False when the case already exists; any other error raises.

    A case the seeder's chain made carries its record's `test_data` mark, copied here and
    never taken from the item a producer built. Every case carries `amount_sort`, the
    pack's amount the Cases table sorts by (`case_query.amount_sort`); the pack's fields
    come from the registry the poller already read.
    """
    case_item = {k: v for k, v in case_item.items() if k != "test_data"}
    fields = (registry.packs().get(case_item.get("pack_id")) or {}).get("fields")
    case_item[case_query.AMOUNT_SORT] = case_query.amount_sort(
        case_item.get("attributes"), fields
    )
    if mark := test_data_mark(case_item.get("case_id", "")):
        case_item["test_data"] = mark
    try:
        table.put_item(
            Item=case_item, ConditionExpression="attribute_not_exists(case_id)"
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def send(
    case_id: str, trigger: str, payload: dict | None = None, dedup_id: str = ""
) -> str:
    """Put one run on the agent queue, ungated; returns its message id. The FIFO group
    is the case. Without `dedup_id` the queue dedupes on content for five minutes."""
    body = {"case_id": case_id, "trigger": trigger}
    if payload:
        body["payload"] = payload
    extra = {"MessageDeduplicationId": dedup_id} if dedup_id else {}
    return _sqs.send_message(
        QueueUrl=QUEUE_URL,
        MessageBody=json.dumps(body),
        MessageGroupId=case_id,
        **extra,
    )["MessageId"]


def enqueue(case_id: str, pack_id: str, trigger: str, gate: Gate | None = None) -> bool:
    """Enqueue an unattended run if the deployment's trigger mode and the pack both say `auto`.
    True if sent.

    `gate` is the switches as the caller read them for its whole invocation; without
    it they are read here, per case.
    """
    if not QUEUE_URL or not (gate or Gate()).allows(pack_id):
        return False
    send(case_id, trigger)
    return True
