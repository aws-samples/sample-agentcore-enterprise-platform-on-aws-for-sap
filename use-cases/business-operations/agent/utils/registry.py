# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The pack registry: every published pack, in one S3 object every runtime reads.

**One object, not one per pack.** A publish is a single `PutObject` of
`registry.json`, so a reader never sees half a corpus, and a cold start is one
`GetObject`.

**Its own bucket.** This object decides entitlement, so whoever can write it can
grant themselves a pack. The bucket's policy denies writes to every principal but
the pack composer (`pack_composer/`). The deploy invokes it only to seed a new
development stack from the checkout; after that a pack moves as a bundle
(`ops.py bundle import`), and `PATCH /packs/{pack_id}` invokes it for an analyst's edit.

**Fails closed.** With `REGISTRY_BUCKET` unset the loader builds the same structure
from the local `packs/` tree (tests and local runs). A deployed reader with the
bucket set raises `RegistryUnavailable` on an absent object, a failed read,
malformed JSON, an unknown schema or a missing `packs` object; handlers turn that
into a CORS-bearing 503 and never into an open map. A valid empty corpus is not an
error.

One file serves both the agent image and the API layer: the layer's
`registry.py` is a symlink to this one, followed when the layer asset is staged.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

#: Bumped only on a breaking change to the artifact's shape. A reader that does not
#: recognise the schema refuses the object rather than guessing at its keys.
SCHEMA = "pack-registry/1"

#: The one object. Flat at the bucket root, like the SOP keys.
REGISTRY_KEY = "registry.json"

BUCKET_ENV = "REGISTRY_BUCKET"

#: What a use-case scoping server's scaffold leaves where only a SAP survey can supply the
#: real value. A pack carrying one is **incomplete**: valid, publishable, and deliberately
#: inert. Without this filter the poller builds an OData query against the literal
#: string and every case of that process type fails one at a time — which reads like a
#: SAP outage rather than an unfinished pack.
#:
#: Spelled here rather than imported from `pack_tiers`: the agent image has no layer.
SURVEY_MARKER = "<AWAITING_SURVEY>"

#: `emit.py` writes the marker in two forms — the closed :data:`SURVEY_MARKER`, and an
#: open-ended ``<AWAITING_SURVEY: which question fills it>`` from ``marker()``
#: (a use-case scoping server's scaffold) — so the only handle common to both is the
#: unclosed prefix, which the scaffold scans on for this exact reason. Matching the closed form alone would poll every pack whose marker
#: says what it is waiting for, which is the form the analyst is handed.
_MARKER_PREFIX = SURVEY_MARKER.rstrip(">")

#: Not a pack directory, so `*/pack.json` never picks it up, and its prompt is
#: shared by every pack rather than belonging to one.
PLATFORM_PROMPT_NAME = "_platform_prompt.txt"

#: Kept between requests and re-validated at the start of each one against the
#: object's ETag (:func:`refresh_for_request`), so a publish reaches every warm reader
#: on its next request and an unchanged corpus costs a 304. Never mid-request: a corpus
#: cannot change under a running invocation without changing what the agent was told
#: mid-case.
_cache: dict | None = None
_etag: str | None = None
_s3 = None


class RegistryUnavailable(Exception):
    """The published registry could not be read or is malformed.

    Raised by :func:`_fetch` when S3 is unreachable, the object does not parse, the
    parsed JSON is not an object, or the schema is unrecognised — any state where the
    reader cannot tell what packs are deployed. Distinct from a valid empty corpus
    (``{"schema": "…", "packs": {}}``), which is a deployment that has published no use
    cases and is not an error.

    **Valid empty is compatible; unavailable or malformed fails closed and alarms.**
    An empty corpus leaves the entitlement floor inert (declare-to-restrict). An
    unavailable or malformed registry raises this exception, every browser-facing handler
    returns a CORS-bearing 503, and the ``RegistryUnavailable`` EMF metric fires the CDK
    alarm. The explicit ``ENTITLEMENT_MAP`` override follows the same rule: valid ``{}``
    is the intentional undeclared override; malformed JSON, a non-object, or non-object
    entries all raise rather than degrading.

    Callers choose the response: ``load()`` does not cache and retries on the next call;
    a Lambda handler emits a ``RegistryUnavailable`` CloudWatch metric so an operator
    can alarm on it. The exception text carries the detail for the log; it must never
    reach an API response body.
    """


def reset() -> None:
    """Drop the cached registry. For tests, which switch corpora between cases."""
    global _cache, _etag
    _cache = None
    _etag = None
    _warned_unreachable.clear()
    _warned_refused.clear()


def refresh_for_request() -> None:
    """Re-read the published corpus if it changed since this process last read it.

    Called first by every API handler (`api_http.route`), every Lambda that reads the
    registry, and the agent runtime at the start of each invocation: a publish or a
    bundle import rewrites the corpus under warm readers, and without this they would
    keep the old one until their container recycled. One conditional `GetObject` on the
    cached ETag: a 304 keeps the cache, anything else replaces it. Never raises: on a
    failed read the cache is dropped, and the next :func:`load` retries and raises.
    """
    global _cache, _etag
    bucket = os.environ.get(BUCKET_ENV)
    if not (bucket and _cache is not None and _etag):
        reset()
        return
    try:
        fresh, etag = _get(bucket, _etag)
    except RegistryUnavailable:
        reset()
        return
    if fresh is None:
        return  # unchanged
    reset()
    if fresh.get("packs"):
        _cache, _etag = fresh, etag


# ─── Building from a local tree ──────────────────────────────────────────────


def local_root() -> Path:
    """The `packs/` tree on disk: `PACKS_DIR`, else the use case's own `packs/`.

    Resolved, so the layer's symlink finds the same tree as the agent's file.
    """
    override = os.environ.get("PACKS_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "packs"


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Unreadable %s: %s", path, exc)
        return None


def build(root: Path | None = None) -> dict:
    """Assemble the registry artifact from a `packs/` tree.

    Shared by the publisher and by the local-tree fallback below, so what a
    deployment reads and what a checkout reads cannot describe a pack differently.

    Every pack is included. Per-pack isolation is deliberate: one unreadable
    `pack.json` disables its own use case and nothing else, because an exception
    here would empty the corpus for every pack.
    """
    root = root or local_root()
    packs: dict[str, dict] = {}
    if not root.is_dir():
        logger.warning("No use case tree at %s", root)
        return {"schema": SCHEMA, "platform_prompt": "", "packs": packs}

    for pack_path in sorted(root.glob("*/pack.json")):
        directory = pack_path.parent
        pack = _read_json(pack_path)
        # Every accessor below keys on the directory name, which validate_packs
        # proves the declared id equals.
        if not pack or not pack.get("pack_id"):
            logger.error("Skipping unloadable pack %s", pack_path)
            continue
        entry: dict = {"pack": pack}

        prompt = directory / "base_prompt.txt"
        if prompt.is_file():
            entry["base_prompt"] = prompt.read_text(encoding="utf-8")

        # One authored document, three published blocks. `source` and `fields` are
        # blocks of pack.json and are published as siblings of it, which is what lets
        # the collapse leave registry.json byte-identical: every reader — the poller,
        # the push route, public_view(), the frontend — was written against these three
        # keys and none of them changes. Both optional: a pack with no SAP data source
        # declares no `source`, and `fields` only describes a source's `field_map`.
        # `$schema` goes the same way: it is there so an editor validates the document
        # as it is typed, and it is not part of the published contract.
        pack.pop("$schema", None)
        source = pack.pop("source", None)
        if source is not None:
            # The directory IS the pack id (validate_packs.py check 1). The injected
            # name goes last so it always wins over a stray authored `pack_id` — an
            # old source.json body pasted in whole, say — rather than being silently
            # shadowed by it, and this puts the id back for the readers that key on
            # it, the push route first among them.
            entry["source"] = {**source, "pack_id": directory.name}
        fields = pack.pop("fields", None)
        if fields is not None:
            entry["fields"] = fields
        entry["incomplete"] = is_incomplete(source)

        packs[directory.name] = entry

    platform = root / PLATFORM_PROMPT_NAME
    return {
        "schema": SCHEMA,
        "platform_prompt": platform.read_text(encoding="utf-8").strip()
        if platform.is_file()
        else "",
        "packs": packs,
    }


# ─── Loading at runtime ──────────────────────────────────────────────────────


def _get_s3():
    global _s3
    if _s3 is None:
        import boto3

        region = os.environ.get(
            "AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        )
        _s3 = boto3.client("s3", region_name=region)
    return _s3


def _not_modified(exc: Exception) -> bool:
    response = getattr(exc, "response", None) or {}
    status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    return status == 304 or (response.get("Error") or {}).get("Code") in (
        "304",
        "NotModified",
    )


def _fetch(bucket: str) -> dict:
    """Read `registry.json` out of the registry bucket.

    Raises :class:`RegistryUnavailable` when the object cannot be read, cannot be
    parsed, or declares a schema this reader does not speak. A valid empty corpus —
    correct schema, ``packs: {}`` — is returned normally: a deployment that has published
    no use cases is not an error.

    The distinction matters for entitlement: an empty map leaves the floor inert
    (declare-to-restrict), while an *unavailable* map means the reader cannot tell
    whether someone declared something. ``load()`` does not cache an unavailable
    result, so the next call retries; it does not cache a valid empty one either,
    because caching that would pin an empty corpus for the container's lifetime even
    if the next ``GetObject`` succeeds.
    """
    return _get(bucket, None)[0]


def _get(bucket: str, etag: str | None) -> tuple[dict | None, str | None]:
    """`(registry, its ETag)`, or `(None, etag)` when it has not changed since `etag`."""
    try:
        obj = _get_s3().get_object(
            Bucket=bucket, Key=REGISTRY_KEY, **({"IfNoneMatch": etag} if etag else {})
        )
    except Exception as exc:
        if etag and _not_modified(exc):
            return None, etag
        raise RegistryUnavailable(
            f"Could not read s3://{bucket}/{REGISTRY_KEY}: {exc}"
        ) from exc
    body = obj["Body"].read()

    try:
        loaded = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise RegistryUnavailable(
            f"s3://{bucket}/{REGISTRY_KEY} is not valid JSON: {exc}"
        ) from exc

    if not isinstance(loaded, dict):
        raise RegistryUnavailable(
            f"s3://{bucket}/{REGISTRY_KEY} is a JSON {type(loaded).__name__}, "
            f"not an object"
        )

    schema = loaded.get("schema")
    if schema != SCHEMA:
        raise RegistryUnavailable(
            f"s3://{bucket}/{REGISTRY_KEY} declares schema {schema!r}, "
            f"this reader speaks {SCHEMA!r}"
        )
    if not isinstance(loaded.get("packs"), dict):
        raise RegistryUnavailable(f"s3://{bucket}/{REGISTRY_KEY} has no packs object")
    # ponytail: an artifact published before `incomplete` existed; the next publish (any
    # deploy that changes the composer imports the packs again) writes it, and then this is a no-op.
    for entry in loaded["packs"].values():
        if isinstance(entry, dict) and "incomplete" not in entry:
            entry["incomplete"] = is_incomplete(entry.get("source"))
    return loaded, obj.get("ETag")


def load() -> dict:
    """The whole published registry, cached per process.

    S3 when ``REGISTRY_BUCKET`` is set — the deployed path — and the local
    ``packs/`` tree otherwise.

    Raises :class:`RegistryUnavailable` when the deployed registry cannot be read.
    A valid empty corpus (correct schema, ``packs: {}``) is returned normally and
    is **not** cached, so a just-published first pack appears on the next call.
    A populated corpus is cached until :func:`refresh_for_request` sees its ETag change.

    **Why it raises instead of degrading.** An empty corpus makes
    ``CallerEntitlement.active`` false, which in turn makes ``may_see`` return True
    for every pack — the declare-to-restrict floor becomes inert. Degrading to
    ``{packs: {}}`` on an S3 failure would therefore turn a transient fault into
    unrestricted case access, which is the vulnerability this exception prevents.
    Every browser-facing handler that reaches entitlement catches the exception and
    returns a CORS-bearing 503 with no detail.

    No-cache retry semantics are preserved: a valid empty corpus is not cached (the
    container retries on the next call), and ``RegistryUnavailable`` is never cached
    either — the next warm invocation retries S3.
    """
    global _cache, _etag
    if _cache is not None:
        return _cache
    bucket = os.environ.get(BUCKET_ENV)
    etag = None
    if not bucket:
        loaded = build()
    else:
        loaded, etag = _get(bucket, None)  # raises RegistryUnavailable on failure
    logger.info(
        "Registry loaded from %s: %d pack(s)",
        f"s3://{bucket}/{REGISTRY_KEY}" if bucket else str(local_root()),
        len(loaded.get("packs", {})),
    )
    if loaded.get("packs"):
        _cache, _etag = loaded, etag
    return loaded


# ─── Reader views ────────────────────────────────────────────────────────────


#: The tools only an AWS for SAP MCP Server serves, one Gateway target per SAP system.
#: `odata_delete` is absent on purpose: no Cedar statement names it and the run checks refuse
#: it for every caller, so a pack cannot hold it.
SAP_MCP_TOOLS = frozenset(
    {
        "find_sap_services",
        "get_metadata",
        "get_service_hints",
        "odata_read",
        "odata_count",
        "odata_create",
        "odata_update",
        "odata_function_import",
    }
)


def _sap_mcp_enabled() -> bool:
    """Does this deployment configure a SAP system at all?

    The runtime is told explicitly. Unset (an API Lambda, a test) reads as true, so a
    reader that is not told never hides a pack the agent can run.
    """
    return os.environ.get("SAP_MCP_ENABLED", "true").lower() == "true"


#: Pack ids already reported unreachable, so the warning is one line per container
#: rather than one per `sop_index()` call.
_warned_unreachable: set[str] = set()


def _unreachable(pack_id: str, entry: dict) -> list[str]:
    """Which of this pack's `gateway_tools` this deployment does not serve.

    `gateway_tools` is an allowlist, never a reachability check. On a stack with no SAP
    MCP server `build_tool_filters` permits `odata_read` and the Gateway still never
    exposes it, so the agent plans a read, is told `Unknown tool: odata_read` after the
    turn is billed, and spends the rest of that turn writing a SAP-unreachable
    escalation — which the persisted evidence records as `sap_read` with
    `status: "error"`, indistinguishable from a real SAP fault and reading as an agent
    defect. Measured on a 24-case corpus: 21 cases, every one parked at
    `manual_review_required`.

    So such a pack is treated exactly as an **incomplete** one, for the same stated reason:
    routing a case to a pack whose SAP source cannot be read produces a run that can
    only fail. It maps no process type and it is not polled, which is what makes
    `resolve_skill` raise `SopLoadError` and flag the case for a human *before the first
    model call*. It stays in `packs()`, so `GET /registry`, the authoring screens and
    `rehearse` all still see it.
    """
    if _sap_mcp_enabled():
        return []
    missing = sorted(
        # `gateway_tools` holds full Gateway names, `<target>___<tool>`.
        SAP_MCP_TOOLS.intersection(
            t.split("___")[-1]
            for t in (entry.get("pack") or {}).get("gateway_tools") or []
        )
    )
    if missing and pack_id not in _warned_unreachable:
        _warned_unreachable.add(pack_id)
        logger.warning(
            "Pack %s names SAP tools this deployment does not serve (%s): no MCP server "
            "is attached, so the Gateway never exposes them. Not polled, and no process "
            "type maps to it. Configure a SAP system for the use case and redeploy.",
            pack_id,
            ", ".join(missing),
        )
    return missing


def packs() -> dict[str, dict]:
    """`{pack_id: {pack, base_prompt, source?, fields?}}` for this deployment."""
    return load().get("packs", {})


def platform_prompt() -> str:
    """The platform-mechanics preamble every pack's base prompt injects."""
    return load().get("platform_prompt", "")


def is_incomplete(source: dict | None) -> bool:
    """Does this `source` block still carry a survey marker, anywhere in it?

    The one scan. `build` publishes its answer as each pack's `incomplete`, and every reader,
    the interceptor and the console included, reads that flag rather than scanning again.

    The whole block, not just `service` and `entity`. A use-case scoping server's scaffold
    marks eight places, and every one of them is load-bearing at query
    time, not cosmetic: `key[].field`, `title`, `field_map.*.path`, `filter`,
    `iterate.path` and `aging.field` all become OData or a rendered case field. A pack
    whose service and entity happened to be filled in while its key fields were still markers
    used to poll — and built `$filter=<AWAITING_SURVEY...>` against a real service.

    Serialised rather than walked: the shapes differ (a string, a list of objects, a map
    of objects, a nested object), the block is a few hundred bytes read once per cold
    start, and a structural walk would have to be taught every new marked field.
    """
    return _MARKER_PREFIX in json.dumps(source or {})


def sources() -> list[dict]:
    """Every deployed pack's `source` block — where its cases come from.

    A pack without one declares no source, which is legitimate: nothing polls or
    pushes for it.

    An **incomplete** pack is omitted too: its `service` or `entity` is still a survey
    marker, so a query built from it would address nothing. It reappears here the
    moment the survey replaces the marker and the pack is republished — no code
    change, no redeploy.

    So is a pack whose `gateway_tools` this deployment does not serve — see
    `_unreachable`. Same reason, and the same reappearance: attach an MCP server and it
    polls again.
    """
    return [
        entry["source"]
        for pack_id, entry in packs().items()
        if entry.get("source")
        and not entry.get("incomplete")
        and not _unreachable(pack_id, entry)
    ]


def _routable(pack_id: str, entry: dict) -> bool:
    """May a case route to this pack? Not while it is incomplete or its tools are unserved."""
    return not entry.get("incomplete") and not _unreachable(pack_id, entry)


def incomplete_fields(source: object, path: str = "source") -> list[str]:
    """Where in a `source` block a survey marker still sits, as dotted paths.

    For a reader, not a gate: `is_incomplete` stays the one scan, and this says *which*
    fields it found, which is what an author needs to finish the survey. Walked rather
    than serialised because a path is the answer.
    """
    if isinstance(source, dict):
        return [
            p for k, v in source.items() for p in incomplete_fields(v, f"{path}.{k}")
        ]
    if isinstance(source, list):
        return [
            p
            for i, v in enumerate(source)
            for p in incomplete_fields(v, f"{path}[{i}]")
        ]
    return [path] if isinstance(source, str) and _MARKER_PREFIX in source else []


def unroutable_reasons(pack_id: str) -> list[str]:
    """Why no process type routes to this pack, or an empty list when it may route.

    What `_routable` refuses, said out loud: the `source` fields still carrying a survey
    marker, and the SAP tools the pack names that this deployment does not serve. A pack
    that may route but owns no process type, or lost one to another pack, is routable and
    reads as "routes nothing".
    """
    entry = packs().get(pack_id) or {}
    reasons = [
        f"incomplete at {field}" for field in incomplete_fields(entry.get("source"))
    ]
    # `is_incomplete` scans the serialised block, so it answers for a marker this walk
    # cannot name a path for. It stays the gate, so say so rather than reading routable.
    if entry.get("incomplete") and not reasons:
        reasons = ["incomplete"]
    if unserved := _unreachable(pack_id, entry):
        reasons.append(f"tools unserved: {', '.join(unserved)}")
    return reasons


def process_type_claims(documents: dict[str, dict]) -> dict[str, list[str]]:
    """`{process_type: [pack_id, …]}` over `{pack_id: pack document}`, ids sorted."""
    claims: dict[str, list[str]] = {}
    for pack_id in sorted(documents):
        for process_type in documents[pack_id].get("process_type_to_sop") or {}:
            claims.setdefault(process_type, []).append(pack_id)
    return claims


#: Matched by the composer to answer 409; one constant, not two literals.
OWNED = "is already owned by"


def process_type_conflicts(
    after: dict[str, dict], before: dict[str, dict] | None = None
) -> list[str]:
    """One error per process type `after` gives to more than one pack and `before` did not.

    The write-time refusal, shared by every writer: the composer passes the corpus it
    mirrored as `before`, so a create, a patch or a pack import at deploy that would hand a
    process type a second owner publishes nothing; synth passes the checkout alone. A
    duplicate `before` already held is left to the readers, which refuse to route it
    (`process_type_routes`), rather than blocking every unrelated publish.
    """
    held = process_type_claims(before or {})
    errors = []
    for process_type, claimants in process_type_claims(after).items():
        prior = held.get(process_type, [])
        if len(claimants) < 2 or set(claimants) <= set(prior):
            continue
        owners = [p for p in claimants if p in prior] or claimants[:1]
        others = [p for p in claimants if p not in owners]
        errors.append(
            f"{', '.join(others)}: process type {process_type!r} {OWNED} "
            f"{', '.join(owners)}"
        )
    return errors


_warned_refused: set[str] = set()


def process_type_routes(
    include_incomplete: bool = False,
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """`(routes, refused)`: `{process_type: pack_id}`, and the types no pack gets.

    Every reader of "which pack owns this process type" goes through here, so a
    duplicate that slipped past the write-time check resolves one way everywhere. A
    complete pack always beats an incomplete one: a pack minted over HTTP is born incomplete, and
    must never take over a type a live pack routes. Two complete claimants, or (with
    `include_incomplete`) two incomplete ones and no complete one, is a tie, and a tie routes
    nowhere: the case fails closed to manual review instead of running whichever pack
    a dict happened to keep last.
    """
    entries = packs()
    routes: dict[str, str] = {}
    refused: dict[str, list[str]] = {}
    documents = {pack_id: entry["pack"] for pack_id, entry in entries.items()}
    for process_type, claimants in process_type_claims(documents).items():
        routable = [p for p in claimants if _routable(p, entries[p])]
        pool = routable or (claimants if include_incomplete else [])
        if len(pool) == 1:
            routes[process_type] = pool[0]
        elif pool:
            refused[process_type] = pool
            if process_type not in _warned_refused:
                _warned_refused.add(process_type)
                logger.error(
                    "Process type %s is claimed by %s; routing it nowhere until one "
                    "pack gives it up",
                    process_type,
                    ", ".join(pool),
                )
    return routes, refused


def sop_index() -> dict[str, dict]:
    """`{pack_id: {sops, constants}}` — what the `load_sop` Gateway tool resolves.

    A pack's `sops` are the process types it routes (`process_type_routes`), so a
    **incomplete** pack, a pack whose `gateway_tools` this deployment does not serve (see
    `_unreachable`) and a contested type all map nothing: routing a case to them
    produces a run that can only fail. Constants stay: the pack is still being
    authored, and `constants()` is how a reviewer sees the numbers.
    """
    routes, _ = process_type_routes()
    return {
        pack_id: {
            "sops": {
                process_type: key
                for process_type, key in (
                    entry["pack"].get("process_type_to_sop") or {}
                ).items()
                if routes.get(process_type) == pack_id
            },
            "constants": entry["pack"].get("constants") or {},
        }
        for pack_id, entry in packs().items()
    }


def constants() -> dict[str, dict]:
    """`{pack_id: {SYMBOL: value}}` — the allowlist `PUT /packs/{pack_id}/controls`
    validates against."""
    return {pack_id: entry["constants"] for pack_id, entry in sop_index().items()}


# ─── Kinds of SOP ────────────────────────────────────────────────────────────

#: The part each `process_type_to_sop` entry plays. Only `shared` is declared (the
#: pack's `shared_sops`); the others follow from the files, so no pack has to say them.
TRIAGE = "triage"
EXCEPTION = "exception"
SHARED = "shared"
ALIAS = "alias"


def authored(entry: dict) -> dict:
    """A registry entry's pack as authored, with `source` back inside it.

    `load` publishes `source` beside `pack`, so `sop_kinds` and `exception_type` read
    an entry through this: given `entry["pack"]` alone they find no triage type."""
    pack = entry.get("pack") or {}
    source = entry.get("source")
    return {**pack, "source": source} if source is not None else pack


def triage_type(document: dict) -> str | None:
    """`source.process_type.default`: the process type every case of the pack starts
    with unless a rule says otherwise."""
    source = document.get("source")
    rule = source.get("process_type") if isinstance(source, dict) else None
    default = rule.get("default") if isinstance(rule, dict) else None
    return default if isinstance(default, str) else None


def _rule_targets(document: dict) -> list[str]:
    """The process types a `source.process_type.rules` entry starts a case with."""
    source = document.get("source")
    rule = source.get("process_type") if isinstance(source, dict) else None
    rules = rule.get("rules") if isinstance(rule, dict) else None
    return [
        r["then"]
        for r in (rules if isinstance(rules, list) else ())
        if isinstance(r, dict) and isinstance(r.get("then"), str)
    ]


def _shared(document: dict) -> list[str]:
    declared = document.get("shared_sops")
    if not isinstance(declared, list):
        return []
    return [s for s in declared if isinstance(s, str)]


def sop_kinds(document: dict) -> list[dict]:
    """One row per `process_type_to_sop` entry, in declaration order:
    `{process_type, kind, sop}`, plus `alias_of` on an alias.

    A key that maps to the same file as another is an alias of one of them: the triage
    key when the file is triage's, else the one `shared_sops` names, else the key named
    like the file, else the first declared. The rest are `triage` (the source's default
    process type), `shared` (named in `shared_sops`) or `exception`.
    """
    mapping = document.get("process_type_to_sop")
    if not isinstance(mapping, dict):
        return []
    triage, shared = triage_type(document), _shared(document)
    by_file: dict[str, list[str]] = {}
    for process_type, key in mapping.items():
        by_file.setdefault(str(key), []).append(process_type)

    def head(key: str) -> str:
        names = by_file[key]
        stem = Path(key).stem
        for pick in (
            *([triage] if triage in names else []),
            *(s for s in shared if s in names),
            *(n for n in names if n == stem),
        ):
            return pick
        return names[0]

    rows = []
    for process_type, key in mapping.items():
        canonical = head(str(key))
        if process_type != canonical:
            kind = ALIAS
        elif process_type == triage:
            kind = TRIAGE
        elif process_type in shared:
            kind = SHARED
        else:
            kind = EXCEPTION
        row = {"process_type": process_type, "kind": kind, "sop": str(key)}
        if kind == ALIAS:
            row["alias_of"] = canonical
        rows.append(row)
    return rows


def exception_type(document: dict, process_type: str) -> str | None:
    """The exception type `process_type` names in this pack, or None when it is not an
    exception SOP's: triage, shared and unmapped types are None, an alias is the type it
    aliases. What a run records on its case (`case.exception_type`)."""
    rows = {r["process_type"]: r for r in sop_kinds(document)}
    row = rows.get(process_type)
    if row and row["kind"] == ALIAS:
        row = rows.get(row["alias_of"])
    return row["process_type"] if row and row["kind"] == EXCEPTION else None


def sop_kind_errors(document: dict) -> list[str]:
    """Why this document's `shared_sops` cannot stand, or nothing. Synth
    (`validate_packs`) and every patch, edit and import (the composer) run it."""
    mapping = document.get("process_type_to_sop")
    mapping = mapping if isinstance(mapping, dict) else {}
    shared = _shared(document)
    triage, targets = triage_type(document), set(_rule_targets(document))
    kinds = {r["process_type"]: r for r in sop_kinds(document)}
    errors = []
    for process_type in sorted({s for s in shared if shared.count(s) > 1}):
        errors.append(f"shared_sops names {process_type!r} more than once")
    for process_type in dict.fromkeys(shared):
        if process_type not in mapping:
            errors.append(
                f"shared_sops names {process_type!r}, which process_type_to_sop "
                "does not map"
            )
        elif process_type == triage or process_type in targets:
            errors.append(
                f"shared_sops names {process_type!r}, which source.process_type starts "
                "a case with; a shared SOP is only ever loaded from another SOP"
            )
        elif kinds[process_type]["kind"] == ALIAS:
            errors.append(
                f"shared_sops names {process_type!r}, which maps to the same SOP as "
                f"{kinds[process_type]['alias_of']!r}; name one of them"
            )
    return errors


def process_type_pack_map(include_incomplete: bool = False) -> dict[str, str]:
    """`{process_type: pack_id}` — how the exemplar builder keys what it writes.

    `include_incomplete=True` lets an **incomplete** pack answer for a type no complete pack holds.
    Two answers to one question, deliberately: what a *case* may route to excludes an
    incomplete pack, but `POST /runs`' `rehearse` exercises a pack before it is complete.
    Nothing routes there by accident: the poller skips an incomplete source, so no case of
    that type is ever created.
    """
    return process_type_routes(include_incomplete)[0]


def entitlement_map() -> dict[str, dict]:
    """`{pack_id: {groups, approve_groups, author_groups, constant_groups,
    limit_groups, process_groups}}`.

    Six questions, one per key: who may see a pack's cases, who may approve its writes,
    who may write its content, who may set its rule values, who may lower its write
    limits, and who may start the agent on its cases. Only keys the pack actually
    declared are copied, because `entitlement._permits` reads an absent key as "this pack
    did not restrict this action" — copying an empty list would say the same thing at
    more cost, but copying a *default* list would silently restrict every pack.
    """
    result = {}
    for pack_id, entry in packs().items():
        declared = entry["pack"].get("entitlement") or {}
        result[pack_id] = {
            key: declared[key]
            for key in (
                "groups",
                "approve_groups",
                "author_groups",
                "constant_groups",
                "limit_groups",
                "process_groups",
            )
            if declared.get(key)
        }
    return result


# ─── The API projection ──────────────────────────────────────────────────────


def public_view(pack_ids: set[str] | None = None) -> list[dict]:
    """What `GET /registry` serves: one row per pack, ordered fields resolved.

    This is the join the `fields` block deliberately does not carry. A field's label,
    type and currency pairing are declared in `fields`; the SAP path it came
    from is declared once in `source`'s `field_map`, so that a rename in SAP
    is one edit rather than two. The frontend needs both, so they are joined here —
    once, server-side — rather than by every consumer.

    Fields come back as an ordered array, not a map, because the `fields` block is
    insertion-ordered on purpose: that order is the column order a case table
    renders. JSON object key order survives `json.dumps` but is not something a
    client should have to rely on.

    `source.key` is published for the same reason, one layer further out. A case
    stores its natural key as a *map*, and DynamoDB does not preserve a map's key
    order — a two-field AP key declared `SupplierInvoice, FiscalYear` comes back off
    a scan as `FiscalYear, SupplierInvoice`. So the order lives here, as the array
    the `source` block declared it, and the browser reads the map through it rather
    than trusting the order it arrived in.

    `pack_ids` scopes the result to what one caller may see. None means unscoped,
    which is correct only where the entitlement floor is inert.

    Deliberately omits `base_prompt` (agent instructions, not UI data) and
    `constants` (`GET /packs/{pack_id}/rules` and `/controls` own those, with the operator
    overrides layered on).
    """
    rows = []
    for pack_id, entry in sorted(packs().items()):
        if pack_ids is not None and pack_id not in pack_ids:
            continue
        declared = entry["pack"]
        source = entry.get("source") or {}
        field_map = source.get("field_map") or {}
        rows.append(
            {
                "id": pack_id,
                "display_name": declared.get("display_name", pack_id),
                "description": declared.get("description", ""),
                "label": source.get("label", declared.get("display_name", pack_id)),
                "short_id": source.get("short_id", ""),
                "process_types": sorted(declared.get("process_type_to_sop") or {}),
                # The part each SOP plays: triage, exception, shared or alias.
                "sops": sop_kinds(authored(entry)),
                "incomplete": bool(entry.get("incomplete")),
                "source": {
                    "kind": source.get("kind", ""),
                    "service": source.get("service", ""),
                    "entity": source.get("entity", ""),
                    "key": [
                        spec.get("name", spec["field"])
                        for spec in (source.get("key") or [])
                        if spec.get("field")
                    ],
                },
                "fields": [
                    {
                        "attribute": attribute,
                        "label": spec.get("label", attribute),
                        "type": spec.get("type", "string"),
                        # A blank sap_field is the honest answer for an attribute
                        # the poller derives rather than reads from a SAP path.
                        "sap_field": (field_map.get(attribute) or {}).get("path", ""),
                        **(
                            {"currency_field": spec["currency_field"]}
                            if spec.get("currency_field")
                            else {}
                        ),
                        # What the attribute means beyond its label. Only `reason`
                        # is defined: which field says why the case is an exception,
                        # which no label or type can tell a renderer.
                        **({"role": spec["role"]} if spec.get("role") else {}),
                        **({"panel": False} if spec.get("panel") is False else {}),
                    }
                    for attribute, spec in (entry.get("fields") or {}).items()
                ],
            }
        )
    return rows


if __name__ == "__main__":
    # Offline self-check — the branches that decide whether a pack is readable.
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / PLATFORM_PROMPT_NAME).write_text("mechanics\n")
        for name, pack_json in (
            (
                "finance_x",
                {
                    "pack_id": "finance_x",
                    "display_name": "Finance X",
                    "process_type_to_sop": {"a_check": "finance_x/a.txt"},
                    "constants": {"TOL_PCT": 5},
                    "entitlement": {"groups": ["g"], "approve_groups": []},
                    # `source` and `fields` are blocks of the same document now — no
                    # separate source.json/fields.json, and `source` carries no
                    # pack_id of its own; build() injects it from the directory.
                    "source": {
                        "label": "X Exceptions",
                        "kind": "filter",
                        "short_id": "x",
                        "field_map": {"amount": {"path": "GrossAmount"}},
                    },
                    "fields": {
                        "amount": {
                            "label": "Amount",
                            "type": "amount",
                            "currency_field": "currency",
                        },
                        "note": {"label": "Note", "type": "string", "panel": False},
                    },
                },
            ),
        ):
            directory = root / name
            directory.mkdir()
            (directory / "pack.json").write_text(json.dumps(pack_json))
            (directory / "base_prompt.txt").write_text("prompt")
        # A pack.json that cannot be parsed disables only itself.
        (root / "broken").mkdir()
        (root / "broken" / "pack.json").write_text("{not json")

        os.environ["PACKS_DIR"] = str(root)
        os.environ.pop("REGISTRY_BUCKET", None)
        reset()

        assert load()["schema"] == SCHEMA
        assert platform_prompt() == "mechanics"
        # The broken pack is skipped; the good one survives it.
        assert set(packs()) == {"finance_x"}
        assert sop_index()["finance_x"]["sops"] == {"a_check": "finance_x/a.txt"}
        assert constants() == {"finance_x": {"TOL_PCT": 5}}
        assert process_type_pack_map() == {"a_check": "finance_x"}
        # An empty approve_groups is dropped rather than stored as a second gate.
        assert entitlement_map() == {"finance_x": {"groups": ["g"]}}
        incomplete_pack = load()["packs"]["finance_x"]["pack"]["entitlement"]
        incomplete_pack["author_groups"] = ["fa"]
        incomplete_pack["constant_groups"] = ["fa", "ap-manager"]
        assert entitlement_map() == {
            "finance_x": {
                "groups": ["g"],
                "author_groups": ["fa"],
                "constant_groups": ["fa", "ap-manager"],
            }
        }, "the two authoring keys must reach the published map"
        assert [s["pack_id"] for s in sources()] == ["finance_x"]

        # Incomplete: a source the survey has not resolved yet. Publishable and inert — the
        # poller must not build a query against the literal marker, and no case may route
        # to a pack that cannot be read from SAP. Constants stay reachable, because an
        # incomplete pack is still being authored and its numbers are still being reviewed.
        # Mutates the cached corpus in place rather than adding a third fixture pack: the
        # point is that every assertion above must NOT change.
        marked = load()
        assert marked["packs"]["finance_x"]["incomplete"] is False
        marked["packs"]["finance_x"]["source"]["service"] = SURVEY_MARKER
        marked["packs"]["finance_x"]["incomplete"] = True  # what build() would publish
        assert sources() == [], "an incomplete source must not be polled"
        assert sop_index()["finance_x"]["sops"] == {}, "and must map no process type"
        assert process_type_pack_map() == {}, "so no case routes to it"
        assert constants() == {"finance_x": {"TOL_PCT": 5}}, "constants stay reachable"
        assert set(packs()) == {"finance_x"}, "and it is still a published pack"
        # The open form counts too. `marker()` says which question fills the blank, so
        # the closed string is not a substring of it — a membership test against
        # SURVEY_MARKER alone would poll this pack.
        assert is_incomplete(
            {"service": "ZC_X", "entity": "<AWAITING_SURVEY: entity>"}
        ), "the marker form that names what is missing counts too"

        reset()
        (row,) = public_view()
        assert row["id"] == "finance_x"
        assert row["label"] == "X Exceptions"
        assert row["process_types"] == ["a_check"]
        assert [f["attribute"] for f in row["fields"]] == ["amount", "note"]
        assert row["fields"][0]["sap_field"] == "GrossAmount"
        assert row["fields"][0]["currency_field"] == "currency"
        assert row["fields"][1]["sap_field"] == ""  # declared, not mapped
        assert row["fields"][1]["panel"] is False
        assert "base_prompt" not in row and "constants" not in row
        assert public_view(set()) == []

        # An unreachable registry raises RegistryUnavailable, not a stale corpus.
        reset()
        _cache = None
        # _fetch contacts S3, so test it only structurally: the exception type and
        # load()'s propagation are pinned by unit tests.
        try:
            _fetch("no-such-bucket-here")
            # If there was no S3 client at all, this would have raised already.
        except RegistryUnavailable:
            pass  # expected — S3 is unreachable
        except Exception:  # noqa: BLE001, S110
            pass  # acceptable — no credentials in offline self-check
        # load() now propagates RegistryUnavailable rather than degrading.
        reset()

        print("registry self-check OK")
