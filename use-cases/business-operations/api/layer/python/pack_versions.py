# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""A pack's stored versions, read-only: `GET /packs/{pack_id}/versions[/{version}]`.

Read from what `pack_composer` keeps in the registry bucket: one sealed bundle per
version under `_bundles/<pack_id>/<v>.json`, and one audit record per verb under
`_audit/<pack_id>/`. The composer is the only writer of both; nothing here writes.
"""

from __future__ import annotations

import base64
import binascii
import json

from pack_status import ORIGINS, read_json
from pack_tiers import CONSTANT_PARTS, EVALUATIONS, PART_NAMES, PARTS, PROCEDURES

DEFAULT_LIMIT, MAX_LIMIT = 20, 50
#: Audit records read, newest first, looking for what activated the page's versions.
AUDIT_SCAN = 200


class TokenError(ValueError):
    pass


def stored_versions(s3, bucket: str, pack_id: str) -> list[int]:
    """Every version stored for the pack, newest first."""
    found = []
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=f"_bundles/{pack_id}/"
    ):
        for obj in page.get("Contents", []):
            stem = obj["Key"].rsplit("/", 1)[-1].removesuffix(".json")
            if stem.isdigit():
                found.append(int(stem))
    return sorted(found, reverse=True)


def bundle_key(pack_id: str, version: int) -> str:
    return f"_bundles/{pack_id}/{version}.json"


def encode_token(before: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"before": before}).encode()).decode()


def decode_token(token: str) -> int:
    try:
        state = json.loads(base64.urlsafe_b64decode(token.encode()))
    except (binascii.Error, ValueError):
        raise TokenError from None
    if not isinstance(state, dict) or set(state) != {"before"}:
        raise TokenError
    before = state["before"]
    if not isinstance(before, int) or isinstance(before, bool) or before < 0:
        raise TokenError
    return before


def constants_changed(b: dict) -> bool:
    keys = (b.get("changes") or {}).get("keys") or {}
    return any(
        "constants" in (keys.get(k) or []) for k in ("added", "removed", "changed")
    )


def _constant_parts(symbols: set[str]) -> set[str]:
    return {CONSTANT_PARTS.get(s, PROCEDURES) for s in symbols}


def changed_parts(b: dict, before: dict | None) -> list[str]:
    """The pack parts `b` changed against the version before it, in the parts' order."""
    changes = b.get("changes") or {}
    keys = changes.get("keys") or {}
    files = changes.get("files") or {}
    parts: set[str] = set()
    for key in (
        *(keys.get("added") or []),
        *(keys.get("removed") or []),
        *(keys.get("changed") or []),
    ):
        if key == "constants":
            new = (b.get("pack") or {}).get("constants") or {}
            old = ((before or {}).get("pack") or {}).get("constants") or {}
            moved = {s for s in set(new) | set(old) if new.get(s) != old.get(s)}
            parts |= _constant_parts(moved) if moved and before else {PROCEDURES}
        else:
            parts.add(PARTS.get(key, PROCEDURES))
    for name in (
        *(files.get("added") or []),
        *(files.get("removed") or []),
        *(files.get("changed") or []),
    ):
        if name == "evals.json":
            parts.add(EVALUATIONS)
        elif name == "base_prompt.txt" or name.startswith("sops/"):
            parts.add(PROCEDURES)
    return [p for p in PART_NAMES if p in parts]


def origins(s3, bucket: str, pack_id: str, wanted: set[str]) -> dict[str, dict]:
    """`{sha256: origin}`: the audit record that made each version active here.

    The earliest matching record that is not a rollback, else the earliest rollback,
    among the newest `AUDIT_SCAN` records; a version none of them names has none.
    """
    keys = [
        obj["Key"]
        for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix=f"_audit/{pack_id}/"
        )
        for obj in page.get("Contents", [])
    ]
    found: dict[str, tuple[bool, dict]] = {}
    for key in sorted(keys, reverse=True)[:AUDIT_SCAN]:
        record = read_json(s3, bucket, key) or {}
        sha = (record.get("to") or {}).get("sha256")
        if record.get("state") in ("refused", "failed") or sha not in wanted:
            continue
        rollback = record.get("verb") == "rollback"
        # Newest first, so a later match is an earlier record; a rollback never
        # replaces the record that first brought the version in.
        if sha in found and rollback and not found[sha][0]:
            continue
        found[sha] = (
            rollback,
            {
                "via": ORIGINS.get(record.get("verb"), record.get("verb")),
                "by": record.get("asserted_actor"),
                "at": record.get("at"),
                "change_ref": record.get("change_ref"),
                "door": record.get("door"),
            },
        )
    return {sha: origin for sha, (_, origin) in found.items()}


def entry(b: dict, before: dict | None, origin: dict | None) -> dict:
    """One version as the Versions tab lists it."""
    provenance = b.get("provenance") or {}
    return {
        "version": b.get("version"),
        "sha256": b.get("sha256"),
        "made_by": provenance.get("edited_by") or provenance.get("exported_by"),
        "made_at": provenance.get("edited_at") or provenance.get("exported_at"),
        "made_in": provenance.get("stack"),
        # The provenance names its verb only for an edit or a create; the composer
        # records every other mint (seed, import, adopt, export) as an export.
        "via": provenance.get("via") or "export",
        "change_note": provenance.get("change_note"),
        # The door an edit came through: the people Gateway's, or None for the console.
        "door": provenance.get("door"),
        "parts": changed_parts(b, before),
        "changes": b.get("changes") or {},
        "origin": origin,
    }


def listing(
    s3, bucket: str, pack_id: str, *, limit: int, before: int | None
) -> tuple[list[dict], int | None]:
    """`(entries, next before)`: one page of versions, newest first."""
    versions = [
        v for v in stored_versions(s3, bucket, pack_id) if before is None or v < before
    ]
    page, more = versions[:limit], len(versions) > limit
    bundles = {v: read_json(s3, bucket, bundle_key(pack_id, v)) or {} for v in page}

    def previous_of(b: dict) -> dict | None:
        link = (b.get("previous") or {}).get("version")
        if not isinstance(link, int) or not constants_changed(b):
            return None
        if link not in bundles:
            bundles[link] = read_json(s3, bucket, bundle_key(pack_id, link)) or {}
        return bundles[link]

    found = origins(
        s3, bucket, pack_id, {bundles[v].get("sha256") for v in page} - {None}
    )
    entries = [
        entry(bundles[v], previous_of(bundles[v]), found.get(bundles[v].get("sha256")))
        for v in page
    ]
    return entries, (page[-1] if more and page else None)


def one(s3, bucket: str, pack_id: str, version: int) -> dict | None:
    """One stored version, its listing entry and its content; None when not stored."""
    b = read_json(s3, bucket, bundle_key(pack_id, version))
    if not b:
        return None
    link = (b.get("previous") or {}).get("version")
    before = (
        read_json(s3, bucket, bundle_key(pack_id, link))
        if isinstance(link, int) and constants_changed(b)
        else None
    )
    origin = origins(s3, bucket, pack_id, {b.get("sha256")}).get(b.get("sha256"))
    return {
        "pack_id": pack_id,
        **entry(b, before, origin),
        "previous": b.get("previous"),
        "retired": bool(b.get("retired")),
        "pack": b.get("pack"),
        "base_prompt": b.get("base_prompt") or "",
        "sops": b.get("sops") or {},
        "evals": b.get("evals") or [],
    }
