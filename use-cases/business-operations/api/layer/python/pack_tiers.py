# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Structure and content, as key sets, and the one check over them.

A pack document mixes two kinds of authority. **Content** is what the agent *says*: its
name, its description, its prompt, the text of its SOPs, and the *values* of the
constants it already declares. **Structure** is everything else: which Gateway tools it
holds, which model tier runs it, which SAP service it reads, who may see it, which
constants exist, and which SOP files exist.

**One rule, `check_patch(keys, roles, env)`.** Content needs a pack author. Structure
needs `business-operations-pack-admins` *and* a development environment: outside DEV a
pack's structure arrives only by importing a bundle (`bundle.py`), which is how it was
reviewed and tested before it got there. A patch naming a key it may not write is refused
**as a whole**: a half-applied patch is a pack nobody authored.

One content artifact is a whole document rather than a key, and travels beside the
patch: `base_prompt`, what the agent is told.

**Constants are the one key read by value** (`constant_tier`), because retuning a
tolerance is the author's everyday change while *declaring* one is not: SOPs cite
constants by name and every write path refuses a SOP citing a name no pack declares. A
value edit also needs the pack's `entitlement.constant_groups`. :data:`LIMIT_SYMBOLS`
stay structure even for a value change: a stack lowers its limits with
`PUT /packs/{pack_id}/controls`, which takes `business-operations-limit-owners` and the
pack's `entitlement.limit_groups` instead (`entitlement.may_lower_limits`).
"""

from __future__ import annotations

import json
import math
import re

import pack_schema

#: What a patch is, from its keys. A create is not a patch: it mints a DEV draft.
CONTENT = "content"
STRUCTURE = "structure"
CREATE = "create"

#: The environment roles (`promotion.role`). DEV mints pack versions; the others import.
DEVELOPMENT = "development"
QUALITY = "quality"
PRODUCTION = "production"

#: What the caller holds, as `check_patch` reads it. A pack-admin is also an author.
AUTHOR = "author"
PACK_ADMIN = "pack_admin"
#: Holds the pack's `entitlement.constant_groups` (`entitlement.may_set_constants`): may
#: set a rule value.
CONSTANT_SETTER = "constant_setter"
#: Created only in a development environment; refused for structure anywhere else.
PACK_ADMINS_GROUP = "business-operations-pack-admins"
# The user pool is shared, so a pack may name only this use case's groups: another
# tenant's or the platform's group would hand its members this pack. `validate_packs`
# holds a checkout pack to it at synth; this holds a patch and an import to it.
GROUP_PREFIX = "business-operations-"

#: What the agent says, as `pack.json` keys.
CONTENT_KEYS = frozenset({"display_name", "description"})
#: What the agent says, as whole documents beside the patch.
CONTENT_DOCUMENTS = frozenset({"base_prompt"})

#: Everything else a pack declares. `constants` is here because declaring or removing a
#: name is structure; a declared constant's *value* is content (`constant_tier`).
STRUCTURE_KEYS = frozenset(
    {
        "pack_id",
        "constants",
        "model_tier",
        "max_turns",
        "run_budget",
        "model_effort",
        "context_editing",
        "sap_service",
        "sap_system",
        "gateway_tools",
        "write_guards",
        "case_document_check",
        "write_amount_field",
        "write_limit_waiver",
        "verifier",
        "process_type_to_sop",
        "shared_sops",
        "dispositions",
        "entitlement",
        "source",
        "fields",
        "seed",
    }
)

#: The pack part (`docs/packs.md#pack-parts`) each top-level `pack.json` key belongs to.
#: `GET /packs/{pack_id}` serves it so the console groups keys from the API, and
#: `pack_access` derives each part's key set from it. `constants` is Procedures; the two
#: write limits inside it are Controls (`CONSTANT_PARTS`). The SOP files and
#: `base_prompt.txt` are Procedures and `evals.json` Evaluations, but are not keys.
INTAKE = "intake"
PROCEDURES = "procedures"
CONTROLS = "controls"
AGENT_CONFIGURATION = "agent_configuration"
EVALUATIONS = "evaluations"
PART_NAMES = (INTAKE, PROCEDURES, CONTROLS, AGENT_CONFIGURATION, EVALUATIONS)
PARTS = {
    **dict.fromkeys(
        ("source", "fields", "case_document_check", "sap_service", "sap_system"), INTAKE
    ),
    **dict.fromkeys(
        (
            "pack_id",
            "display_name",
            "description",
            "process_type_to_sop",
            "shared_sops",
            "dispositions",
            "constants",
        ),
        PROCEDURES,
    ),
    **dict.fromkeys(
        (
            "entitlement",
            "write_guards",
            "verifier",
            "write_amount_field",
            "write_limit_waiver",
        ),
        CONTROLS,
    ),
    **dict.fromkeys(
        (
            "model_tier",
            "max_turns",
            "gateway_tools",
            "run_budget",
            "model_effort",
            "context_editing",
        ),
        AGENT_CONFIGURATION,
    ),
    "seed": EVALUATIONS,
}


def part_keys(part: str) -> frozenset[str]:
    """The `pack.json` keys of one part."""
    return frozenset(k for k, v in PARTS.items() if v == part)


#: The SOP suffixes anything publishes: what the agent reads as text and the knowledge
#: base indexes verbatim. The SOP bucket's policy denies every other suffix to everyone.
TEXT_EXTENSIONS = (".md", ".txt")

#: Amazon Bedrock reads `<document>.metadata.json` beside a document as its metadata, never as a
#: document. The SOP search filters on its `pack_id`; the bucket admits these two
#: suffixes besides TEXT_EXTENSIONS.
METADATA_SUFFIX = ".metadata.json"


def sop_metadata(key: str) -> tuple[str, bytes] | None:
    """`(sidecar key, body)` naming the pack a SOP belongs to (its key's folder), or
    None for a SOP at the corpus root, which belongs to no pack and is never searched."""
    pack_id, _, rest = key.partition("/")
    if not rest or not key.endswith(TEXT_EXTENSIONS):
        return None
    body = json.dumps({"metadataAttributes": {"pack_id": pack_id}})
    return key + METADATA_SUFFIX, body.encode()


#: What the agent substitutes in a SOP (`agent/utils/config_overrides._PLACEHOLDER_RE`,
#: pinned equal by a test). A symbol it cannot resolve reaches the model verbatim.
SYMBOL_RE = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")
#: Contacts are the deployment's directory, not the pack's, so they are not checked here.
CONTACT_PREFIX = "CONTACT_"


def undeclared_symbols(key: str, text: str, constants: dict | None) -> str | None:
    """Why the SOP at `key` may not be written, or None. One check for every SOP write
    path: synth (`validate_packs`) and the SOP routes (`sop_store.checked_put`)."""
    missing = sorted(
        {
            s
            for s in SYMBOL_RE.findall(text)
            if not s.startswith(CONTACT_PREFIX) and s not in (constants or {})
        }
    )
    if not missing:
        return None
    pack_id = key.partition("/")[0] if "/" in key else "(no pack)"
    cited = ", ".join("{{" + s + "}}" for s in missing)
    return (
        f"{key} cites {cited}, which pack {pack_id!r} does not declare in its "
        "constants, so the model would read the placeholder verbatim. Declare it, "
        "or fix the spelling."
    )


#: Not authority at all. `$schema` points an editor at the schema and means nothing at
#: runtime, so a patch may carry it without claiming anything.
IGNORED_KEYS = frozenset({"$schema"})

#: The one value a pack document's `$schema` may hold — `packs/pack.schema.json` says so
#: outright, and every pack in the checkout carries it. The composer **pins** it on write
#: rather than validating it: a patch may legally carry the key, and there is only one
#: value it may carry, so a refusal would tell an author nothing they could act on.
#: `registry.build` strips it, so it exists for the next reader of the document itself —
#: a checkout, or an editor resolving the relative path.
SCHEMA_REF = "../pack.schema.json"

#: What a use-case scoping server's scaffold writes where only a SAP survey can fill in the
#: real value. Refused in a prompt because the marker is a note to the *next role*, not
#: text to send to a model — and a pack whose prompt still carries one was published by
#: mistake. Nothing at runtime detects one, which is why the refusal has to happen here.
SURVEY_MARKER = "<AWAITING_SURVEY>"

#: `emit.py` writes the marker in two forms, the closed :data:`SURVEY_MARKER` and an
#: open-ended ``<AWAITING_SURVEY: which question fills it>``, so the only handle common
#: to both is the unclosed prefix. Matching the closed form alone would wave through
#: every marker that says what it is waiting for, which is the form an analyst is most
#: likely to paste.
_MARKER_PREFIX = SURVEY_MARKER.rstrip(">")

#: Both placeholders the runtime substitutes. A prompt missing either silently drops the
#: SOP text or the platform mechanics — the agent then answers from domain training alone,
#: which reads like a working deploy and is the worst failure mode this file can prevent.
REQUIRED_PLACEHOLDERS = ("{SOP_CONTENT}", "{PLATFORM_MECHANICS}")

#: Bounds by symbol suffix. A percentage over 100 or a negative tolerance is not a
#: preference, it is a broken predicate — a `PRICE_VARIANCE_PCT` of 150 silently retires
#: the check it gates, because no variance can ever exceed it. Symbols with no recognised
#: suffix take the conservative fallback rather than going unchecked.
#:
#: **Lives here because a constant has two writers and one meaning.**
#: `PUT /packs/{pack_id}/controls` writes
#: an operator's *override* of a symbol; a patch or an import writes the pack's *default*
#: for the same symbol. Both read this, so a value the override path refuses can never be
#: published as the default it overrides.
_BOUNDS_BY_SUFFIX = {
    "_PCT": (0, 100),
    "_DAYS": (0, 365),
}
_BOUNDS_FALLBACK = (0, 1_000_000_000)

#: The constants bounded by more than a range, so their *value* stays structure. The
#: run checks read them on every SAP write (`business_operations_check/run_checks.py`) and
#: `PUT /packs/{pack_id}/controls` refuses to override either above the pack's published
#: default, so an
#: author who could set the default could raise the ceiling on writes nobody approved.
LIMIT_SYMBOLS = frozenset({"AUTO_WRITE_LIMIT_USD", "DUAL_APPROVAL_LIMIT_USD"})
#: Constants that belong to another part than `constants` does.
CONSTANT_PARTS = dict.fromkeys(sorted(LIMIT_SYMBOLS), CONTROLS)


def bounds(symbol: str) -> tuple[float, float]:
    """The `(low, high)` a constant of this name may take, inclusive."""
    for suffix, limits in _BOUNDS_BY_SUFFIX.items():
        if symbol.endswith(suffix):
            return limits
    return _BOUNDS_FALLBACK


def classify(keys) -> str | None:
    """`STRUCTURE` when any key is not content, `CONTENT` otherwise, None for no key."""
    keys = set(keys) - IGNORED_KEYS
    if not keys:
        return None
    return STRUCTURE if keys - CONTENT_KEYS - CONTENT_DOCUMENTS else CONTENT


def constant_tier(patch, declared) -> tuple[str, str | None]:
    """Which tier a `constants` patch is, and why it is not content.

    `CONTENT` only when every symbol it names is one the pack already declares and
    neither is a write limit. Either side missing (a pack whose document could not be
    read, a `constants` that is not an object) falls back to `STRUCTURE`, which is what
    this key was before a value could be content.
    """
    if not isinstance(patch, dict) or not isinstance(declared, dict):
        return STRUCTURE, None
    if added := sorted(set(patch) - set(declared)):
        return STRUCTURE, (
            f"{', '.join(added)} is not a constant this pack declares. SOPs cite "
            "constants by name, so declaring one is the pack's structure"
        )
    if limits := sorted(set(patch) & LIMIT_SYMBOLS):
        return STRUCTURE, (
            f"{', '.join(limits)} bounds what the agent may write to SAP without a "
            "human, which the pack's owner reviewed"
        )
    return CONTENT, None


def check_patch(keys, roles, env: str, constants=None, declared=None) -> list[str]:
    """Every reason a caller holding `roles` may not write `keys` in environment `env`.

    `keys` are the patch's `pack.json` keys plus `base_prompt` when it carries the
    prompt. Empty means the caller may; the values are `check_values`'.

    `constants` is the patch's `constants` and `declared` the live pack's, which decide
    whether that key is this patch's content or its structure (`constant_tier`).
    """
    keys = set(keys) - IGNORED_KEYS
    if not keys:
        return ["A patch must carry `pack`, `base_prompt`, or both"]
    roles = set(roles)
    errors: list[str] = []
    content = keys & (CONTENT_KEYS | CONTENT_DOCUMENTS)
    structure = keys - content
    why_structure = None
    # Pack-admin is a development role. Elsewhere its group name is not stack-qualified,
    # so honouring it would let any member write every pack's content there.
    development = env == DEVELOPMENT
    if "constants" in structure:
        tier, why_structure = constant_tier(constants, declared)
        if tier == CONTENT:
            structure -= {"constants"}
            content |= {"constants"}
            # A pack-admin counts here, as it does for content: it declares the constant
            # and its first value, so a narrower grant for the next value gates nothing.
            setters = (
                {CONSTANT_SETTER, PACK_ADMIN} if development else {CONSTANT_SETTER}
            )
            if not roles & setters:
                errors.append(
                    "A constant's value needs the pack's `entitlement.constant_groups`"
                )
    unknown = sorted(structure - STRUCTURE_KEYS)
    if unknown:
        errors.append(f"{', '.join(unknown)} is not a pack key")
    if structure - set(unknown):
        named = ", ".join(sorted(structure - set(unknown)))
        because = f" {why_structure}." if why_structure else ""
        if not development:
            errors.append(
                f"{named} is the pack's structure.{because} Outside development it "
                "arrives only by importing a bundle exported from development "
                "(`ops.py bundle import`)."
            )
        elif PACK_ADMIN not in roles:
            errors.append(
                f"{named} is the pack's structure, which needs "
                f"{PACK_ADMINS_GROUP}.{because}"
            )
    writers = {AUTHOR, PACK_ADMIN} if development else {AUTHOR}
    if content and not roles & writers:
        errors.append("Content needs the pack's `entitlement.author_groups`")
    return errors


def check_values(pack: dict | None, base_prompt: str | None = None) -> list[str]:
    """The values a patch, a create or an import may write. Every error, not the first."""
    errors: list[str] = []
    if pack is not None:
        if not isinstance(pack, dict):
            return ["`pack` must be a JSON object"]
        errors += _check_values(pack)
    if base_prompt is not None:
        errors += _check_prompt(base_prompt)
    return errors


def _check_values(pack: dict) -> list[str]:
    """Each key against its `pack.schema.json` declaration, then what the schema cannot
    say. The composer also checks the whole document it writes; this is the same rule,
    key by key, so a patch is refused before anything is read."""
    properties = pack_schema.SCHEMA["properties"]
    errors: list[str] = []
    for key, value in pack.items():
        if key in properties:
            errors += pack_schema.schema_errors(value, properties[key], key)
    for key in ("display_name", "description"):
        if isinstance(pack.get(key), str) and not pack[key].strip():
            errors.append(f"`{key}` must be a non-empty string")
    if isinstance(pack.get("constants"), dict):
        errors += constant_errors(pack["constants"])

    entitlement = pack.get("entitlement")
    if isinstance(entitlement, dict):
        for named in entitlement.values():
            for group in named if isinstance(named, list) else ():
                if not str(group).startswith(GROUP_PREFIX):
                    errors.append(f"Group {group!r} must start with {GROUP_PREFIX!r}")
    return errors


def constant_errors(constants: dict) -> list[str]:
    """What the schema's `{"type": "number"}` admits and a constant may not hold: a
    value outside `bounds()`, or `nan`/`inf`, which `json.loads` accepts as bare tokens
    and `json.dumps` writes back, so one would make every reader of `registry.json`
    reject the whole object. Synth, a patch and an import all run it."""
    errors = []
    for name, value in constants.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue  # the schema's to refuse
        if not math.isfinite(value):
            errors.append(f"Constant {name!r} must be a finite number")
            continue
        low, high = bounds(str(name))
        if not low <= value <= high:
            errors.append(
                f"Constant {name!r} must be between {low} and {high} — the same "
                "range every write path enforces"
            )
    auto, dual = (
        constants.get("AUTO_WRITE_LIMIT_USD"),
        constants.get("DUAL_APPROVAL_LIMIT_USD"),
    )
    if dual is not None and auto is None:
        # The run checks gate unapproved writes on the auto limit alone: a dual limit with
        # no auto limit would let an unapproved write of any amount through.
        errors.append(
            "DUAL_APPROVAL_LIMIT_USD needs AUTO_WRITE_LIMIT_USD as well: without it "
            "no write needs an approval at all"
        )
    if all(isinstance(v, (int, float)) for v in (auto, dual)) and dual < auto:
        # Below the auto limit a write needs no approval at all, so a second one is moot.
        errors.append(
            "DUAL_APPROVAL_LIMIT_USD must be at or above AUTO_WRITE_LIMIT_USD"
        )
    return errors


#: `registry.json` inlines every pack's prompt and every runtime reads it at cold start.
#: Ten times the shipped prompt; a prompt is not in `pack.json`, so the schema cannot say it.
MAX_PROMPT = 64 * 1024


def _check_prompt(base_prompt: str) -> list[str]:
    """A replacement prompt has to keep the two substitution points and carry no markers."""
    if not isinstance(base_prompt, str) or not base_prompt.strip():
        return ["`base_prompt` must be a non-empty string"]
    if len(base_prompt) > MAX_PROMPT:
        return [
            f"`base_prompt` is {len(base_prompt)} characters, limit is {MAX_PROMPT}"
        ]

    errors = [
        f"`base_prompt` must contain {placeholder} — without it the runtime silently "
        "drops that section"
        for placeholder in REQUIRED_PLACEHOLDERS
        if placeholder not in base_prompt
    ]
    if _MARKER_PREFIX in base_prompt:
        errors.append(
            f"`base_prompt` still contains an {SURVEY_MARKER} marker. That is a note to "
            "the next role, not text for the agent — resolve it before publishing."
        )
    return errors


def edit_refusal(env: str) -> list[str]:
    """Why nothing is edited over HTTP in `env`: `[error, detail]`, or `[]` in development.

    Outside development a pack's content and SOPs arrive only as a version imported with
    a change reference, as its structure does, so every change there has a version and
    an audit record. `PUT /deployment/settings` and `PUT /packs/{pack_id}/controls` (contacts and
    lowered limits) are current settings, not an
    edit, and stays.
    """
    if env == DEVELOPMENT:
        return []
    return [
        f"Packs and SOPs are not edited in {env}",
        (
            "Make the edit in development, export it there, and import it here with a "
            "change reference (`ops.py bundle import <file> --change-ref <ref> --apply`)."
        ),
    ]
