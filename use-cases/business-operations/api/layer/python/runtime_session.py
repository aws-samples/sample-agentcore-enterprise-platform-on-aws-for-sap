# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Ask the agent runtime to abandon one session: `POST /runs/stop` for one case, and
`POST /deployment/stop` for every live run.

Best-effort by design. Failures are logged by class and reported as False, never
raised: each caller still has its own release to make, and a stop someone asked for
must not fail on a transport hiccup.
"""

from __future__ import annotations

import logging
import urllib.parse
import urllib.request
from collections.abc import Callable

import boto3

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 10


def stop(
    session_id: str,
    *,
    runtime_arn_parameter: str,
    region: str,
    token: Callable[[], str],
    timeout: float = TIMEOUT_SECONDS,
) -> bool:
    """Whether the runtime agreed to stop `session_id`. False with no runtime wired.

    `token` mints the delegating client's bearer, called only once the runtime's ARN is
    known.
    """
    if not runtime_arn_parameter:
        return False
    try:
        runtime_arn = boto3.client("ssm").get_parameter(Name=runtime_arn_parameter)[
            "Parameter"
        ]["Value"]
        escaped = urllib.parse.quote(runtime_arn, safe="")
        url = (
            f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{escaped}"
            "/stopruntimesession?qualifier=DEFAULT"
        )
        request = urllib.request.Request(
            url,
            data=b"{}",
            headers={
                "Authorization": f"Bearer {token()}",
                "Content-Type": "application/json",
                "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
            },
            method="POST",
        )
        with urllib.request.urlopen(  # nosec B310 — URL from trusted SSM config  # nosemgrep: dynamic-urllib-use-detected
            request, timeout=timeout
        ) as response:
            response.read()
        return True
    except Exception as error:  # noqa: BLE001 - logged by class, never echoed
        # The exception class, never its message: `putheader` raises
        # ValueError("Invalid header value %r") with the offending value inlined, and
        # the value here is `Bearer <machine token>`. CloudWatch is persistence, and no
        # credential may be persisted. The class alone (HTTPError / URLError /
        # timeout / ValueError) is what triages a best-effort stop anyway.
        logger.warning(
            f"[RUN] stop session {session_id} failed: {type(error).__name__}"
        )
        return False
