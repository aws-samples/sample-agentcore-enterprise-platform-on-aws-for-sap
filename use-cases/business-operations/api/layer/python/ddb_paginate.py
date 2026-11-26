# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Read every page of a DynamoDB ``query`` or ``scan``, not just the first.

DynamoDB caps a single response at 1 MB and reports the rest with
``LastEvaluatedKey``. A caller that ignores it gets a *silently* short answer:
no error, no warning, just a page of results that looks like the whole table.
On the read paths this repo has, that reads as a quiet day — a dashboard missing
the cases past the cap, an exemplar corpus built from a fraction of what
completed — which is worse than a failure, because nothing signals it.

Four callers across four Lambdas needed the same loop (``api/cases`` twice,
``workers/exemplar_builder``, ``demo/tickets``), so it lives here rather than in
each of them: this layer is already on all four, and one of them had written the
loop correctly three lines from where it had not.

Deliberately unbounded. Every caller here is reading a working set an operator is
about to look at, and truncating it is the bug being fixed; a cap would just move
the silent-short-answer problem into this function. If a table ever grows past
what a Lambda can hold, the fix is a query the caller can bound (a key condition,
a `Limit` with a cursor the client passes back), not a ceiling hidden in here.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def paginate(op: Callable[..., dict], **kwargs: Any) -> list[dict]:
    """Every ``Items`` entry from ``op`` (a bound ``table.query`` / ``table.scan``).

    ``kwargs`` are passed through unchanged on the first call and reused for each
    subsequent page with ``ExclusiveStartKey`` set, which is exactly how DynamoDB
    expects a continued read to be issued.
    """
    items: list[dict] = []
    while True:
        resp = op(**kwargs)
        items.extend(resp.get("Items", []))
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            return items
        kwargs["ExclusiveStartKey"] = last_key
