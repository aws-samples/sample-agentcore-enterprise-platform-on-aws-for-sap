# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Validate `packs/` at synth: what would otherwise fail silently at run time.

Run by `business_operations/packs.py` on every synth, so a broken pack fails
`cdk synth` and the deploy instead of a case at 3am. The shape comes from
`packs/pack.schema.json`; everything here is what a schema cannot express:

1. `pack_id` is the directory name.
2. Each `process_type_to_sop` target is `<this pack>/<file>` and resolves to a text
   SOP under this pack's `sops/`; no `sops/` file is anything but `.txt` or `.md`
   (the agent reads a SOP as text, and the bucket refuses other suffixes).
3. Each `gateway_tools` name is one of this use case's tools (an unknown name is
   dropped from the agent's tool list with no error).
4. No `process_type` is claimed by two packs.
5. `base_prompt.txt` passes `pack_tiers.check_values` (both placeholders, no survey
   marker, a size cap), as does every constant (its `bounds()`); the platform
   prompt carries `{SAP_SERVICE_INFO}`, and its `[[tools: …]]` blocks are balanced and
   name real tools.
6. Entitlement groups carry the use case's prefix (the pool is shared), and every
   approver, author or constant setter can also read the pack.
7. `source`: no `field_map` key shadows a case property; no two packs share a
   `short_id`; `key` fields are distinct; `kind` names an executor and carries what it
   needs; `poll` is only on a polled kind, and `sweep` or `off` only with `push`.
8. `fields` and `field_map` agree both ways; an `amount` names its currency field; at
   most one `reason`.
9. `seed`: inputs and presets agree with each other.
10. A pack granting `get_precedent` or `check_vendor_risk` produces `supplier_number`,
    the key both read on.
11. Every `{{SYMBOL}}` a SOP cites is one of the pack's `constants` (`CONTACT_*` aside),
    and every model tier the pack names is one this deployment serves.
12. A pack with a `source` that grants a SAP write declares `case_document_check` (without
    one the run checks refuse every write), and it maps only to `source.key` fields.
13. A pack that grants a SAP write declares `AUTO_WRITE_LIMIT_USD` (or says why not in
    `write_limit_waiver`: without a limit nothing holds a write to an amount) and
    `write_guards` (or `write_guards: []`, which says it chose none).

Tool names are checked against this use case's own targets.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

UC = Path(__file__).resolve().parents[1]
PACKS = UC / "packs"
_LAYER = UC / "api" / "layer" / "python"
if str(_LAYER) not in sys.path:
    sys.path.insert(0, str(_LAYER))

from bundle import named_tiers
from pack_compose import RESERVED_ATTRIBUTE_NAMES
from pack_schema import schema_errors
from pack_tiers import (
    TEXT_EXTENSIONS,
    check_values,
    constant_errors,
    undeclared_symbols,
)
from registry import process_type_conflicts, sop_kind_errors
from sap_layer.names import SAP_WRITE_TOOLS
from seed_steps import BUILTINS, TemplateError, template_names

PACK_ID_PATTERN = r"^[a-z][a-z0-9_]*$"
GROUP_PREFIX = "business-operations-"
SUPPLIER_KEYED_TOOLS = frozenset({"get_precedent", "check_vendor_risk"})
UNBUILT_KINDS = {
    "comparison": "it compares two sources, which the poller cannot express; compare "
    "in a CDS view and push the result instead",
    "derived": "the value that makes the record an exception is on no single record; "
    "compute it in a CDS view and push it",
}

# ─── The checks ───────────────────────────────────────────────────────────────

_TOOL_OPEN = re.compile(r"^\[\[tools: *([^\]\n]+)\]\][ \t]*$")
_TOOL_CLOSE = re.compile(r"^\[\[/tools\]\][ \t]*$")


def _tool_blocks(text: str, bare_tools: set[str]) -> list[str]:
    errors, open_line = [], None
    for n, line in enumerate(text.splitlines(), 1):
        if "[[tools" not in line and "[[/tools" not in line:
            continue
        opened, closed = _TOOL_OPEN.match(line), _TOOL_CLOSE.match(line)
        if opened:
            if open_line:
                errors.append(f"_platform_prompt.txt:{n}: nested [[tools:]] block")
            open_line = n
            unknown = sorted(
                {t.strip() for t in opened.group(1).split(",")} - bare_tools
            )
            if unknown:
                errors.append(
                    f"_platform_prompt.txt:{n}: [[tools:]] names unknown {unknown}"
                )
        elif closed:
            if not open_line:
                errors.append(
                    f"_platform_prompt.txt:{n}: [[/tools]] with no open block"
                )
            open_line = None
        else:
            errors.append(
                f"_platform_prompt.txt:{n}: a tools marker must be alone on its line"
            )
    if open_line:
        errors.append(
            f"_platform_prompt.txt:{open_line}: [[tools:]] block never closed"
        )
    return errors


def _pack(
    pack: str,
    cfg: dict,
    root: Path,
    known_tools: set[str],
    served_tiers: set[str] | None,
) -> list[str]:
    name = f"{pack}/pack.json"
    errors = [f"{name}: {m}" for m in schema_errors(cfg, _schema(root))]
    if isinstance(cfg.get("constants"), dict):
        # Bounds as `PUT /packs/{pack_id}/controls` and an import apply them, so a `_PCT` of 150 fails here.
        errors += [f"{name}: {m}" for m in constant_errors(cfg["constants"])]

    pack_id = cfg.get("pack_id")
    if not isinstance(pack_id, str) or not re.match(PACK_ID_PATTERN, pack_id):
        errors.append(f"{name}: pack_id {pack_id!r} must match {PACK_ID_PATTERN}")
    elif pack_id != pack:
        errors.append(f"{name}: pack_id {pack_id!r} is not the directory name {pack!r}")

    sops = root / pack / "sops"
    for path in sorted(sops.glob("*")) if sops.is_dir() else ():
        if path.is_file() and not path.name.endswith(TEXT_EXTENSIONS):
            errors.append(
                f"{pack}/sops/{path.name}: only {' and '.join(TEXT_EXTENSIONS)} SOPs; the "
                "agent reads a SOP as text, so a PDF would run the case with no procedure"
            )
        elif path.is_file() and (
            error := undeclared_symbols(
                f"{pack}/{path.name}",
                path.read_text(encoding="utf-8"),
                cfg.get("constants"),
            )
        ):
            errors.append(error)
    for where, tier in named_tiers(cfg) if served_tiers is not None else ():
        if tier not in served_tiers:
            errors.append(
                f"{name}: {where} is {tier!r}, which this deployment does not serve "
                f"(it serves {sorted(served_tiers)}): set use_cases.business-operations"
                f".runtime.{tier}_model_id to a model in agents.allowed_models, or "
                "pick a served tier"
            )
    for process_type, sop_key in (cfg.get("process_type_to_sop") or {}).items():
        owner, _, filename = str(sop_key).partition("/")
        stem = Path(filename).stem
        if owner != pack or not filename or "/" in filename:
            errors.append(
                f"{name}: {process_type} maps to {sop_key!r}, not '{pack}/<file>'"
            )
        elif not filename.endswith(TEXT_EXTENSIONS):
            errors.append(
                f"{name}: {process_type} maps to {sop_key!r}, which is not a text SOP"
            )
        elif not any((sops / f"{stem}{ext}").is_file() for ext in TEXT_EXTENSIONS):
            errors.append(
                f"{name}: {process_type} maps to {sop_key!r}, which is not in sops/"
            )

    errors += [f"{name}: {m}" for m in sop_kind_errors(cfg)]

    unknown = sorted(set(cfg.get("gateway_tools") or []) - known_tools)
    if unknown:
        errors.append(f"{name}: gateway_tools {unknown} are not this use case's tools")
    # Precedent and vendor-risk rows are keyed on the case's supplier
    # (exemplar_builder._has_precedent_key), so without one these always read empty.
    keyed = sorted(
        t
        for t in cfg.get("gateway_tools") or []
        if t.split("___")[-1] in SUPPLIER_KEYED_TOOLS
    )
    source = cfg.get("source") if isinstance(cfg.get("source"), dict) else {}
    if keyed and "supplier_number" not in (source.get("field_map") or {}):
        errors.append(
            f"{name}: gateway_tools {keyed} read rows keyed on supplier_number, which "
            "this pack's source.field_map never produces, so they always return nothing"
        )

    writes = sorted(
        t
        for t in cfg.get("gateway_tools") or []
        if t.split("___")[-1] in SAP_WRITE_TOOLS
    )
    document_check = cfg.get("case_document_check")
    if source and writes and not document_check:
        errors.append(
            f"{name}: gateway_tools {writes} write to SAP, but no case_document_check ties a "
            "write to the case's document, so the run checks refuse every one"
        )
    if writes:
        if (
            "AUTO_WRITE_LIMIT_USD" not in (cfg.get("constants") or {})
            and not str(cfg.get("write_limit_waiver") or "").strip()
        ):
            errors.append(
                f"{name}: gateway_tools {writes} write to SAP, but constants has no "
                "AUTO_WRITE_LIMIT_USD, so no amount limit holds a write. Declare it, or "
                "say why not in write_limit_waiver"
            )
        if "write_guards" not in cfg:
            errors.append(
                f"{name}: gateway_tools {writes} write to SAP, but the pack declares no "
                "write_guards. Declare them, or write_guards: [] to say it chose none"
            )
    if source and isinstance(document_check, dict):
        key_fields = {
            spec.get("name", spec.get("field"))
            for spec in source.get("key") or []
            if isinstance(spec, dict)
        }
        unknown = sorted(set(document_check.values()) - key_fields)
        if unknown:
            errors.append(
                f"{name}: case_document_check maps to {unknown}, which source.key does not "
                "declare, so the run checks refuse every write"
            )

    entitlement = cfg.get("entitlement") or {}
    if isinstance(entitlement, dict):
        for key, groups in entitlement.items():
            for group in groups if isinstance(groups, list) else []:
                if not str(group).startswith(GROUP_PREFIX):
                    errors.append(
                        f"{name}: entitlement.{key} names {group!r}; the user pool is "
                        f"shared, so this use case's groups start with {GROUP_PREFIX!r}"
                    )
        readers = set(entitlement.get("groups") or [])
        for key in (
            "approve_groups",
            "author_groups",
            "constant_groups",
            "limit_groups",
            "process_groups",
        ):
            orphans = (
                sorted(set(entitlement.get(key) or []) - readers) if readers else []
            )
            if orphans:
                errors.append(
                    f"{name}: entitlement.{key} {orphans} are not in entitlement.groups, "
                    "so they can never act: every gate needs read entitlement first"
                )

    prompt = root / pack / "base_prompt.txt"
    if not prompt.is_file():
        errors.append(f"{name}: no base_prompt.txt beside it")
    else:
        # The rule a patch and an import apply: placeholders, markers, size.
        text = prompt.read_text(encoding="utf-8")
        errors += [f"{pack}/base_prompt.txt: {m}" for m in check_values(None, text)]
    return errors


def _source(pack: str, source: dict, short_ids: dict) -> list[str]:
    name = f"{pack}/pack.json source"
    errors = [
        f"{name}: field_map key {key!r} shadows the case property of that name"
        for key in source.get("field_map") or {}
        if key in RESERVED_ATTRIBUTE_NAMES
    ]
    short_id = source.get("short_id")
    if short_id in short_ids:
        errors.append(
            f"{name}: short_id {short_id!r} is already {short_ids[short_id]}'s"
        )
    else:
        short_ids[short_id] = pack
    seen = set()
    for spec in source.get("key") or []:
        if not isinstance(spec, dict) or not spec.get("field"):
            errors.append(f"{name}: key entry {spec!r} declares no field")
            continue
        field = spec.get("name", spec["field"])
        if field in seen:
            errors.append(f"{name}: key field {field!r} is declared twice")
        seen.add(field)
        if spec.get("as") and spec["as"] not in RESERVED_ATTRIBUTE_NAMES:
            errors.append(
                f"{name}: key field {field!r} stores as {spec['as']!r}, not a case property"
            )
    kind = source.get("kind")
    if kind in UNBUILT_KINDS:
        errors.append(f"{name}: kind {kind!r} has no executor: {UNBUILT_KINDS[kind]}")
    elif kind == "absence" and not (source.get("iterate") or {}).get("path"):
        errors.append(
            f"{name}: kind 'absence' needs iterate.path, the collection to find empty"
        )
    elif kind == "aging" and source.get("aging") is None:
        errors.append(f"{name}: kind 'aging' needs an aging block")
    if source.get("aging") and kind != "aging":
        errors.append(f"{name}: aging is read only for kind 'aging'")
    if kind == "event" and source.get("push") != "records":
        errors.append(
            f"{name}: kind 'event' has no query to re-read, so it needs push 'records'"
        )
    poll = source.get("poll")
    if poll and kind == "event":
        errors.append(f"{name}: poll is read only for a polled kind; 'event' never is")
    elif poll in ("sweep", "off") and source.get("push") == "off":
        errors.append(
            f"{name}: poll {poll!r} is for a pack SAP pushes, and push 'off' refuses "
            "every sender; set push, or poll 'schedule'"
        )
    return errors


def _fields(pack: str, fields: dict, field_map: dict) -> list[str]:
    name = f"{pack}/pack.json fields"
    if not isinstance(fields, dict):
        return []
    errors = []
    if undeclared := sorted(set(field_map) - set(fields)):
        errors.append(
            f"{name}: the source writes {undeclared}, which fields does not declare"
        )
    if unwritten := sorted(set(fields) - set(field_map)):
        errors.append(f"{name}: declares {unwritten}, which no field_map entry writes")
    for field, spec in fields.items():
        if not isinstance(spec, dict):
            continue
        currency = spec.get("currency_field")
        if spec.get("type") == "amount" and currency not in fields:
            errors.append(
                f"{name}: amount {field!r} needs a currency_field this pack declares"
            )
        elif spec.get("type") != "amount" and currency:
            errors.append(
                f"{name}: {field!r} is not an amount, so currency_field is unread"
            )
    reasons = sorted(
        f
        for f, s in fields.items()
        if isinstance(s, dict) and s.get("role") == "reason"
    )
    if len(reasons) > 1:
        errors.append(
            f"{name}: {reasons} all claim role 'reason'; a case has one reason"
        )
    return errors


def _seed(pack: str, seed: dict, source: dict | None = None) -> list[str]:
    name = f"{pack}/pack.json seed"
    inputs = seed.get("inputs") if isinstance(seed, dict) else None
    if not isinstance(inputs, list):
        return []
    errors = _seed_steps(name, seed, inputs, source)
    ids = [i["id"] for i in inputs if isinstance(i, dict) and "id" in i]
    declared = {i["id"]: i for i in inputs if isinstance(i, dict) and "id" in i}
    if duplicated := sorted({i for i in ids if ids.count(i) > 1}):
        errors.append(f"{name}: {duplicated} declared more than once")
    expected = {"number": (int, float), "boolean": bool, "select": str, "string": str}
    for input_id, spec in declared.items():
        kind, default = spec.get("type"), spec.get("default")
        allowed = expected.get(kind)
        if (
            default is not None
            and allowed
            and (
                isinstance(default, bool) is not (kind == "boolean")
                or not isinstance(default, allowed)
            )
        ):
            errors.append(
                f"{name}: {input_id!r} is {kind} but its default is {default!r}"
            )
        if (kind == "select") != bool(spec.get("options")):
            errors.append(
                f"{name}: {input_id!r}: options belong to a select, and only there"
            )
        if kind != "number" and spec.get("min") is not None:
            errors.append(f"{name}: {input_id!r} is {kind}, so its min is unread")
        gate = spec.get("disabled_when")
        if gate and (declared.get(gate) or {}).get("type") != "boolean":
            errors.append(
                f"{name}: {input_id!r} is disabled_when {gate!r}, not a boolean input"
            )
    for preset in seed.get("presets") or []:
        values = preset.get("values") if isinstance(preset, dict) else None
        if isinstance(values, dict) and (
            unknown := sorted(set(values) - set(declared))
        ):
            errors.append(
                f"{name}: preset {preset.get('label')!r} sets undeclared {unknown}"
            )
    return errors


def _seed_steps(name: str, seed: dict, inputs: list, source: dict | None) -> list[str]:
    """Every template reads only what exists when it runs: an input, `posting_date`,
    or a step before it. And the case it opens is keyed the way the poller keys it."""
    known = {i["id"] for i in inputs if isinstance(i, dict) and "id" in i} | BUILTINS
    errors = []
    steps = [s for s in seed.get("steps") or [] if isinstance(s, dict)]
    for step in steps:
        sid = step.get("id")
        try:
            when = "{{" + step["when"] + "}}" if "when" in step else ""
            unknown = template_names([step.get("payload"), when]) - known
        except TemplateError as e:
            errors.append(f"{name} step {sid!r}: {e}")
            unknown = set()
        if unknown:
            errors.append(
                f"{name} step {sid!r} reads {sorted(unknown)} before they exist"
            )
        if sid in known:
            errors.append(
                f"{name} step {sid!r}: the name is already an input or a step"
            )
        known.add(sid)
    case = seed.get("case")
    if isinstance(case, dict):
        try:
            if unknown := template_names(case) - known:
                errors.append(
                    f"{name} case reads {sorted(unknown)}, which nothing sets"
                )
        except TemplateError as e:
            errors.append(f"{name} case: {e}")
        keyed = {k.get("name", k.get("field")) for k in (source or {}).get("key") or []}
        if set(case.get("key") or {}) != keyed:
            errors.append(
                f"{name} case.key names {sorted(case.get('key') or {})}; "
                f"the source keys a case by {sorted(keyed)}"
            )
    state = seed.get("sap_state")
    if isinstance(state, dict):
        keyed = {k.get("name", k.get("field")) for k in (source or {}).get("key") or []}
        try:
            if unknown := template_names(state.get("filter")) - keyed:
                errors.append(
                    f"{name} sap_state reads {sorted(unknown)}; it has only the "
                    f"case key {sorted(keyed)}"
                )
        except TemplateError as e:
            errors.append(f"{name} sap_state: {e}")
    return errors


def _schema(root: Path) -> dict:
    return json.loads((root / "pack.schema.json").read_text(encoding="utf-8"))


def validate(
    known_tools: set[str], root: Path = PACKS, served_tiers: set[str] | None = None
) -> list[str]:
    """Every reason `root` may not be published. Empty means publish it.
    `served_tiers` None skips the tier check (no deployment to check against)."""
    errors: list[str] = []
    documents: dict[str, dict] = {}
    short_ids: dict[str, str] = {}
    for directory in sorted(
        p for p in root.iterdir() if p.is_dir() and not p.name.startswith("_")
    ):
        pack = directory.name
        pack_file = directory / "pack.json"
        if not pack_file.is_file():
            errors.append(f"{pack}/: no pack.json")
            continue
        try:
            cfg = json.loads(pack_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"{pack}/pack.json: not JSON: {exc}")
            continue
        errors += _pack(pack, cfg, root, known_tools, served_tiers)
        documents[pack] = cfg
        source = cfg.get("source")
        if isinstance(source, dict):
            errors += _source(pack, source, short_ids)
        field_map = (source.get("field_map") or {}) if isinstance(source, dict) else {}
        if cfg.get("fields") is not None:
            errors += _fields(pack, cfg["fields"], field_map)
        elif field_map:
            errors.append(
                f"{pack}/pack.json: the source writes {sorted(field_map)} but no fields block declares them"
            )
        if cfg.get("seed") is not None:
            errors += _seed(pack, cfg["seed"], source)

    # The same check the composer runs against the published corpus at every write.
    errors += [f"packs/{e}" for e in process_type_conflicts(documents)]

    platform = (root / "_platform_prompt.txt").read_text(encoding="utf-8")
    if "{SAP_SERVICE_INFO}" not in platform:
        errors.append("_platform_prompt.txt: no {SAP_SERVICE_INFO} placeholder")
    errors += _tool_blocks(platform, {t.split("___")[-1] for t in known_tools})
    return errors


def require_valid(
    known_tools: set[str], root: Path = PACKS, served_tiers: set[str] | None = None
) -> None:
    errors = validate(known_tools, root, served_tiers)
    if errors:
        raise ValueError("packs/ is not publishable:\n  " + "\n  ".join(errors))
