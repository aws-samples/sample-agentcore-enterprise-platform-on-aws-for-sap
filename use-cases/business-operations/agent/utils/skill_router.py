# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Skill Router — maps process_type to a use case pack and loads the SOP from S3.

Architecture:
  Case payload contains process_type (e.g. "invoice_matching", "price_variance")
  Router:
    1. Reads the published registry to find which pack handles that process_type
    2. Takes that pack's base_prompt.txt (domain expertise) from the same read
    3. Injects the shared platform-mechanics preamble at {PLATFORM_MECHANICS}
    4. Fetches the matching SOP from S3 (or local fallback for dev)
    5. Injects SOP content into the base prompt at {SOP_CONTENT} placeholder
    6. Returns the assembled system prompt + skill config

The pack corpus arrives from `registry.py` — one S3 object, read at cold start —
rather than from a copy of `packs/` baked into this container. That is what
makes a new pack live without rebuilding the agent.
"""

import logging
import os
import re

import boto3

from . import config_overrides, registry

logger = logging.getLogger(__name__)

# agent/utils/skill_router.py → the use case's root. Only the dev fallbacks below
# use it; in Lambda and the container the packaged absolute paths win.


class SopLoadError(Exception):
    """Raised when a SOP a pack maps could not be loaded — fetch/parse failure (S3
    error, decode failure) or simply not there.

    A mapping is an assertion that the document exists, so "absent" is not a
    softer case than "unreadable": both mean the run has no procedure. Basic agent
    turns this into `_flag_case_for_review`, which is the behaviour
    `_platform_prompt.txt` describes to the model — see resolve_skill for why the
    routed path may not carry on without one."""


class ContestedProcessType(SopLoadError):
    """Two packs claim the process type, so neither is trusted to run it.

    A `SopLoadError` so the case takes the same road, flagged for manual review before
    the first model call, and its class name is the reason the case record shows."""


# Cache: loaded once per Lambda cold start
_skills_index: dict | None = None
#: The registry object the index was built from; a refreshed corpus is a new object.
_skills_index_of: dict | None = None
_s3 = None


def _get_s3():
    global _s3
    if _s3 is None:
        region = os.environ.get(
            "AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        )
        _s3 = boto3.client("s3", region_name=region)
    return _s3


def _substitute(text: str, config: dict) -> str:
    """Resolve {{CONTACT_*}} from the contact directory and {{SYMBOL}} from the
    skill's `constants` block, operator overrides winning over both.

    Tunable thresholds live in pack.json rather than baked into SOP prose, so a
    finance team can retune one without editing and re-syncing the SOP document.

    The rules themselves live in config_overrides.substitute so the `load_sop`
    Gateway tool applies the identical ones — the prompt and a mid-case SOP
    reload must never state different tolerances.
    """
    return config_overrides.substitute(
        text,
        config_overrides.deployed_contacts(),
        config.get("constants") or {},
        config.get("pack_id", ""),
    )


# Every authored SOP carries "Version N.N" in its header block. Anchored to the
# line so a version cited in body prose cannot be read as the document's own.
_SOP_VERSION_RE = re.compile(r"^Version\s+(\S+)", re.MULTILINE)


def sop_version(text: str) -> str:
    """The SOP's own declared version, or "" if it declares none.

    A precedent row cites the authority that drove the outcome, so the string has
    to come from the document the run actually read — not from the SOP as it
    stands whenever the precedent is written. The `NOT NULL` fallback lives at that
    write, so an unversioned SOP reads as absent here rather than as a version.
    """
    match = _SOP_VERSION_RE.search(text or "")
    return match.group(1) if match else ""


# A line `[[tools: a, b]]` opens a block of platform text that only matters when the
# routed pack is granted `a` or `b`; a line `[[/tools]]` closes it. The preamble is
# shared by every pack, so without this a pack with a narrow `gateway_tools` pays for
# — and is told to call — tools it cannot reach. Blocks do not nest.
# business_operations/validate_packs.py rejects an unbalanced block or an unknown tool name.
_TOOL_BLOCK_RE = re.compile(
    r"^\[\[tools: *([^\]\n]+)\]\][ \t]*\n(.*?)^\[\[/tools\]\][ \t]*(?:\n|\Z)",
    re.MULTILINE | re.DOTALL,
)


def filter_tool_blocks(text: str, granted: list[str] | None) -> str:
    """Keep each `[[tools: …]]` block only if `granted` holds one of its tools.

    `granted=None` (no allowlist) keeps every block, matching
    mcp_topology.build_tool_filters. The marker lines never survive either way, and
    the output depends only on the text and the set, so a pack's cached prefix is
    the same on every run.
    """
    # `granted` holds full Gateway names (`<target>___<tool>`); blocks name bare tools.
    allowed = None if granted is None else {g.split("___")[-1] for g in granted}
    # Tools this deployment does not serve at all (a knowledge feature turned off), so a
    # discovery run, which has no allowlist, is not told to call them either.
    unserved = {t for t in os.environ.get("UNSERVED_TOOLS", "").split(",") if t}

    def keep(match: re.Match) -> str:
        tools = {t.strip() for t in match.group(1).split(",")} - unserved
        return match.group(2) if tools and (allowed is None or tools & allowed) else ""

    out = _TOOL_BLOCK_RE.sub(keep, text)
    if "[[tools" in out or "[[/tools]]" in out:
        raise ValueError("platform prompt has an unbalanced [[tools: …]] block")
    return out


def platform_mechanics() -> str:
    """The platform-mechanics preamble shared by every skill.

    Tool names, OData query parameters, write semantics and the ticket protocol are
    properties of the deployment, not of a domain — so they live in one file that
    resolve_skill injects, rather than being copied into each base_prompt.txt, where
    the copies drift.

    It is published alongside the packs rather than beside them, because
    `packs/_platform_prompt.txt` is not a pack directory and `*/pack.json` never
    picked it up.
    """
    text = registry.platform_prompt()
    if not text:
        raise ValueError(
            f"The published registry carries no {registry.PLATFORM_PROMPT_NAME} — "
            f"every base prompt injects it, so nothing can be assembled without it"
        )
    return text


_NO_PINNED_SERVICE = (
    "No pinned service for this skill — use find_sap_services/get_metadata to discover."
)


def _sap_service_info(sap_service: dict | None) -> str:
    """Render a skill's pinned SAP service(s) for {SAP_SERVICE_INFO}.

    Two shapes are accepted. A single `service` with a flat `entities` map, and a
    `services` list of {service, entities} for domains whose entities are spread
    across several OData services — three-way matching reads the invoice, the PO and
    the material document, and each lives in its own service. The single-service
    shape cannot express that: naming one service forces the other entities to be
    listed under it, and the prompt declares the pinned names authoritative, so the
    agent 404s on them and falls back to the discovery this pin exists to avoid.

    An entity's value is either a plain name string, or a dict `{"name": ...,
    "fields": [...]}` when the skill also pins that entity's field set — without
    it the agent invents plausible-but-nonexistent field names from general SAP
    knowledge (e.g. `DocumentReferenceID`, `SupplierName`) and 404s on every one.

    A group's optional `function_imports` map pins Release/Post/Cancel-style
    function imports the same way: name -> {"params": [{"name", "type", "required",
    ...}]}. `get_metadata` has no scoping parameter — a call for one function
    import's params returns the entire service's metadata (every entity, every
    field, every function import), so pinning here lets the agent skip that call
    for anything already listed rather than paying for it on every write.
    """
    if not sap_service:
        return _NO_PINNED_SERVICE

    groups = sap_service.get("services") or [sap_service]
    lines = []
    for group in groups:
        entities = group.get("entities", {})
        lines.append(f"Service: `{group['service']}`")
        lines.append("Entities:")
        for label, entity in entities.items():
            if isinstance(entity, dict):
                lines.append(f"  - {label}: `{entity['name']}`")
                fields = entity.get("fields")
                if fields:
                    lines.append(f"    Fields: {', '.join(fields)}")
            else:
                lines.append(f"  - {label}: `{entity}`")

        function_imports = group.get("function_imports", {})
        if function_imports:
            lines.append("Function Imports:")
            for name, spec in function_imports.items():
                lines.append(f"  - `{name}`:")
                for param in spec.get("params", []):
                    bits = [
                        param["type"],
                        "required" if param.get("required") else "optional",
                    ]
                    if "default" in param:
                        bits.append(f"default {param['default']!r}")
                    if "note" in param:
                        bits.append(param["note"])
                    lines.append(f"    - {param['name']} ({', '.join(bits)})")
    return "\n".join(lines)


def _load_skills_index() -> dict:
    """Build index: process_type → (pack_id, sop_s3_key, base_prompt).

    Every pack, its prompt and the demo gate all come from `registry.packs()`, so
    this is a reshape of one read rather than a directory walk. The per-pack
    isolation that used to live here — one malformed pack.json disables only its own
    use case — lives in the registry loader now, on both sides of the publish.
    """
    global _skills_index, _skills_index_of
    if _skills_index is not None and _skills_index_of is registry.load():
        return _skills_index
    _skills_index_of = registry.load()

    # Only the owner `process_type_routes` names, so an incomplete pack never overrides a
    # complete one's type and a contested type is in nobody's index.
    routes, _ = registry.process_type_routes(include_incomplete=True)
    _skills_index = {}
    for pack_id, entry in registry.packs().items():
        config = entry["pack"]
        for process_type, sop_key in (config.get("process_type_to_sop") or {}).items():
            if routes.get(process_type) != pack_id:
                continue
            _skills_index[process_type] = {
                "sop_s3_key": sop_key,
                "base_prompt": entry.get("base_prompt", ""),
                # The directory the registry globbed, not the id pack.json declares:
                # validate_packs rule 1 makes them the same string, and this one is
                # the one the SOP index and the exemplar key are actually filed under.
                "pack_id": pack_id,
                "config": config,
            }

    logger.info(
        f"Loaded {len(_skills_index)} process_type mappings across {len(set(e['pack_id'] for e in _skills_index.values()))} packs"  # noqa: C401
    )
    return _skills_index


def _fetch_sop_from_s3(bucket: str, key: str) -> tuple[str, str | None] | None:
    """`(text, S3 VersionId)` of the SOP, trying the mapped key and then the other text
    extension. None if no candidate exists; resolve_skill raises on that.

    Text only: a SOP is decoded as UTF-8 and anything else is a load failure, never a
    placeholder. A PDF once came back as a note saying it could not be parsed, which is
    non-empty, so the case ran with no procedure. The VersionId names the exact bytes
    the run was given; the bucket denies version deletes, so it stays readable.
    """
    for candidate_key in _sop_key_candidates(key):
        try:
            resp = _get_s3().get_object(Bucket=bucket, Key=candidate_key)
            return resp["Body"].read().decode("utf-8"), resp.get("VersionId")
        except _get_s3().exceptions.NoSuchKey:
            continue
        except Exception as e:
            logger.error(f"Failed to fetch SOP from s3://{bucket}/{candidate_key}: {e}")
            raise SopLoadError(
                f"Could not load SOP from s3://{bucket}/{candidate_key}: {e}"
            ) from e

    logger.warning(f"SOP not found in s3://{bucket}/ for any candidate of '{key}'")
    return None


def _sop_key_candidates(key: str) -> list[str]:
    """The mapped key, then the same stem with the other text extension. A key with any
    other extension resolves to nothing, so its process type fails closed."""
    stem, ext = os.path.splitext(key)
    if ext not in _SOP_EXTENSIONS:
        return []
    return [key, *(stem + alt for alt in _SOP_EXTENSIONS if alt != ext)]


_SOP_EXTENSIONS = (".txt", ".md")


def exemplar_s3_key(pack_id: str, process_type: str) -> str:
    """S3 key for a process_type's machine-generated exemplar file.

    Public because `pipeline/exemplar_builder.py` must produce the byte-identical key:
    the two drifted once and _fetch_exemplars swallowed the resulting 404, so the
    continual-learning loop was dead with no error anywhere. The agent container does
    not mount the API layer, so there is one copy on each side, and no test holds them
    together.

    Read from EXEMPLAR_BUCKET, never the SOP bucket: an Amazon Bedrock S3 data source
    ingests a whole bucket, and its `inclusionPrefixes` allowlist holds at most one
    entry, so no prefix inside the SOP bucket can keep these LLM-condensed traces
    out of the vector index. Sharing the bucket would surface them as
    `search_sap_sops` results indistinguishable from an authored SOP.
    """
    return f"{pack_id}/{process_type}_exemplars.md"


def _fetch_exemplars(bucket: str | None, key: str) -> str | None:
    """Load exemplar file from S3. Returns None silently if not found."""
    if not bucket:
        return None
    try:
        resp = _get_s3().get_object(Bucket=bucket, Key=key)
        return resp["Body"].read().decode("utf-8")
    except Exception:  # noqa: BLE001
        return None  # Exemplars are optional — missing is fine


def _fetch_sop_local(pack_id: str, sop_key: str) -> str | None:
    """Dev fallback: load the SOP out of the pack whose config named it.

    The S3 key is `<pack>/<file>` and the pack keeps the same file under
    `packs/<pack_id>/sops/`, so only the file name is read from the key — the
    directory comes from the pack that claimed this process_type, which is what
    keeps one pack from serving another's SOP by naming its key.

    Only reachable with no `SOP_BUCKET` set, i.e. a checkout rather than a
    deployment, so the local `packs/` tree is the right place to look.
    """
    sops_dir = registry.local_root() / pack_id / "sops"
    for candidate in _sop_key_candidates(sop_key):
        local_path = sops_dir / os.path.basename(candidate)
        if local_path.exists():
            return local_path.read_text()
    return None


def resolve_skill(process_type: str, sop_bucket: str | None = None) -> dict:
    """
    Resolve a process_type to a fully assembled skill.

    Returns:
        {
            "pack_id": "finance_ops",
            "process_type": "price_variance",
            "system_prompt": "You are an expert... {SOP injected}",
            "config": { full pack.json },
            "sop_version": "2.1",
            "sop_key": "finance_ops/invoice_matching.txt",
        }

    Raises ValueError if process_type is not mapped to any skill, and SopLoadError if
    it is mapped to a SOP that cannot be loaded (`ContestedProcessType` when two packs
    claim it). There is no third outcome where a
    skill comes back without its procedure — that is the point of the guard below.
    """
    index = _load_skills_index()

    if process_type not in index:
        contested = registry.process_type_routes(include_incomplete=True)[1]
        if process_type in contested:
            raise ContestedProcessType(
                f"process_type '{process_type}' is claimed by "
                f"{', '.join(contested[process_type])}; refusing to route it"
            )
        available = sorted(index.keys())
        raise ValueError(
            f"Unknown process_type '{process_type}'. Available: {available}"
        )

    entry = index[process_type]
    config = entry["config"]
    sop_key = entry["sop_s3_key"]

    base_prompt = entry["base_prompt"]
    if not base_prompt:
        raise ValueError(
            f"Pack '{entry['pack_id']}' was published with no base_prompt.txt — "
            f"it carries the domain expertise, so there is no prompt to assemble"
        )

    # Shared mechanics first: the preamble itself carries {SAP_SERVICE_INFO}, so it
    # has to be in place before that substitution runs.
    base_prompt = base_prompt.replace(
        "{PLATFORM_MECHANICS}",
        filter_tool_blocks(platform_mechanics(), config.get("gateway_tools") or None),
    )

    # Load SOP: try S3 first, then local fallback
    sop_content = None
    bucket = sop_bucket or os.environ.get("SOP_BUCKET")

    sop_version_id = None
    if bucket:
        fetched = _fetch_sop_from_s3(bucket, sop_key)
        if fetched:
            sop_content, sop_version_id = fetched
    else:
        sop_content = _fetch_sop_local(entry["pack_id"], sop_key)

    # Absent is a load failure, not a softer case. This used to substitute
    # "[No SOP loaded for … — operating on domain expertise only]" and return, so a
    # pack that mapped a process_type to a SOP nobody had ever uploaded ran every
    # case of that type on general SAP knowledge — against a real ERP, writes
    # included — and said so nowhere but a `print()`. Meanwhile `load_sop` errors on
    # the same missing key mid-run, `discovery_skills` omits the skill entirely, and
    # a SOP is deleted only with its mappings: this path was the only
    # one that carried on, and the only one that can post.
    #
    # It is also what `_platform_prompt.txt` already promises the model ("a genuine
    # SOP load failure never reaches you — the system fails the case before invoking
    # you"), so telling the truth here is cheaper than teaching every base prompt to
    # distrust its own SOP block.
    if not sop_content:
        raise SopLoadError(
            f"No SOP found at '{sop_key}' for process_type '{process_type}' "
            f"(pack '{entry['pack_id']}'). The pack maps it, so the corpus is "
            f"incomplete: author it on the /packs screen, or add it to the pack's sops/ "
            f"and deploy."
        )

    # Read off the SOP text, not the assembled prompt: the version has to name the
    # document, not the wrapper.
    version = sop_version(sop_content)

    # Inject pinned SAP service/entity names, if this skill declares them, so the
    # agent can call odata_read/odata_count directly instead of discovering via
    # find_sap_services/get_metadata on every run.
    sap_service_info = _sap_service_info(config.get("sap_service"))
    base_prompt = base_prompt.replace("{SAP_SERVICE_INFO}", sap_service_info)

    # Inject SOP into base prompt (wrapped in delimiters for prompt separation)
    system_prompt = base_prompt.replace(
        "{SOP_CONTENT}", f"<sop_document>\n{sop_content}\n</sop_document>"
    )

    system_prompt = _substitute(system_prompt, config)
    # The SOP knowledge base is shared by every pack and filters on this argument.
    system_prompt += (
        f"\n\nThis run's pack is `{entry['pack_id']}`: pass it as `pack_id` to every "
        "`search_sap_sops` call."
    )
    # update_case_state refuses any other label, so name them before the agent closes.
    if dispositions := config.get("dispositions"):
        system_prompt += (
            "\n\nWhen you close the case, set `disposition` to one of: "
            + ", ".join(f"`{d}`" for d in dispositions)
            + ". Any other label is refused."
        )

    # Load exemplars (generated by exemplar_builder Lambda) — optional, silent fail.
    # Exemplars are the legacy continual-learning path. When agent knowledge is
    # deployed, precedent arrives through the keyed get_precedent tool instead,
    # and appending exemplars here would both duplicate it and break prompt
    # caching by varying the system prompt per invocation.
    if os.environ.get("AGENT_KNOWLEDGE_ENABLED", "false").lower() != "true":
        # Its own bucket, not a prefix in the SOP bucket — see exemplar_s3_key.
        exemplar_bucket = os.environ.get("EXEMPLAR_BUCKET")
        exemplar_key = exemplar_s3_key(entry["pack_id"], process_type)
        exemplars = _fetch_exemplars(exemplar_bucket, exemplar_key)
        if exemplars:
            system_prompt += f"\n\n{exemplars}"

    return {
        "pack_id": entry["pack_id"],
        "process_type": process_type,
        "system_prompt": system_prompt,
        "config": config,
        "sop_version": version,
        # Which document that version came from, so a reader of the case can open the
        # text the run was governed by. Unconditional now — a resolved skill always
        # loaded its SOP, so there is no case where this would link to an error page.
        #
        # This names the *injected* SOP, which is what `sop_baseline` is seeded
        # from and so what a citation is usually graded against — but that baseline
        # also accumulates whatever `load_sop`/`search_sap_sops` returned, and it is a
        # flat string with no per-document provenance. A run that quotes a discovered
        # SOP still points here. Upgrade path is making that baseline remember which
        # document each span came from (`utils/hooks.py`); until then the UI
        # says "the SOP this run was given" rather than "where this quote is from".
        "sop_key": sop_key,
        # The S3 version of those bytes; None for a local checkout read.
        "sop_version_id": sop_version_id,
        "model_tier": config.get("model_tier", "sonnet"),
        "max_turns": config.get("max_turns", 15),
    }


def _skill_name(process_type: str) -> str:
    """process_type -> an AgentSkills.io-legal skill name.

    The spec allows lowercase alphanumerics and hyphens only, and `Skill.from_content`
    raises on anything else under strict=True. Our process types use underscores, so
    they are hyphenated here rather than renamed at the source: `process_type` is a
    persisted case field and the key into `process_type_to_sop`, `SOP_INDEX_JSON` and
    the Cedar policies."""
    return process_type.replace("_", "-")


def discovery_skills(
    sop_bucket: str | None = None, packs: frozenset | None = None
) -> list:
    """Build one `Skill` per process_type for the chat path, where the agent has to
    work out which exception it is looking at.

    Returns pre-substituted in-process `Skill` instances, NOT filesystem SKILL.md
    paths. That is deliberate and load-bearing: `AgentSkills` returns
    `skill.instructions` verbatim, with no substitution seam, so a filesystem skill
    would hand the model raw `{{QTY_VARIANCE_PCT}}` / `{{CONTACT_*}}` placeholders and
    silently discard both the pack.json constants and the operator overrides from
    the /config table. Tolerances decide whether money moves, so they must be resolved
    before the text is ever offered to the model.

    Instructions are the same assembled prompt `resolve_skill` produces, so the
    queued path (deterministic injection) and the chat path (model-selected) cannot
    disagree about what a SOP says.

    A skill whose SOP cannot be loaded is omitted rather than offered empty: an
    activatable skill with no procedure behind it is worse than an absent one. The
    routed path now refuses the same case by raising, so the two agree — this one
    degrades (the model classifies without that skill), that one stops.
    Requires strands>=1.50; returns [] if the plugin is unavailable.

    `packs` restricts the offer to the caller's entitled packs (None = every pack, the
    inert default). It is a real leak to close, not tidiness: instructions here are
    pre-substituted, so an offered skill hands over that pack's tolerances and contacts
    the moment the model activates it — no tool call, no case, nothing else to gate.
    """
    try:
        from strands.vended_plugins.skills import Skill
    except ImportError:
        logger.warning(
            "strands.vended_plugins.skills unavailable — no discovery skills"
        )
        return []

    skills = []
    for process_type, entry in sorted(_load_skills_index().items()):
        if packs is not None and entry["pack_id"] not in packs:
            continue
        try:
            resolved = resolve_skill(process_type, sop_bucket=sop_bucket)
        except (SopLoadError, ValueError) as e:
            # Covers the absent SOP too, which used to need a second check on
            # `sop_loaded` right here — resolve_skill raises on it now.
            logger.warning(f"Skipping discovery skill for {process_type}: {e}")
            continue

        config = resolved["config"]
        skills.append(
            Skill(
                name=_skill_name(process_type),
                description=(
                    f"{config.get('display_name', resolved['pack_id'])} — "
                    f"{process_type.replace('_', ' ')}. "
                    f"Activate once the exception is classified as this type."
                ),
                instructions=resolved["system_prompt"],
                # Advisory in strands 1.50 (documented "not yet enforced"), but it
                # records the same grant `gateway_tools` already declares.
                allowed_tools=config.get("gateway_tools"),
            )
        )

    logger.info(f"Built {len(skills)} discovery skills")
    return skills


def list_skills(packs: frozenset | None = None) -> list[dict]:
    """Return summary of published packs and their process types.

    `packs` restricts it to the caller's entitled packs (None = every pack).
    """
    index = _load_skills_index()
    summary = {}
    for pt, entry in index.items():
        pack_id = entry["pack_id"]
        if packs is not None and pack_id not in packs:
            continue
        if pack_id not in summary:
            summary[pack_id] = {
                "pack_id": pack_id,
                "display_name": entry["config"].get("display_name", pack_id),
                "description": entry["config"].get("description", ""),
                "process_types": [],
            }
        summary[pack_id]["process_types"].append(pt)
    return list(summary.values())
