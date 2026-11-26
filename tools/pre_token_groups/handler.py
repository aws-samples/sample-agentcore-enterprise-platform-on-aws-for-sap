# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Cognito pre token generation trigger: map brokered-IdP groups to Cognito groups.

A federated user carries no Cognito groups. The IdP's group claim reaches this
function through the provider's attribute mapping (custom:idp_groups), and
GROUP_MAP says which Cognito groups each IdP value grants. Only groups named in
the map are ever added, and existing groups are kept, so an IdP value can never
mint a group name. Applies only to users federated through PROVIDER_NAME: a
native pool user could write custom:idp_groups itself.

Event version 1: its group override also sets cognito:groups in the access
token, on every feature plan.
"""

from __future__ import annotations

import json
import os


def idp_values(raw: str | None) -> list[str]:
    """The IdP claim as Cognito stored it: a JSON array, `[a, b]`, or `a,b`.

    Matching is against an allowlist, so a loose parse can only miss a value,
    never grant one."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = [v.strip().strip("\"'") for v in raw.strip("[]").split(",")]
    if isinstance(parsed, str):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return []
    return [str(v) for v in parsed if v]


def is_federated_by(attributes: dict, provider: str) -> bool:
    try:
        identities = json.loads(attributes.get("identities") or "[]")
    except ValueError:
        return False
    return isinstance(identities, list) and any(
        isinstance(i, dict) and i.get("providerName") == provider for i in identities
    )


def mapped_groups(
    existing: list[str], values: list[str], group_map: dict[str, list[str]]
) -> list[str]:
    added = [g for v in values for g in group_map.get(v, [])]
    return list(dict.fromkeys([*existing, *added]))


def handler(event, _context):
    group_map = json.loads(os.environ.get("GROUP_MAP") or "{}")
    request = event.get("request") or {}
    attributes = request.get("userAttributes") or {}
    if not group_map or not is_federated_by(
        attributes, os.environ.get("PROVIDER_NAME", "")
    ):
        return event
    config = request.get("groupConfiguration") or {}
    existing = config.get("groupsToOverride") or []
    groups = mapped_groups(
        existing, idp_values(attributes.get("custom:idp_groups")), group_map
    )
    if groups == existing:
        return event
    # groupOverrideDetails replaces the whole group configuration, so the IAM
    # roles and preferred role ride along unchanged.
    override = {
        "groupsToOverride": groups,
        "iamRolesToOverride": config.get("iamRolesToOverride") or [],
    }
    if config.get("preferredRole"):
        override["preferredRole"] = config["preferredRole"]
    event["response"] = {
        **(event.get("response") or {}),
        "claimsOverrideDetails": {"groupOverrideDetails": override},
    }
    return event
