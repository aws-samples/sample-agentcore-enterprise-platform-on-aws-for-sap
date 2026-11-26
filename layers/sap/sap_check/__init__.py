# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Interceptor check: the SAP layer's claim gate.

The platform's Gateway interceptor runs `check` for the SAP targets only
(gateway.interceptor_extensions in platform.yaml, `layers/sap/sap_check`,
request_headers: true). `decide.decide` holds the decision; this module does its
reads, all with the interceptor's role:

- `<SSM_PREFIX>/layers/sap/check`, the layer's settings: its targets, whether Cedar
  enforces, and the operator's ceiling per use case.
- `<SSM_PREFIX>/layers/sap/claims/<use case>` for each use case the ceiling names,
  each written by that use case's stack. A claim of a use case outside the ceiling
  is never read; one absent, that the role may not read, or whose latest version a
  stack deploy did not write, claims nothing.

Each is read with GetParameterHistory (`trusted.py`), so its latest version's writer is known: the
settings, and a claim, count only when CloudFormation wrote them.

Both are kept for TTL_SECONDS; only a successful read is kept. Anything this module
raises refuses the call (the interceptor fails closed for the SAP targets), so an
unreadable parameter or a malformed token is a refusal.

A call that names its run (`x-audit-correlation-id`) logs `interceptor run {"run_id":
...}`, so the interceptor's verdict line, in the same invocation, can be tied to it.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time

from . import decide as _decide
from .trusted import Untrusted, cloudformation_wrote

logger = logging.getLogger(__name__)
TTL_SECONDS = 60
RUN_ID_HEADER = "x-audit-correlation-id"
SETTINGS = "/layers/sap/check"
CLAIMS = "/layers/sap/claims/"

_ssm = None
_cache: tuple[float, dict, list[dict]] | None = None


def _client():
    global _ssm
    if _ssm is None:
        import boto3

        _ssm = boto3.client("ssm")
    return _ssm


def _read() -> tuple[dict, list[dict]]:
    """(settings, claims), at most TTL_SECONDS old. Raises when the settings cannot be
    read or were not written by CloudFormation."""
    global _cache
    now = time.monotonic()
    if _cache is not None and now - _cache[0] < TTL_SECONDS:
        return _cache[1], _cache[2]
    prefix = os.environ["SSM_PREFIX"]
    ssm = _client()
    settings = json.loads(cloudformation_wrote(ssm, prefix + SETTINGS))
    claims = []
    for uc in sorted(settings["ceilings"]):
        name = prefix + CLAIMS + uc
        try:
            claim = json.loads(cloudformation_wrote(ssm, name))
        except ssm.exceptions.ParameterNotFound:
            continue
        except Untrusted as e:
            # Its callers stay unclaimed, so refused; every other use case works on.
            logger.warning("ignoring a claim: %s", e)
            continue
        except ssm.exceptions.ClientError as e:
            # The role reads a claim only through the grant claim() creates with
            # it, so a use case that never claimed is denied rather than not found:
            # it claims nothing. Any other failure refuses the call.
            if e.response["Error"]["Code"] != "AccessDeniedException":
                raise
            continue
        # A claim speaks only for the use case whose parameter holds it.
        if claim.get("use_case") != uc:
            raise ValueError(f"{name} claims for another use case")
        claims.append(claim)
    _cache = (now, settings, claims)
    return settings, claims


def caller_key(claims: dict) -> str:
    """The calling app: `client_id` (Cognito) or `azp` (Entra, a person's token too)."""
    key = claims.get("client_id") or claims.get("azp")
    if not key:
        raise ValueError("the bearer names no client_id or azp")
    return str(key)


def _caller(headers: dict | None) -> str:
    """The caller key of the bearer the Gateway's authorizer verified."""
    auth = next(
        (v for k, v in (headers or {}).items() if k.lower() == "authorization"), ""
    )
    token = auth.split(" ", 1)[1] if auth.lower().startswith("bearer ") else auth
    payload = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    return caller_key(claims)


def check(body: dict, headers: dict | None) -> str | None:
    params = body.get("params") or {}
    run_id = next(
        (v for k, v in (headers or {}).items() if k.lower() == RUN_ID_HEADER), None
    )
    if run_id:
        logger.info("interceptor run %s", json.dumps({"run_id": run_id}))
    reason = _decide.decide(
        tool=str(params.get("name") or ""),
        settings=lambda: _read()[0],
        claims=lambda: _read()[1],
        caller=lambda: _caller(headers),
    )
    if reason is None:
        return None
    return f"{reason}. This is an authorization decision; retrying will not change it."
