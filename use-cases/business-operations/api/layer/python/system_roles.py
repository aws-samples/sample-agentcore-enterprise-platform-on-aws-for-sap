# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Which SAP systems this deployment reaches, what each one is, and the deployment's mode.

What the console needs to mark a sandbox and to say "in production": per pack in
`GET /registry`, every system and the mode in `GET /deployment/settings`. Served behind
sign-in and
never in `aws-exports.json`, which anyone can fetch.

`SAP_SYSTEM_ROLES` is `{system: system_role}` for every SAP system this use case may read,
rendered at synth from `layers.sap.systems.<s>.system_role` (required there, one of
`SYSTEM_ROLES`). A pack's system follows the poller's rule: its `sap_system`, or the only
system there is. The system role is the operator's word; nothing here asks SAP.
"""

from __future__ import annotations

import json
import os

#: `layers.sap.systems.<s>.system_role`, as the SAP layer's config takes it.
SYSTEM_ROLES = ("sandbox", "development", "quality", "production")
PRODUCTION = "production"


def systems() -> dict[str, str]:
    """`{system: system_role}`, read per call so a test can set the environment."""
    raw = json.loads(os.environ.get("SAP_SYSTEM_ROLES") or "{}")
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def pack_system(document: dict) -> dict | None:
    """`{name, system_role}` of the system a pack reads from and writes to, or None when
    it names one this use case does not reach, or names none and there are several."""
    known = systems()
    named = document.get("sap_system") if isinstance(document, dict) else None
    if named:
        name = named if named in known else None
    else:
        name = next(iter(known)) if len(known) == 1 else None
    return {"name": name, "system_role": known[name]} if name else None


def deployment(promotion_role: str) -> dict:
    """The `deployment` block of `GET /deployment/settings`.

    `non_production` is the console's band: true only when there is a system and every
    one has a system role other than `production`. A system role it does not know counts as
    production, so a typo can only hide the band.
    """
    known = systems()
    return {
        "mode": os.environ.get("DEPLOYMENT_MODE") or "workshop",
        "promotion_role": promotion_role,
        # Every pack and SOP write over HTTP is refused outside development
        # (`pack_tiers.edit_refusal`). Here, after sign-in, not in `aws-exports.json`.
        "promotion_only": promotion_role != "development",
        "sap_systems": [
            {"name": name, "system_role": system_role}
            for name, system_role in sorted(known.items())
        ],
        "non_production": bool(known)
        and all(
            system_role in SYSTEM_ROLES and system_role != PRODUCTION
            for system_role in known.values()
        ),
    }
