# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The test data table: one record per seed, written only by the seeder.

Keyed `pack_id` + `post_id` (`<UTC second>#<8 hex>`, so a pack's posts sort by time),
with a `case_id` index for the case producers. The seeder writes a record before its
first SAP call and settles it after its last (`test_data/index.py`). The case's
`test_data` mark is copied from it, by whoever gets there second: the producer that
opens the case (`pipeline/case_store.put_case`) or the seeder when it settles. Neither
copy takes a value from anyone else, so the mark says what the seeder really posted.
"""

from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime

from boto3.dynamodb.conditions import Key

#: The table's `case_id` index (business_operations/test_data.py builds it).
CASE_INDEX = "by-case"
#: What a case carries of its record: enough to show and grade it, nothing about SAP.
MARK_FIELDS = ("post_id", "posted_at", "posted_by", "preset", "inputs", "expected")
#: The most posts the page lists.
PAGE = 50
#: `<UTC second>#<8 hex>`: sorts by time inside a pack, and is unique per seed.
POST_ID_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ#[0-9a-f]{8}$")


def new_post_id(now: datetime | None = None) -> str:
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{stamp}#{secrets.token_hex(4)}"


def mark(record: dict) -> dict:
    """The `test_data` attribute a case carries for this record."""
    return {k: record[k] for k in MARK_FIELDS if k in record}


def for_case(table, case_id: str) -> dict | None:
    """The newest record whose chain made `case_id`, or None."""
    items = table.query(
        IndexName=CASE_INDEX, KeyConditionExpression=Key("case_id").eq(case_id)
    ).get("Items", [])
    return max(items, key=lambda i: i.get("post_id", "")) if items else None


def for_pack(table, pack_id: str, limit: int = PAGE) -> list[dict]:
    """A pack's newest posts first, at most `limit`."""
    return table.query(
        KeyConditionExpression=Key("pack_id").eq(pack_id),
        ScanIndexForward=False,
        Limit=limit,
    ).get("Items", [])
