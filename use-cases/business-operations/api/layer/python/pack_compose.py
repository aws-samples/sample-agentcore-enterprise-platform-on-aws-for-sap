# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Compose a whole new pack document from what an analyst can answer without a survey.

A content patch can *revise* a pack over HTTP and cannot create one, because every key
that makes a pack a pack (`source`, `fields`, `gateway_tools`, `process_type_to_sop`) is
structure. This is the DEV draft: refused outside a development environment.

**It does not open the structure.** The submitter sends a *spec*, and this module
composes the structure itself: `gateway_tools` is :func:`created_tools`, chosen
at synth by the stack, and there is no field the submitter can send that widens it.

**The created pack is born incomplete.** Every SAP value in `source` is an
`<AWAITING_SURVEY>` marker, which `registry.is_incomplete` reads: the poller is never given the
pack and no `process_type` routes to it, which is what makes mapping SOPs nobody has
uploaded yet safe. Binding it to SAP is a structure change, by a pack-admin in DEV.

The shape mirrors the scoping repo's `emit.py`, which composes the same document from a
recorded interview.
"""

from __future__ import annotations

import json
import os
import re

import pack_schema
import pack_tiers
from contracts import SHORT_ID_PATTERN

#: The kind a create request travels under: not a patch, so `check_patch` never sees it.
CREATE = pack_tiers.CREATE

#: The `gateway_tools` a created pack holds, full Gateway names, set by the stack from
#: the use case's own name rules: reads, case state and a ticket, never a SAP write and
#: never `send_notification`. **A real grant**, because the pack document is the agent's
#: allowlist, which is why the submitter cannot choose it. Unset grants nothing.
CREATED_TOOLS_ENV = "CREATED_PACK_TOOLS"


def created_tools() -> list[str]:
    return list(json.loads(os.environ.get(CREATED_TOOLS_ENV) or "[]"))


#: The three exception kinds a created pack may declare — `polling_engine.POLLED_KINDS`.
#: `packs/pack.schema.json` admits three more and none of them belongs here: `event` needs a
#: push integration nothing in a create form can configure, and `comparison` and `derived`
#: are named in the schema so `validate_packs.py` can refuse them with the capability they
#: are waiting on. Accepting one would compose a document the next checkout rejects.
CREATE_KINDS = ("filter", "absence", "aging")

#: What a renderer can do with an attribute — `fields.additionalProperties.type`'s enum.
ATTRIBUTE_TYPES = ("string", "date", "amount", "decimal")

#: Attribute names a pack may not claim: every property of a case record. An attribute
#: of the same name is written into the case's open `attributes` map, so the platform's
#: own column is what a reader loses.
RESERVED_ATTRIBUTE_NAMES = frozenset(
    {
        "case_id",
        "key",
        "document_number",
        "item_id",
        "pack_id",
        "process_type",
        "exception_type",
        "test_data",
        "title",
        "description",
        "priority",
        "status",
        "status_reason",
        "disposition",
        "created_at",
        "updated_at",
        "origin",
        "attributes",
        "ticket_id",
        "ticket_response_type",
        "inquiry_sent_at",
        "decision_options",
        "action_log",
        "ttl",
        "ratings",
        "agent_traces",
        "cost_summary",
        "traces_dropped",
        "live_run",
    }
)

#: From the schema, which also checks the composed document: these only make a spec's
#: refusal name the field the submitter typed.
_SHORT_ID = re.compile(SHORT_ID_PATTERN)
_PROCESS_TYPE = re.compile(
    pack_schema.SCHEMA["properties"]["process_type_to_sop"]["propertyNames"]["pattern"]
)
_ATTRIBUTE_NAME = re.compile(
    pack_schema.SCHEMA["properties"]["fields"]["propertyNames"]["pattern"]
)

#: A ceiling on every free-text field. The whole pack corpus is inlined into
#: `registry.json`, which every runtime reads at cold start, so an unbounded `description`
#: is a cold-start cost paid by every Lambda in the stack — not just a large S3 object.
_MAX_TEXT = 2000
#: The same ceiling on the two lists, for the same reason. Five times the largest shipped
#: pack, so no real pack meets it.
_MAX_PROCESS_TYPES = 50
_MAX_ATTRIBUTES = 100

#: What the browser may send. An unknown key is refused rather than ignored, for the same
#: reason `pack.schema.json` refuses one: a submitter who typed `gateway_tools` here
#: needs to be told it is not theirs to set, not to have it silently dropped and believe
#: the pack holds it.
SPEC_KEYS = frozenset(
    {
        "display_name",
        "description",
        "short_id",
        "kind",
        "definition",
        "process_types",
        "attributes",
        "constants",
        "older_than_days",
    }
)

_REQUIRED = ("display_name", "description", "short_id", "kind", "definition")


def marker(what: str) -> str:
    """A marker that also says which question fills it. `emit.py`'s function, same words.

    One line, always: the value lands in a JSON string a human reads in a diff, and
    `registry.is_incomplete` serialises the whole `source` block to find it.
    """
    return f"{pack_tiers.SURVEY_MARKER[:-1]}: {' '.join(str(what).split())}>"


def _text(spec: dict, key: str, errors: list[str]) -> str:
    """One required free-text field, or "" with the reason appended to `errors`."""
    value = spec.get(key)
    if not isinstance(value, str) or not value.strip():
        errors.append(f"`{key}` must be a non-empty string")
        return ""
    if len(value) > _MAX_TEXT:
        errors.append(f"`{key}` is {len(value)} characters, limit is {_MAX_TEXT}")
        return ""
    return value.strip()


def check_spec(spec: object) -> list[str]:
    """Every reason this spec may not become a pack. Empty means compose it.

    A list rather than a raise, matching `pack_tiers.check_patch`: an author who sent three
    bad attributes should read all three rather than fix them one round trip at a time.

    **Shape only.** Whether the `pack_id` is already taken and whether the `short_id`
    collides with another pack are corpus questions, and only `pack_composer` can see the
    corpus — it checks both against the mirror before it writes.
    """
    if not isinstance(spec, dict):
        return ["`create` must be a JSON object describing the use case"]

    errors: list[str] = []

    unknown = sorted(set(spec) - SPEC_KEYS)
    if unknown:
        errors.append(
            f"A new pack's spec may not set {', '.join(unknown)} "
            f"(it may set: {', '.join(sorted(SPEC_KEYS))})"
        )
        privileged = sorted(set(unknown) & pack_tiers.STRUCTURE_KEYS)
        if privileged:
            errors.append(
                f"{', '.join(privileged)} changes what the agent can reach or what it "
                "costs, so a new pack is composed with values this deployment chose: "
                "read-only Gateway tools, and every SAP field left as an "
                f"{pack_tiers.SURVEY_MARKER} marker for the survey to fill. A "
                "pack-admin widens it in development."
            )

    for key in _REQUIRED:
        _text(spec, key, errors)

    short_id = spec.get("short_id")
    if isinstance(short_id, str) and not _SHORT_ID.match(short_id):
        errors.append(
            f"`short_id` {short_id!r} must be 2-8 characters, lowercase letters and "
            "digits, starting with a letter — it is the prefix of every case id this "
            "pack mints"
        )

    kind = spec.get("kind")
    if isinstance(kind, str) and kind and kind not in CREATE_KINDS:
        errors.append(
            f"`kind` {kind!r} is not one of: {', '.join(CREATE_KINDS)}. "
            "A pack of another kind needs a push integration or a capability this "
            "deployment does not have, so it is authored from a checkout."
        )

    errors += _check_process_types(spec.get("process_types"))
    errors += _check_attributes(spec.get("attributes"))

    if kind == "aging":
        days = spec.get("older_than_days")
        if isinstance(days, bool) or not isinstance(days, int) or days < 1:
            errors.append(
                "`older_than_days` must be a whole number of days, at least 1 — an "
                "`aging` pack's exception is defined by how old a record is"
            )
    elif "older_than_days" in spec:
        errors.append("`older_than_days` is read only for an `aging` pack")

    if "constants" in spec:
        # The same names, numeric types and ranges `PUT /packs/{pack_id}/controls`
        # enforces on an
        # operator's override of the very same symbol.
        errors += pack_tiers.check_values({"constants": spec["constants"]})

    return errors


def _check_process_types(process_types: object) -> list[str]:
    """At least one, each a legal `process_type`, no duplicates.

    Required rather than optional: `process_type_to_sop` is what routes a case to this
    pack's SOPs, it is privileged, and so a pack created without one could never be given
    one except from a checkout — which is the situation this module exists to end.
    """
    if not isinstance(process_types, list) or not process_types:
        return [
            (
                "`process_types` must be a list of at least one name — each one is a "
                "situation this pack handles, and the SOP you upload for it is what "
                "routes a case here"
            )
        ]
    if len(process_types) > _MAX_PROCESS_TYPES:
        return [f"`process_types` names more than {_MAX_PROCESS_TYPES} process types"]
    errors: list[str] = []
    for name in process_types:
        if not isinstance(name, str) or not _PROCESS_TYPE.match(name):
            errors.append(
                f"process_type {name!r} must be lowercase letters, digits and "
                "underscores, starting with a letter"
            )
    if len(set(process_types)) != len(process_types):
        errors.append("`process_types` names the same process type twice")
    return errors


def _check_attributes(attributes: object) -> list[str]:
    """The attribute list, or the reasons it is not one.

    **Optional, and worth saying why it is worth filling in now.** `fields` and
    `field_map` are privileged, so an attribute this spec does not declare cannot be added
    over HTTP later — it is a checkout change. An empty list composes a pack whose cases
    carry no attributes at all.
    """
    if attributes is None:
        return []
    if not isinstance(attributes, list):
        return ["`attributes` must be a list"]
    if len(attributes) > _MAX_ATTRIBUTES:
        return [f"`attributes` declares more than {_MAX_ATTRIBUTES} attributes"]

    errors: list[str] = []
    names: list[str] = []
    for index, attr in enumerate(attributes):
        where = f"attributes[{index}]"
        if not isinstance(attr, dict):
            errors.append(f"{where} must be a JSON object")
            continue

        name = attr.get("name")
        if not isinstance(name, str) or not _ATTRIBUTE_NAME.match(name):
            errors.append(
                f"{where}.name must be lowercase letters, digits and underscores, "
                "starting with a letter"
            )
        elif name in RESERVED_ATTRIBUTE_NAMES:
            errors.append(
                f"{where}.name {name!r} is a platform property of every case. An "
                "attribute of that name is written into the case's `attributes` map, so "
                f"the platform's own {name!r} is what a reader would lose — rename it"
            )
        else:
            names.append(name)

        label = attr.get("label")
        if not isinstance(label, str) or not label.strip():
            errors.append(f"{where}.label must be a non-empty string")
        elif len(label) > _MAX_TEXT:
            errors.append(f"{where}.label is longer than {_MAX_TEXT} characters")

        if attr.get("type") not in ATTRIBUTE_TYPES:
            errors.append(f"{where}.type must be one of: {', '.join(ATTRIBUTE_TYPES)}")

        unknown = sorted(
            set(attr) - {"name", "label", "type", "currency_field", "panel"}
        )
        if unknown:
            errors.append(f"{where} may not set {', '.join(unknown)}")

        if "panel" in attr and not isinstance(attr["panel"], bool):
            errors.append(f"{where}.panel must be true or false")

    if len(set(names)) != len(names):
        errors.append("`attributes` declares the same name twice")

    # A figure rendered without its currency is a number, not money — and the reference
    # has to be to a field this pack declares, which is `validate_packs.py`'s check and
    # not something the schema can express.
    declared = set(names)
    for index, attr in enumerate(attributes):
        if not isinstance(attr, dict) or attr.get("type") != "amount":
            continue
        currency = attr.get("currency_field")
        if not isinstance(currency, str) or currency not in declared:
            errors.append(
                f"attributes[{index}].currency_field must name another attribute this "
                "pack declares — an `amount` without its currency is a bare number"
            )

    return errors


def _fields(attributes: list[dict]) -> dict:
    """`fields` — what each attribute is called and how it renders. `emit.py`'s shape."""
    fields: dict[str, dict] = {}
    for attr in attributes:
        declaration: dict[str, object] = {
            "label": str(attr["label"]).strip(),
            "type": str(attr["type"]),
        }
        if attr["type"] == "amount":
            declaration["currency_field"] = str(attr["currency_field"])
        if attr.get("panel") is False:
            declaration["panel"] = False
        fields[str(attr["name"])] = declaration
    return fields


def _source(spec: dict, attributes: list[dict]) -> dict:
    """`source` — where the exceptions come from, with every SAP value a marker.

    The markers are the whole point: they are what `registry.is_incomplete` reads, so this block
    is what makes the pack inert until a survey resolves them. `emit.py`'s shape, key for
    key, so one drift test can hold the two together.
    """
    kind = str(spec["kind"])
    definition = str(spec["definition"]).strip()
    source: dict[str, object] = {
        # No `pack_id`: the directory is the id and `registry.build` injects it into the
        # published block. A collapsed document declares its identity once.
        "short_id": str(spec["short_id"]),
        "label": str(spec["display_name"]).strip(),
        "kind": kind,
        "service": pack_tiers.SURVEY_MARKER,
        "entity": pack_tiers.SURVEY_MARKER,
        "key": [
            {"field": marker("the record's own number"), "as": "document_number"},
            {"field": marker("the line or item number, if any"), "as": "item_id"},
        ],
        "process_type": {"default": str(spec["process_types"][0])},
        "title": marker(f"one line an operator can scan — {definition}"),
        "field_map": {
            str(attr["name"]): {"path": marker(str(attr["label"]))}
            for attr in attributes
        },
    }
    if kind == "filter":
        source["filter"] = marker(f"OData filter for: {definition}")
    elif kind == "absence":
        source["iterate"] = {"path": marker("the collection that is empty")}
    elif kind == "aging":
        source["aging"] = {
            "field": marker("the date the clock runs from"),
            "older_than_days": int(spec["older_than_days"]),
        }
    return source


def base_prompt(spec: dict) -> str:
    """A skeleton `base_prompt.txt` the content tier can then replace wholesale.

    It exists so the pack is publishable the moment it is created — both substitution
    points already in the right two places — rather than needing a second request before
    it is well-formed. `emit.py`'s skeleton, and it carries no marker on purpose:
    `pack_tiers._check_prompt` refuses one, because a marker is a note to the next role
    and not text to send to a model.
    """
    display_name = str(spec["display_name"]).strip()
    description = str(spec["description"]).strip()
    definition = str(spec["definition"]).strip()
    process_types = "\n".join(
        f"- `{name}` — see sops/{name}.txt" for name in spec["process_types"]
    )
    banner = "=" * 80
    return f"""You are an expert {display_name} specialist working ERP exceptions.

{description}

{{PLATFORM_MECHANICS}}

## WHAT MAKES THIS AN EXCEPTION

{definition}

## THE SITUATIONS YOU HANDLE

{process_types}

## WORKFLOW PATTERN

1. **Read the case.** Every attribute on the work item came from SAP; nothing
   in it is your inference.
2. **Follow the SOP for this process type.** Quote the clause you are acting
   on, whole, so the decision is traceable to it.
3. **Gather what the SOP tells you to gather** — and nothing the SOP does not
   ask for.
4. **State the finding, the clause it rests on, and the recommended action.**
5. **Escalate by role when the SOP says to**, using the contact placeholder
   rather than a name.

{banner}
STANDARD OPERATING PROCEDURE
{banner}

{{SOP_CONTENT}}
"""


def compose(pack_id: str, spec: dict) -> tuple[dict, str]:
    """`(pack document, base_prompt.txt)` for a brand-new pack, born incomplete.

    Call :func:`check_spec` first — this assumes a spec that passed it. Every value the
    submitter did not send is chosen here, which is what keeps the privileged tier closed:
    there is no field in `spec` that reaches `gateway_tools`, `model_tier` or `entitlement`.

    **No `entitlement` block, and that is the grant working as intended.**
    `registry.entitlement_map()` gives every pack an entry — `{}` for a pack that declares
    nothing — so `CallerEntitlement._permits` returns True for a pack that restricted
    nothing, and the created pack stays authorable by anyone who clears the deployment's
    `sop_author_groups` floor. Which is exactly the set who could create it. Narrowing it
    to named groups is a privileged change, from a checkout.
    """
    attributes = [a for a in (spec.get("attributes") or []) if isinstance(a, dict)]
    return {
        "$schema": pack_tiers.SCHEMA_REF,
        "pack_id": pack_id,
        "display_name": str(spec["display_name"]).strip(),
        "description": str(spec["description"]).strip(),
        # Not submittable: `model_tier` and `max_turns` are what a run costs.
        "model_tier": "sonnet",
        "max_turns": 15,
        "constants": dict(spec.get("constants") or {}),
        "gateway_tools": created_tools(),
        # Safe to map SOPs that do not exist yet only because the pack is incomplete:
        # `registry.sop_index` publishes it with no `sops`, so nothing routes here and
        # `resolve_skill` is never asked for one. Write them with
        # `PUT /packs/{pack_id}/sops/{process_type}`.
        "process_type_to_sop": {
            name: f"{pack_id}/{name}.txt" for name in spec["process_types"]
        },
        "source": _source(spec, attributes),
        "fields": _fields(attributes),
    }, base_prompt(spec)
