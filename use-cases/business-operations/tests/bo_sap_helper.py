# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""For the business-operations tests that still write a `sap:` block in its config.

Since the SAP layer, the operator writes the systems under `layers.sap` instead, with
an `access` row for the use case; `load` does that move for a test and returns the
use case's config with the layer's view, as stack.py builds it.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

from infra_utils.use_case_context import UseCaseContext

REPO = Path(__file__).resolve().parents[3]
for _path in (
    REPO / "layers" / "sap" / "lib",
    REPO / "use-cases" / "business-operations",
):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from business_operations import config as uc_config
from sap_layer import access

NAME = "business-operations"


def layer_block(sap: dict) -> dict:
    """`layers.sap` for an old `sap:` block: every read and write, and every system
    with a URL and a secret called directly, but an obo one without `unattended`.
    An `ingest` list there becomes the access row's."""
    sap = dict(sap)
    ingest = sap.pop("ingest", [])
    direct = [
        s
        for s, c in sap.get("systems", {}).items()
        if (c.get("flow", "basic") != "obo" or c.get("unattended"))
        and c.get("base_url")
        and c.get("secret_name")
    ]
    return {
        **sap,
        "access": {
            NAME: {
                "read": "*",
                "write": "*",
                "bypass_gateway": direct,
                "ingest": ingest,
            }
        },
    }


def load(
    ctx: UseCaseContext, settings: dict | None
) -> tuple[UseCaseContext, uc_config.BusinessOperationsConfig]:
    """ctx, with an old `sap:` block moved into its platform, and the use case's config."""
    settings = dict(settings or {})
    sap = settings.pop("sap", None)
    platform = ctx.platform
    if sap is not None or "sap" not in platform.layers:
        layers = {**platform.layers, "sap": layer_block(sap or {})}
        ctx = replace(ctx, platform=platform.model_copy(update={"layers": layers}))
    view = access.SapAccess(ctx, NAME)
    return ctx, uc_config.load(
        settings, sap=view.config.model_copy(update={"systems": view.systems})
    )


def config(settings: dict | None) -> uc_config.BusinessOperationsConfig:
    """The use case's config without a ctx: an old `sap:` block becomes its layer view."""
    from sap_layer.config import load as load_layer

    settings = dict(settings or {})
    sap = settings.pop("sap", None)
    view = load_layer(layer_block(sap)) if sap is not None else None
    return uc_config.load(settings, sap=view)
