# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Structure and content: who may write which pack key, in which environment."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(
    0,
    str(
        Path(__file__).resolve().parents[3]
        / "use-cases"
        / "business-operations"
        / "api"
        / "layer"
        / "python"
    ),
)

import pack_tiers as tiers

DEV, QAS, PRD = tiers.DEVELOPMENT, tiers.QUALITY, tiers.PRODUCTION
AUTHOR, ADMIN = tiers.AUTHOR, tiers.PACK_ADMIN


def test_an_author_writes_content_in_every_environment():
    for env in (DEV, QAS, PRD):
        assert tiers.check_patch({"display_name", "base_prompt"}, {AUTHOR}, env) == []


def test_structure_needs_a_pack_admin_in_development():
    assert tiers.check_patch({"gateway_tools"}, {ADMIN}, DEV) == []
    errors = tiers.check_patch({"gateway_tools"}, {AUTHOR}, DEV)
    assert any(tiers.PACK_ADMINS_GROUP in e for e in errors)


@pytest.mark.parametrize("env", [QAS, PRD])
def test_structure_is_refused_outside_development_even_to_a_pack_admin(env):
    """Outside DEV a pack's structure arrives only by import, which is how it was tested."""
    for key in ("constants", "gateway_tools", "source", "process_type_to_sop"):
        errors = tiers.check_patch({key}, {AUTHOR, ADMIN}, env)
        assert any("bundle import" in e for e in errors), key


def test_a_constants_key_is_structure_by_name_alone():
    """`classify` reads keys and nothing else, so it answers for the route's "does this
    patch name a writable key at all". Which tier a `constants` patch really is needs
    its values, which is `check_patch`'s question below."""
    assert tiers.classify({"constants"}) == tiers.STRUCTURE
    assert tiers.classify({"description", "base_prompt"}) == tiers.CONTENT
    assert tiers.classify({"$schema"}) is None


SETTER = tiers.CONSTANT_SETTER
#: What the pack declares: two tolerances and both write limits.
DECLARED = {
    "TOL_PCT": 5,
    "MAX_DAYS": 10,
    "AUTO_WRITE_LIMIT_USD": 100,
    "DUAL_APPROVAL_LIMIT_USD": 200,
}


def _constants(patch, roles, env=DEV):
    return tiers.check_patch(
        {"constants"}, roles, env, constants=patch, declared=DECLARED
    )


def test_an_author_retunes_a_declared_constant_with_constant_groups():
    assert _constants({"TOL_PCT": 4}, {AUTHOR, SETTER}) == []
    assert _constants({"TOL_PCT": 4, "MAX_DAYS": 9}, {AUTHOR, SETTER}) == []
    # A value is content, so it is not refused for want of a pack-admin in any stack.
    for env in (DEV, QAS, PRD):
        assert _constants({"TOL_PCT": 4}, {AUTHOR, SETTER}, env) == []


def test_a_constant_value_needs_the_packs_constant_groups():
    """A rule value is a business decision with its own grant, apart from the prompt's."""
    errors = _constants({"TOL_PCT": 4}, {AUTHOR})
    assert any("constant_groups" in e for e in errors), errors
    assert _constants({"TOL_PCT": 4}, {SETTER}), "and the content grant as well"
    # A pack-admin declares the constant and its first value, so gating the next value
    # on a narrower grant would gate nothing. Only in development, as for content.
    assert _constants({"TOL_PCT": 4}, {ADMIN}) == []
    assert _constants({"TOL_PCT": 4}, {ADMIN}, QAS)


def test_declaring_a_constant_is_structure():
    """SOPs cite constants by name, and every SOP write path refuses an undeclared one."""
    errors = _constants({"NEW_PCT": 4}, {AUTHOR, SETTER})
    assert any(tiers.PACK_ADMINS_GROUP in e and "NEW_PCT" in e for e in errors), errors
    assert _constants({"NEW_PCT": 4}, {AUTHOR, SETTER, ADMIN}) == []
    assert _constants({"NEW_PCT": 4}, {AUTHOR, SETTER, ADMIN}, QAS), "import only"


@pytest.mark.parametrize("symbol", sorted(tiers.LIMIT_SYMBOLS))
def test_a_write_limits_value_stays_structure(symbol):
    """The run checks read both on every SAP write and `PUT /config` refuses to override
    either above the pack's value, so an author who could set the value could widen the
    ceiling on writes nobody approved."""
    errors = _constants({symbol: 1}, {AUTHOR, SETTER})
    assert any(tiers.PACK_ADMINS_GROUP in e and symbol in e for e in errors), errors
    assert _constants({symbol: 1}, {AUTHOR, SETTER, ADMIN}) == []
    assert _constants({symbol: 1}, {AUTHOR, SETTER, ADMIN}, PRD), "import only"


def test_a_patch_changing_a_value_and_a_limit_together_needs_a_pack_admin():
    """Refused as a whole, the way every mixed patch is: the tier is the patch's, not
    the key's, so one limit in it makes all of it structure."""
    mixed = {"TOL_PCT": 4, "AUTO_WRITE_LIMIT_USD": 90}
    assert _constants(mixed, {AUTHOR, SETTER})
    assert _constants(mixed, {AUTHOR, SETTER, ADMIN}) == []
    assert _constants(mixed, {AUTHOR, SETTER, ADMIN}, QAS)
    # Content beside it travels as content, and still needs the author grant.
    both = tiers.check_patch(
        {"constants", "description"},
        {AUTHOR, SETTER},
        DEV,
        constants={"TOL_PCT": 4},
        declared=DECLARED,
    )
    assert both == []
    assert tiers.check_patch(
        {"constants", "description"},
        {SETTER},
        DEV,
        constants={"TOL_PCT": 4},
        declared=DECLARED,
    )


def test_constants_with_nothing_to_compare_against_stay_structure():
    """A pack whose document could not be read tells us nothing about what it declares,
    and the safe answer is the tier this key had before a value could be content."""
    for declared in (None, [], "{}"):
        errors = tiers.check_patch(
            {"constants"},
            {AUTHOR, SETTER},
            DEV,
            constants={"TOL_PCT": 4},
            declared=declared,
        )
        assert any(tiers.PACK_ADMINS_GROUP in e for e in errors), declared
    assert tiers.check_patch(
        {"constants"}, {AUTHOR, SETTER}, DEV, constants=[1], declared=DECLARED
    )


def test_content_without_an_author_grant_is_refused():
    assert tiers.check_patch({"description"}, set(), DEV)


def test_a_key_the_schema_does_not_declare_is_refused():
    assert tiers.check_patch({"typo_key"}, {ADMIN}, DEV)
    assert tiers.check_patch({"handoff"}, {AUTHOR, ADMIN}, DEV)
    assert tiers.check_patch({"multi_agent"}, {AUTHOR, ADMIN}, DEV)


def test_an_empty_patch_is_refused():
    assert tiers.check_patch(set(), {AUTHOR}, DEV)
    assert tiers.check_patch({"$schema"}, {AUTHOR}, DEV)


def test_a_prompt_missing_a_placeholder_is_refused():
    errors = tiers.check_values(None, "No placeholders here.")
    assert any("{SOP_CONTENT}" in e for e in errors)
    assert any("{PLATFORM_MECHANICS}" in e for e in errors)


def test_a_marker_in_the_prompt_is_refused():
    prompt = f"Handle {tiers.SURVEY_MARKER}.\n{{SOP_CONTENT}}{{PLATFORM_MECHANICS}}"
    assert tiers.check_values(None, prompt)


def test_an_open_ended_marker_in_the_prompt_is_refused():
    """`emit.py` also writes `<AWAITING_SURVEY: what>`, the form an analyst is most
    likely to paste."""
    prompt = (
        "Handle <AWAITING_SURVEY: the date the clock runs from>.\n"
        "{SOP_CONTENT}{PLATFORM_MECHANICS}"
    )
    assert tiers.check_values(None, prompt)


def test_constant_names_must_be_shouty_scalars():
    assert tiers.check_values({"constants": {"lower": 1}})
    assert tiers.check_values({"constants": {"NESTED": {"a": 1}}})
    assert tiers.check_values({"constants": "not a mapping"})


def test_a_constant_must_be_a_number():
    """`packs/pack.schema.json` admits `{"type": "number"}` and nothing else."""
    assert tiers.check_values({"constants": {"TOL_PCT": 5}}) == []
    assert tiers.check_values({"constants": {"TOL_PCT": 2.5}}) == []
    assert tiers.check_values({"constants": {"TOL_PCT": "five"}})


def test_a_boolean_constant_is_refused():
    """JSON `true` is not a JSON number, but `isinstance(True, int)` is `True` in Python —
    so the type check has to exclude `bool` by name or this slips through."""
    errors = tiers.check_values({"constants": {"STRICT": True}})
    assert errors, "a boolean is not a number the schema admits"
    assert any("STRICT" in e for e in errors)


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_a_non_finite_constant_is_refused(literal):
    """`json.loads` accepts these bare tokens as an extension and `json.dumps` writes them
    back, so one accepted here lands in `registry.json` — where it is not JSON at all and
    every browser's `JSON.parse` of `GET /config` fails, for every pack, not just this one."""
    value = json.loads(literal)
    errors = tiers.check_values({"constants": {"TOL_PCT": value}})
    assert errors, f"{literal} is not a number the schema admits"
    assert any("TOL_PCT" in e for e in errors)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PRICE_VARIANCE_PCT", 150),
        ("PRICE_VARIANCE_PCT", -1),
        ("AGE_DAYS", 400),
        ("SOME_LIMIT", -5),
    ],
)
def test_a_constant_outside_its_range_is_refused(name, value):
    """The first live test of the headless transport published `PRICE_VARIANCE_PCT: 150`
    and got a 201: the type was fine, and nothing here looked at the range. A percentage
    of 150 silently retires the check it gates — no variance can exceed it — and `PUT
    /config` had always refused that same value as an override. Both writers of a symbol
    now read one bound."""
    errors = tiers.check_values({"constants": {name: value}})
    assert errors, f"{name}={value} is outside the range this symbol admits"
    assert any(name in e and "between" in e for e in errors)


def test_a_constant_inside_its_range_is_accepted():
    assert (
        tiers.check_values(
            {"constants": {"PRICE_VARIANCE_PCT": 2.5, "AGE_DAYS": 365, "COUNT": 0}},
            None,
        )
        == []
    )


def test_a_non_numeric_constant_reports_its_type_and_not_its_range():
    """The range check cannot run on a string — `low <= "five"` raises — so the type
    failure has to stop that constant rather than fall through to it."""
    errors = tiers.check_values({"constants": {"TOL_PCT": "five"}})
    assert any("expected number" in e for e in errors)
    assert not any("between" in e for e in errors)


def test_bounds_are_by_suffix_with_a_conservative_fallback():
    assert tiers.bounds("PRICE_VARIANCE_PCT") == (0, 100)
    assert tiers.bounds("INVOICE_AGE_DAYS") == (0, 365)
    low, high = tiers.bounds("UNRECOGNISED")
    assert low == 0 and high > 0, "an unknown suffix is still bounded, not unchecked"


def test_display_name_must_be_a_non_empty_string():
    assert tiers.check_values({"display_name": ""})
    assert tiers.check_values({"display_name": 42})


def test_every_key_of_a_patch_is_checked_against_the_schema():
    """Not only the keys this module names: a structure key such as `max_turns` is
    held to its declaration too, and text fields to their `maxLength`."""
    assert tiers.check_values({"max_turns": 99})
    assert tiers.check_values({"model_tier": "standard"})
    assert tiers.check_values({"display_name": "x" * 201})
    assert tiers.check_values({"max_turns": 10, "display_name": "Finance"}) == []


def test_a_prompt_has_a_size_cap():
    prompt = "{SOP_CONTENT}{PLATFORM_MECHANICS}"
    assert tiers.check_values(None, prompt + "x" * tiers.MAX_PROMPT)
    assert tiers.check_values(None, prompt) == []


def test_a_patch_or_import_may_name_only_this_use_cases_groups():
    # The pool is shared: a platform or other-tenant group would hand its members the pack.
    ok = {"groups": ["business-operations-ap-clerk"], "approve_groups": []}
    assert tiers.check_values({"entitlement": ok}) == []
    errors = tiers.check_values(
        {"entitlement": {"groups": ["admins"], "approve_groups": ["hr-approvers"]}}
    )
    assert len(errors) == 2 and all("business-operations-" in e for e in errors)


def test_pack_admin_writes_content_only_in_development():
    # The group is not stack-qualified, so outside development content stays the authors'.
    assert tiers.check_patch({"display_name"}, {ADMIN}, tiers.DEVELOPMENT) == []
    for env in (tiers.QUALITY, tiers.PRODUCTION):
        assert tiers.check_patch({"display_name"}, {ADMIN}, env)
        assert tiers.check_patch({"display_name"}, {AUTHOR}, env) == []
