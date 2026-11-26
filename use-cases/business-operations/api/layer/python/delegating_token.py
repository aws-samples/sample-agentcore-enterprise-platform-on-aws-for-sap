# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The delegating client's client_credentials token, for calls to the agent runtime.

A client of its own, held only by the run route and the invoker. The runtime accepts
an acting human's `sub` beside a token from this client and from no other, so the
machine secret handed to pollers and MCP hosts cannot assert a person.

Env: DELEGATING_CLIENT_ID, DELEGATING_SECRET_ARN, DELEGATING_SCOPE, and the token
endpoint: DELEGATING_TOKEN_URL (Entra, under the platform's direct mode), else
AUTH_DOMAIN_URL (`auth/domain-url`) plus `/oauth2/token`.
"""

import base64
import json
import os
import time
import urllib.parse
import urllib.request

import boto3

_cached: tuple[str, float] | None = None


def get_delegating_token() -> str:
    global _cached
    if _cached and time.monotonic() < _cached[1]:
        return _cached[0]
    client_id = os.environ["DELEGATING_CLIENT_ID"]
    secret = boto3.client("secretsmanager").get_secret_value(
        SecretId=os.environ["DELEGATING_SECRET_ARN"]
    )["SecretString"]
    url = os.environ.get("DELEGATING_TOKEN_URL") or (
        os.environ["AUTH_DOMAIN_URL"].rstrip("/") + "/oauth2/token"
    )
    if not url.startswith("https://"):
        raise ValueError("the delegating token endpoint must be https")
    # Form-encoded before Basic, as RFC 6749 2.3.1 asks: an Entra secret can carry `~`.
    pair = f"{urllib.parse.quote_plus(client_id)}:{urllib.parse.quote_plus(secret)}"
    basic = base64.b64encode(pair.encode()).decode()
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "scope": os.environ["DELEGATING_SCOPE"],
            }
        ).encode(),
        method="POST",
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(request, timeout=10) as resp:  # nosec B310 - https asserted above  # nosemgrep: dynamic-urllib-use-detected
        body = json.loads(resp.read())
    # A minute's margin so a token is never handed out about to expire.
    _cached = (body["access_token"], time.monotonic() + int(body["expires_in"]) - 60)
    return _cached[0]
