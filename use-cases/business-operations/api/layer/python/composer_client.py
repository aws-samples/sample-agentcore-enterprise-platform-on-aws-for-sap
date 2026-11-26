# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Invoking `pack_composer`, the only writer of the pack corpus and the SOP bucket.

The composer runs one write at a time (reserved concurrency 1), so an invoke that
overlaps another write is throttled before the composer runs. A throttle is retried with
backoff: nothing ran, so the retry cannot apply anything twice. Any other failure is not
retried, because a write that timed out may still have happened.
"""

from __future__ import annotations

import json
import os
import time

import boto3
from api_http import HttpError
from botocore.exceptions import ClientError

#: Seconds before each retry of a throttled invoke from a route. A save takes a few
#: seconds; the route's own timeout bounds the total.
ROUTE_BACKOFF = (0.5, 1.0, 2.0)


def invoke(
    payload: dict, backoff=ROUTE_BACKOFF, function: str | None = None
) -> tuple[dict, bool] | None:
    """`(answer, failed)`: the composer's answer and whether its handler raised. None
    when it stayed busy through every retry."""
    client = boto3.client("lambda")
    name = function or os.environ["PACK_COMPOSER_FUNCTION"]
    for delay in (*backoff, None):
        try:
            out = client.invoke(
                FunctionName=name,
                InvocationType="RequestResponse",
                Payload=json.dumps(payload).encode(),
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "TooManyRequestsException":
                raise
            if delay is None:
                return None
            time.sleep(delay)
            continue
        return json.loads(out["Payload"].read() or "{}"), bool(out.get("FunctionError"))


def call(payload: dict) -> dict:
    """A route's invoke: the composer's answer, or the `HttpError` a browser can read."""
    got = invoke(payload)
    if got is None:
        raise HttpError(
            409,
            "Another publish is in progress",
            detail="Nothing was written. Try again in a few seconds.",
        )
    answer, failed = got
    # A handler exception arrives as a 200 with `FunctionError`; without this it would
    # read to the client as a successful write with a strange body.
    if failed:
        raise HttpError(
            502,
            "The composer failed",
            detail="Nothing was written. See the pack-composer log group.",
        )
    return answer
