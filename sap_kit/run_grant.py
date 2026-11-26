# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Run grants: the generic half of a use case's Gateway run checks.

A run grant is a short-lived row the use case's run starter writes before it starts a
run, in a table only that starter may write. The use case's interceptor check refuses
a gated call that does not present a live one. This module is the part every such
check shares; which tools are gated, what a rehearsal may not do, and the refusal text
stay in the use case.

Stdlib only, with no package-relative import, so a check links this file into its own
package (the interceptor bundles each check's directory alone) and imports it as
`from . import run_grant`.

The row: `run_id` (the key), `client_id` (the caller key, `caller_key`), `secret_sha256`
(SHA-256, hex, of a secret only the starter and the run hold), `expires_at` (epoch
seconds, also the table's TTL) and `rehearsal` (true only for a rehearsal). Any other
attribute is the use case's own. The run sends its id on RUN_ID_HEADER and the secret
on `grant_header(<use case>)`.

A check must raise, not default, when a read fails: the interceptor refuses that
check's own targets on a raise.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass

RUN_ID_HEADER = "x-audit-correlation-id"


def grant_header(use_case: str) -> str:
    """The header the run's grant secret travels on."""
    return f"x-{use_case}-run-grant"


@dataclass(frozen=True)
class Grant:
    """A grant's common fields, for a check that keeps no type of its own. `live`
    reads only `client_id`, `expires_at` and `secret_sha256`, so a use case's own
    grant type with those attributes works as well."""

    run_id: str
    client_id: str
    expires_at: int
    #: SHA-256 (hex) of the secret the starter gave the run; empty matches nothing.
    secret_sha256: str = ""
    rehearsal: bool = False

    @classmethod
    def of(cls, item: dict) -> Grant:
        return cls(
            run_id=str(item["run_id"]),
            client_id=str(item["client_id"]),
            expires_at=int(item["expires_at"]),
            secret_sha256=str(item.get("secret_sha256") or ""),
            rehearsal=item.get("rehearsal") is True,
        )


def header(headers: dict | None, name: str) -> str | None:
    return next((v for k, v in (headers or {}).items() if k.lower() == name), None)


def caller_key(claims: dict) -> str:
    """Who a run grant must name, from the claims of a bearer the Gateway verified.

    Cognito: the client (`client_id`). Entra, under the platform's direct mode: a
    person's token (it carries `scp`) is `user:<oid>`, because every console token
    shares one `azp` and one person must not ride another's grant; an app's token is
    its `azp`. The run starter writes the same key.
    """
    if claims.get("client_id"):
        return str(claims["client_id"])
    if claims.get("scp"):
        return f"user:{claims['oid']}"
    return str(claims["azp"])


def bearer_claims(headers: dict | None) -> dict:
    """The claims of the bearer the Gateway's authorizer verified."""
    auth = header(headers, "authorization") or ""
    token = auth.split(" ", 1)[1] if auth.lower().startswith("bearer ") else auth
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def read(table, run_id: str) -> dict | None:
    """The grant's row, read consistently (it was written just before the run), or
    None. `table` is a boto3 DynamoDB Table."""
    return table.get_item(Key={"run_id": run_id}, ConsistentRead=True).get("Item")


def holds_secret(grant, secret: str | None) -> bool:
    if not grant.secret_sha256 or not secret:
        return False
    digest = hashlib.sha256(secret.encode()).hexdigest()
    return hmac.compare_digest(digest, grant.secret_sha256)


def live(grant, caller: str, secret: str | None, now: int) -> bool:
    """Does this grant authorize this caller's call now? It must name the caller,
    be unexpired, and match the secret the call presents."""
    return (
        grant.client_id == caller
        and grant.expires_at > now
        and holds_secret(grant, secret)
    )
