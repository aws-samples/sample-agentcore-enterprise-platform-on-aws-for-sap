# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""POST /erp-events: SAP says an exception happened, over HTTPS.

The handler checks the caller, validates the message exactly as the ingest consumer
will (`sap_ingest.parse` and `accept`), puts it on the use case's ingest queue as the
event the SAP layer's bus delivers, and answers 202. The consumer makes the case. Nothing here
calls SAP or writes a case.

The body is a nudge (`types/sap-nudge.schema.json`) or the typed envelope
(`types/sap-events.schema.json`). Two credentials get in, and both speak for any
system this use case calls directly, so the event's `source` is `sap.<the pack's own
system>`:

- the shared token, as Bearer or as an SM59 type-G destination's Basic password,
  compared in constant time;
- a Cognito client-credentials access token of the use case's ingest client, holding
  its ingest scope: RS256 with a key from the pool's JWKS, its issuer, unexpired,
  `token_use` access and that client's id (PyJWT, `requirements.txt`).

Never log the body: records carry supplier names and amounts, and CloudWatch Logs is a
wider audience than the table. A refusal logs its fixed reason only.
"""

import base64
import binascii
import hmac
import json
import logging
import os
import time
from functools import cache

import boto3
import registry
from polling_engine import load_pack_sources
from sap_ingest import MAX_BODY_BYTES, Rejected, accept, as_event, parse
from sap_poller import system_of

logger = logging.getLogger()
logger.setLevel(logging.INFO)

QUEUE_URL = os.environ.get("INGEST_QUEUE_URL", "")
#: The ingest client's pool, client id and scope; all three, or OAuth is off.
ISSUER = os.environ.get("INGEST_ISSUER", "")
CLIENT_ID = os.environ.get("INGEST_CLIENT_ID", "")
SCOPE = os.environ.get("INGEST_SCOPE", "")
# A rotated token is accepted, and the old one refused, within TOKEN_TTL_SECONDS in
# every warm container, and at once on the first push presenting the new one. The
# floor bounds the re-read a wrong token triggers, so a flood of them cannot turn
# into one Secrets Manager call each.
TOKEN_TTL_SECONDS = 5 * 60
REREAD_FLOOR_SECONDS = 30


def _load_token() -> str:
    """The shared token from the secret. Empty refuses everything."""
    arn = os.environ.get("INGEST_SECRET_ARN", "")
    if not arn:
        return ""
    try:
        secret = boto3.client("secretsmanager").get_secret_value(SecretId=arn)
        return json.loads(secret["SecretString"]).get("token", "")
    except Exception as e:  # noqa: BLE001 - fail closed below
        logger.warning(f"Could not read the ingest token: {type(e).__name__}")
        return ""


_cached = ["", float("-inf")]  # token, monotonic read time


def _token(max_age: float) -> str:
    if time.monotonic() - _cached[1] > max_age:
        _cached[:] = [_load_token(), time.monotonic()]
    return _cached[0]


@cache
def _jwks():
    import jwt

    # Cached keys; an unknown `kid` refetches, which the route's throttle bounds.
    return jwt.PyJWKClient(f"{ISSUER}/.well-known/jwks.json", lifespan=3600)


def client_credentials(token: str) -> bool:
    """Whether `token` is an access token of the ingest client with its scope."""
    if not (ISSUER and CLIENT_ID and SCOPE) or token.count(".") != 2:
        return False
    import jwt

    try:
        key = _jwks().get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            issuer=ISSUER,
            options={"require": ["exp", "iss", "token_use", "client_id", "scope"]},
        )
    except jwt.PyJWTError:
        return False
    return (
        claims["token_use"] == "access"
        and claims["client_id"] == CLIENT_ID
        and SCOPE in str(claims["scope"]).split()
    )


def _resp(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def presented_token(headers: dict) -> str:
    """The token from `Authorization`, Bearer or Basic.

    Basic is what an SM59 type-G destination sends on its own, with the token in the
    destination's password field, which keeps it out of ABAP source.
    """
    auth = headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    if auth.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth[6:], validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return ""
        return decoded.partition(":")[2]
    return ""


def _authorized(headers: dict) -> int | None:
    """None when the caller may push; otherwise the status to answer."""
    presented = presented_token(headers)
    token = _token(TOKEN_TTL_SECONDS)
    # Bytes, because compare_digest refuses a non-ASCII str with a TypeError (a 502).
    if not token or not hmac.compare_digest(presented.encode(), token.encode()):
        token = _token(REREAD_FLOOR_SECONDS)  # rotated since the last read?
    if token and hmac.compare_digest(presented.encode(), token.encode()):
        return None
    bearer = headers.get("authorization", "").startswith("Bearer ")
    if bearer and client_credentials(presented):
        return None
    if not token and not ISSUER:
        logger.error("The ingest secret holds no token: refusing every push")
        return 503
    return 401


def _send(body: str) -> None:
    boto3.client("sqs").send_message(QueueUrl=QUEUE_URL, MessageBody=body)


def handler(event: dict, context) -> dict:
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    # An unauthenticated route with no credential check would create cases for anyone.
    status = _authorized(headers)
    if status == 503:
        return _resp(503, {"error": "Ingest not configured"})
    if status:
        return _resp(401, {"error": "Invalid token"})

    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        try:
            raw = base64.b64decode(raw).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return _resp(400, {"error": "Undecodable body"})
    if len(raw.encode("utf-8")) > MAX_BODY_BYTES:
        return _resp(413, {"error": "Payload too large"})
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return _resp(400, {"error": "Malformed JSON"})
    if not isinstance(payload, dict) or not isinstance(payload.get("pack_id"), str):
        return _resp(400, {"error": "Invalid message", "reason": "shape"})

    registry.refresh_for_request()
    try:
        sources = load_pack_sources()
    except registry.RegistryUnavailable as e:
        logger.error(f"Registry unavailable: {e}")
        return _resp(503, {"error": "Service temporarily unavailable; resend"})
    config = {c["pack_id"]: c for c in sources}.get(payload["pack_id"])
    if config is None:
        logger.warning("Refused a push for an unknown pack")
        return _resp(400, {"error": f"Unknown pack_id: {payload['pack_id']}"})
    door = system_of(config)
    if door is None:
        logger.warning(f"Refused a push for {config['pack_id']}: no system served")
        return _resp(
            400, {"error": "This pack's SAP system is not one this route serves"}
        )
    body = as_event(door, payload)
    try:
        door, shape, msg = parse(body)
        accept(door, shape, msg, config)
    except Rejected as r:
        logger.warning(f"Refused a push for {config['pack_id']}: {r.reason}")
        status = 413 if r.reason == "too-large" else 400
        return _resp(status, {"error": "Invalid message", "reason": r.reason})
    try:
        _send(body)
    except Exception as e:  # noqa: BLE001 - a 500 is how SAP learns to resend
        logger.error(
            f"Could not queue a push for {config['pack_id']}: {type(e).__name__}"
        )
        return _resp(500, {"error": "Could not queue the message; resend"})
    logger.info(f"Queued a {shape} for {config['pack_id']} on {door}")
    return _resp(202, {"queued": shape})
