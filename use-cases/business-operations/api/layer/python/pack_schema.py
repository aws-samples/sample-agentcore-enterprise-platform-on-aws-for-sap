# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""`packs/pack.schema.json`, enforced: the one shape check for every pack document.

Synth runs it over the checkout (`business_operations/validate_packs.py`), and the
composer over every document it is about to write: a create, a patched pack and every
bundle imported by an operator or by the deploy. `pack.schema.json` here is a symlink to the packs' own, so
the two cannot differ. A stdlib subset of JSON Schema, refusing any keyword it does
not implement.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

#: The schema itself; `properties[key]` checks one key of a patch.
SCHEMA: dict = json.loads(
    (Path(__file__).resolve().parent / "pack.schema.json").read_text(encoding="utf-8")
)

_ASSERTIONS = frozenset(
    {
        "type",
        "enum",
        "pattern",
        "maxLength",
        "minLength",
        "minimum",
        "maximum",
        "minItems",
        "required",
        "properties",
        "additionalProperties",
        "propertyNames",
        "items",
    }
)
_ANNOTATIONS = frozenset(
    {"$schema", "$id", "$comment", "title", "description", "default", "examples"}
)
_TYPES: dict[str, Any] = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "null": type(None),
}


def _type_matches(value: Any, name: str) -> bool:
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _TYPES[name])


def schema_errors(instance: Any, schema: dict, where: str = "") -> list[str]:
    """One message per violation. An unknown keyword raises: an ignored keyword is a
    check that looks enforced and is not."""
    unknown = set(schema) - _ASSERTIONS - _ANNOTATIONS
    if unknown:
        raise ValueError(
            f"pack.schema.json uses {sorted(unknown)} at {where or '<root>'}"
        )
    at = where or "pack"
    declared = schema.get("type")
    if declared is not None:
        names = declared if isinstance(declared, list) else [declared]
        if not any(_type_matches(instance, n) for n in names):
            return [f"{at}: expected {' or '.join(names)}, got {instance!r}"]
    errors: list[str] = []
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{at}: {instance!r} is not one of {schema['enum']}")
    if isinstance(instance, str):
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            errors.append(f"{at}: {instance!r} does not match {schema['pattern']}")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(f"{at}: longer than {schema['maxLength']} characters")
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{at}: shorter than {schema['minLength']} characters")
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{at}: {instance} is below {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{at}: {instance} is above {schema['maximum']}")
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{at}: needs at least {schema['minItems']} item(s)")
        for i, item in enumerate(instance if "items" in schema else []):
            errors += schema_errors(item, schema["items"], f"{at}[{i}]")
    if isinstance(instance, dict):
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if instance.get(name) is None:
                errors.append(f"{at}: {name} is required")
        naming = schema.get("propertyNames")
        extra = schema.get("additionalProperties")
        for key, value in instance.items():
            child = f"{where}.{key}" if where else key
            if naming is not None:
                errors += schema_errors(key, naming, child)
            if key in properties:
                errors += schema_errors(value, properties[key], child)
            elif extra is False:
                errors.append(f"{child}: not a key pack.schema.json declares")
            elif isinstance(extra, dict):
                errors += schema_errors(value, extra, child)
    return errors


def errors(document: Any) -> list[str]:
    """Every way `document` (a whole `pack.json`) breaks the schema. Empty means valid."""
    return schema_errors(document, SCHEMA)
