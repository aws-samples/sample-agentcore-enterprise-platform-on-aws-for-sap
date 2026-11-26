# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""A fixture use case's run checks: one gated tool, on `sap_kit/run_grant.py`.

The least a use case that starts runs writes on top of the library. The test sets
`TABLE`, its grant table.
"""

from __future__ import annotations

import time

from . import run_grant

GATED = "vendor-lookup-tools___hold_payment"
GRANT_HEADER = run_grant.grant_header("vendor-lookup")
TABLE = None


def check(body: dict, headers: dict | None) -> str | None:
    if (body.get("params") or {}).get("name") != GATED:
        return None
    run_id = run_grant.header(headers, run_grant.RUN_ID_HEADER)
    item = run_grant.read(TABLE, run_id) if run_id else None
    caller = run_grant.caller_key(run_grant.bearer_claims(headers))
    secret = run_grant.header(headers, GRANT_HEADER)
    if not item or not run_grant.live(
        run_grant.Grant.of(item), caller, secret, int(time.time())
    ):
        return "this call belongs to no run"
    return None
