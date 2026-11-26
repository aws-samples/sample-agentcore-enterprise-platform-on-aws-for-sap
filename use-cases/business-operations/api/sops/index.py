# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""A pack's SOPs, addressed by the process types that map them.

    GET    /packs/{pack_id}/sops                           the pack's SOPs, one row per
                                                           process type
    POST   /packs/{pack_id}/sops                           a new process type and its SOP
    GET    /packs/{pack_id}/sops/{process_type}            one SOP's text
    PUT    /packs/{pack_id}/sops/{process_type}            replace it
    DELETE /packs/{pack_id}/sops/{process_type}            remove it and its mappings
    POST   /packs/{pack_id}/sops/{process_type}/check      lint a text without saving
    POST   /sops/sync                                      reindex the SOP search

**A SOP file is always mapped.** Every route names a process type, and the file is the
one `process_type_to_sop` maps it to. A new SOP comes with its process type and the
mapping is added in the same pack version; a delete removes the file with every process
type mapping to it. An alias reads the file it aliases and is refused a write, so one
file never has two write paths. What a legal write is lives in `sop_store`; this file is
the HTTP half.

**This route holds no write.** After its gates it invokes `pack_composer`, the only
principal the SOP bucket lets write, with the file, the text and the caller's roles. In
development the write is the pack's next version; a body's `expected_version` turns a
lost update into a 409.

**Who.** `entitlement.admit` first (this API's client, the base group). Reads follow pack
visibility. A write needs the authoring floor (`SOP_AUTHOR_GROUPS`, closed when empty) and
the pack's own `author_groups`; a new SOP and a delete change the mapping, which is
structure, so they also need `business-operations-pack-admins`. The composer re-checks.
"""

from __future__ import annotations

import logging
import re

import composer_client
import pack_access
import pack_status
import registry
import sop_store as store
from api_http import HttpError, field, json_body, respond, route
from contracts import PACK_ID_PATTERN
from entitlement import (
    CallerEntitlement,
    caller_door,
    caller_groups,
    caller_identity,
    load_map,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_PACK_ID = re.compile(PACK_ID_PATTERN)
#: A file name a new SOP may take: one segment, a text suffix.
_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.(md|txt)$")


def _actor(event: dict) -> str:
    return caller_identity(event).display or "unknown"


def _params(event: dict) -> tuple[str, str]:
    params = event.get("pathParameters") or {}
    pack_id = str(params.get("pack_id") or "")
    if not _PACK_ID.fullmatch(pack_id):
        raise HttpError(400, "`pack_id` is not a pack id")
    return pack_id, str(params.get("process_type") or "")


def _document(pack_id: str, caller: CallerEntitlement) -> dict:
    """The published pack, for a caller who may see it; 404 before 403 hides nothing,
    because a pack the caller may not see answers the same 404."""
    entry = registry.packs().get(pack_id)
    if not entry or not caller.may_see(pack_id):
        raise HttpError(404, f"No published pack {pack_id}")
    return entry.get("pack") or {}


def _row(document: dict, process_type: str) -> dict | None:
    """`{process_type, kind, sop, alias_of?}` for one mapped process type, or None."""
    return next(
        (r for r in registry.sop_kinds(document) if r["process_type"] == process_type),
        None,
    )


def _author_gate(groups: frozenset[str], pack_id: str) -> None:
    if not store.may_author_any(groups):
        raise HttpError(
            403,
            "Not a SOP author",
            detail="Changing a SOP changes the agent's instructions. Ask an "
            "administrator to add you to the authors group.",
        )
    if not store.may_author(groups, pack_id, f"{pack_id}/sop.txt"):
        raise HttpError(
            403,
            f"You may not author SOPs for {pack_id}",
            detail="A SOP needs the pack's `entitlement.author_groups`.",
        )


def _write(event, pack_id: str, groups, caller, sop: dict, expected) -> dict:
    """Have the composer write it; the route has already authorized the pack."""
    if expected is not None and (
        isinstance(expected, bool) or not isinstance(expected, int)
    ):
        raise HttpError(400, "`expected_version` must be an integer")
    payload = {
        "pack_id": pack_id,
        "sop": sop,
        # What the caller holds, as the composer checks it: a mapping is structure.
        "roles": pack_access.roles(caller, groups, pack_id),
        "actor": _actor(event),
        **({"expected_version": expected} if expected is not None else {}),
        # The people target's door, recorded on the version the composer mints.
        **({"door": door} if (door := caller_door(event)) else {}),
    }
    composed = composer_client.call(payload)
    status = int(composed.get("status") or 200)
    key = f"{pack_id}/{sop['name']}"
    if status >= 400:
        return respond(status, composed, event)
    if sop["op"] == "delete":
        return respond(
            200, {**composed, "key": key, "deleted_by": payload["actor"]}, event
        )
    size = len(sop["content"].encode("utf-8"))
    written = {"key": key, "bytes": size, "uploaded_by": payload["actor"]}
    return respond(201 if "map_as" in sop else 200, {**composed, **written}, event)


def _checked(key: str, content: object) -> str:
    """The text of a write `sop_store` would allow at `key`, or the refusal."""
    store.require_development()
    key, text = store.checked_put({"name": key, "content": content})
    return text


def _list(event: dict, pack_id: str, groups, caller) -> dict:
    document = _document(pack_id, caller)
    files = {
        s["key"]: s for s in store.list_sops() if store.pack_of(s["key"]) == pack_id
    }
    rows = [
        {
            **row,
            "key": row["sop"],
            **{k: v for k, v in (files.get(row["sop"]) or {}).items() if k != "key"},
        }
        for row in registry.sop_kinds(document)
    ]
    ingestion = store.latest_ingestion()
    active = pack_status.pointer(store.s3, store.REGISTRY_BUCKET, pack_id)
    return respond(
        200,
        {
            "pack_id": pack_id,
            "version": active["version"] if active else None,
            "sops": rows,
            "ingestion": ingestion,
            "search": store.search_states(list(files.values()), ingestion),
            "can_author": store.may_author(groups, pack_id, f"{pack_id}/sop.txt"),
        },
        event,
    )


def _create(event: dict, pack_id: str, groups, caller) -> dict:
    document = _document(pack_id, caller)
    _author_gate(groups, pack_id)
    body = json_body(event)
    process_type = field(body, "process_type", required=True)
    if not _PACK_ID.fullmatch(process_type):
        raise HttpError(
            400, "`process_type` is lowercase letters, digits and underscores"
        )
    if process_type in (document.get("process_type_to_sop") or {}):
        raise HttpError(
            409,
            f"{pack_id} already maps {process_type}",
            detail="Replace its SOP with PUT on the process type.",
        )
    name = field(body, "name") or f"{process_type}.txt"
    if not _FILE.fullmatch(name):
        raise HttpError(400, "`name` must be one file name ending .md or .txt")
    text = _checked(f"{pack_id}/{name}", body.get("content"))
    sop = {"op": "put", "name": name, "content": text, "map_as": process_type}
    return _write(event, pack_id, groups, caller, sop, body.get("expected_version"))


def _one(event: dict, method: str, pack_id: str, process_type: str, groups, caller):
    document = _document(pack_id, caller)
    row = _row(document, process_type)
    if method == "POST" and str(event.get("resource") or "").endswith("/check"):
        body = json_body(event)
        # A type not mapped yet checks the file a create would write.
        key = row["sop"] if row else f"{pack_id}/{process_type}.txt"
        text = _checked(key, body.get("content"))
        findings = store.lint(key, text)
        return respond(
            200, {"key": key, "ok": not findings, "findings": findings}, event
        )
    if row is None:
        raise HttpError(404, f"{pack_id} maps no process type {process_type}")
    key, name = row["sop"], row["sop"].partition("/")[2]
    if method == "GET":
        read = store.read(key)
        extra = {k: row[k] for k in ("process_type", "kind", "alias_of") if k in row}
        return respond(
            200, {**read, **extra, "can_author": store.may_author_any(groups)}, event
        )
    _author_gate(groups, pack_id)
    if row["kind"] == registry.ALIAS:
        raise HttpError(
            409,
            f"{process_type} is an alias of {row['alias_of']}",
            detail=f"Write the SOP by {row['alias_of']}, the process type that owns it.",
        )
    body = json_body(event)
    if method == "DELETE":
        store.require_development()
        if row["kind"] == registry.TRIAGE:
            raise HttpError(409, f"{process_type} is the pack's triage SOP")
        sop = {"op": "delete", "name": name}
    else:
        sop = {"op": "put", "name": name, "content": _checked(key, body.get("content"))}
    return _write(event, pack_id, groups, caller, sop, body.get("expected_version"))


@route
def handler(event):
    try:
        return _dispatch(event)
    except store.SopRefusal as refusal:
        return respond(refusal.status, refusal.body, event)


def _dispatch(event: dict) -> dict:
    method = str(event.get("httpMethod") or "GET").upper()
    resource = str(event.get("resource") or "")
    groups = caller_groups(event)
    if resource.endswith("/sops/sync") and method == "POST":
        if not store.may_author_any(groups):
            raise HttpError(403, "Not a SOP author")
        return respond(
            202, store.sync(f"Started from /sops/sync by {_actor(event)}"), event
        )
    pack_id, process_type = _params(event)
    caller = CallerEntitlement(groups, load_map())
    if resource.endswith("/sops"):
        if method == "GET":
            return _list(event, pack_id, groups, caller)
        if method == "POST":
            return _create(event, pack_id, groups, caller)
    elif "{process_type}" in resource and method in ("GET", "PUT", "DELETE", "POST"):
        return _one(event, method, pack_id, process_type, groups, caller)
    return respond(405, {"error": "Method not allowed"}, event)
