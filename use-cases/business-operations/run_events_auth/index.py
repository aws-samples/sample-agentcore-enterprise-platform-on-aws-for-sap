# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Who may subscribe to which run channel: the API's entitlement, on the socket.

AppSync Events calls this on every subscribe to the `runs` namespace, after Cognito
has validated the id token and its client. The channel grammar the agent publishes on:

* `/runs/<short_id>/<case_id>` for a case run. `<short_id>` is the pack's, the prefix
  every one of its case ids carries, so a subscriber names the pack in the path and
  `/runs/<short_id>/*` follows every run in it.
* `/runs/<session_id>` for a run off a case, keyed by a UUID the browser minted.

Admitted: a Business Operations user (`entitlement.admit`, the API's own floor) on a
pack they may see, or on a session channel. Everything else is refused, `/runs/*`
included. An unreadable registry raises, so the subscription fails rather than opens.
"""

from __future__ import annotations

import re

import entitlement
import registry

#: `crypto.randomUUID()`, the only session key the console mints. Unguessable, which is
#: the whole of a session channel's protection: it belongs to no pack.
_SESSION = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
REFUSED = {"error": "Unauthorized"}


def _pack_for(short_id: str) -> str | None:
    for pack_id, entry in registry.packs().items():
        if short_id and (entry.get("source") or {}).get("short_id") == short_id:
            return pack_id
    return None


def verdict(event: dict) -> dict | None:
    """None to admit the subscription, else the error AppSync refuses it with."""
    identity = event.get("identity") or {}
    claims = dict(identity.get("claims") or {})
    # AppSync lifts `cognito:groups` onto `identity.groups`; read both, as the API does.
    claims["groups"] = identity.get("groups") or []
    caller = {"requestContext": {"authorizer": {"claims": claims}}}
    if entitlement.admit(caller):
        return REFUSED
    path = ((event.get("info") or {}).get("channel") or {}).get("path") or ""
    segments = path.strip("/").split("/")
    if len(segments) == 2 and _SESSION.match(segments[1]):
        return None
    if len(segments) == 3:
        _, short_id, leaf = segments
        if leaf != "*" and not leaf.startswith(f"{short_id}-"):
            return REFUSED
        registry.refresh_for_request()
        pack_id = _pack_for(short_id)
        if pack_id and entitlement.CallerEntitlement.resolve(caller).may_see(pack_id):
            return None
    return REFUSED


def handler(event, _context):
    refusal = verdict(event)
    if refusal:
        path = ((event.get("info") or {}).get("channel") or {}).get("path")
        sub = (event.get("identity") or {}).get("sub")
        print(f"[RUN-EVENTS] subscribe refused: {path} for {sub}")
    return refusal
