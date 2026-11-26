# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The only writer of a pack document and of `registry.json`.

**Four callers.** The use case's deploy, through a custom resource, sends a *pack import at deploy*: the
checkout's `packs/` as a CDK asset. `PATCH /packs/{pack_id}` sends a checked *patch* of
an existing pack, and `POST /packs` a *create*. The SOP routes send a SOP *put* or
*delete* (with its mapping when a SOP is added or removed), and
`/packs/{pack_id}/evaluations/{test_id}` an example case's (`example_cases.py`). `ops.py
bundle …`, over IAM, sends a *verb*: `export`, `import`, `rollback`, `retire`, `status`,
`versions` or `import_status`; `evals-run --publish` sends `record_evaluation`, a run's
results, which are no pack's content and go under `_evaluations/`. All of them end in the
same writes in the same order, because there is no second path to the registry or the SOP
bucket.

**Packs move as bundles** (`bundle.py`). Each pack's active version is the pointer
`_bundles/<id>/active.json`, written only after everything it names is in place, and
every bundle it ever pointed at stays beside it under `_bundles/<id>/<version>.json`, so
`rollback` re-imports one. One audit record per verb goes under `_audit/<id>/`.

**In development every edit is a version.** A patch or a SOP write is the `edit` step: the
composer builds a draft from the live pack and the request's own fields (no route sends a
bundle, so none can carry structure past `pack_tiers`), and imports it, which mints the
next version with the author in its provenance and audit record. A write that names the
version it was made to (`expected_version`) is refused with a 409 once another has
moved the pointer. `export` *publishes* the active version (stored as `_bundles/<id>/released.json`);
each version minted after it carries that published version as `base`, which is what another
environment's import chains on, so the edits between published versions never travel on their own.
Elsewhere no route edits anything: content and SOPs arrive there as structure does, by
an import with a change reference.

**A pack with no pointer is version 0.** An existing stack's packs were imported before
bundles existed. The first verb that needs a version stores the live state as version 0,
so the first import chains onto it and `rollback --to 0` restores it.

**In development the checkout is the source.** The pack import at deploy takes a checkout pack with no
version, and a checkout pack whose content changed since the last deploy import took it
(stored as `_bundles/<id>/seeded.json`) as the next version, verb `seed` (its stored name) by `deploy`, note `checkout`. An
unchanged checkout is skipped, so a deploy never reverts a console edit. When the checkout
and an edit minted here both changed a pack since the last deploy import, the deploy fails for that
pack, naming both, before it imports anything (`_checkout_step`). Every stack adopts its
live packs as version 0 and republishes the platform prompt; outside development a pack
arrives only by `bundle import`.

**Edits made in place before edits were versions.** Earlier releases wrote a content edit
straight over the live pack, so a live file could differ from the active version. The
seed settles that once: in development it mints such edits as a version (`adopted
edits`); elsewhere it fails the deploy, naming each pack and file, so a hotfix is never
dropped without anyone being told. Likewise, in every role, it fails while the Config table
holds a stack's override of a rule constant, a value that is now the pack's alone.

**Why a Lambda of its own.** Writing the registry is the power to grant yourself a pack,
so the browser-facing route must not hold it. What bounds this role is that it re-checks
every patch with `pack_tiers` and every bundle with the import checks before it writes,
and every pack document it writes against `pack.schema.json` (`pack_schema`).

**Order and rollback.** SOPs first, then pack documents, `registry.json` last, and every
object put back if a later write fails: the registry carries each pack's content inline,
so a document written without its registry update would be live for the next rebuild.

**One at a time.** Reserved concurrency 1: two overlapping publishes would each mirror
the old corpus, and the later registry write would drop the earlier pack. Its invokers
retry a throttle (`composer_client`).
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import shutil
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import boto3
import bundle
import config_overrides
import example_cases
import guard_rules
import pack_compose
import pack_schema
import pack_status
import pack_tiers
import registry
from botocore.exceptions import ClientError
from contracts import PACK_ID_PATTERN
from ddb_paginate import paginate
from pack_tiers import DEVELOPMENT, TEXT_EXTENSIONS, sop_metadata

BUCKET = os.environ["REGISTRY_BUCKET"]
SOPS_BUCKET = os.environ["SOPS_BUCKET"]
#: This environment's `promotion.role`.
ROLE = os.environ.get("PROMOTION_ROLE") or DEVELOPMENT
#: The pool a bundle's groups must exist in. Unset (direct mode): not checked here.
USER_POOL_ID = os.environ.get("USER_POOL_ID", "")
#: What this deployment offers a pack: `{"tools", "model_tiers", "contacts"}`, from synth.
ENVIRONMENT = json.loads(os.environ.get("PACK_ENVIRONMENT") or "{}")
STACK = os.environ.get("STACK_NAME", "")

BUNDLES = "_bundles"
AUDIT = "_audit"

#: Matched on a refusal's text to pick the status code; a constant, not two literals.
_ABSENT = "No published pack"
_TAKEN = "already published here"
_PACK_ID = re.compile(PACK_ID_PATTERN)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _missing(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404")


def _get(bucket: str, key: str) -> dict | None:
    try:
        return s3.get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if _missing(exc):
            return None
        raise


def _get_json(key: str) -> dict | None:
    got = _get(BUCKET, key)
    return json.loads(got["Body"].read()) if got else None


def _refuse(status: int, *errors: str, **extra) -> dict:
    return {
        "status": status,
        "error": errors[0],
        "detail": " ".join(errors[1:]) or None,
        **extra,
    }


def _corpus_keys() -> list[str]:
    """Every pack-document key in the bucket. Excludes `registry.json`, which is output,
    and the `_bundles/` and `_audit/` records, which no rebuild reads.

    Paginated: the bucket is small — one object per pack per file — but a bucket has no
    small guarantee, and a truncated listing would silently publish a corpus missing its
    last packs.
    """
    keys: list[str] = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.startswith("_") and key != registry.PLATFORM_PROMPT_NAME:
                continue
            if key != registry.REGISTRY_KEY and (
                key.endswith(("/pack.json", "/base_prompt.txt"))
                or key == registry.PLATFORM_PROMPT_NAME
            ):
                keys.append(key)
    return keys


def _mirror(root: Path) -> dict[str, bytes]:
    """Copy the corpus into `root` so `registry.build` can walk it as a directory.

    Returns what it read, keyed by object key: the corpus **before** the change, which is
    what `_restore` puts back without a second read of a half-written state.
    """
    before: dict[str, bytes] = {}
    for key in _corpus_keys():
        body = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
        before[key] = body
        target = root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    return before


def _put(key: str, body: bytes, actor: str, stamp: str | None = None) -> None:
    """One corpus object. `published-by` is the audit trail, not authorization; the
    `bundle-sha256` stamp says which bundle wrote it."""
    metadata = {"published-by": actor[:256]}
    if stamp:
        metadata["bundle-sha256"] = stamp
    s3.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=body,
        ContentType="application/json" if key.endswith(".json") else "text/plain",
        Metadata=metadata,
    )


def _restore(keys: list[str], before: dict[str, bytes]) -> None:
    """Put every key in `keys` back the way `_mirror` found it, or delete it if it had no
    `before`.

    **Best effort, and deliberately so.** A rollback that raised would replace the real
    failure with its own. It logs instead, so the object that could not be put back is
    nameable from the log rather than inferred. No generation check is needed: reserved
    concurrency 1 means no other publish is writing while this one restores.
    """
    for key in keys:
        try:
            if key in before:
                _put(key, before[key], "rollback")
            else:
                s3.delete_object(Bucket=BUCKET, Key=key)
        except Exception:
            logger.exception("Rollback could not restore %s — the corpus is mixed", key)


def _short_ids(root: Path) -> dict[str, str]:
    """`short_id` -> the pack that claims it, across the whole mirrored corpus.

    Two packs sharing a prefix make their cases indistinguishable on sight, so a create
    claiming a taken one is refused up front (`validate_packs.py` is the same refusal
    for a checkout).
    """
    claimed: dict[str, str] = {}
    for pack_file in sorted(root.glob("*/pack.json")):
        try:
            document = json.loads(pack_file.read_text())
        except (OSError, json.JSONDecodeError):
            continue  # `registry.build` skips it, and the response says so
        short_id = (document.get("source") or {}).get("short_id")
        if isinstance(short_id, str):
            claimed.setdefault(short_id, pack_file.parent.name)
    return claimed


def _create(root: Path, pack_id: str, spec: object) -> tuple[list[str], list[str]]:
    """Compose a brand-new pack into the mirror. `(errors, keys_to_write)`.

    **The inverse guard to an edit's.** An edit refuses a `pack_id` it has no document
    for; a create refuses one it already has, because overwriting would replace an
    existing pack's structure with markers and the read set.
    """
    errors = pack_compose.check_spec(spec)
    if errors:
        return errors, []

    pack_dir = root / pack_id
    if (pack_dir / "pack.json").is_file():
        return [
            f"{pack_id!r} is {_TAKEN}",
            "Revise it with a patch instead of creating it again.",
        ], []

    assert isinstance(spec, dict)  # nosec B101 -- check_spec refused every other type
    short_id = str(spec["short_id"])
    claimed = _short_ids(root)
    if short_id in claimed:
        return [
            f"short_id {short_id!r} is already claimed by {claimed[short_id]!r}",
            "Two packs sharing a prefix make their cases indistinguishable on sight.",
        ], []

    document, base_prompt = pack_compose.compose(pack_id, spec)
    if errors := pack_schema.errors(document):
        return errors, []
    pack_dir.mkdir(parents=True, exist_ok=True)
    (pack_dir / "pack.json").write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n"
    )
    (pack_dir / "base_prompt.txt").write_text(base_prompt)
    return [], [f"{pack_id}/pack.json", f"{pack_id}/base_prompt.txt"]


def _patch_errors(
    event: dict, live: dict | None = None
) -> tuple[list[str], dict | None, str | None]:
    """`(errors, patch, base_prompt)` of a patch request.

    The route already ran `check_patch` for a readable 400; this is the gate, against this
    function's own `PROMOTION_ROLE` rather than anything the event says about it.

    `live` is the pack document as it stands, which decides whether a `constants` patch
    is this pack's content (a declared value) or its structure. Unreadable or absent
    reads as structure, which is what the key was before a value could be content.
    """
    pack_id = str(event.get("pack_id") or "")
    # An empty-but-present `{}` names no key; normalised so it is refused as one.
    patch = event.get("pack") or None
    base_prompt = event.get("base_prompt")
    # A non-object `pack` names no key; `check_values` says what is wrong with it.
    keys = set(patch) if isinstance(patch, dict) else set()
    if base_prompt is not None:
        keys.add("base_prompt")
    errors = pack_tiers.check_patch(
        keys,
        event.get("roles") or (),
        ROLE,
        constants=(patch or {}).get("constants") if isinstance(patch, dict) else None,
        declared=(live or {}).get("constants") if isinstance(live, dict) else None,
    )
    errors += pack_tiers.check_values(patch, base_prompt)
    if isinstance(patch, dict) and patch.get("pack_id", pack_id) != pack_id:
        errors.append("A patch may not change `pack_id`")
    return errors, patch, base_prompt


def _merge(document: dict, patch: dict) -> dict:
    """`patch` over `document`. `constants` is merged key by key: assigning it would
    delete every constant the patch does not name. Removing one is a bundle import."""
    for key, value in patch.items():
        existing = document.get(key)
        if (
            key == "constants"
            and isinstance(existing, dict)
            and isinstance(value, dict)
        ):
            document[key] = {**existing, **value}
        else:
            document[key] = value
    return document


# ─── One publish ─────────────────────────────────────────────────────────────


def _documents(objects: dict[str, bytes]) -> dict[str, dict]:
    """`{pack_id: pack document}` out of mirrored objects; an unreadable one is skipped,
    as `registry.build` skips it."""
    documents = {}
    for key, body in objects.items():
        if key.endswith("/pack.json"):
            try:
                document = json.loads(body)
            except ValueError:
                continue
            if isinstance(document, dict):
                documents[key.split("/")[0]] = document
    return documents


def _annotate(artifact: dict, pointers: dict[str, dict]) -> None:
    """`bundle: {version, sha256}` on every pack entry that has an active version."""
    for pack_id, entry in artifact.get("packs", {}).items():
        pointer = pointers.get(pack_id) or _pointer(pack_id)[0]
        if pointer:
            entry["bundle"] = {
                "version": pointer["version"],
                "sha256": pointer["sha256"],
            }


def _publish(
    root: Path,
    change,
    actor: str,
    must_survive: list[str],
    *,
    stamp: str | None = None,
    pointers: dict[str, dict] | None = None,
    dry_run: bool = False,
) -> dict:
    """Mirror, change, rebuild, write documents then the registry; or write nothing."""
    root.mkdir(parents=True, exist_ok=True)
    before = _mirror(root)
    errors, written = change(root)
    if errors:
        first = errors[0]
        return {
            "status": 404 if _ABSENT in first else 409 if _TAKEN in first else 400,
            "error": first,
            "detail": " ".join(errors[1:]) or None,
        }

    artifact = registry.build(root)
    # `registry.build` skips a pack it cannot load, so a change that made a document
    # unreadable would publish a corpus silently missing it.
    lost = sorted(set(must_survive) - set(artifact.get("packs", {})))
    if lost:
        return {
            "status": 500,
            "error": f"{', '.join(lost)} did not survive the rebuild",
            "detail": "The document could not be loaded, so nothing was published.",
        }
    # A process type routes to one pack (import check 5). Checked on the rebuilt corpus,
    # and against what was published before, so the error names the current owner.
    contested = registry.process_type_conflicts(
        {
            pack_id: entry["pack"]
            for pack_id, entry in artifact.get("packs", {}).items()
        },
        _documents(before),
    )
    if contested:
        return {
            "status": 409,
            "error": contested[0],
            "detail": " ".join(
                [
                    *contested[1:],
                    (
                        "A process type routes to exactly one pack: name another, or "
                        "retire it from its owner first. Nothing was published."
                    ),
                ]
            ),
        }
    gone = sorted(key for key in before if not (root / key).is_file())
    mirrored = {path.parent.name for path in root.glob("*/pack.json")}
    skipped = sorted(mirrored - set(artifact.get("packs", {})))
    if dry_run:
        return {"packs": len(artifact.get("packs", {})), "skipped": skipped}
    _annotate(artifact, pointers or {})
    body = (json.dumps(artifact, indent=2, sort_keys=True) + "\n").encode()

    done: list[str] = []
    try:
        for key in written:
            _put(key, (root / key).read_bytes(), actor, stamp)
            done.append(key)
        _put(registry.REGISTRY_KEY, body, actor)
    except Exception:
        # `done`, not `written`: the key that just failed was never written.
        _restore(done, before)
        raise
    # After the registry: a document left by a failed delete is no longer published.
    for key in gone:
        s3.delete_object(Bucket=BUCKET, Key=key)
    return {
        "schema": artifact.get("schema"),
        "packs": len(artifact.get("packs", {})),
        "bytes": len(body),
        "written": [*written, registry.REGISTRY_KEY],
        "skipped": skipped,
        "removed": sorted(
            {key.split("/")[0] for key in gone if key.endswith("/pack.json")}
        ),
    }


# ─── Bundles ─────────────────────────────────────────────────────────────────


def _pointer_key(pack_id: str) -> str:
    return f"{BUNDLES}/{pack_id}/active.json"


def _pointer(pack_id: str) -> tuple[dict | None, str | None]:
    """The active version and its ETag, or `(None, None)` for a pack never versioned."""
    got = _get(BUCKET, _pointer_key(pack_id))
    if not got:
        return None, None
    return json.loads(got["Body"].read()), got.get("ETag")


def _set_pointer(pack_id: str, pointer: dict, etag: str | None) -> str:
    """Move the pointer, unless it moved since it was read. Returns the new ETag."""
    condition = {"IfMatch": etag} if etag else {"IfNoneMatch": "*"}
    out = s3.put_object(
        Bucket=BUCKET,
        Key=_pointer_key(pack_id),
        Body=bundle.canonical(pointer),
        ContentType="application/json",
        **condition,
    )
    return out.get("ETag")


def _versions(pack_id: str) -> list[int]:
    found = []
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=BUCKET, Prefix=f"{BUNDLES}/{pack_id}/"
    ):
        for obj in page.get("Contents", []):
            stem = obj["Key"].rsplit("/", 1)[-1].removesuffix(".json")
            if stem.isdigit():
                found.append(int(stem))
    return sorted(found)


def _stored(pack_id: str, version: int) -> dict | None:
    b = _get_json(f"{BUNDLES}/{pack_id}/{version}.json")
    if b and "seal_sha256" not in b and b.get("sha256") == bundle.content_sha(b):
        # Stored before the seal covered version, previous and provenance. A bucket
        # policy lets only this composer write here, so seal what it wrote and re-store.
        b["seal_sha256"] = bundle.seal_sha(b)
        _store(b)
    return b


def _store(b: dict) -> None:
    key = f"{BUNDLES}/{b['pack_id']}/{b['version']}.json"
    s3.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=json.dumps(b, indent=2, sort_keys=True).encode(),
        ContentType="application/json",
        Metadata={"bundle-sha256": b["sha256"]},
    )


def _audit(pack_id: str, verb: str, record: dict) -> str:
    """One record per verb; returns its id, which `import_status` reads back.

    `asserted_actor` is who the invoker said it was. A Lambda invoke carries no verified
    caller identity, so only a CloudTrail trail that logs this function's data events
    names the IAM principal; the platform's trail logs management events only.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    record_id = f"{pack_id}/{stamp}-{verb}"
    s3.put_object(
        Bucket=BUCKET,
        Key=f"{AUDIT}/{record_id}.json",
        Body=json.dumps(
            {"id": record_id, "verb": verb, "at": _now(), **record}
        ).encode(),
        ContentType="application/json",
    )
    return record_id


def _sop_objects(pack_id: str) -> dict[str, bytes]:
    """The pack's SOP texts as stored, keyed by object key; never a sidecar."""
    objects = {}
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=SOPS_BUCKET, Prefix=f"{pack_id}/"
    ):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith(TEXT_EXTENSIONS) and key.count("/") == 1:
                objects[key] = s3.get_object(Bucket=SOPS_BUCKET, Key=key)["Body"].read()
    return objects


def _snapshot(pack_id: str, evals: list | None = None) -> dict | None:
    """The live pack as a draft bundle, or None when no pack document is published."""
    got = _get(BUCKET, f"{pack_id}/pack.json")
    if not got:
        return None
    try:
        pack = json.loads(got["Body"].read())
    except ValueError:
        pack = None
    prompt = _get(BUCKET, f"{pack_id}/base_prompt.txt")
    sops = {
        key.split("/", 1)[1]: body.decode("utf-8", "replace")
        for key, body in _sop_objects(pack_id).items()
    }
    return bundle.unsealed(
        pack_id,
        pack,
        prompt["Body"].read().decode("utf-8") if prompt else "",
        sops,
        evals or [],
    )


def _active(
    pack_id: str, *, adopt: bool
) -> tuple[dict | None, str | None, dict | None]:
    """`(pointer, etag, active bundle)`. A live pack with no pointer is version 0: its
    live state, sealed, and stored when `adopt` so a later rollback can return to it."""
    pointer, etag = _pointer(pack_id)
    if pointer:
        return pointer, etag, _stored(pack_id, pointer["version"])
    live = _snapshot(pack_id)
    if live is None:
        return None, None, None
    v0 = bundle.seal(
        live, 0, None, {"adopted_at": _now(), "stack": STACK, "change_note": "adopted"}
    )
    pointer = {"version": 0, "sha256": v0["sha256"], "activated_at": _now()}
    if adopt:
        _store(v0)
        etag = _set_pointer(pack_id, pointer, None)
        _audit(pack_id, "adopt", {"asserted_actor": "composer", "to": _brief(v0)})
        logger.info("Adopted %s as version 0 (%s)", pack_id, v0["sha256"])
    return pointer, etag, v0


def _brief(b: dict | None) -> dict | None:
    return {"version": b["version"], "sha256": b["sha256"]} if b else None


def _drift(live: dict | None, active: dict | None) -> list[str]:
    """The files the live state no longer shares with the active bundle: edits made in
    place before every edit was a version, which only the pack import at deploy still looks for."""
    if not live or not active:
        return []
    # Recomputed rather than read from `files`, which the bundle's sha256 does not cover.
    ours, theirs = bundle.file_hashes(live), bundle.file_hashes(active)
    return sorted(k for k in set(ours) | set(theirs) if ours.get(k) != theirs.get(k))


def _content_errors(b: dict) -> list[str]:
    """Import checks 3 and 4: the values, the SOP symbols, and every mapped SOP present."""
    if b.get("retired"):
        return []
    pack_id, pack, sops = b["pack_id"], b["pack"], b.get("sops") or {}
    errors = []
    if pack.get("pack_id") != pack_id:
        errors.append(
            f"pack.json names pack_id {pack.get('pack_id')!r}, not {pack_id!r}"
        )
    errors += pack_tiers.check_values(
        {
            k: v
            for k, v in pack.items()
            if k in ("display_name", "description", "constants", "entitlement")
        },
        b.get("base_prompt") or "",
    )
    # The same messages for a key both name; each said once.
    errors = list(dict.fromkeys(errors + pack_schema.errors(pack)))
    errors += registry.sop_kind_errors(pack)
    for name, text in sorted(sops.items()):
        if error := pack_tiers.undeclared_symbols(
            f"{pack_id}/{name}", text, pack.get("constants")
        ):
            errors.append(error)
    # An incomplete pack routes nothing, so a SOP it maps may be written after it: a created
    # pack gets its SOPs one write at a time.
    mapped = (
        {}
        if registry.is_incomplete(pack.get("source"))
        else pack.get("process_type_to_sop")
    )
    for process_type, key in (mapped or {}).items():
        owner, _, name = str(key).partition("/")
        stem = os.path.splitext(name)[0]
        if owner != pack_id or not any(
            f"{stem}{ext}" in sops for ext in TEXT_EXTENSIONS
        ):
            errors.append(
                f"{process_type} maps to {key!r}, which the bundle does not carry"
            )
    return errors


def _group_exists(group: str) -> bool:
    try:
        boto3.client("cognito-idp").get_group(GroupName=group, UserPoolId=USER_POOL_ID)
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            return False
        raise


def _env_errors(b: dict) -> tuple[list[str], list[str]]:
    """Import check 6: `(errors, warnings)` for what the bundle requires of this stack.

    An unset contact is a warning, as it is at synth: the agent reads the placeholder
    verbatim, but `PUT /deployment/settings` can still set it after the import.
    """
    needs = bundle.requires(b)
    errors, warnings = [], []
    offered = {k: set(ENVIRONMENT[k]) for k in ENVIRONMENT}
    if missing := sorted(
        set(needs["tools"]) - offered.get("tools", set(needs["tools"]))
    ):
        errors.append(f"This stack offers no tool {', '.join(missing)}")
    served = offered.get("model_tiers", set(needs["model_tiers"]))
    if missing := sorted(set(needs["model_tiers"]) - served):
        errors.append(f"This stack serves no model tier {', '.join(missing)}")
    if USER_POOL_ID:
        for group in needs["groups"]:
            if not _group_exists(group):
                errors.append(
                    f"Group {group} does not exist here; create it with "
                    f"`ops.py add-group {group}` and import again"
                )
    contacts = offered.get("contacts", set(needs["contacts"]))
    if missing := sorted(set(needs["contacts"]) - contacts):
        warnings.append(
            f"Contacts {', '.join(missing)} are unset; set them in PUT /deployment/settings"
        )
    return errors, warnings


def _put_sop(key: str, body: bytes, actor: str, stamp: str | None) -> str | None:
    """The SOP, then its sidecar naming its pack. Returns the SOP's S3 version id, which
    case evidence records."""
    metadata = {"uploaded-by": actor[:256], "uploaded-at": _now()}
    if stamp:
        metadata["bundle-sha256"] = stamp
    out = s3.put_object(
        Bucket=SOPS_BUCKET,
        Key=key,
        Body=body,
        ContentType="text/markdown" if key.endswith(".md") else "text/plain",
        Metadata=metadata,
    )
    if sidecar := sop_metadata(key):
        s3.put_object(
            Bucket=SOPS_BUCKET,
            Key=sidecar[0],
            Body=sidecar[1],
            ContentType="application/json",
        )
    return out.get("VersionId")


def _delete_sop(key: str) -> None:
    s3.delete_object(Bucket=SOPS_BUCKET, Key=key)
    if sidecar := sop_metadata(key):
        s3.delete_object(Bucket=SOPS_BUCKET, Key=sidecar[0])


def _restore_sops(keys: list[str], before: dict[str, bytes]) -> None:
    """`_restore` for the SOP bucket: put back or delete, logging what it cannot."""
    for key in keys:
        try:
            if key in before:
                _put_sop(key, before[key], "rollback", None)
            else:
                _delete_sop(key)
        except Exception:
            logger.exception(
                "Rollback could not restore SOP %s — the corpus is mixed", key
            )


def _reindex(description: str) -> dict:
    """Start the SOP search's ingestion, or flag one for when the running job ends. Best
    effort: the run's injected SOP is read from S3 on every run, so only search lags if
    this fails."""
    try:
        import sop_store

        return sop_store.start_ingestion(description)
    except Exception as exc:  # never fail an import already written
        logger.exception("Could not start SOP ingestion")
        return {"status": "FAILED", "detail": str(exc)[:200]}


def _write_pack(root: Path, target: dict) -> tuple[list[str], list[str]]:
    """Stage `target`'s documents into the mirror; a tombstone removes the pack."""
    pack_dir = root / target["pack_id"]
    if target.get("retired"):
        shutil.rmtree(pack_dir, ignore_errors=True)
        return [], []
    pack_dir.mkdir(parents=True, exist_ok=True)
    document = {"$schema": pack_tiers.SCHEMA_REF, **target["pack"]}
    (pack_dir / "pack.json").write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n"
    )
    written = [f"{target['pack_id']}/pack.json"]
    prompt = pack_dir / "base_prompt.txt"
    if target.get("base_prompt"):
        prompt.write_text(target["base_prompt"])
        written.append(f"{target['pack_id']}/base_prompt.txt")
    elif prompt.is_file():
        prompt.unlink()
    return [], written


def _import(b: object, **kw) -> dict:
    """`_checked_import`, with a refused apply audited as one: a refusal is evidence too."""
    result = _checked_import(b, **kw)
    pack_id = b.get("pack_id") if isinstance(b, dict) else None
    applied = kw.get("dry_run", True) is False
    if applied and result.get("error") and _PACK_ID.fullmatch(str(pack_id or "")):
        _audit(
            str(pack_id),
            kw.get("verb", "import"),
            {
                "asserted_actor": kw.get("actor", "unknown"),
                "state": "refused",
                "error": result["error"],
                "change_ref": kw.get("change_ref"),
            },
        )
        _note_refusal(str(pack_id), kw.get("verb", "import"), result, kw)
    return result


def _note_refusal(pack_id: str, verb: str, result: dict, kw: dict) -> None:
    """Overwrite the pack's one refusal record, which `status` reads without walking
    the audit trail. Best effort: it never replaces the refusal being answered."""
    try:
        s3.put_object(
            Bucket=BUCKET,
            Key=pack_status.refusal_key(pack_id),
            Body=json.dumps(
                {
                    "at": _now(),
                    "verb": verb,
                    "status": result.get("status"),
                    "error": result["error"],
                    "detail": result.get("detail"),
                    "asserted_actor": kw.get("actor", "unknown"),
                    "change_ref": kw.get("change_ref"),
                }
            ).encode(),
            ContentType="application/json",
        )
    except Exception:
        logger.exception("Could not record %s's refusal", pack_id)


def _checked_import(
    b: object,
    *,
    dry_run: bool = True,
    change_ref: str | None = None,
    force: bool = False,
    actor: str = "unknown",
    verb: str = "import",
    door: dict | None = None,
) -> dict:
    """Import checks 2-7 and the diff; then, unless `dry_run`, apply.

    Check 1, the signature, comes with signing: a bundle's `signatures` is not read yet.
    """
    errors = bundle.shape_errors(b)
    if errors:
        return _refuse(400, *errors)
    assert isinstance(b, dict)  # nosec B101 -- shape_errors refused every other type
    pack_id = b.get("pack_id")
    if not isinstance(pack_id, str) or not _PACK_ID.fullmatch(pack_id):
        return _refuse(400, "`pack_id` is not a pack id")
    change_ref = str(change_ref or "").strip() or None
    if change_ref and len(change_ref) > bundle.CHANGE_REF_MAX:
        return _refuse(
            400, f"The change reference exceeds {bundle.CHANGE_REF_MAX} characters"
        )
    if ROLE != DEVELOPMENT and not change_ref:
        return _refuse(
            400,
            f"A change reference is required in {ROLE}",
            "Name the approved change (ChaRM, ServiceNow, Jira) this import carries out.",
        )

    pointer, etag, active = _active(pack_id, adopt=False)
    live = _snapshot(pack_id, (active or {}).get("evals"))
    sha = bundle.content_sha(b)

    if pointer and sha == pointer["sha256"]:
        return {
            "status": 200,
            "result": "already active",
            "pack_id": pack_id,
            **_brief(active),
        }
    if bundle.is_sealed(b):
        target = b
        got = bundle.chain_link(b)
        expected = _brief(pointer) if pointer else None
        # With no version here at all there is nothing to diverge from, so a fresh
        # stack takes any sealed version as its first.
        if not force and pointer and got != expected:
            return _refuse(
                409,
                f"{pack_id} v{b['version']} follows {got or 'nothing'}, and this "
                f"environment is at {expected or 'nothing'}",
                "Import the versions in between first. `force` skips them, and is "
                "audited.",
            )
        stored = _stored(pack_id, b["version"])
        if stored and stored["sha256"] != sha:
            return _refuse(409, f"{pack_id} v{b['version']} here is a different bundle")
    elif ROLE == DEVELOPMENT:
        target = _mint(b, pointer, active, _provenance(verb, actor, change_ref, door))
    else:
        return _refuse(
            400,
            f"Only development mints a version, and this environment is {ROLE}",
            "This bundle is a draft: it has no version, or its sha256 no longer "
            "matches its content because it was edited after export. Export it "
            "from development.",
        )

    checks = _content_errors(target)
    env_errors, warnings = _env_errors(target)
    checks += env_errors
    summary = {
        "pack_id": pack_id,
        "dry_run": dry_run,
        "from": _brief(active),
        "to": _brief(target),
        "diff": bundle.diff(live, target),
        "warnings": warnings,
        "change_ref": change_ref,
    }
    if checks:
        return _refuse(400, *checks, **summary)

    must_survive = [] if target.get("retired") else [pack_id]
    with tempfile.TemporaryDirectory(prefix="import-") as staging:
        built = _publish(
            Path(staging),
            lambda root: _write_pack(root, target),
            actor,
            must_survive,
            dry_run=True,
        )
    if "error" in built:
        return {**built, **summary}
    if dry_run:
        return {"status": 200, "result": "would activate", **summary}

    if pointer and etag is None:  # version 0, never stored: keep it to roll back to
        pointer, etag, active = _active(pack_id, adopt=True)
    before = _sop_objects(pack_id)
    sops = {
        f"{pack_id}/{name}": text.encode("utf-8")
        for name, text in (target.get("sops") or {}).items()
    }
    written: list[str] = []
    try:
        # Only what changes: an edit to one SOP leaves the others' S3 versions, which
        # case evidence records, where they were.
        for key, body in sops.items():
            if before.get(key) == body:
                continue
            written.append(key)
            _put_sop(key, body, actor, target["sha256"])
        new_pointer = {
            **_brief(target),
            "activated_at": _now(),
            "activated_by": actor,
            "change_ref": change_ref,
        }
        with tempfile.TemporaryDirectory(prefix="import-") as staging:
            result = _publish(
                Path(staging),
                lambda root: _write_pack(root, target),
                actor,
                must_survive,
                stamp=target["sha256"],
                pointers={pack_id: new_pointer},
            )
        if "error" in result:
            _restore_sops(written, before)
            return {**result, **summary}
    except Exception:
        _restore_sops(written, before)
        _audit(pack_id, verb, {"asserted_actor": actor, "state": "failed", **summary})
        raise
    for key in sorted(set(before) - set(sops)):
        _delete_sop(key)
    if not _stored(pack_id, target["version"]):
        _store(target)
    _set_pointer(pack_id, new_pointer, etag)
    record = _audit(
        pack_id,
        verb,
        {
            "asserted_actor": actor,
            "state": "active",
            "forced": force,
            **({"door": door} if door else {}),
            **summary,
        },
    )
    return {
        "status": 201,
        "result": "active",
        "import_id": record,
        "ingestion": _reindex(f"{pack_id} {verb} by {actor}"),
        **summary,
    }


#: The verbs that mint from an author's change, whose provenance names the editor.
_EDITS = ("edit", "create")


#: The people target's door (`entitlement.PEOPLE_DOOR`): its exact shape, or none.
_DOOR_KEYS = ("via", "client", "by", "display")


def _door(event: dict) -> dict | None:
    """`event["door"]` when it is the people target's door, else None. The route
    built it from verified claims; a malformed one is dropped, never stored."""
    door = event.get("door")
    if (
        isinstance(door, dict)
        and set(door) == set(_DOOR_KEYS)
        and door.get("via") == "people-gateway"
        and all(isinstance(door[k], str) and 0 < len(door[k]) <= 256 for k in door)
    ):
        return {k: door[k] for k in _DOOR_KEYS}
    return None


def _provenance(
    verb: str, actor: str, note: str | None, door: dict | None = None
) -> dict:
    if verb in _EDITS:
        out = {"edited_by": actor, "edited_at": _now(), "stack": STACK, "via": verb}
        return {**out, "door": door} if door else out
    return {
        "exported_by": actor,
        "exported_at": _now(),
        "stack": STACK,
        "change_note": note,
    }


def _last_published_key(pack_id: str) -> str:
    return f"{BUNDLES}/{pack_id}/released.json"


def _last_published(pack_id: str) -> dict | None:
    """The last version `export` published here, `{version, sha256}`."""
    return _brief(_get_json(_last_published_key(pack_id)))


def _mark_published(pack_id: str, b: dict, actor: str) -> None:
    """Mark `b` as published: what the versions minted after it chain on elsewhere.

    Stored under the names it had before D86 (`released.json`, `released_at`,
    `released_by`, audit verb `release`), so a stack's chain survives the rename."""
    if _last_published(pack_id) == _brief(b):
        return
    s3.put_object(
        Bucket=BUCKET,
        Key=_last_published_key(pack_id),
        Body=bundle.canonical(
            {**_brief(b), "released_at": _now(), "released_by": actor}
        ),
        ContentType="application/json",
    )
    # `released`, not `to`: publishing activates nothing, so `status` never reads it as
    # where the active version came from.
    _audit(pack_id, "release", {"asserted_actor": actor, "released": _brief(b)})


def _mint(b: dict, pointer: dict | None, active: dict | None, provenance: dict) -> dict:
    """The next version here, sealed from a draft: after every version stored, following
    what was active, and based on the last published version."""
    pack_id = b["pack_id"]
    version = max([*_versions(pack_id), pointer["version"] if pointer else 0]) + 1
    return bundle.seal(
        b,
        version,
        _brief(pointer) if pointer else None,
        provenance,
        bundle.diff(active, b),
        base=_last_published(pack_id),
    )


def _mint_live(
    live: dict,
    pointer: dict | None,
    etag: str | None,
    active: dict | None,
    actor: str,
    verb: str,
    note: str | None = None,
    door: dict | None = None,
) -> dict:
    """Store the live state, already published, as the next version, and activate it."""
    minted = _mint(live, pointer, active, _provenance(verb, actor, note, door))
    _store(minted)
    _set_pointer(
        live["pack_id"],
        {
            **_brief(minted),
            "activated_at": _now(),
            "activated_by": actor,
            "change_ref": None,
        },
        etag,
    )
    _audit(
        live["pack_id"],
        verb,
        {
            "asserted_actor": actor,
            **({"door": door} if door else {}),
            "from": _brief(active),
            "to": _brief(minted),
        },
    )
    return minted


# ─── An author's edit ────────────────────────────────────────────────────────


def _edit(event: dict) -> dict:
    """DEV: a patch or a SOP put or delete, imported as the next version."""
    pack_id = str(event.get("pack_id") or "")
    actor = str(event.get("actor") or "unknown")
    draft = _edit_draft(event, pack_id)
    if "error" in draft:
        if draft["status"] != 404 and _PACK_ID.fullmatch(pack_id):
            verb = next((v for v in ("sop", "example_case") if v in event), "publish")
            _note_refusal(pack_id, verb, draft, {"actor": actor})
        return draft
    result = _import(draft, dry_run=False, actor=actor, verb="edit", door=_door(event))
    if "error" not in result:
        result["version"] = (result.get("to") or {}).get(
            "version", result.get("version")
        )
    return result


def _edit_draft(event: dict, pack_id: str) -> dict:
    """The live pack with the request's change applied, as a draft; or a refusal.

    Built here from the request's fields alone: a route never sends a bundle, so no
    author can carry structure past `pack_tiers` inside one.
    """
    if not _PACK_ID.fullmatch(pack_id):
        return _refuse(400, "`pack_id` is not a pack id")
    if "example_case" in event:
        return _example_draft(event, pack_id)
    sop = event.get("sop")
    if sop is None:
        # The live document, for the constants it declares: which of them a `constants`
        # patch names is what makes it this pack's content or its structure.
        live = _get_json(f"{pack_id}/pack.json")
        errors, patch, base_prompt = _patch_errors(event, live)
        if errors:
            return _refuse(400, *errors)
    elif not isinstance(sop, dict):
        return _refuse(400, "`sop` must name the SOP and what to do with it")
    elif pack_tiers.AUTHOR not in (event.get("roles") or ()):
        return _refuse(403, "A SOP needs the pack's `entitlement.author_groups`")
    pointer, _, active = _active(pack_id, adopt=False)
    if active is None or active.get("retired"):
        return _refuse(404, f"{_ABSENT} {pack_id!r}")
    if stale := _stale(pack_id, pointer, event.get("expected_version")):
        return stale
    draft = _snapshot(pack_id, active.get("evals"))
    if sop is None:
        if patch:
            named = {k: v for k, v in patch.items() if k not in pack_tiers.IGNORED_KEYS}
            draft["pack"] = _merge(dict(draft["pack"] or {}), named)
        if base_prompt is not None:
            draft["base_prompt"] = base_prompt
        return draft
    return _sop_draft(draft, pack_id, sop, set(event.get("roles") or ()))


def _sop_draft(draft: dict, pack_id: str, sop: dict, roles: set[str]) -> dict:
    """The draft with one SOP put or deleted, keeping every SOP file mapped.

    A SOP file is always mapped to a process type: a put names a file a process type
    already maps, or brings its own (`map_as`), added to `process_type_to_sop` in the
    same version; a delete removes the file with every process type mapping to it. A
    mapping is structure, so `map_as` and a delete also need the pack-admin role.
    """
    name = str(sop.get("name") or "")
    key = f"{pack_id}/{name}"
    pack = dict(draft["pack"] or {})
    mapping = dict(pack.get("process_type_to_sop") or {})
    mapped = sorted(t for t, k in mapping.items() if k == key)
    map_as = sop.get("map_as")
    if (map_as is not None or sop.get("op") == "delete") and (
        pack_tiers.PACK_ADMIN not in roles
    ):
        return _refuse(
            403,
            "Adding or removing a SOP changes which process types route where, which "
            f"is the pack's structure and needs {pack_tiers.PACK_ADMINS_GROUP}",
        )
    if sop.get("op") == "delete":
        if draft["sops"].pop(name, None) is None:
            return _refuse(404, f"No SOP at {key}")
        if registry.triage_type(pack) in mapped:
            return _refuse(409, f"{key} is the pack's triage SOP; it cannot be removed")
        pack["process_type_to_sop"] = {
            t: k for t, k in mapping.items() if t not in mapped
        }
        shared = [t for t in pack.get("shared_sops") or [] if t not in mapped]
        if "shared_sops" in pack:
            pack["shared_sops"] = shared
        draft["pack"] = pack
        return draft
    content = sop.get("content")
    if not isinstance(content, str) or not content.strip():
        return _refuse(400, "`content` must be the SOP's text")
    if map_as is not None:
        if not (isinstance(map_as, str) and _PACK_ID.fullmatch(map_as)):
            return _refuse(400, "`map_as` must be a process type")
        if map_as in mapping:
            return _refuse(409, f"{pack_id} already maps {map_as} to {mapping[map_as]}")
        if name in draft["sops"]:
            return _refuse(
                409, f"{key} exists; write it by a process type that maps it"
            )
        mapping[map_as] = key
        pack["process_type_to_sop"] = mapping
        draft["pack"] = pack
    elif not mapped:
        return _refuse(
            400,
            f"No process type of {pack_id} maps {key}",
            "A SOP file is always mapped: create it with its process type.",
        )
    draft["sops"][name] = content
    return draft


def _stale(pack_id: str, pointer: dict, expected) -> dict | None:
    """The 409 for an edit made to a version the pointer has moved past, or None."""
    if expected is None or (
        not isinstance(expected, bool) and expected == pointer["version"]
    ):
        return None
    return _refuse(
        409,
        f"{pack_id} is at v{pointer['version']}, and this edit was made to v{expected}",
        "Someone saved it since you opened it, and nothing was written. Reload "
        "it, make your change again, and save.",
        version=pointer["version"],
    )


def _example_draft(event: dict, pack_id: str) -> dict:
    """The live pack with one example case put or deleted, as a draft; or a refusal.

    An example case is content: it says what the agent should do and changes nothing it
    does, so it needs the pack's author grant, as a SOP does. `example_cases.check` is
    the rule; the route checks it first for a readable refusal.
    """
    change = event["example_case"]
    if not isinstance(change, dict) or change.get("op") not in ("put", "delete"):
        return _refuse(400, "`example_case` must name a put or a delete")
    if pack_tiers.AUTHOR not in (event.get("roles") or ()):
        return _refuse(
            403, "An example case needs the pack's `entitlement.author_groups`"
        )
    test_id = str(change.get("test_id") or "")
    pointer, _, active = _active(pack_id, adopt=False)
    if active is None or active.get("retired"):
        return _refuse(404, f"{_ABSENT} {pack_id!r}")
    if stale := _stale(pack_id, pointer, event.get("expected_version")):
        return stale
    draft = _snapshot(pack_id, active.get("evals"))
    case = change.get("case")
    if change["op"] == "put":
        if errors := example_cases.check(test_id, case, draft["pack"] or {}):
            return _refuse(400, *errors, details=errors)
    elif not example_cases.TEST_ID.fullmatch(test_id):
        return _refuse(400, "`test_id` is not an example case id")
    cases = example_cases.apply(
        list(draft.get("evals") or []), change["op"], test_id, case
    )
    if cases is None:
        return _refuse(404, f"No example case {test_id} in {pack_id}")
    if len(cases) > example_cases.MAX_CASES:
        return _refuse(
            400, f"A pack holds at most {example_cases.MAX_CASES} example cases"
        )
    draft["evals"] = cases
    return draft


def _record_evaluation(event: dict) -> dict:
    """A run's results, from `evals-run --publish` over IAM: stored under
    `_evaluations/<pack_id>/`, as that run and as the latest. No pack changes, so no
    version is minted; any stack may record one."""
    pack_id = str(event.get("pack_id") or "")
    if not _PACK_ID.fullmatch(pack_id):
        return _refuse(400, "`pack_id` is not a pack id")
    pointer, _ = _pointer(pack_id)
    if pointer is None and f"{pack_id}/pack.json" not in _corpus_keys():
        return _refuse(404, f"{_ABSENT} {pack_id!r}")
    record = event.get("results")
    if errors := example_cases.check_results(pack_id, record):
        return _refuse(400, *errors, details=errors)
    body = json.dumps(
        {
            **record,
            "recorded_at": _now(),
            "recorded_by": str(event.get("actor") or "unknown")[:256],
        },
        indent=2,
        sort_keys=True,
        default=str,
    ).encode()
    for key in (
        example_cases.results_key(pack_id, record["run_id"].replace(":", "")),
        example_cases.results_key(pack_id),
    ):
        s3.put_object(Bucket=BUCKET, Key=key, Body=body, ContentType="application/json")
    return {"pack_id": pack_id, "run_id": record["run_id"], "recorded": True}


def _export(event: dict) -> dict:
    """The active version; in development, published, which is what the versions minted
    after it chain on elsewhere. With `snapshot`, or for an incomplete pack, the live state
    unversioned and nothing published: an incomplete pack is a draft, and its directory form
    is where an integrator completes it and imports it back."""
    pack_id = str(event.get("pack_id") or "")
    if not _PACK_ID.fullmatch(pack_id):
        return _refuse(400, "`pack_id` is not a pack id")
    _, _, active = _active(pack_id, adopt=True)
    if active and active.get("retired") and ROLE == DEVELOPMENT:
        _mark_published(pack_id, active, str(event.get("actor") or "unknown"))
        return {"status": 200, "bundle": active}  # the tombstone carries the removal
    if active is None or active.get("retired"):
        return _refuse(404, f"{_ABSENT} {pack_id!r}")
    if event.get("snapshot") or registry.is_incomplete(
        (active.get("pack") or {}).get("source")
    ):
        return {"status": 200, "bundle": _snapshot(pack_id, active.get("evals"))}
    if ROLE == DEVELOPMENT:
        _mark_published(pack_id, active, str(event.get("actor") or "unknown"))
    return {"status": 200, "bundle": active}


def _search() -> dict[str, str] | None:
    """Per pack, whether SOP search is current (`sop_store.search_states`); None
    without SOP search, and no pack's answer when it could not be read."""
    try:
        import sop_store

        rows = [
            {"key": obj["Key"], "last_modified": obj["LastModified"].isoformat()}
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=SOPS_BUCKET)
            for obj in page.get("Contents", [])
            if obj["Key"].endswith(TEXT_EXTENSIONS)
        ]
        return sop_store.search_states(rows, sop_store.latest_ingestion())
    except Exception:  # a status read never fails on the search half
        logger.exception("Could not read the SOP search's state")
        return {}


def _status(event: dict) -> dict:
    """One pack's whole state, or every pack's (no `pack_id`). Read-only."""
    if not event.get("pack_id"):
        registry.refresh_for_request()
        search = _search()
        listed = {
            key.split("/")[1]
            for key in _keys(f"{BUNDLES}/")
            if key.endswith("/active.json")
        }
        return {
            "role": ROLE,
            "packs": [
                _pack_status(pack_id, search)
                for pack_id in sorted(listed | set(_live_packs()))
            ],
        }
    pack_id = str(event["pack_id"])
    if not _PACK_ID.fullmatch(pack_id):
        return _refuse(400, "`pack_id` is not a pack id")
    registry.refresh_for_request()
    return _pack_status(pack_id, _search()) or _refuse(404, f"{_ABSENT} {pack_id!r}")


def _keys(prefix: str) -> list[str]:
    return [
        obj["Key"]
        for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=BUCKET, Prefix=prefix
        )
        for obj in page.get("Contents", [])
    ]


def _pack_status(pack_id: str, search: dict | None) -> dict | None:
    pointer, etag, active = _active(pack_id, adopt=False)
    if pointer is None:
        return None
    live = _snapshot(pack_id, (active or {}).get("evals"))
    facts = pack_status.summary(s3, BUCKET, pack_id)
    document = (registry.packs().get(pack_id) or {}).get("pack") or {}
    declared = document.get("constants") or {}
    return {
        "pack_id": pack_id,
        "role": ROLE,
        "active": {
            **pointer,
            "stored": etag is not None,
            "retired": bool((active or {}).get("retired")),
        },
        # Version 0 not stored yet is the live state from before bundles.
        "origin": facts["origin"] or ({"via": "adopted"} if etag is None else None),
        "versions": _versions(pack_id),
        # Per file, so a checkout can say which of its files differ from what is live.
        "files": bundle.file_hashes(live) if live else {},
        "incomplete": facts["incomplete"],
        "routes": facts["routes"],
        "unroutable": facts["unroutable"],
        # `off` without SOP search; None when it has none of this pack's SOPs.
        "search": "off" if search is None else search.get(pack_id),
        "last_refusal": facts["last_refusal"],
        # What a case resolves each `{{SYMBOL}}` to here, its kind and its source, and
        # the enabled write guards that read it.
        "values": guard_rules.annotate(
            config_overrides.effective_values(
                declared, pack_id, f"pack v{pointer['version']}"
            ),
            document.get("write_guards"),
        ),
    }


def _history(event: dict) -> dict:
    pack_id = str(event.get("pack_id") or "")
    if not _PACK_ID.fullmatch(pack_id):
        return _refuse(400, "`pack_id` is not a pack id")
    records = []
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=BUCKET, Prefix=f"{AUDIT}/{pack_id}/"
    ):
        for obj in page.get("Contents", []):
            r = _get_json(obj["Key"]) or {}
            records.append(
                {
                    "asserted_actor": r.get("asserted_actor") or r.get("actor"),
                    **{
                        k: r.get(k)
                        for k in (
                            "id",
                            "verb",
                            "at",
                            "state",
                            "from",
                            "to",
                            "change_ref",
                        )
                    },
                }
            )
    versions = []
    for version in _versions(pack_id):
        b = _stored(pack_id, version) or {}
        versions.append(
            {
                "version": version,
                "sha256": b.get("sha256"),
                **(b.get("provenance") or {}),
            }
        )
    return {"pack_id": pack_id, "versions": versions, "audit": records}


def _import_status(event: dict) -> dict:
    record_id = str(event.get("import_id") or "")
    if not re.fullmatch(r"[a-z][a-z0-9_]*/\d{8}T\d{12}Z-[a-z]+", record_id):
        return _refuse(400, "`import_id` is not an import id")
    record = _get_json(f"{AUDIT}/{record_id}.json")
    return record if record else _refuse(404, f"No import {record_id}")


def _rollback(event: dict) -> dict:
    """Re-import a version stored here. Validated again, because this environment's
    facts may have moved; the chain is not checked, because returning to known bytes is
    the point."""
    pack_id = str(event.get("pack_id") or "")
    to = event.get("to")
    if (
        not _PACK_ID.fullmatch(pack_id)
        or isinstance(to, bool)
        or not isinstance(to, int)
    ):
        return _refuse(400, "Rollback needs a `pack_id` and a version `to`")
    _active(pack_id, adopt=True)
    stored = _stored(pack_id, to)
    if stored is None:
        return _refuse(
            404, f"No {pack_id} v{to} is stored here", f"Stored: {_versions(pack_id)}"
        )
    return _import(
        stored,
        dry_run=event.get("dry_run", True) is not False,
        change_ref=event.get("change_ref"),
        force=True,
        actor=str(event.get("actor") or "unknown"),
        verb="rollback",
    )


def _retire(event: dict) -> dict:
    """DEV mints a tombstone, which imports like any bundle, so a removal is transported."""
    pack_id = str(event.get("pack_id") or "")
    if not _PACK_ID.fullmatch(pack_id):
        return _refuse(400, "`pack_id` is not a pack id")
    if ROLE != DEVELOPMENT:
        return _refuse(
            400, f"Only development retires a pack; import its tombstone in {ROLE}"
        )
    tombstone = {**bundle.unsealed(pack_id, None, "", {}, []), "retired": True}
    return _import(
        tombstone,
        dry_run=event.get("dry_run", True) is not False,
        change_ref=event.get("change_ref"),
        actor=str(event.get("actor") or "unknown"),
        verb="retire",
    )


def _verb(event: dict) -> dict:
    verb = event["verb"]
    if verb == "import":
        return _import(
            event.get("bundle"),
            dry_run=event.get("dry_run", True) is not False,
            change_ref=event.get("change_ref"),
            force=bool(event.get("force")),
            actor=str(event.get("actor") or "unknown"),
        )
    handlers = {
        "export": _export,
        "status": _status,
        "versions": _history,
        "import_status": _import_status,
        "rollback": _rollback,
        "retire": _retire,
        "record_evaluation": _record_evaluation,
    }
    if verb not in handlers:
        return _refuse(400, f"Unknown verb {verb!r}")
    return handlers[verb](event)


# ─── The pack import at deploy ───────────────────────────────────────────────


def _checkout_packs(checkout: Path) -> list[Path]:
    """Pack directories in the checkout. A leading underscore means "not a pack"."""
    return [p.parent for p in sorted(checkout.glob("[!_]*/pack.json"))]


def _live_packs() -> list[str]:
    return sorted({k.split("/")[0] for k in _corpus_keys() if k.endswith("/pack.json")})


def _edited_in_place(pack_id: str) -> list[str]:
    """The pack's files that differ from its active version, or [] for none."""
    _, _, active = _active(pack_id, adopt=False)
    if not active or active.get("retired"):
        return []
    return _drift(_snapshot(pack_id, active.get("evals")), active)


def _refuse_edits_in_place(live: list[str]) -> None:
    """Outside development: fail the deploy, before it writes anything, while a pack
    still holds an edit made in place, so a hotfix is never orphaned in silence. The
    remedy names only verbs this release's `ops.py` has. Keeping an edit takes the
    release this deploy replaces, which the rollback restores, so that step is the
    promotion page's."""
    edited = {p: files for p in live if (files := _edited_in_place(p))}
    if not edited:
        return
    named = "; ".join(f"{p} ({', '.join(f)})" for p, f in edited.items())
    raise RuntimeError(
        f"Edits made in place in this {ROLE} stack differ from the active version: "
        f"{named}. This release takes no edits outside development, so the deploy "
        "stops before writing anything and rolls back. To keep the edits, move them "
        "into development with the previous release's checkout, as "
        "use-cases/business-operations/docs/packs/promotion.md (Upgrading a stack that "
        "took edits in place) says. To discard them: `ops.py bundle rollback <pack_id> "
        "--to <active version> --change-ref <ref> --apply`. Then deploy again."
    )


def _rule_overrides() -> dict[str, list[str]]:
    """`{pack_id: [symbol]}`: the Config table's rows overriding a rule constant."""
    name = os.environ.get("CONFIG_TABLE")
    if not name:
        return {}
    found: dict[str, list[str]] = {}
    for row in paginate(boto3.resource("dynamodb").Table(name).scan):
        namespace, symbol = str(row.get("namespace")), str(row.get("config_key"))
        if namespace.startswith("constant#") and symbol not in pack_tiers.LIMIT_SYMBOLS:
            found.setdefault(namespace.removeprefix("constant#"), []).append(symbol)
    return {p: sorted(found[p]) for p in sorted(found)}


def _refuse_rule_overrides() -> None:
    """Fail the deploy, before it writes anything, while this stack still overrides a
    rule constant: that value is the pack's alone now, and the agent ignores the row, so
    without this the stack would quietly change the number it compares against."""
    rows = _rule_overrides()
    if not rows:
        return
    named = "; ".join(f"{p} ({', '.join(symbols)})" for p, symbols in rows.items())
    fold = (
        "set each value in the pack as an edit here (the pack's `constants`, "
        "`PATCH /packs/{pack_id}` or the console's pack editor), which makes a version"
        if ROLE == DEVELOPMENT
        else "set each value in the pack in development and import that version here "
        "with --change-ref"
    )
    raise RuntimeError(
        f"This {ROLE} stack overrides rule constants, whose value is now the pack's "
        f"alone: {named}. The deploy stops before writing anything and rolls back. With "
        f"the release still running here, {fold}; then delete each row (`PUT /packs/<pack_id>/controls` "
        'with `{"limits": {"<SYMBOL>": null}}`, or clear its field '
        "on the console's Packs page and save). Then deploy again."
    )


def _adopt_edits(pack_id: str) -> None:
    """Development: an edit made in place before every edit was a version becomes the
    next version, `adopted edits`, so the live pack is a version again."""
    pointer, etag, active = _active(pack_id, adopt=False)
    if not active or active.get("retired"):
        return
    live = _snapshot(pack_id, active.get("evals"))
    if _drift(live, active):
        minted = _mint_live(
            live, pointer, etag, active, "deploy", "adopt", "adopted edits"
        )
        logger.info("Adopted %s's edits as version %s", pack_id, minted["version"])


def _deploy_import_key(pack_id: str) -> str:
    return f"{BUNDLES}/{pack_id}/seeded.json"


def _record_deploy_import(pack_id: str, sha: str, version: int) -> None:
    """Remember the checkout content the stack last took from a deploy, and as which
    version, so the next deploy imports only a change to it."""
    s3.put_object(
        Bucket=BUCKET,
        Key=_deploy_import_key(pack_id),
        Body=bundle.canonical(
            {"content_sha": sha, "version": version, "seeded_at": _now()}
        ),
        ContentType="application/json",
    )


def _is_edit(b: dict) -> bool:
    """A version an author's change minted (console, REST, MCP), or one the deploy import minted
    from an edit an earlier release made in place."""
    p = b.get("provenance") or {}
    return "edited_by" in p or p.get("change_note") == "adopted edits"


def _checkout_step(draft: dict) -> str:
    """Development: what a deploy does with a checkout pack that already has a version.
    `skip`, `import`, or the reason it refuses the pack.

    The baseline is the version the last deploy import took or found the checkout in
    (`seeded.json`). A stack imported before that record existed has none, so its baseline
    is its newest version that no edit minted: every edit after it is one the checkout
    may not hold. The checkout is unchanged when the record names its content, or when a
    version from the baseline on holds it (an `export --to-dir` or an explicit `bundle
    import` put it there). A changed checkout is imported unless an edit was minted
    after the baseline; then neither side wins, and the deploy says how to settle it.
    """
    pack_id, sha = draft["pack_id"], bundle.content_sha(draft)
    seeded = _get_json(_deploy_import_key(pack_id))
    if seeded and seeded.get("content_sha") == sha:
        return "skip"
    pointer, _ = _pointer(pack_id)
    stored = [b for v in _versions(pack_id) if (b := _stored(pack_id, v))]
    if any(b["version"] == pointer["version"] and b.get("retired") for b in stored):
        return "skip"  # retired on purpose: the checkout should drop it, not revive it
    since = (
        seeded["version"]
        if seeded
        else max((b["version"] for b in stored if not _is_edit(b)), default=-1)
    )
    landed = [
        b["version"] for b in stored if b["sha256"] == sha and b["version"] >= since
    ]
    if landed or pointer["sha256"] == sha:
        _record_deploy_import(pack_id, sha, max(landed, default=pointer["version"]))
        return "skip"
    # An import, rollback or deploy import after an edit replaced it on purpose, so only the
    # edits since the newest such version are still live and could be lost.
    settled = max((b["version"] for b in stored if not _is_edit(b)), default=since)
    edits = [b for b in stored if b["version"] > max(since, settled) and _is_edit(b)]
    if not edits:
        return "import"
    base = next((b for b in stored if b["version"] == since), None) or next(
        (b for b in stored if b["version"] == pointer["version"]), None
    )
    files = _drift(draft, base) or ["its content"]
    by = ", ".join(
        f"v{b['version']} by "
        f"{(b.get('provenance') or {}).get('edited_by') or 'an edit made in place'}"
        for b in edits
    )
    return (
        f"{pack_id}: the checkout changed since the last deploy took it "
        f"({', '.join(files)}), and so did this stack ({by}). The deploy takes neither, "
        "so nobody's edit is replaced unseen, and rolls back. Keep the stack's edits: "
        f"move use-cases/business-operations/packs/{pack_id} aside, run `ops.py bundle "
        f"export {pack_id} --to-dir use-cases/business-operations/packs`, and merge "
        "your checkout change into it. Or replace them with the checkout: `ops.py "
        f"bundle import use-cases/business-operations/packs/{pack_id} --apply`. Then "
        "deploy again."
    )


_DEPLOY_IMPORT_PROOF: str | None = None
#: The verb a pack import at deploy records. Stored in every version and audit record it
#: wrote before D86 renamed the step, so it keeps that name.
DEPLOY_IMPORT_VERB = "seed"


def _deploy_import_proof() -> str:
    """The secret only the deploy import invoker is given (`business_operations/packs.py`)."""
    global _DEPLOY_IMPORT_PROOF
    if _DEPLOY_IMPORT_PROOF is None:
        _DEPLOY_IMPORT_PROOF = boto3.client("secretsmanager").get_secret_value(
            SecretId=os.environ["DEPLOY_IMPORT_PROOF_ARN"]
        )["SecretString"]
    return _DEPLOY_IMPORT_PROOF


def _is_the_deploys_import(event: dict) -> bool:
    """A pack import at deploy publishes the platform prompt and adopts live packs in every role, with
    no seal or change reference: only the deploy's import invoker may ask for one, and it
    proves it with a secret no operator policy grants. Fails closed."""
    proof = event.get("DeployImportProof")
    return isinstance(proof, str) and hmac.compare_digest(
        proof.encode(), _deploy_import_proof().encode()
    )


def _deploy_import_request(event: dict) -> dict:
    """A CloudFormation request through the custom-resource provider."""
    physical = (
        event.get("PhysicalResourceId") or "pack-seed"
    )  # its physical id from before D86
    if event["RequestType"] == "Delete":
        return {"PhysicalResourceId": physical}
    props = event["ResourceProperties"]
    with tempfile.TemporaryDirectory(prefix="deploy-import-") as staging:
        checkout = Path(staging) / "checkout"
        archive = Path(staging) / "packs.zip"
        s3.download_file(props["AssetBucket"], props["AssetKey"], str(archive))
        with zipfile.ZipFile(archive) as z:
            z.extractall(checkout)
        live = _live_packs()
        # Only the template that asks for it: a rollback's restores the old one, so
        # it never fails on this even if it reaches this code before the composer's
        # own rollback does.
        if ROLE != DEVELOPMENT and props.get("InPlaceEdits") == "refuse":
            _refuse_edits_in_place(live)
        if props.get("RuleOverrides") == "refuse":
            _refuse_rule_overrides()
        for pack_id in live:
            _, _, active = _active(pack_id, adopt=True)
            if ROLE != DEVELOPMENT or not active:
                continue
            if _last_published(pack_id) is None:
                # Before publishing, the version DEV held is the one other stacks chain on.
                _mark_published(pack_id, active, "deploy")
            _adopt_edits(pack_id)
        drafts, refused = {}, {}
        for pack_dir in _checkout_packs(checkout) if ROLE == DEVELOPMENT else ():
            draft = bundle.from_dir(pack_dir)
            if not _pointer(pack_dir.name)[0]:
                drafts[pack_dir.name] = draft
            # Only the template that asks for it, for the same reason as above.
            elif props.get("CheckoutChanges") == "import":
                step = _checkout_step(draft)
                if step == "import":
                    drafts[pack_dir.name] = draft
                elif step != "skip":
                    refused[pack_dir.name] = step
        for pack_id, why in refused.items():
            refusal = {"error": why, "status": 409}
            _audit(
                pack_id,
                DEPLOY_IMPORT_VERB,
                {"asserted_actor": "deploy", "state": "refused", **refusal},
            )
            _note_refusal(pack_id, DEPLOY_IMPORT_VERB, refusal, {"actor": "deploy"})
        if refused:
            # Before importing any pack, so a refused deploy changes no pack.
            raise RuntimeError(" ".join(refused.values()))
        imported = []
        for pack_id, draft in drafts.items():
            again = _pointer(pack_id)[0] is not None
            result = _import(
                draft,
                dry_run=False,
                actor="deploy",
                verb=DEPLOY_IMPORT_VERB,
                change_ref="checkout" if again else None,
            )
            if "error" in result:
                raise RuntimeError(
                    f"{pack_id}: {result['error']}: {result.get('detail')}"
                )
            _record_deploy_import(
                pack_id, bundle.content_sha(draft), result.get("to", result)["version"]
            )
            imported.append(pack_id)

        def platform(root: Path) -> tuple[list[str], list[str]]:
            # Not authorable over HTTP, so the checkout's copy is the one.
            source = checkout / registry.PLATFORM_PROMPT_NAME
            target = root / registry.PLATFORM_PROMPT_NAME
            if not source.is_file() or (
                target.is_file() and target.read_bytes() == source.read_bytes()
            ):
                return [], []
            target.write_bytes(source.read_bytes())
            return [], [registry.PLATFORM_PROMPT_NAME]

        result = _publish(
            Path(staging) / "corpus", platform, "deploy", must_survive=live
        )
    if "error" in result:
        raise RuntimeError(f"{result['error']}: {result.get('detail')}")
    logger.info(
        "Imported %s in %s; adopted %s; published %s", imported, ROLE, live, result
    )
    return {"PhysicalResourceId": physical}


def handler(event, _context):
    """A pack import from the deploy, a verb from `ops.py`, or an edit or create from a route.

    A refusal comes back as a normal return carrying `status`: the caller is a Lambda
    invoke, and a raise would reach it as an opaque `FunctionError`. A failed *write*
    does raise, after the rollback: the route turns that into its 502.
    """
    if "RequestType" in event:
        if not _is_the_deploys_import(event):
            # A raise, so the deploy fails loudly if its own proof is ever wrong.
            raise PermissionError(
                "A pack import at deploy comes only from the deploy's import invoker; invoke the "
                "composer with a verb instead"
            )
        return _deploy_import_request(event)
    if "verb" in event:
        return _verb(event)
    if refused := pack_tiers.edit_refusal(ROLE):
        return _refuse(403, *refused)
    if event.get("tier") != pack_tiers.CREATE:
        return _edit(event)
    pack_id = str(event.get("pack_id") or "")
    actor = str(event.get("actor") or "unknown")
    with tempfile.TemporaryDirectory(prefix="corpus-") as staging:
        result = _publish(
            Path(staging),
            lambda root: _create(root, pack_id, event.get("create")),
            actor,
            [pack_id],
        )
    if "error" not in result:
        pointer, etag = _pointer(pack_id)
        active = _stored(pack_id, pointer["version"]) if pointer else None
        live = _snapshot(pack_id)
        result["version"] = _mint_live(
            live, pointer, etag, active, actor, "create", door=_door(event)
        )["version"]
    return result
