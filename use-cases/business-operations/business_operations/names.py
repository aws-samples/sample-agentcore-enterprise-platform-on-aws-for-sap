# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The names this use case gives AWS resources, checked at synth against service limits.

The limits and the generic checks are shared (`sap_kit/names.py`,
which cites the service models); this module builds this use case's own names on
them, so a ValueError says which setting to shorten.
"""

from __future__ import annotations

import re

from sap_layer import names as sap_names

from sap_kit.names import (
    CREDENTIAL_PROVIDER_NAME,
    CREDENTIAL_PROVIDER_NAME_MAX,
    EVENT_API_NAME,
    EVENT_API_NAME_MAX,
    GUARDRAIL_NAME,
    GUARDRAIL_NAME_MAX,
    KB_NAME,
    RUNTIME_NAME,
    RUNTIME_NAME_MAX,
    checked,
    deployment_hash,
    tool_name,
)
from sap_kit.names import policy_name as _policy_name

NAME = "business-operations"

# The use case's own Gateway targets besides the SAP ones. Every one starts with
# the use case's name, which is what the interceptor check and Cedar match on.
CASE_TARGET = f"{NAME}-case-management"
NOTIFICATION_TARGET = f"{NAME}-notification"
TICKETS_TARGET = f"{NAME}-tickets"
KNOWLEDGE_TARGET = f"{NAME}-knowledge"
# `load_sop` reads the SOP bucket, not a knowledge base, so it has a target of its
# own that exists whether or not SOP search is deployed.
SOPS_TARGET = f"{NAME}-sops"
AGENT_KNOWLEDGE_TARGET = f"{NAME}-agent-knowledge"

# The user pool group that may lower a pack's write limits in this stack, apart from
# the authors so a customer can map it to its own IdP group (`identity.group_map`).
LIMIT_OWNERS_GROUP = f"{NAME}-limit-owners"
# The group that may start the agent on a case to work it (Process, Start runs), apart
# from the approvers. A run passes a SAP write under the auto-write limit for whoever
# started it, so map it to people trusted with that.
CASE_WORKERS_GROUP = f"{NAME}-case-workers"
# Who may author packs and SOPs through the platform's people Gateway, on top of the
# authors, so a customer chooses who may do it from an external AI on its own.
REMOTE_AUTHORS_GROUP = f"{NAME}-remote-authors"


def runtime_name(project: str, environment: str, generation: int = 1) -> str:
    """The AgentCore runtime name, built the way RuntimeStack builds it.

    RuntimeStack names a runtime `{project}_{environment}_{component}` with
    hyphens replaced by underscores, and adds `_g<N>` when the runtime is
    regenerated. The component is this use case's name.
    """
    name = f"{project}_{environment}_{NAME}".replace("-", "_")
    suffix = f"_g{generation}" if generation > 1 else ""
    name += suffix
    if not RUNTIME_NAME.fullmatch(name):
        room = RUNTIME_NAME_MAX - len(f"_{NAME}{suffix}")
        raise ValueError(
            f"AgentCore runtime name {name!r} is {len(name)} characters; the limit "
            f"is {RUNTIME_NAME_MAX} (pattern {RUNTIME_NAME.pattern}). Shorten "
            f"`project` or `environment` in platform.yaml: '{project}_{environment}' "
            f"is {len(project) + 1 + len(environment)} characters and may be at most "
            f"{room}{' with the ' + suffix + ' suffix' if suffix else ''}."
        )
    return name


def created_pack_tools(sap_system: str | None = None) -> tuple[str, ...]:
    """The `gateway_tools` a pack created in the browser is given, target-prefixed.

    Reads, case state, and escalation through a ticket: a pack nobody privileged
    reviewed can read and park a case but changes nothing outside this use case.
    No SAP write, and no `send_notification`, which mails a recipient the model
    chose. Bare names would admit another tenant's same-named tools on the shared
    Gateway. With no SAP system the pack gets no SAP tools.
    """
    tools = [
        tool_name(CASE_TARGET, "get_case_state"),
        tool_name(CASE_TARGET, "update_case_state"),
        tool_name(AGENT_KNOWLEDGE_TARGET, "get_precedent"),
        tool_name(KNOWLEDGE_TARGET, "search_sap_sops"),
        tool_name(SOPS_TARGET, "load_sop"),
        tool_name(KNOWLEDGE_TARGET, "search_sap_api_docs"),
        tool_name(TICKETS_TARGET, "demo_create_ticket"),
        tool_name(TICKETS_TARGET, "demo_get_ticket"),
    ]
    if sap_system:
        target = sap_names.sap_target(sap_system)
        tools += [tool_name(target, t) for t in sap_names.SAP_READ_TOOLS]
    return tuple(tools)


def policy_name(project: str, environment: str, policy: str) -> str:
    """A Cedar policy name unique to this deployment and this use case: `<policy>_<hash>`.

    The hash input must never change: a new name replaces every policy on the
    next deploy.
    """
    return _policy_name(project, environment, NAME, policy)


def knowledge_base_name(prefix: str, corpus: str) -> str:
    """An Amazon Bedrock knowledge base name, `<project>-<env>-business-operations-<corpus>`."""
    name = f"{prefix}-{NAME}-{corpus}"
    if not KB_NAME.fullmatch(name):
        raise ValueError(
            f"Knowledge base name {name!r} does not match {KB_NAME.pattern}: "
            "letters and digits, single hyphens or underscores between them, at "
            "most 100 characters. Shorten `project` or `environment` in platform.yaml."
        )
    return name


def _deployment_hash(project: str, environment: str) -> str:
    return deployment_hash(project, environment, NAME)


def evaluator_name(project: str, environment: str, evaluator: str) -> str:
    """An AgentCore evaluator name, `<evaluator>_<hash>`.

    CreateEvaluator names (pattern
    ``[a-zA-Z][a-zA-Z0-9_]{0,47}``) are unique per account and Region, so the name
    carries the deployment hash: a bare name gave a second stack the first one's judge.
    """
    return checked(
        f"{evaluator}_{_deployment_hash(project, environment)}",
        RUNTIME_NAME,
        RUNTIME_NAME_MAX,
        "Evaluator",
    )


def credential_provider_name(prefix: str) -> str:
    """The token-vault provider holding the agent's Gateway client. Unique per account
    and Region, like every provider in the shared vault."""
    return checked(
        f"{prefix}-{NAME}-agent",
        CREDENTIAL_PROVIDER_NAME,
        CREDENTIAL_PROVIDER_NAME_MAX,
        "Credential provider",
    )


def run_events_api_name(prefix: str) -> str:
    """The AppSync Events API a run's events are published on.

    Cut to the limit rather than refused: an API name need not be unique (the API id
    is what everything uses), so a long prefix costs only readability.
    """
    name = f"{prefix}-{NAME}-run-events"[:EVENT_API_NAME_MAX]
    return checked(name, EVENT_API_NAME, EVENT_API_NAME_MAX, "Event API")


def guardrail_name(project: str, environment: str, purpose: str) -> str:
    """An Amazon Bedrock guardrail name, `business-operations-<purpose>-<hash>`: unique per
    account and Region, like a policy name."""
    return checked(
        f"{NAME}-{purpose}-{_deployment_hash(project, environment)}",
        GUARDRAIL_NAME,
        GUARDRAIL_NAME_MAX,
        "Guardrail",
    )


#: EventBridge PutRule's `Name`.
_RULE_NAME = re.compile(r"[.\-_A-Za-z0-9]+")


def sap_events_rule_name(prefix: str) -> str:
    """The rule routing this use case's SAP events to its ingest queue. Named, so its
    `FailedInvocations` alarm can say which rule (the metric's `RuleName`)."""
    return checked(f"{prefix}-{NAME}-sap-events", _RULE_NAME, 64, "EventBridge rule")


def ingest_key_alias(prefix: str) -> str:
    """The alias of the ingest queues' own key, unique per account and Region."""
    return f"alias/{prefix}-{NAME}-ingest"


#: The trigger-mode parameters, under `<ssm_prefix>/use-cases/<NAME>/autonomy/`: the
#: deployment trigger mode, one mode per pack under `packs/`, and who last set each pack's mode
#: under `pack-actors/`, outside `packs/` so a reader of that path finds modes only. The
#: runtime appends `/<pack_id>` to the two prefixes (pipeline/case_store.py, api/trigger_mode).
DEPLOYMENT_TRIGGER_MODE = "trigger-mode"
TRIGGER_MODE_PACKS = "packs"
TRIGGER_MODE_PACK_ACTORS = "pack-actors"
#: Whether an emergency stop stands (`api/layer/python/emergency_stop.py`).
EMERGENCY_STOP = "emergency-stop"


def trigger_mode_parameters(ssm_prefix: str) -> dict[str, str]:
    """`{prefix, switch, packs, pack_actors, stop}`: the trigger-mode names for one
    deployment."""
    prefix = f"{ssm_prefix}/use-cases/{NAME}/autonomy"
    return {
        "prefix": prefix,
        "switch": f"{prefix}/{DEPLOYMENT_TRIGGER_MODE}",
        "packs": f"{prefix}/{TRIGGER_MODE_PACKS}",
        "pack_actors": f"{prefix}/{TRIGGER_MODE_PACK_ACTORS}",
        "stop": f"{prefix}/{EMERGENCY_STOP}",
    }
