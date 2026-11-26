# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The HTTP envelope every console API handler shares.

Not `http.py`: the layer's directory is on a Lambda's path ahead of the standard
library, so that name would shadow `http.client` for urllib and botocore.

`@route` is the one order a request goes through: re-read the registry if it changed,
`entitlement.admit`, the handler, and then whatever it raised. Every answer carries the
console's CORS header, so a refusal, a 503 or a crash reaches the browser as a status it
can read rather than as "Failed to fetch":

* :class:`HttpError` (and :func:`json_body` / :func:`field`, which raise it) is the
  status and body it names. A typed field the caller got wrong is a 400, never a 500.
* `registry.RegistryUnavailable` is a 503 with no detail, plus the metric the alarm
  watches: an unreadable registry never resolves to an open entitlement floor.
* Anything else is a 500 with no detail. The log gets the class, the AWS error code and
  the stack, never the message, which can carry a header value or a token.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import sys
import time
import traceback
from decimal import Decimal

import registry
from entitlement import admit

logger = logging.getLogger(__name__)

REGISTRY_UNAVAILABLE_BODY = {"error": "Service temporarily unavailable"}
INTERNAL_ERROR_BODY = {"error": "Internal server error"}


class HttpError(Exception):
    """Answer with this status and body."""

    def __init__(self, status: int, error: str, **extra):
        super().__init__(error)
        self.status = status
        self.body = {"error": error, **extra}


def origin(event: dict) -> str:
    headers = event.get("headers") or {}
    return next((v for k, v in headers.items() if k.lower() == "origin"), "") or ""


def cors(event: dict) -> dict[str, str]:
    """The request's origin, echoed only when the deployment allowlists it.

    Read per call and fail-shut: unset or empty allows nothing, and an absent Origin is
    never matched by the empty entry `"".split(",")` would give.
    """
    allowed = {o.strip() for o in os.environ.get("CORS_ALLOWED_ORIGINS", "").split(",")}
    allowed.discard("")
    asked = origin(event)
    return (
        {"Access-Control-Allow-Origin": asked, "Vary": "Origin"}
        if asked in allowed
        else {}
    )


def _default(value):
    if isinstance(value, Decimal):
        return int(value) if value == int(value) else float(value)
    return str(value)  # datetimes off S3 and DynamoDB sets render as text


def respond(status: int, body: object, event: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", **cors(event)},
        "body": json.dumps(body, default=_default),
    }


def json_body(event: dict) -> dict:
    """The request body as a JSON object, or a 400."""
    try:
        body = json.loads(event.get("body") or "{}")
    except (TypeError, ValueError):
        raise HttpError(400, "Body is not valid JSON") from None
    if not isinstance(body, dict):
        raise HttpError(400, "Body must be a JSON object")
    return body


_KIND_NAMES = {
    str: "a string",
    bool: "true or false",
    dict: "an object",
    list: "a list",
}


def field(body: dict, name: str, kind: type = str, *, required: bool = False,
          max_len: int | None = None):  # fmt: skip
    """`body[name]`, checked to be `kind`, or a 400. None when absent and not required."""
    value = body.get(name)
    if value is None:
        if required:
            raise HttpError(400, f"`{name}` is required")
        return None
    ok = isinstance(value, kind) and not (kind is int and isinstance(value, bool))
    if not ok:
        raise HttpError(400, f"`{name}` must be {_KIND_NAMES.get(kind, kind.__name__)}")
    if max_len is not None and len(value) > max_len:
        raise HttpError(400, f"`{name}` must be {max_len} characters or fewer")
    return value


def _registry_unavailable_metric() -> None:
    """An Embedded Metric Format `RegistryUnavailable` count, which the CDK alarm reads."""
    print(
        json.dumps(
            {
                "_aws": {
                    "Timestamp": int(time.time() * 1000),
                    "CloudWatchMetrics": [
                        {
                            "Namespace": os.environ.get(
                                "METRICS_NAMESPACE", "BusinessOperations"
                            ),
                            "Dimensions": [["Stack"]],
                            "Metrics": [
                                {"Name": "RegistryUnavailable", "Unit": "Count"}
                            ],
                        }
                    ],
                },
                "Stack": os.environ.get("STACK_NAME", "unknown"),
                "RegistryUnavailable": 1,
            }
        ),
        file=sys.stdout,
        flush=True,
    )


def route(fn=None, *, refresh: bool = True):
    """Wrap a handler `fn(event) -> response` in the shared order. `refresh=False` for a
    route that never reads the registry."""
    if fn is None:
        return functools.partial(route, refresh=refresh)

    @functools.wraps(fn)
    def handler(event: dict, _context=None) -> dict:
        try:
            if refresh:
                registry.refresh_for_request()
            refusal = admit(event)
            if refusal:
                return respond(403, refusal, event)
            return fn(event)
        except HttpError as error:
            return respond(error.status, error.body, event)
        except registry.RegistryUnavailable:
            logger.exception("Registry unavailable")
            try:
                _registry_unavailable_metric()
            except Exception:  # noqa: BLE001, S110 - telemetry must not turn a 503 into a 500
                pass
            return respond(503, REGISTRY_UNAVAILABLE_BODY, event)
        except Exception as error:  # noqa: BLE001 - logged, never echoed
            response = getattr(error, "response", None)
            detail = response.get("Error") if isinstance(response, dict) else None
            code = detail.get("Code", "") if isinstance(detail, dict) else ""
            logger.error(
                "Unhandled %s %s in %s %s\n%s",
                type(error).__name__,
                code,
                event.get("httpMethod", ""),
                event.get("resource", ""),
                "".join(traceback.format_tb(error.__traceback__)),
            )
            return respond(500, INTERNAL_ERROR_BODY, event)

    return handler
