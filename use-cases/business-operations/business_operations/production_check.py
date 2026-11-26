# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""`ops.py production-check`: a read-only list of what stands between this deployment
and production, one row per control, each naming its fix.

Reads platform.yaml (and, unless offline, EventBridge); writes nothing. A FAIL row
makes the verb exit 1. A WARN row is a known gap that does not block release, or one
that cannot be read. Row ids in brackets are the threat model's
(`docs/reference/threat-model.md`, the SAP layer's for `SAP-`).
"""

from __future__ import annotations

import copy
from typing import NamedTuple

from business_operations import config as uc_config
from business_operations import packs
from business_operations.audit_alerts import TRIGGER_MODE_PATH
from business_operations.names import NAME
from infra_utils.use_case_context import UseCaseContext

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
_CONTROLS = "deployment.mode 'production' requires all production controls:\n"


class Row(NamedTuple):
    status: str
    check: str
    detail: str
    fix: str = ""


def platform_gaps(raw: dict) -> list[str]:
    """The platform controls `deployment.mode: production` would refuse this file for,
    by running the platform's own validator with the mode switched."""
    from pydantic import ValidationError

    from infra_utils.platform_config import PlatformConfig

    trial = copy.deepcopy(raw)
    trial.setdefault("deployment", {})["mode"] = "production"
    # The people Gateway's production refusal runs before the controls list and would
    # hide it, so name each unacknowledged remote client here and accept it for the trial.
    gaps: list[str] = []
    people = (trial.get("gateway") or {}).get("people") or {}
    if people.get("enabled"):
        entries = list(people.get("remote_clients") or [])
        if people.get("issuer"):
            entries.append(people["issuer"])
        for entry in entries:
            if not entry.get("allow_in_production"):
                name = entry.get("name", "issuer")
                gaps.append(
                    f"gateway.people {name}: set allow_in_production: true, once your "
                    "security team accepts that anyone who signs in through it calls "
                    "the people Gateway's tools as themselves"
                )
                entry["allow_in_production"] = True
    try:
        PlatformConfig.model_validate(trial)
    except ValidationError as e:
        for err in e.errors():
            msg = str(err.get("msg", ""))
            if _CONTROLS in msg:
                return [
                    ln.removeprefix("- ") for ln in msg.split(_CONTROLS)[1].splitlines()
                ] + gaps
        return [
            str(err.get("msg", "")).removeprefix("Value error, ") for err in e.errors()
        ] + gaps
    return gaps


def config_rows(raw: dict) -> list[Row]:
    """Every row that platform.yaml alone answers."""
    from infra_utils.platform_config import PlatformConfig

    platform = PlatformConfig.model_validate(raw)
    uc = uc_config.load(platform.use_cases.get(NAME) or {})
    rows: list[Row] = []

    mode = platform.deployment.mode
    if mode == "production":
        rows.append(Row(PASS, "deployment.mode", "production"))
    else:
        gaps = platform_gaps(raw)
        rows.append(
            Row(
                FAIL,
                "deployment.mode",
                f"{mode}: retained state and the platform's production controls are off"
                + (f"; still unset for production: {'; '.join(gaps)}" if gaps else ""),
                "set deployment.mode: production; the config is then refused until "
                "every control listed is set (hours: an enterprise IdP is the long one)",
            )
        )

    cedar = platform.security.cedar
    if cedar.enabled and cedar.mode == "ENFORCE":
        rows.append(Row(PASS, "security.cedar", "enabled, ENFORCE"))
    else:
        state = f"enabled, {cedar.mode}" if cedar.enabled else "disabled"
        rows.append(
            Row(
                FAIL,
                "security.cedar",
                f"{state}: Cedar refuses nothing, so tenants are not separated and the "
                "reader client can call write tools the run checks do not gate [BO-41]",
                "security.cedar.enabled: true and mode: ENFORCE, then `deploy.sh "
                "verify` proves the reader cannot write (minutes)",
            )
        )

    sec = platform.security
    if sec.egress_filter:
        rows.append(Row(PASS, "security.egress_filter", "on"))
    else:
        rows.append(
            Row(
                FAIL,
                "security.egress_filter",
                "off: no guardrail scans tool calls and responses for PII and secrets "
                "on the shared Gateway",
                "security.egress_filter: true (minutes)",
            )
        )
    # Read defensively: the platform setting may not exist in every checkout.
    action = getattr(sec, "egress_request_action", None)
    if action == "block":
        rows.append(Row(PASS, "security.egress_request_action", "block"))
    else:
        rows.append(
            Row(
                FAIL,
                "security.egress_request_action",
                f"{action or 'unset'}: a tool call the guardrail would rewrite goes "
                "through masked instead of being refused, so a write can post a value "
                "the agent did not send",
                "security.egress_request_action: block (minutes)",
            )
        )

    ctx = UseCaseContext(
        platform.project,
        platform.environment,
        platform,
        production=platform.deployment.mode == "production",
    )
    role = uc_config.promotion_role(ctx, uc.promotion)
    if why := uc_config.promotion_contradiction(ctx, uc.promotion):
        rows.append(Row(FAIL, "promotion.role", why, "remove promotion.role (minutes)"))
    elif mode == "production" and role != "production":
        rows.append(
            Row(
                WARN,
                "promotion.role",
                f"{role} under deployment.mode: production: right for a QAS stack only",
                "unset it on the production stack",
            )
        )
    else:
        rows.append(Row(PASS, "promotion.role", role))

    # Phase 1 of pack promotion signs nothing: `signatures` stays empty and no
    # `promotion.trust` setting exists yet.
    rows.append(
        Row(
            FAIL if role == "production" else WARN,
            "bundle signing trust",
            "bundles are not signed, so the sha256 detects corruption, not a deliberate "
            "edit: anyone who may invoke the pack composer can import a hand-edited "
            f"bundle into this {role} stack; the change reference is the only "
            "attestation [BO-54]",
            "not available yet: restrict lambda:InvokeFunction on the composer to the "
            "release pipeline's role, and require a change reference (hours)",
        )
    )

    if platform.identity.self_sign_up:
        rows.append(
            Row(
                FAIL,
                "identity.self_sign_up",
                "on: anyone can register in the shared user pool [BO-02]",
                "identity.self_sign_up: false; add users with `add-user` (minutes)",
            )
        )
    else:
        rows.append(Row(PASS, "identity.self_sign_up", "off"))

    if uc.api.sample_ticketing:
        rows.append(
            Row(
                FAIL,
                "api.sample_ticketing",
                "on: approvals and notifications go to the sample ticket table, a "
                "stand-in for an ITSM",
                "connect ServiceNow or Jira, then api.sample_ticketing: false (days)",
            )
        )
    else:
        rows.append(Row(PASS, "api.sample_ticketing", "off"))

    if uc.test_data.enabled:
        rows.append(
            Row(
                FAIL,
                "test_data.enabled",
                "on: the seeder posts real SAP documents outside the Gateway, Cedar "
                "and the run checks [BO-40]",
                "test_data.enabled: false (minutes)",
            )
        )
    else:
        rows.append(Row(PASS, "test_data.enabled", "off"))

    days = platform.observability.log_retention_days
    if days >= 365:
        rows.append(Row(PASS, "observability.log_retention_days", str(days)))
    else:
        rows.append(
            Row(
                FAIL if days < 90 else WARN,
                "observability.log_retention_days",
                f"{days} days: the run logs and model content on spans are the audit "
                "record of what the agent read and did [BO-22]",
                "365, or what your retention policy names (minutes)",
            )
        )

    missing = packs.unconfigured_contacts(uc)
    placeholder = sorted(
        k for k, v in uc.contacts.items() if v.strip().lower().endswith("example.com")
    )
    if missing:
        rows.append(
            Row(
                FAIL,
                "contacts",
                f"the SOPs cite {', '.join(missing)}, which contacts leaves unset: the "
                "agent reads those placeholders verbatim",
                f"set use_cases.{NAME}.contacts.<name> for each (minutes)",
            )
        )
    elif placeholder:
        rows.append(
            Row(
                WARN,
                "contacts",
                f"{', '.join(placeholder)} point at example.com",
                "replace them with monitored addresses (minutes)",
            )
        )
    else:
        rows.append(Row(PASS, "contacts", f"{len(uc.contacts)} set"))

    rows.append(
        Row(
            WARN,
            "AWS for SAP MCP Server TLS",
            "the SAP layer turns on verification of SAP's certificate with "
            "MCP_SERVER_SESSION_VERIFY [SAP-12, BO-35]",
            "`deploy.sh verify` fails if a SAP runtime's start banner says "
            "`Session Verify: False`; run it after each image change",
        )
    )
    return rows


def trigger_mode_parameter(project: str, environment: str) -> str:
    return f"/{project}/{environment}/use-cases/{NAME}/{TRIGGER_MODE_PATH}"


def audit_alarm_row(region: str | None, parameter: str) -> Row:
    """WARN unless an EventBridge rule watches this deployment's trigger-mode parameter.

    The registry and SOP notifications deploy with that rule (`audit_alerts.py`), so
    the rule stands for all three."""
    import boto3

    try:
        events = boto3.client("events", region_name=region)
        patterns = []
        for page in events.get_paginator("list_rules").paginate():
            patterns += [r.get("EventPattern") or "" for r in page["Rules"]]
    except Exception as e:  # noqa: BLE001 - no creds or no grant: say so, not crash
        return Row(
            WARN, "audit-change alarms", f"could not list EventBridge rules: {e}"
        )
    if any(f'"{parameter}"' in p for p in patterns):
        return Row(
            PASS,
            "audit-change alarms",
            "the deployment's trigger mode, registry.json and the SOPs alert the ops topic",
        )
    return Row(
        WARN,
        "audit-change alarms",
        f"no EventBridge rule watches {parameter}, so a trigger-mode change, a registry "
        "write or a SOP write goes unnoticed [BO-51]",
        "deploy this use case's main stack (minutes); the trigger-mode rule fires only "
        "while a trail logs management events in this Region",
    )


def report(rows: list[Row]) -> int:
    for r in rows:
        line = f"{r.status}  {r.check}: {r.detail}"
        print(line + (f"\n      fix: {r.fix}" if r.fix and r.status != PASS else ""))
    fails = sum(r.status == FAIL for r in rows)
    warns = sum(r.status == WARN for r in rows)
    if fails:
        print(f"FAIL: {fails} row(s) block production, {warns} warning(s)")
        return 1
    print(f"OK: no row blocks production, {warns} warning(s)")
    return 0
