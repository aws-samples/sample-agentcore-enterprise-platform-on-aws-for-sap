# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The pack routes an analyst reaches with no checkout and no AWS account.

    POST  /packs                                  create a pack (born incomplete)
    GET   /packs/status                           each visible pack's state
    GET   /packs/{pack_id}                        the document, its version, its parts
    PATCH /packs/{pack_id}                        an edit: content or structure keys
    GET   /packs/{pack_id}/base_prompt            the prompt a patch replaces
    GET   /packs/{pack_id}/versions               the stored versions, newest first, paged
    GET   /packs/{pack_id}/versions/{version}     one stored version's document
    GET   /packs/{pack_id}/evaluations            example cases and the last run
    PUT   /packs/{pack_id}/evaluations/{test_id}  one example case
    DELETE                                        (same path)

`GET /packs/{pack_id}` serves the document as the registry stores it, with the part each
top-level key belongs to (`pack_tiers.PARTS`), so the console groups keys from the API.
`GET /packs/status` reads the registry bucket rather than the composer, whose single slot
an import or a publish must not lose to a page load.

**A `POST /packs` body mints a pack; a `PATCH` body revises one.** Same authorization,
same single writer. `pack_compose` composes every privileged value of a created pack
itself, and the pack is born incomplete: inert to the poller, unroutable. In development each
edit is a new version of the pack; `expected_version`, the version the author read, turns a
lost update into a 409. A rule's value is written here, as `pack.constants`.

The evaluations: `GET` serves the pack's example cases from its active bundle and the last
run `evals-run --publish` recorded; `PUT` and `DELETE` change one example case, through
the composer like any edit: content, so the floor and the pack's `author_groups`, in
development.

**This route holds no write.** It authorizes, then invokes `pack_composer`, the only
principal the registry bucket lets write: the registry carries the entitlement map, so a
browser-facing route holding that write could grant its own caller a pack.

**Gates, in order:** `entitlement.admit` (this API's client, the base group); the authoring
floor (`SOP_AUTHOR_GROUPS`, closed when empty), checked before any pack is looked up; then
`pack_tiers.check_patch` over the patch's *keys*: content needs the pack's own
`author_groups`, and structure needs `business-operations-pack-admins`. All of it is
development's: outside development nothing is edited or created here, and a pack arrives
only by an import with a change reference (`pack_tiers.edit_refusal`). A create has no
pack to read a grant from, so the floor authorizes it. The composer re-checks against its
own `PROMOTION_ROLE`: this route's check buys a readable refusal, the composer's is the
gate.
"""

from __future__ import annotations

import logging
import os
import re

import boto3
import composer_client
import example_cases
import pack_access
import pack_compose
import pack_status
import pack_tiers
import pack_versions
import registry
from api_http import HttpError, field, json_body, respond, route
from botocore.exceptions import ClientError
from contracts import PACK_ID_PATTERN
from entitlement import (
    CallerEntitlement,
    author_floor,
    caller_door,
    caller_groups,
    caller_identity,
    load_map,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

#: The pack corpus. Read here, never written — the composer holds that, and the bucket's
#: own Deny enforces it regardless of what this function's role says.
REGISTRY_BUCKET = os.environ["REGISTRY_BUCKET"]
#: This environment's `promotion.role`; the composer reads its own copy.
ROLE = os.environ.get("PROMOTION_ROLE") or pack_tiers.DEVELOPMENT

s3 = boto3.client("s3")

#: A pack id, the schema's pattern. Checked here because the composer joins it into a
#: path under its mirror, so `finance_ops/../other` would address another pack.
_PACK_ID = re.compile(PACK_ID_PATTERN)


def _actor(event: dict) -> str:
    """Identity from the authorizer's claims, never from the body."""
    return caller_identity(event).display or "unknown"


def _door(event: dict) -> dict:
    """`{"door": ...}` for a request the people target made, else nothing: the
    composer records it on the version it mints."""
    door = caller_door(event)
    return {"door": door} if door else {}


def _require_floor(groups: set[str]) -> None:
    """The deployment-wide floor, or a 403.

    Closed when unconfigured, and checked before any pack is looked up on either route,
    so an unauthorized caller learns nothing about which packs exist.
    """
    floor = author_floor()
    if not (floor and groups & floor):
        raise HttpError(
            403,
            "Not a pack author",
            detail="Your groups are not in this deployment's authors group.",
        )


#: The whole-document read, keyed by the last path segment of the route that serves it:
#: response field -> object suffix. An allowlist rather than a derivation, because the
#: segment reaches an S3 key and one this map does not name must not become a lookup.
_READABLE = {"base_prompt": "base_prompt.txt"}


def _read_document(event: dict) -> dict:
    """GET /packs/{pack_id}/base_prompt: the document a content-tier patch replaces,
    and the only way to read it back.

    **Gated on the write grant, not on visibility.** `may_author` is exactly the set of
    callers who may replace these documents, and a caller who may replace it can already
    read it back by replacing it and watching what the agent does — so gating the read any
    tighter buys nothing and leaves the only headless revision a blind overwrite. It is
    deliberately *not* the visibility gate either: a prompt is the agent's own
    instructions, not case data, which is why `GET /registry` omits it for everyone.

    **Read from S3, not from `registry.packs().get()`.** The registry is cached for the process
    lifetime, so a warm container would hand back the text a publish had just replaced —
    the one thing a read-modify-write loop cannot tolerate.
    """
    # Which document, from the route template rather than the raw path: API Gateway fills
    # `resource` from the method wired in CDK, so this is a closed set.
    name = str(event.get("resource") or "").rsplit("/", 1)[-1]
    suffix = _READABLE.get(name)
    if suffix is None:
        raise HttpError(404, "No such pack document")

    pack_id = str((event.get("pathParameters") or {}).get("pack_id") or "").strip()
    # API Gateway will not route a `pack_id` containing a separator to this method at
    # all, but the string reaches an S3 key below, so it is checked here like the write's.
    if not _PACK_ID.fullmatch(pack_id):
        raise HttpError(400, "`pack_id` is not a pack id")

    groups = caller_groups(event)
    _require_floor(groups)
    if not CallerEntitlement(groups, load_map()).may_author(pack_id):
        raise HttpError(
            403,
            f"You may not read {pack_id}'s {name}",
            detail="A pack's prompt is readable by the callers who may author its "
            "content — the same `entitlement.author_groups` grant.",
        )

    try:
        body = s3.get_object(Bucket=REGISTRY_BUCKET, Key=f"{pack_id}/{suffix}")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            raise HttpError(
                404,
                f"No published {name} for {pack_id}",
                detail="Either the pack is not published here, or it was "
                f"published without a {suffix}.",
            ) from None
        raise

    # The version an edit of this text names as `expected_version`. None before the
    # first write stores version 0.
    active = pack_status.pointer(s3, REGISTRY_BUCKET, pack_id)
    return respond(
        200,
        {
            "pack_id": pack_id,
            name: body["Body"].read().decode("utf-8"),
            "version": active["version"] if active else None,
        },
        event,
    )


def _status(event: dict) -> dict:
    """GET /packs/status: what the Packs card shows, for every pack the caller may see,
    the visibility `GET /registry` applies."""
    caller = CallerEntitlement(caller_groups(event), load_map())
    return respond(
        200,
        {
            "packs": [
                pack_status.summary(s3, REGISTRY_BUCKET, pack_id)
                for pack_id in sorted(registry.packs())
                if caller.may_see(pack_id)
            ]
        },
        event,
    )


def _path_pack(event: dict) -> str:
    pack_id = str((event.get("pathParameters") or {}).get("pack_id") or "").strip()
    if not _PACK_ID.fullmatch(pack_id):
        raise HttpError(400, "`pack_id` is not a pack id")
    return pack_id


def _evaluations(event: dict) -> dict:
    """GET /packs/{pack_id}/evaluations, for whoever may see the pack."""
    pack_id = _path_pack(event)
    groups = caller_groups(event)
    caller = CallerEntitlement(groups, load_map())
    if not caller.may_see(pack_id):
        raise HttpError(403, "Not entitled to this pack")
    document = (registry.packs().get(pack_id) or {}).get("pack") or {}
    active = pack_status.pointer(s3, REGISTRY_BUCKET, pack_id)
    stored = (
        pack_status.read_json(
            s3, REGISTRY_BUCKET, f"_bundles/{pack_id}/{active['version']}.json"
        )
        if active
        else None
    ) or {}
    cases = [
        {**case, "group": example_cases.group(case)}
        for case in stored.get("evals") or []
    ]
    last_run = pack_status.read_json(
        s3, REGISTRY_BUCKET, example_cases.results_key(pack_id)
    )
    access = pack_access.access(groups, caller, pack_id, document)
    return respond(
        200,
        {
            "pack_id": pack_id,
            "version": active["version"] if active else None,
            "example_cases": cases,
            "last_run": last_run,
            "estimate_usd": example_cases.estimate(len(cases), last_run),
            "may_edit": access["edit"]["example_cases"],
            # Runs start from the command line until a runner exists in AWS.
            "may_run": False,
            "run_refusal": "Runs from the command line today: ops.py evals-run "
            f"--pack {pack_id}",
        },
        event,
    )


def _example_case(event: dict, method: str) -> dict:
    """PUT or DELETE /packs/{pack_id}/evaluations/{test_id}: one example case."""
    pack_id = _path_pack(event)
    test_id = str((event.get("pathParameters") or {}).get("test_id") or "")
    if not example_cases.TEST_ID.fullmatch(test_id):
        raise HttpError(400, "`test_id` is not an example case id")
    body = json_body(event)
    expected = field(body, "expected_version", int)
    groups = caller_groups(event)
    _require_floor(groups)
    if refused := pack_tiers.edit_refusal(ROLE):
        raise HttpError(403, refused[0], detail=refused[1])
    caller = CallerEntitlement(groups, load_map())
    if not caller.may_author(pack_id):
        raise HttpError(
            403,
            f"You may not change {pack_id}'s example cases",
            detail="Example cases are the pack's content, so they need its "
            "`entitlement.author_groups`.",
        )
    change: dict = {"op": "delete" if method == "DELETE" else "put", "test_id": test_id}
    if method == "PUT":
        case = field(body, "case", dict, required=True)
        document = (registry.packs().get(pack_id) or {}).get("pack") or {}
        if errors := example_cases.check(test_id, case, document):
            raise HttpError(400, "Invalid example case", details=errors)
        change["case"] = case
    payload = {
        "pack_id": pack_id,
        "roles": pack_access.roles(caller, groups, pack_id),
        "actor": _actor(event),
        "example_case": change,
        **({"expected_version": expected} if expected is not None else {}),
        **_door(event),
    }
    composed = composer_client.call(payload)
    if "status" in composed:
        return respond(int(composed["status"]), composed, event)
    return respond(200, {"version": composed.get("version")}, event)


def _document(event: dict) -> dict:
    """GET /packs/{pack_id}: the document, its version and origin, and its parts."""
    pack_id = _path_pack(event)
    groups = caller_groups(event)
    caller = CallerEntitlement(groups, load_map())
    # 404 before 403, as the case and config routes answer.
    entry = registry.packs().get(pack_id)
    if not entry:
        raise HttpError(404, f"No published pack {pack_id}")
    if not caller.may_see(pack_id):
        raise HttpError(403, "Not entitled to this pack")
    document = entry.get("pack") or {}
    summary = pack_status.summary(s3, REGISTRY_BUCKET, pack_id)
    return respond(
        200,
        {
            "pack_id": pack_id,
            "version": summary["version"],
            "origin": summary["origin"],
            "pack": document,
            "parts": {
                k: pack_tiers.PARTS[k] for k in document if k in pack_tiers.PARTS
            },
            "constant_parts": {
                symbol: part
                for symbol, part in pack_tiers.CONSTANT_PARTS.items()
                if symbol in (document.get("constants") or {})
            },
            "access": pack_access.access(groups, caller, pack_id, document),
        },
        event,
    )


def _visible_pack(event: dict) -> str:
    """The path's pack, published and visible to the caller: 404 before 403."""
    pack_id = _path_pack(event)
    if pack_id not in registry.packs():
        raise HttpError(404, f"No published pack {pack_id}")
    if not CallerEntitlement(caller_groups(event), load_map()).may_see(pack_id):
        raise HttpError(403, "Not entitled to this pack")
    return pack_id


def _versions(event: dict) -> dict:
    """GET /packs/{pack_id}/versions: the stored versions, newest first, paged."""
    pack_id = _visible_pack(event)
    qs = event.get("queryStringParameters") or {}
    for name in qs:
        if name not in ("limit", "next_token"):
            raise HttpError(400, f"Unknown parameter {name}", field=name)
    try:
        limit = int(qs.get("limit") or pack_versions.DEFAULT_LIMIT)
    except ValueError:
        limit = 0
    if not 1 <= limit <= pack_versions.MAX_LIMIT:
        raise HttpError(
            400, f"limit must be an integer 1-{pack_versions.MAX_LIMIT}", field="limit"
        )
    before = None
    if qs.get("next_token"):
        try:
            before = pack_versions.decode_token(qs["next_token"])
        except pack_versions.TokenError:
            raise HttpError(400, "Invalid next_token", field="next_token") from None
    entries, more = pack_versions.listing(
        s3, REGISTRY_BUCKET, pack_id, limit=limit, before=before
    )
    active = pack_status.pointer(s3, REGISTRY_BUCKET, pack_id)
    return respond(
        200,
        {
            "pack_id": pack_id,
            "active": active["version"] if active else None,
            "versions": entries,
            "next_token": pack_versions.encode_token(more)
            if more is not None
            else None,
        },
        event,
    )


def _version(event: dict) -> dict:
    """GET /packs/{pack_id}/versions/{version}: one stored version's document."""
    pack_id = _visible_pack(event)
    raw = str((event.get("pathParameters") or {}).get("version") or "")
    if not raw.isdigit() or len(raw) > 9:
        raise HttpError(400, "`version` is not a version number")
    found = pack_versions.one(s3, REGISTRY_BUCKET, pack_id, int(raw))
    if found is None:
        raise HttpError(404, f"No {pack_id} v{int(raw)} is stored here")
    return respond(200, found, event)


def _create(event: dict) -> dict:
    """POST /packs: mint an incomplete development draft."""
    body = json_body(event)
    pack_id = _body_pack(body)
    create = field(body, "create", dict, required=True)
    if field(body, "pack", dict) is not None or field(body, "base_prompt") is not None:
        raise HttpError(
            400,
            "A create request carries no patch",
            detail="Creating a pack already writes its document and its "
            "prompt. Create it, then revise it with PATCH /packs/{pack_id}.",
        )
    groups = caller_groups(event)
    # Decided by the rule `GET /deployment/settings` reports as `may_create_pack`; the
    # two checks below only say which half refused.
    if not pack_access.may_create_pack(groups, ROLE):
        _require_floor(groups)
        refused = pack_tiers.edit_refusal(ROLE)
        raise HttpError(403, refused[0], detail=refused[1])
    # A create has no document yet to declare an `entitlement`, which is why the floor
    # above is the whole authorization, and why it is a development draft.
    if errors := pack_compose.check_spec(create):
        raise HttpError(400, errors[0], detail=" ".join(errors[1:]) or None)
    composed = composer_client.call(
        {
            "pack_id": pack_id,
            "roles": [],
            "actor": _actor(event),
            "tier": pack_tiers.CREATE,
            "create": create,
            **_door(event),
        }
    )
    status = int(composed.get("status") or 201)
    return respond(status if status >= 400 else 201, composed, event)


def _patch(event: dict) -> dict:
    """PATCH /packs/{pack_id}: content or structure keys, as the pack's next version."""
    pack_id = _path_pack(event)
    body = json_body(event)
    if "create" in body:
        raise HttpError(400, "A patch carries no `create`; POST /packs creates a pack")
    patch = field(body, "pack", dict)
    base_prompt = field(body, "base_prompt")
    expected = field(body, "expected_version", int)
    keys = set(patch or {}) | ({"base_prompt"} if base_prompt is not None else set())
    if pack_tiers.classify(keys) is None:
        raise HttpError(
            400,
            "A patch must name at least one writable key",
            detail="Content: "
            + ", ".join(sorted(pack_tiers.CONTENT_KEYS))
            + " or `base_prompt`. Structure, in development: any other pack key.",
        )
    unknown = sorted(
        keys
        - pack_tiers.CONTENT_KEYS
        - pack_tiers.CONTENT_DOCUMENTS
        - pack_tiers.STRUCTURE_KEYS
        - pack_tiers.IGNORED_KEYS
    )
    if unknown:
        raise HttpError(400, f"{', '.join(unknown)} is not a pack key")

    groups = caller_groups(event)
    _require_floor(groups)
    if refused := pack_tiers.edit_refusal(ROLE):
        raise HttpError(403, refused[0], detail=refused[1])
    caller = CallerEntitlement(groups, load_map())
    # The same derivation `GET /registry` reports as the pack's `access`.
    roles = pack_access.roles(caller, groups, pack_id)
    # A constant's value is content; declaring one, or either write limit, is
    # structure. Which it is needs the constants the pack already declares.
    declared = ((registry.packs().get(pack_id) or {}).get("pack") or {}).get(
        "constants"
    )
    denied = pack_tiers.check_patch(
        keys,
        roles,
        ROLE,
        constants=(patch or {}).get("constants"),
        declared=declared,
    )
    if denied:
        raise HttpError(
            403,
            f"You may not write this patch to {pack_id}",
            detail=" ".join(denied),
        )
    if errors := pack_tiers.check_values(patch, base_prompt):
        raise HttpError(400, errors[0], detail=" ".join(errors[1:]) or None)

    payload = {
        "pack_id": pack_id,
        "roles": roles,
        "actor": _actor(event),
        **_door(event),
    }
    if patch is not None:
        payload["pack"] = patch
    if base_prompt is not None:
        payload["base_prompt"] = base_prompt
    if expected is not None:
        payload["expected_version"] = expected
    composed = composer_client.call(payload)
    status = int(composed.get("status") or 200)
    return respond(status if status >= 400 else 200, composed, event)


def _body_pack(body: dict) -> str:
    pack_id = (field(body, "pack_id") or "").strip()
    if not pack_id:
        raise HttpError(400, "`pack_id` is required")
    # Before anything else looks at it: every gate and the composer itself treat this
    # string as a pack's identity, and one carrying a separator is a different pack's path.
    if not _PACK_ID.fullmatch(pack_id):
        raise HttpError(
            400,
            "`pack_id` is not a pack id",
            detail="A pack id is lowercase letters, digits and underscores, "
            "starting with a letter — the directory name under `packs/`.",
        )
    return pack_id


@route
def handler(event):
    method = str(event.get("httpMethod") or "").upper()
    resource = str(event.get("resource") or "")
    if resource.endswith("/evaluations/{test_id}") and method in ("PUT", "DELETE"):
        return _example_case(event, method)
    if resource.endswith("/packs") and method == "POST":
        return _create(event)
    if resource.endswith("/packs/{pack_id}"):
        if method == "GET":
            return _document(event)
        if method == "PATCH":
            return _patch(event)
    if method == "GET":
        if resource.endswith("/packs/status"):
            return _status(event)
        if resource.endswith("/evaluations"):
            return _evaluations(event)
        if resource.endswith("/base_prompt"):
            return _read_document(event)
        if resource.endswith("/versions"):
            return _versions(event)
        if resource.endswith("/versions/{version}"):
            return _version(event)
    return respond(405, {"error": "Method not allowed"}, event)
