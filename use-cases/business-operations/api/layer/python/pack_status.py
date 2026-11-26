# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""One pack's state, read-only: what `ops.py pack status` and `GET /packs/status` show.

Read from the records `pack_composer` keeps in the registry bucket and from the published
registry. Nothing here writes: the composer is the only writer of that bucket, including
the one refusal record per pack this module reads back.
"""

from __future__ import annotations

import json

import registry
from botocore.exceptions import ClientError

#: Where an active version came from, by the audit verb that activated it.
ORIGINS = {
    "seed": "deploy_import",  # the stored verb keeps its name from before D86
    "adopt": "adopted",
    "import": "import",
    "edit": "edit",
    "create": "create",
    # Kept for the records of an older release, whose export minted DEV's live edits.
    "export": "export",
    "rollback": "rollback",
    "retire": "retired",
}
#: Audit records read back, newest first, looking for the one that activated the
#: version. Only refused or failed writes in between push it further back.
_SCAN = 25


def refusal_key(pack_id: str) -> str:
    """The pack's last refused write: one object, overwritten by the next refusal."""
    return f"_refusals/{pack_id}.json"


def read_json(s3, bucket: str, key: str) -> dict | None:
    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise


def pointer(s3, bucket: str, pack_id: str) -> dict | None:
    return read_json(s3, bucket, f"_bundles/{pack_id}/active.json")


def origin(s3, bucket: str, pack_id: str, sha256: str) -> dict | None:
    """The audit record that made `sha256` active, or None when none is left to say.
    `by` is who the invoker said it was: for an edit, the author the route named."""
    keys = [
        obj["Key"]
        for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix=f"_audit/{pack_id}/"
        )
        for obj in page.get("Contents", [])
    ]
    for key in sorted(keys, reverse=True)[:_SCAN]:
        record = read_json(s3, bucket, key) or {}
        if record.get("state") in ("refused", "failed"):
            continue
        if (record.get("to") or {}).get("sha256") == sha256:
            return {
                "via": ORIGINS.get(record.get("verb"), record.get("verb")),
                "at": record.get("at"),
                "by": record.get("asserted_actor"),
                "change_ref": record.get("change_ref"),
            }
    return None


def routes(pack_id: str) -> list[str]:
    """The process types that route to this pack now. An incomplete pack routes none."""
    routed, _ = registry.process_type_routes()
    return sorted(t for t, owner in routed.items() if owner == pack_id)


def summary(s3, bucket: str, pack_id: str) -> dict:
    """What the console's Packs card shows: version, where it came from, whether it
    routes and why not, and the last refused write with the rule that refused it.

    `unroutable` names the reasons the pack itself cannot route: the `source` fields
    still carrying a survey marker, and the SAP tools it names that this deployment does
    not serve. Both are visible in the document or in the deployment and neither was on
    the pack's card, which is what made an unfinished pack look like a broken one.
    """
    active = pointer(s3, bucket, pack_id)
    entry = registry.packs().get(pack_id) or {}
    refusal = read_json(s3, bucket, refusal_key(pack_id))
    return {
        "pack_id": pack_id,
        "version": active["version"] if active else None,
        "origin": origin(s3, bucket, pack_id, active["sha256"]) if active else None,
        "incomplete": bool(entry.get("incomplete")),
        "routes": routes(pack_id),
        "unroutable": registry.unroutable_reasons(pack_id),
        "last_refusal": {k: refusal.get(k) for k in ("at", "verb", "error", "detail")}
        if refusal
        else None,
    }


#: `{bundle sha256: {SYMBOL: [process types]}}`, per function instance: a version's
#: SOPs never change, so its citations are read once.
_CITATIONS: dict[str, dict[str, list[str]]] = {}


def citations(b: dict) -> dict[str, list[str]]:
    """`{SYMBOL: [process types]}`: which process types' SOP names each `{{SYMBOL}}`, from
    one stored bundle (its SOPs by file name and its `process_type_to_sop`). An alias is
    listed with the type it aliases, since both run that file."""
    from pack_tiers import SYMBOL_RE

    sops = b.get("sops") or {}
    mapping = (b.get("pack") or {}).get("process_type_to_sop") or {}
    cited: dict[str, set[str]] = {}
    for process_type, key in mapping.items():
        text = sops.get(str(key).rsplit("/", 1)[-1])
        if not isinstance(text, str):
            continue
        for symbol in set(SYMBOL_RE.findall(text)):
            cited.setdefault(symbol, set()).add(process_type)
    return {symbol: sorted(types) for symbol, types in cited.items()}


def active_citations(s3, bucket: str, pack_id: str, active: dict | None):
    """The citations of the active version, or None when no version is stored.

    One GET of the active bundle, and only when the active version changed since this
    instance last read one; `active` is the pointer the caller already read.
    """
    if not active or not active.get("sha256"):
        return None
    sha = active["sha256"]
    if sha not in _CITATIONS:
        b = read_json(s3, bucket, f"_bundles/{pack_id}/{active['version']}.json")
        if not b or b.get("sha256") != sha:
            return None
        _CITATIONS[sha] = citations(b)
    return _CITATIONS[sha]
