# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""TypeScript twins of the runtime data contracts in `types/*.schema.json`.

`deploy.sh usecase run business-operations types` writes them; `types --check` (and
the drift test) fails when a committed twin differs from what its schema produces.

Stdlib only, so the check runs offline in the test suite. It understands the subset
of JSON Schema those files use and refuses any other keyword, so a schema that
outgrows it fails loudly instead of generating a wrong type.

The values more than one language reads (:data:`CONSTANTS`) are emitted too: into the
TypeScript twins, and into `agent/utils/contracts.py`, which the API layer takes by
symlink. So no reader states a status list, an id pattern or the live-run window itself.
"""

from __future__ import annotations

import json
import re
import textwrap
from pathlib import Path

UC = Path(__file__).resolve().parents[1]
SCHEMAS = UC / "types"
TWINS = {
    "cases.schema.json": UC / "frontend" / "src" / "types" / "generated-cases.ts",
    "tickets.schema.json": UC / "frontend" / "src" / "types" / "generated-tickets.ts",
}
# Keywords that change no TypeScript type are accepted and ignored.
_KNOWN = {
    "$schema", "$comment", "title", "description", "type", "properties", "required",
    "additionalProperties", "items", "$ref", "enum", "definitions",
    "pattern", "minProperties", "minItems", "maxItems", "default",
    "x-stale-after-seconds", "x-set-by-platform",
}  # fmt: skip
_SCALARS = {"string": "string", "integer": "number", "number": "number",
            "boolean": "boolean", "null": "null"}  # fmt: skip
WIDTH = 100

#: Name -> `<schema file>#<JSON pointer>`, the file relative to `types/`.
CONSTANTS = {
    "CASE_STATUSES": "cases.schema.json#/definitions/CaseStatus/enum",
    # The poller creates a case `detected` and the invoker moves it to `processing`;
    # `update_case_state` refuses both, and its tool spec advertises the rest.
    "PLATFORM_CASE_STATUSES": "cases.schema.json#/definitions/CaseStatus/x-set-by-platform",
    "CASE_ID_PATTERN": "cases.schema.json#/properties/case_id/pattern",
    "PACK_ID_PATTERN": "cases.schema.json#/definitions/PackId/pattern",
    "SHORT_ID_PATTERN": "../packs/pack.schema.json#/properties/source/properties/short_id/pattern",
    "LIVE_RUN_STALE_SECONDS": "cases.schema.json#/definitions/LiveRun/x-stale-after-seconds",
}
PY_TWIN = UC / "agent" / "utils" / "contracts.py"
#: The run client is its own package, so it gets its own copy of the one value it reads.
RUN_CLIENT_TWIN = UC / "packages" / "run-client" / "src" / "generated-contract.ts"
_HEAD = (
    "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.",
    "SPDX-License-Identifier: Apache-2.0",
    "GENERATED from use-cases/business-operations/types/. Do not edit:",
    "change the schema, then run `deploy.sh usecase run business-operations types`.",
)


def _pascal(text: str) -> str:
    return "".join(
        w[:1].upper() + w[1:].lower() for w in re.split(r"[^A-Za-z0-9]+", text) if w
    )


def _doc(text: str | None, indent: str) -> list[str]:
    if not text:
        return []
    lines = [f"{indent}/**"]
    for i, para in enumerate(text.split("\n\n")):
        if i:
            lines.append(f"{indent} *")
        wrapped = textwrap.wrap(para, WIDTH - len(indent) - 3, break_on_hyphens=False)
        lines += [f"{indent} * {line}" for line in wrapped]
    return lines + [f"{indent} */"]


class _Emitter:
    def __init__(self, schema: dict, source: str):
        self.defs = schema.get("definitions", {})
        self.source = source
        self.blocks: list[list[str]] = []
        self.enums: dict[str, list] = {}

    def _check(self, spec: dict, where: str) -> None:
        unknown = set(spec) - _KNOWN
        if unknown:
            raise ValueError(
                f"{self.source} {where}: unsupported keyword(s) {sorted(unknown)}"
            )

    def _enum(self, name: str, spec: dict) -> str:
        values = [v for v in spec["enum"] if v is not None]
        if name in self.enums:
            if self.enums[name] != values:
                raise ValueError(f"{self.source}: two enums would both be named {name}")
            return name
        self.enums[name] = values
        members = [f'  {_pascal(v)} = "{v}",' for v in values]
        self.blocks.append(
            [
                *_doc(spec.get("description"), ""),
                f"export enum {name} {{",
                *members,
                "}",
            ]
        )
        return name

    def type_of(self, spec: dict, where: str, name: str) -> str:
        self._check(spec, where)
        if "$ref" in spec:
            ref = spec["$ref"].rsplit("/", 1)[-1]
            target = self.defs[ref]
            # A named scalar (a string with a pattern) has no TypeScript name of its own.
            named = "properties" in target or "enum" in target
            return ref if named else self.type_of(target, f"definitions/{ref}", ref)
        types = spec.get("type")
        if "enum" in spec:
            enum = self._enum(_pascal(name), spec)
            return (
                "null | " + enum
                if isinstance(types, list) and "null" in types
                else enum
            )
        if isinstance(types, list):
            return " | ".join(
                sorted(self.type_of({**spec, "type": t}, where, name) for t in types)
            )
        if types in _SCALARS:
            return _SCALARS[types]
        if types == "array":
            item = self.type_of(spec["items"], f"{where}/items", name)
            return f"({item})[]" if " " in item else f"{item}[]"
        if types == "object" and "properties" not in spec:
            extra = spec.get("additionalProperties", True)
            value = "any" if extra is True else self.type_of(extra, f"{where}/*", name)
            return f"{{ [key: string]: {value} }}"
        raise ValueError(
            f"{self.source} {where}: name this {types or 'schema'} under definitions"
        )

    def interface(self, name: str, spec: dict, where: str) -> None:
        self._check(spec, where)
        block = [*_doc(spec.get("description"), ""), f"export interface {name} {{"]
        self.blocks.append(block)  # before the enums its properties register
        required = set(spec.get("required", ()))
        for prop, sub in spec["properties"].items():
            ts = self.type_of(sub, f"{where}/{prop}", prop)
            block += [
                *_doc(sub.get("description"), "  "),
                f"  {prop}{'' if prop in required else '?'}: {ts}",
            ]
        extra = spec.get("additionalProperties", True)
        if extra is not False:
            if extra is not True:
                raise ValueError(f"{self.source} {where}: properties plus a typed map")
            block.append("  [property: string]: any")
        block.append("}")

    def emit(self, schema: dict) -> str:
        self.interface(schema["title"], schema, "")
        for name, spec in self.defs.items():
            if "properties" in spec:
                self.interface(name, spec, f"definitions/{name}")
            elif "enum" in spec:
                self._enum(name, spec)
        head = [
            "// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.",
            "// SPDX-License-Identifier: Apache-2.0",
            f"// GENERATED from use-cases/business-operations/types/{self.source}. Do not edit:",
            "// change the schema, then run `deploy.sh usecase run business-operations types`.",
        ]
        return "\n\n".join("\n".join(b) for b in [head, *self.blocks]) + "\n"


def constants() -> dict[str, object]:
    """:data:`CONSTANTS`, each read from its schema."""
    out = {}
    for name, ref in CONSTANTS.items():
        file, _, pointer = ref.partition("#")
        value = json.loads((SCHEMAS / file).read_text())
        for part in pointer.strip("/").split("/"):
            value = value[part]
        out[name] = (
            [v for v in value if v is not None] if isinstance(value, list) else value
        )
    return out


def _ts_constants(names) -> list[str]:
    out = []
    for name in names:
        value = constants()[name]
        if isinstance(value, list):
            items = "".join(f"  {json.dumps(v)},\n" for v in value)
            out.append(f"export const {name} = [\n{items}] as const")
        else:
            out.append(f"export const {name} = {json.dumps(value)} as const")
    return out


def render(schema_name: str) -> str:
    schema = json.loads((SCHEMAS / schema_name).read_text())
    text = _Emitter(schema, schema_name).emit(schema)
    if schema_name == "cases.schema.json":
        text += "\n" + "\n".join(_ts_constants(CONSTANTS)) + "\n"
    return text


def _render_py() -> str:
    lines = [
        *(f"# {h}" for h in _HEAD),
        "",
        '"""Constants the runtime data contracts declare."""',
        "",
    ]
    for name, value in constants().items():
        if isinstance(value, list):
            lines += [f"{name} = (", *(f"    {json.dumps(v)}," for v in value), ")"]
        else:
            lines.append(f"{name} = {json.dumps(value)}")
    return "\n".join(lines) + "\n"


def _render_run_client() -> str:
    head = [f"// {h}" for h in _HEAD]
    return "\n".join([*head, "", *_ts_constants(["LIVE_RUN_STALE_SECONDS"])]) + "\n"


def _outputs() -> dict[Path, str]:
    return {
        **{out: render(name) for name, out in TWINS.items()},
        PY_TWIN: _render_py(),
        RUN_CLIENT_TWIN: _render_run_client(),
    }


def write() -> list[Path]:
    outputs = _outputs()
    for out, text in outputs.items():
        out.write_text(text)
    return list(outputs)


def stale() -> list[Path]:
    """Twins that differ from what their schema produces now."""
    return [
        out
        for out, text in _outputs().items()
        if not out.exists() or out.read_text() != text
    ]
