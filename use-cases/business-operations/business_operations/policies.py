# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Cedar statements on this use case's own Gateway targets, and how they are created.

The platform's policy engine is shared, so every statement names a target of this
use case (`business-operations-*`) and, for a permit, a scope this use case's
clients are granted. Unconditioned, a permit would admit any client the shared
Gateway admits, another tenant's included.

`create` hands them to the shared chained loader
(`sap_kit/cedar_policies.add_policies`), which holds what CreatePolicy decides at
deploy: one policy at a time, after every attached target.

Cedar repeats no check a Lambda already makes in every mode: the notification
Lambda holds the recipient allowlist and the PII check, and the run checks hold the run.

SAP statements are per system and live with the SAP targets; `odata_delete` is in
no statement anywhere, so Cedar's default deny withholds it from `tools/list` too.
"""

from __future__ import annotations

from dataclasses import dataclass

from aws_cdk import aws_bedrockagentcore as agentcore
from constructs import Construct

from business_operations import cedar, names
from sap_kit.cedar_policies import PolicySpec, add_policies

SCOPE = f"uc-{names.NAME}"
GATEWAY = "__GATEWAY_ARN__"


@dataclass(frozen=True)
class Statement:
    id: str  # through names.policy_name; never rename a deployed one
    target: str
    tool: str
    summary: str  # the policy's description, after "<prefix> business-operations: "
    text: str  # with GATEWAY to fill in

    @property
    def action(self) -> str:
        return names.tool_name(self.target, self.tool)


def _permit(id: str, target: str, tool: str, scope: str) -> Statement:
    action = names.tool_name(target, tool)
    return Statement(
        id,
        target,
        tool,
        f"{tool} needs {SCOPE}/{scope}",
        cedar.scoped_permit(action, scope, GATEWAY),
    )


STATEMENTS = (
    _permit("read_cases", names.CASE_TARGET, "get_case_state", "read"),
    _permit("write_cases", names.CASE_TARGET, "update_case_state", "write"),
    _permit("load_sop", names.SOPS_TARGET, "load_sop", "read"),
    # Escalation to a human: the tool a case reaches for when it cannot decide.
    # Filing a ticket is a write, so it needs write like a case update.
    _permit("tickets_create", names.TICKETS_TARGET, "demo_create_ticket", "write"),
    _permit("tickets_get", names.TICKETS_TARGET, "demo_get_ticket", "read"),
    # Mail reaches a person outside the run, at any priority, so it needs write.
    _permit("notifications", names.NOTIFICATION_TARGET, "send_notification", "write"),
)


def create(
    scope: Construct,
    *,
    targets: dict[str, agentcore.CfnGatewayTarget],
    engine_id: str,
    gateway_arn: str,
    project: str,
    environment: str,
    prefix: str,
    principals: dict[str | None, list[str]] | None = None,
) -> list[agentcore.CfnPolicy]:
    """One CfnPolicy per statement whose target is in `targets`, chained one at a time.

    `principals` is `identity.cedar_principals` (None in brokered mode).
    """
    specs = [
        PolicySpec(
            id=s.id,
            name=names.policy_name(project, environment, s.id),
            description=f"{prefix} {names.NAME}: {s.summary}",
            text=cedar.for_mode(
                s.text.replace(GATEWAY, gateway_arn), principals=principals
            ),
        )
        for s in STATEMENTS
        if s.target in targets
    ]
    return add_policies(scope, specs, engine_id=engine_id, depends_on=targets.values())
