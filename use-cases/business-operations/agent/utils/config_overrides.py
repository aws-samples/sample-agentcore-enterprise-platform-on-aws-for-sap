# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Resolves the {{CONTACT_*}} / {{SYMBOL}} values SOPs cite, and the operator
overrides that win over the deployed defaults.

Every path that hands SOP text to the model resolves here: the agent's
skill_router (prompt assembly), the `load_sop` Gateway tool (mid-case reload) and
`search_sap_sops` (knowledge search). If they held separate copies of the contact
directory, the override read or the placeholder rules, the prompt could state one
tolerance and a reloaded or searched SOP another, or hand the model a raw
`{{CONTACT_*}}` to copy into a ticket. The agent image bundles this file and the
API layer symlinks it, so every Lambda reads the one copy.

Each value has one home, by kind (`kind`). A **rule** constant (a tolerance, an
escalation age) is the pack's alone: its value is pack content, versioned, and moves with
the pack's bundles, so no stack overrides it. A **contact** is the stack's alone:
`platform.yaml`'s `contacts`, then this stack's override. A **limit** (`LIMIT_SYMBOLS`) is
both: the pack carries the ceiling, and this stack's override may only lower it.

Overrides layer over the deploy-time defaults; an absent table, an empty table,
or a failed read all mean "no overrides", so a fresh deploy and a throttled read
both resolve the deployed value rather than blanking a threshold.
"""

import json
import logging
import os
import re
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger(__name__)

_CONTACT_NS = "contact"
_CONSTANT_NS_PREFIX = "constant#"

# One pattern for both symbol families: a contact placeholder is just a symbol
# whose name starts with CONTACT_. Two patterns drifted once — the narrower one
# excluded digits, so {{CONTACT_TIER1_OWNER}} resolved on the load_sop path and
# reached the model verbatim in the injected prompt.
_PLACEHOLDER_RE = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")

#: The constants a stack may override, and only downwards: the pack's value is the
#: ceiling on what the agent writes to SAP without a human (`pack_tiers.LIMIT_SYMBOLS`,
#: which the API layer holds; a test keeps the two equal).
LIMIT_SYMBOLS = frozenset({"AUTO_WRITE_LIMIT_USD", "DUAL_APPROVAL_LIMIT_USD"})

RULE, LIMIT, CONTACT = "rule", "limit", "contact"

_table = None
_contacts: dict | None = None
#: `(pack_id, symbol)` rule overrides already logged, so a stale row warns once per
#: process rather than on every case.
_ignored: set = set()


def kind(symbol: str) -> str:
    """`contact`, `limit` or `rule`: which of the three homes the symbol's value has."""
    if symbol.startswith("CONTACT_"):
        return CONTACT
    return LIMIT if symbol in LIMIT_SYMBOLS else RULE


def deployed_contacts() -> dict:
    """The deploy-time directory, {CONTACT_<KEY>: address}, from CONTACTS_JSON.

    Cached for the process: the deploy sets it from the use case's `contacts`, and
    operator overrides are layered on in `substitute`, never here."""
    global _contacts
    if _contacts is None:
        raw = json.loads(os.environ.get("CONTACTS_JSON") or "{}")
        _contacts = {f"CONTACT_{k.upper()}": v for k, v in raw.items()}
    return _contacts


def _get_table():
    """None when CONFIG_TABLE is unset — local dev and pre-P3 deployments."""
    global _table
    if _table is None:
        name = os.environ.get("CONFIG_TABLE")
        if not name:
            return None
        _table = boto3.resource("dynamodb").Table(name)
    return _table


def _query(namespace: str) -> dict:
    """{config_key: value} for one namespace, or {} if anything goes wrong.

    Deliberately not cached: an operator who saves a tolerance expects the next
    case to use it, and the agent container outlives any sane TTL. One Query per
    case resolution is cheaper than a config edit that appears to do nothing.
    """
    table = _get_table()
    if table is None:
        return {}
    try:
        items = table.query(KeyConditionExpression=Key("namespace").eq(namespace)).get(
            "Items", []
        )
    except Exception as e:  # noqa: BLE001
        # Falling back to the deployed default is the safe direction: the SOP
        # still names a real threshold, just not the edited one.
        logger.warning(f"Config override read failed for {namespace!r}: {e}")
        return {}
    return {i["config_key"]: i["value"] for i in items if "value" in i}


def contact_overrides() -> dict:
    """{CONTACT_<KEY>: address} — keyed to match the placeholder, not the config."""
    return {f"CONTACT_{k.upper()}": v for k, v in _query(_CONTACT_NS).items()}


def constant_overrides(pack_id: str) -> dict:
    """{SYMBOL: value} for one pack's write limits. Values are Decimal from DynamoDB.

    Only `LIMIT_SYMBOLS`: a rule constant's value is the pack's, so a row overriding
    one (left from a release that took them) is ignored, and logged once.
    """
    if not pack_id:
        return {}
    rows = _query(_CONSTANT_NS_PREFIX + pack_id)
    for symbol in sorted(set(rows) - LIMIT_SYMBOLS):
        if (pack_id, symbol) not in _ignored:
            _ignored.add((pack_id, symbol))
            logger.warning(
                f"Ignoring the stack override of rule constant {symbol} for {pack_id}: "
                "its value is the pack's. Set it in the pack and delete the row."
            )
    return {k: v for k, v in rows.items() if k in LIMIT_SYMBOLS}


def _plain(value):
    """A DynamoDB Decimal as the JSON int or float it was written as."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def effective_constants(declared: dict, overrides: dict, origin: str = "pack") -> dict:
    """`{SYMBOL: {"value", "kind", "source"}}` for a pack's declared constants.

    `origin` names the pack's value (`pack v7`). A limit override applies only while it
    is below the pack's ceiling, so a later version that lowers the ceiling is never
    widened by an older override; a rule constant takes no override at all.
    """
    rows = {}
    for symbol, ceiling in declared.items():
        row = {"value": _plain(ceiling), "kind": kind(symbol), "source": origin}
        lower = overrides.get(symbol) if row["kind"] == LIMIT else None
        try:
            lowered = lower is not None and Decimal(str(lower)) < Decimal(str(ceiling))
        except ArithmeticError:  # not a number: the ceiling holds
            lowered = False
        if lowered:
            row.update(
                value=_plain(lower),
                source=f"stack override (lowered from {_plain(ceiling)})",
            )
        rows[symbol] = row
    return rows


def effective_contacts(deployed: dict, overrides: dict) -> dict:
    """`{CONTACT_<KEY>: {"value", "kind", "source"}}` from `platform.yaml`'s `contacts`
    (`deployed`) and this stack's overrides, both keyed as in the config."""
    return {
        f"CONTACT_{key.upper()}": {
            "value": overrides[key] if key in overrides else deployed.get(key),
            "kind": CONTACT,
            "source": "stack contact override" if key in overrides else "platform.yaml",
        }
        for key in sorted({*deployed, *overrides})
    }


def resolved_constants(declared: dict, overrides: dict) -> dict:
    """`{SYMBOL: value}`: a pack's declared constants with this stack's limit
    `overrides` (`constant_overrides`) applied. What every reader of the numbers uses."""
    return {
        s: row["value"] for s, row in effective_constants(declared, overrides).items()
    }


def effective_values(declared: dict, pack_id: str, origin: str) -> dict:
    """Every symbol a pack's SOPs can cite, read live: its constants and the
    deployment's contacts, each with its value, kind and source."""
    return {
        **effective_contacts(
            json.loads(os.environ.get("CONTACTS_JSON") or "{}"), _query(_CONTACT_NS)
        ),
        **effective_constants(declared, constant_overrides(pack_id), origin),
    }


def substitute(text: str, contacts: dict, constants: dict, pack_id: str = "") -> str:
    """Resolve every {{SYMBOL}} in `text`: contact overrides win, a limit override
    applies while it is lower than the pack's value, and a rule is the pack's.

    Args:
        text: SOP or prompt text.
        contacts: deploy-time directory, already keyed `CONTACT_<KEY>`.
        constants: the owning pack's declared `constants` block.
        pack_id: whose constant namespace to read overrides from. Empty means
            contacts only.

    A symbol neither map declares is left verbatim: a silently-blank threshold
    would have the agent compare against nothing and report the comparison as
    done, where a visible `{{...}}` is an obvious defect.

    A symbol declared with no value counts as undeclared for the same reason.
    A contact key left empty in `platform.yaml` arrives as `{"procurement": None}`,
    and substituting that would put the
    literal string "None" in front of the model as an address to email.
    """
    contacts = {**contacts, **contact_overrides()}
    # Keyed on the declared symbols only: an override for a symbol this pack never
    # declared cannot introduce one, and only a limit takes an override at all.
    constants = resolved_constants(constants, constant_overrides(pack_id))

    def _resolved(source: dict, symbol: str):
        """The rendered value, or None if `source` carries no usable one.

        Not a falsiness check: a threshold of 0 is a real tolerance and must
        substitute, where an unset contact must not.
        """
        if symbol not in source:
            return None
        value = source[symbol]
        if value is None:
            return None
        rendered = str(value)
        return rendered if rendered.strip() else None

    def _replace(match):
        symbol = match.group(1)
        for source in (contacts, constants):
            rendered = _resolved(source, symbol)
            if rendered is not None:
                return rendered
        return match.group(0)

    return _PLACEHOLDER_RE.sub(_replace, text)
