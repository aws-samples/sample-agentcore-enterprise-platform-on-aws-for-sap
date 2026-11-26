# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The SOP corpus, and every rule about changing it.

**S3 is the corpus, and `pack_composer` its only writer.** This module reads it and says
what a legal write is; the SOP routes (`/packs/{pack_id}/sops`) check a write here, for a
readable refusal, then invoke the composer, which checks it again and writes it. A write
is a new version of the pack.

**Only in development.** Elsewhere a SOP, like the rest of a pack, arrives only by an
import with a change reference, so a write and a delete are both refused.

**What is NOT here: authorization beyond the author gates.** The route checks the
caller's client and base group first (`entitlement.admit`).

**Refusals travel as `SopRefusal`**, carrying the HTTP status and the body the route
returns.

**A write starts an ingestion job** (`start_ingestion`, which the composer calls after
it writes). The SOP knowledge base behind `search_sap_sops` indexes the bucket only when
a job runs, so every put and delete starts one. Amazon Bedrock
runs one job per data source at a time: when one is already running the write may
miss it, so it sets the needs-reindex flag (an SSM parameter) and answers `QUEUED`;
the knowledge stack's follow-up (`knowledge_search/reindex.py`) starts one more job
once that one finishes. The injected SOP a run is given is read from S3 on every run,
so it is live at once.

**Text only.** `.md` and `.txt`, matched case-sensitively, the same rule the bucket
policy enforces. Each SOP in a pack folder has a `<key>.metadata.json` beside it naming
its pack, which the SOP search filters on; the composer writes and deletes it with the
SOP, and this module never lists it. A PDF is refused here and nowhere converted: the agent reads the
object as UTF-8 text.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime

import boto3
import entitlement
import pack_status
import registry
import sop_lint
from botocore.exceptions import ClientError
from entitlement import CallerEntitlement
from pack_tiers import DEVELOPMENT, TEXT_EXTENSIONS, edit_refusal, undeclared_symbols

BUCKET = os.environ.get("SOPS_BUCKET", "")
#: SSM parameter names, read per call: the knowledge stack publishes them and deploys
#: after the stack this function is in, and may be off altogether.
KB_ID_PARAM = os.environ.get("SOPS_KB_ID_PARAM", "")
DATA_SOURCE_ID_PARAM = os.environ.get("SOPS_DATA_SOURCE_ID_PARAM", "")
#: Set when a write finds a job running; the knowledge stack's follow-up clears it.
REINDEX_FLAG_PARAM = os.environ.get("SOPS_REINDEX_FLAG_PARAM", "")
#: Its absence means the delete guard cannot see what a pack maps to, so `delete`
#: refuses rather than deleting unguarded.
REGISTRY_BUCKET = os.environ.get("REGISTRY_BUCKET", "")
#: This environment's `promotion.role`.
ROLE = os.environ.get("PROMOTION_ROLE") or DEVELOPMENT


#: One optional folder segment, because the corpus is `<pack_id>/<sop>.txt`. Every
#: segment must open with an alphanumeric, which rejects `..`, a dotfile and a leading
#: `/` without a second traversal check.
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)?$")
_MAX_KEY_LEN = 200
#: Bounds the request, not a useful SOP size (the largest shipped is 24 KB).
_MAX_CONTENT_BYTES = 1024 * 1024

s3 = boto3.client("s3")


class SopRefusal(Exception):
    def __init__(self, status: int, error: str, **extra):
        super().__init__(error)
        self.status = status
        self.body = {"error": error, **extra}


def pack_of(key: str) -> str:
    """The pack a SOP key belongs to, or `""` for one at the corpus root."""
    head, _, tail = key.partition("/")
    return head if tail else ""


def may_author(groups: frozenset[str], pack_id: str, path: str) -> bool:
    """May a caller in these groups write this SOP, for this pack? Three gates.

    1. The corpus floor, `SOP_AUTHOR_GROUPS`: closed when empty, because a SOP is the
       agent's instructions.
    2. The pack's own `author_groups`, which only narrows within the floor. A pack the
       registry does not know is writable by no one.
    3. The key belongs to the pack: `finance_ops/x.txt` is finance_ops's SOP.
    """
    floor = entitlement.author_floor()
    if not floor or not (groups & floor):
        return False
    if not pack_id or pack_of(path) != pack_id:
        return False
    return CallerEntitlement(groups, entitlement.load_map()).may_author(pack_id)


def may_author_any(groups: frozenset[str]) -> bool:
    """May this caller write *some* pack's SOP? The listing's `can_author`."""
    floor = entitlement.author_floor()
    if not floor or not (groups & floor):
        return False
    entitlements = entitlement.load_map()
    caller = CallerEntitlement(groups, entitlements)
    return any(caller.may_author(pack_id) for pack_id in entitlements)


def validate_key(name: object) -> tuple[str] | str:
    """`(key,)` for a name that may address the corpus, else the reason it is refused."""
    key = str(name or "").strip().lstrip("/")
    if not key:
        return "`name` is required"
    if len(key) > _MAX_KEY_LEN:
        return f"`name` exceeds {_MAX_KEY_LEN} characters"
    if not _KEY_RE.match(key):
        return (
            "`name` must be `file.md` or `folder/file.md`, using only letters, "
            "digits, dot, dash and underscore"
        )
    if not key.endswith(TEXT_EXTENSIONS):
        return f"Only {' and '.join(TEXT_EXTENSIONS)} files are accepted"
    return (key,)


def validate_upload(body: object) -> tuple[str, bytes] | str:
    """`(key, utf-8 bytes)` for a well-formed upload, else the reason it is refused."""
    if not isinstance(body, dict):
        return "Body must be a JSON object"
    validated = validate_key(body.get("name"))
    if isinstance(validated, str):
        return validated
    (key,) = validated
    content = body.get("content")
    if not isinstance(content, str):
        return "`content` must be a string of the file's text"
    encoded = content.encode("utf-8")
    if not encoded.strip():
        return "`content` is empty"
    if len(encoded) > _MAX_CONTENT_BYTES:
        return f"`content` exceeds {_MAX_CONTENT_BYTES // 1024} KB"
    return key, encoded


def require_key(name: object) -> str:
    validated = validate_key(name)
    if isinstance(validated, str):
        raise SopRefusal(400, validated)
    return validated[0]


def list_sops() -> list[dict]:
    """Every SOP in the corpus. Paginated because a bucket has no small guarantee."""
    sops: list[dict] = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET):
        for obj in page.get("Contents", []):
            if not obj["Key"].endswith(TEXT_EXTENSIONS):
                continue  # e.g. a knowledge-base metadata sidecar
            sops.append(
                {
                    "key": obj["Key"],
                    "size": obj["Size"],
                    "last_modified": obj["LastModified"].isoformat(),
                }
            )
    sops.sort(key=lambda s: s["key"])
    return sops


def read(name: object) -> dict:
    """One SOP's text, its version and who last wrote it."""
    key = require_key(name)
    try:
        obj = s3.get_object(Bucket=BUCKET, Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            raise SopRefusal(404, f"No SOP at {key}") from exc
        raise
    try:
        content = obj["Body"].read().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SopRefusal(
            415, f"{key} is not UTF-8 text, so it is not editable"
        ) from exc
    metadata = obj.get("Metadata") or {}
    active = (
        pack_status.pointer(s3, REGISTRY_BUCKET, pack_of(key))
        if REGISTRY_BUCKET and pack_of(key)
        else None
    )
    return {
        "key": key,
        "content": content,
        "size": obj["ContentLength"],
        "last_modified": obj["LastModified"].isoformat(),
        "version_id": obj.get("VersionId"),
        # The pack version an edit of this text names as `expected_version`.
        "pack_version": active["version"] if active else None,
        # Absent for a SOP the deploy imported rather than a write here.
        "uploaded_by": metadata.get("uploaded-by"),
        "uploaded_at": metadata.get("uploaded-at"),
    }


def require_development() -> None:
    """A `SopRefusal` outside development, where no SOP is written over HTTP."""
    if refused := edit_refusal(ROLE):
        raise SopRefusal(403, refused[0], detail=refused[1])


def checked_put(body: object) -> tuple[str, str]:
    """`(key, text)` of a SOP write this module would allow, or a `SopRefusal`."""
    require_development()
    validated = validate_upload(body)
    if isinstance(validated, str):
        raise SopRefusal(400, validated)
    key, content = validated
    text = content.decode("utf-8")
    constants = registry.constants().get(pack_of(key))
    if error := undeclared_symbols(key, text, constants):
        raise SopRefusal(400, error)
    return key, text


def lint(key: str, text: str) -> list[str]:
    """What `lint_sop.py` would say about this SOP against its pack as published: the
    sections, the Version line, RFC 2119, the symbols and the mapping. Findings, not
    refusals: a write refuses only what `checked_put` refuses."""
    pack_id = pack_of(key)
    pack = (registry.packs().get(pack_id) or {}).get("pack") or {}
    return sop_lint.problems(
        text,
        key.rpartition("/")[2],
        {
            "pack_id": pack_id,
            "constants": pack.get("constants") or {},
            "process_type_to_sop": pack.get("process_type_to_sop") or {},
        },
    )


def _knowledge_base() -> tuple[str, str] | None:
    """(knowledge base id, data source id), or None when SOP search is not deployed."""
    if not (KB_ID_PARAM and DATA_SOURCE_ID_PARAM):
        return None
    ssm = boto3.client("ssm")
    try:
        return tuple(
            ssm.get_parameter(Name=name)["Parameter"]["Value"]
            for name in (KB_ID_PARAM, DATA_SOURCE_ID_PARAM)
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ParameterNotFound":
            return None
        raise


def start_ingestion(description: str) -> dict:
    """Start the job that indexes the corpus as it now stands. Never raises a conflict:
    a write has already happened, and the caller is told whether it will be indexed."""
    kb = _knowledge_base()
    if kb is None:
        return {"status": "NOT_DEPLOYED"}
    try:
        job = boto3.client("bedrock-agent").start_ingestion_job(
            knowledgeBaseId=kb[0], dataSourceId=kb[1], description=description[:200]
        )["ingestionJob"]
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConflictException":
            return _queue_reindex()
        raise
    return {"job_id": job["ingestionJobId"], "status": job["status"]}


def _queue_reindex() -> dict:
    """A job is running and may miss this change: flag one more for when it ends."""
    # The write already happened. Unflagged, only search lags, and Sync reaches it.
    unflagged = {
        "status": "ALREADY_RUNNING",
        "detail": "A job was already indexing; Sync once it finishes to be sure this "
        "change is searchable.",
    }
    if not REINDEX_FLAG_PARAM:
        return unflagged
    try:
        boto3.client("ssm").put_parameter(
            Name=REINDEX_FLAG_PARAM,
            Value=datetime.now(UTC).isoformat(),
            Type="String",
            Overwrite=True,
        )
    except ClientError:
        return unflagged
    return {
        "status": "QUEUED",
        "detail": "A job was already indexing; another starts on its own once it "
        "finishes, so search serves this change within minutes.",
    }


def latest_ingestion() -> dict | None:
    """The most recent ingestion job, so the screen can say whether indexing is done."""
    kb = _knowledge_base()
    if kb is None:
        return None
    jobs = (
        boto3.client("bedrock-agent")
        .list_ingestion_jobs(
            knowledgeBaseId=kb[0],
            dataSourceId=kb[1],
            sortBy={"attribute": "STARTED_AT", "order": "DESCENDING"},
            maxResults=1,
        )
        .get("ingestionJobSummaries", [])
    )
    if not jobs:
        return None
    job = jobs[0]
    return {
        "job_id": job["ingestionJobId"],
        "status": job["status"],
        "started_at": job["startedAt"].isoformat(),
        "documents": (job.get("statistics") or {}).get("numberOfDocumentsScanned"),
    }


def search_states(sops: list[dict], job: dict | None) -> dict[str, str] | None:
    """Per pack, whether SOP search serves its SOPs as they now stand, from
    `list_sops()` rows and `latest_ingestion()`. None without a job (no SOP search).

    `stale` when a SOP was written after the latest job started (a job indexes the
    corpus as it stood when it started), or that job did not complete; `indexing` while
    it runs; else `current`.
    """
    if job is None:
        return None
    # ponytail: a delete leaves no row to compare, so a SOP deleted after the latest
    # job reads as current until the next one; list object versions if that matters.
    newest: dict[str, datetime] = {}
    for sop in sops:
        pack, at = pack_of(sop["key"]), datetime.fromisoformat(sop["last_modified"])
        newest[pack] = max(newest.get(pack, at), at)
    started = datetime.fromisoformat(job["started_at"])
    running = job["status"] in ("STARTING", "IN_PROGRESS")
    states = {}
    for pack, at in newest.items():
        if at > started or not (running or job["status"] == "COMPLETE"):
            states[pack] = "stale"
        else:
            states[pack] = "indexing" if running else "current"
    states.pop("", None)  # the corpus root belongs to no pack
    return states


def sync(description: str) -> dict:
    """`POST /sops/sync`: start a job, refusing when one is running or none exists."""
    result = start_ingestion(description)
    if result["status"] == "NOT_DEPLOYED":
        raise SopRefusal(503, "This deployment has no SOP knowledge base to index into")
    if result["status"] in ("ALREADY_RUNNING", "QUEUED"):
        raise SopRefusal(
            409, "An ingestion job is already running.", detail=result["detail"]
        )
    return result
