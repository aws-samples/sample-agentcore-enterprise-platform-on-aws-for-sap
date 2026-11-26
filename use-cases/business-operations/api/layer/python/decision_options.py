# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The outcomes a person may choose between when the agent stops for a decision.

The agent records them with `update_case_state` (`case_management/handler.py`), each
with the exact write it would make or none. A decision route takes one option's id; the
action log entry it writes carries that option's write and its digest, and the invoker
builds the run's grant from it, so the run checks let through that write and nothing else
(`business_operations_check/run_checks.py`, `is_approved_write`, unchanged).

Everything here is pure: the case tool narrows what the model sent, and the decision
routes pick an option out of what the case holds.
"""

from __future__ import annotations

import hashlib
import json
import re

#: The writes a proposal may describe, as the trace's `proposed_write` (`evidence.py`).
WRITE_OPS = ("update", "create", "function_import")
MIN_OPTIONS, MAX_OPTIONS = 2, 6
MAX_LABEL, MAX_EFFECT = 80, 240
MAX_FIELDS = 40
_ID = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class OptionsError(ValueError):
    """The options the model sent are not a set a person can choose from."""


def valid_id(value: object) -> bool:
    return isinstance(value, str) and bool(_ID.match(value))


def _text(value: object, name: str, limit: int, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OptionsError(f"{where}: {name} is required")
    value = value.strip()
    if len(value) > limit or _CONTROL.search(value):
        raise OptionsError(
            f"{where}: {name} must be one line of at most {limit} characters"
        )
    return value


def _scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)


def narrow_write(write: object, where: str) -> dict | None:
    """The option's write in `ProposedWrite`'s shape, or None for an option without one.

    Refused rather than dropped, unlike the trace's proposal: an option whose write was
    quietly cut would be approved as a different write than the one the model meant.
    """
    if write is None:
        return None
    if not isinstance(write, dict):
        raise OptionsError(f"{where}: write must be an object or null")
    op = write.get("op")
    if op not in WRITE_OPS:
        raise OptionsError(f"{where}: write.op must be one of {', '.join(WRITE_OPS)}")
    out: dict = {"op": op}
    for key in ("service", "entity", "key"):
        value = write.get(key)
        if value is not None:
            if not isinstance(value, str) or len(value) > 200 or _CONTROL.search(value):
                raise OptionsError(f"{where}: write.{key} must be a short string")
            out[key] = value
    if not out.get("service") or not out.get("entity"):
        raise OptionsError(f"{where}: write needs its service and entity")
    fields = write.get("fields")
    if not isinstance(fields, list) or not fields or len(fields) > MAX_FIELDS:
        raise OptionsError(f"{where}: write.fields must list 1-{MAX_FIELDS} fields")
    narrowed, names = [], set()
    for field in fields:
        name = field.get("name") if isinstance(field, dict) else None
        if not isinstance(name, str) or not name or name in names:
            raise OptionsError(f"{where}: every write field needs a distinct name")
        if any(isinstance(field.get(k), (dict, list)) for k in ("proposed", "current")):
            raise OptionsError(f"{where}: write field {name} must hold a plain value")
        names.add(name)
        entry = {"name": name, "proposed": _scalar(field.get("proposed"))}
        if field.get("current") is not None:
            entry["current"] = _scalar(field.get("current"))
        narrowed.append(entry)
    out["fields"] = narrowed
    return out


def digest(write: object) -> str | None:
    """`decisions.write_digest`, here so the case tool needs no decision module."""
    if not write:
        return None
    canonical = json.dumps(write, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def narrow(raw: object) -> list[dict]:
    """The model's options, checked and narrowed, in its order. Raises OptionsError."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raise OptionsError("decision_options must be a list") from None
    if not isinstance(raw, list) or not MIN_OPTIONS <= len(raw) <= MAX_OPTIONS:
        raise OptionsError(
            f"decision_options must list {MIN_OPTIONS}-{MAX_OPTIONS} options"
        )
    options, ids, writes = [], set(), set()
    for i, option in enumerate(raw):
        where = f"decision_options[{i}]"
        if not isinstance(option, dict):
            raise OptionsError(f"{where} must be an object")
        unknown = set(option) - {"id", "label", "effect", "write"}
        if unknown:
            raise OptionsError(f"{where}: unknown keys {sorted(unknown)}")
        if not valid_id(option.get("id")) or option["id"] in ids:
            raise OptionsError(
                f"{where}: id must be a distinct snake_case identifier of at most 40"
            )
        ids.add(option["id"])
        write = narrow_write(option.get("write"), where)
        key = digest(write)
        if key and key in writes:
            raise OptionsError(f"{where}: another option makes the same write")
        if key:
            writes.add(key)
        options.append(
            {
                "id": option["id"],
                "label": _text(option.get("label"), "label", MAX_LABEL, where),
                "effect": _text(option.get("effect"), "effect", MAX_EFFECT, where),
                # Absent, not null, for an outcome that makes no write.
                **({"write": write} if write else {}),
            }
        )
    return options


def set_id(options: list[dict]) -> str:
    return hashlib.sha256(
        json.dumps(options, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]


def offered(case: dict) -> dict | None:
    """The case's option set as stored, or None when it offers none."""
    block = case.get("decision_options")
    if (
        isinstance(block, dict)
        and isinstance(block.get("set_id"), str)
        and isinstance(block.get("options"), list)
        and block["options"]
    ):
        return block
    return None


def choose(block: dict, option_id: str) -> dict | None:
    """The option with this id in the set, or None."""
    return next(
        (
            o
            for o in block["options"]
            if isinstance(o, dict) and o.get("id") == option_id
        ),
        None,
    )
