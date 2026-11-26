# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""What one caller may do in one pack, by pack part: what `GET /registry` returns as
each pack's `access`, so the console can badge and disable its controls.

Every answer calls the function the write's own route calls, with the caller's groups
and this stack's promotion role, so the two cannot disagree. It decides nothing: each
route still makes its own decision, and a `True` here lets no request through.

The parts are the pack parts (`docs/packs.md#pack-parts`). Each `edit` answer asks
`pack_tiers.check_patch` about the keys that part holds, behind the author floor and
`edit_refusal` that `PATCH /packs/{pack_id}` applies first.
"""

from __future__ import annotations

import os

import entitlement
import pack_tiers
import sop_store
from entitlement import CallerEntitlement

#: This stack's promotion role, as the pack and SOP routes read it.
ROLE = os.environ.get("PROMOTION_ROLE") or pack_tiers.DEVELOPMENT

#: The content `PATCH /packs/{pack_id}` takes beside a SOP: name, description, prompt.
TEXT_KEYS = frozenset({"display_name", "description", "base_prompt"})
#: The `pack.json` keys each structure part holds, as `PATCH /packs/{pack_id}` takes
#: them: `pack_tiers.PARTS`, so the console's grouping and these answers are one map.
#: Procedures' structure is its keys less the text, `constants` (read by value) and
#: `pack_id` (not patchable). `seed` is Evaluations' one key; its example cases are
#: `example_cases`.
PART_KEYS = {
    "intake": pack_tiers.part_keys(pack_tiers.INTAKE),
    "procedures_structure": pack_tiers.part_keys(pack_tiers.PROCEDURES)
    - TEXT_KEYS
    - {"constants", "pack_id"},
    "controls": pack_tiers.part_keys(pack_tiers.CONTROLS),
    "agent_configuration": pack_tiers.part_keys(pack_tiers.AGENT_CONFIGURATION),
    "evaluations": pack_tiers.part_keys(pack_tiers.EVALUATIONS),
}


def roles(caller: CallerEntitlement, groups, pack_id: str) -> list[str]:
    """The `pack_tiers` roles a caller holds in a pack: what `PATCH /packs/{pack_id}` sends
    `check_patch` and the composer."""
    held = []
    if caller.may_author(pack_id):
        held.append(pack_tiers.AUTHOR)
    if caller.may_set_constants(pack_id):
        held.append(pack_tiers.CONSTANT_SETTER)
    if pack_tiers.PACK_ADMINS_GROUP in groups:
        held.append(pack_tiers.PACK_ADMIN)
    return held


def clears_floor(groups) -> bool:
    """The deployment-wide author floor, closed when unset (`api/packs._require_floor`)."""
    floor = entitlement.author_floor()
    return bool(floor and set(groups) & floor)


def may_create_pack(groups, env: str | None = None) -> bool:
    """Whether `POST /packs` passes its gates for these groups: the author floor, in a
    development stack. The route decides with this; `GET /deployment/settings` reports it."""
    return clears_floor(groups) and not pack_tiers.edit_refusal(env or ROLE)


def may_edit_contacts(caller: CallerEntitlement) -> bool:
    """Whether `PUT /deployment/settings` takes this caller's contacts: the author floor,
    closed when unset, in any stack (contacts are a current setting, not an edit)."""
    return caller.clears_author_floor()


def may_process(caller: CallerEntitlement, pack_id: str) -> bool:
    """Whether Process (`POST /runs`, `intent: process`) and Start runs
    (`POST /cases/enqueue`) take this caller on a case of `pack_id`: the route's own
    rule (`entitlement.case_pack_verdict` with `drive`)."""
    return caller.may_process(pack_id)


def may_lower_limits(caller: CallerEntitlement, pack_id: str) -> bool:
    """Whether `PUT /packs/{pack_id}/controls` takes this caller's limits: a member of
    `business-operations-limit-owners`, then the pack's `entitlement.limit_groups` where
    it sets them, in any stack (a lowered limit is a current setting, not an edit)."""
    return caller.may_lower_limits(pack_id)


def access(
    groups, caller: CallerEntitlement, pack_id: str, document: dict, env: str = ROLE
) -> dict:
    """`{see, edit: {part: bool}, lower_limits, set_trigger_mode, approve, process,
    rehearse}` for this caller in `pack_id`, whose published document is `document`."""
    groups = frozenset(groups)
    declared = document.get("constants") if isinstance(document, dict) else None
    declared = declared if isinstance(declared, dict) else {}
    held = roles(caller, groups, pack_id)
    publishes = clears_floor(groups) and not pack_tiers.edit_refusal(env)

    def may_patch(keys, constants=None) -> bool:
        return publishes and not pack_tiers.check_patch(
            keys, held, env, constants=constants, declared=declared
        )

    rules = {s: v for s, v in declared.items() if s not in pack_tiers.LIMIT_SYMBOLS}
    edit = {
        "intake": may_patch(PART_KEYS["intake"]),
        "procedures_text": may_patch(TEXT_KEYS)
        and sop_store.may_author(groups, pack_id, f"{pack_id}/sop.txt"),
        "rule_values": bool(rules) and may_patch({"constants"}, rules),
        "procedures_structure": may_patch(PART_KEYS["procedures_structure"]),
        "controls": may_patch(PART_KEYS["controls"]),
        "agent_configuration": may_patch(PART_KEYS["agent_configuration"]),
        "evaluations": may_patch(PART_KEYS["evaluations"]),
        # `PUT`/`DELETE /packs/{id}/evaluations/{test_id}`: content, as a SOP is.
        "example_cases": publishes and caller.may_author(pack_id),
    }
    return {
        "see": caller.may_see(pack_id),
        "edit": edit,
        # `PUT /packs/{id}/controls`, for a pack that declares a limit to lower.
        "lower_limits": bool(set(declared) & pack_tiers.LIMIT_SYMBOLS)
        and may_lower_limits(caller, pack_id),
        "set_trigger_mode": entitlement.is_admin(groups),
        "approve": caller.may_approve(pack_id),
        # Process and Start runs take a case worker (`case_pack_verdict` with `drive`);
        # asking about a case needs only `see`.
        "process": may_process(caller, pack_id),
        "rehearse": caller.clears_author_floor() and caller.may_author(pack_id),
    }
