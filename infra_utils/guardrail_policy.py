# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""PII and prompt-attack policy for the platform's Amazon Bedrock guardrails.

The Gateway egress guardrail and each runtime's baseline guardrail start from
the control-library artifact ``guardrail.egress-default``. Under
``deployment.mode: production`` a floor holds that configuration cannot lower:
the prompt-attack filter at HIGH on input, the ``PII_FLOOR`` types at BLOCK, and on a
runtime the ``CREDENTIAL_FLOOR`` types at BLOCK on the model's output.

Every consumer runs on a published version (``published_version``), never DRAFT.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from infra_utils.policy_loader import load_control

# botocore's bedrock GuardrailPiiEntityType and GuardrailSensitiveInformationAction.
PiiEntityType = Literal[
    "ADDRESS",
    "AGE",
    "AWS_ACCESS_KEY",
    "AWS_SECRET_KEY",
    "CA_HEALTH_NUMBER",
    "CA_SOCIAL_INSURANCE_NUMBER",
    "CREDIT_DEBIT_CARD_CVV",
    "CREDIT_DEBIT_CARD_EXPIRY",
    "CREDIT_DEBIT_CARD_NUMBER",
    "DRIVER_ID",
    "EMAIL",
    "INTERNATIONAL_BANK_ACCOUNT_NUMBER",
    "IP_ADDRESS",
    "LICENSE_PLATE",
    "MAC_ADDRESS",
    "NAME",
    "PASSWORD",
    "PHONE",
    "PIN",
    "SWIFT_CODE",
    "UK_NATIONAL_HEALTH_SERVICE_NUMBER",
    "UK_NATIONAL_INSURANCE_NUMBER",
    "UK_UNIQUE_TAXPAYER_REFERENCE_NUMBER",
    "URL",
    "USERNAME",
    "US_BANK_ACCOUNT_NUMBER",
    "US_BANK_ROUTING_NUMBER",
    "US_INDIVIDUAL_TAX_IDENTIFICATION_NUMBER",
    "US_PASSPORT_NUMBER",
    "US_SOCIAL_SECURITY_NUMBER",
    "VEHICLE_IDENTIFICATION_NUMBER",
]
PiiAction = Literal["BLOCK", "ANONYMIZE", "NONE"]

#: BLOCK on input and output, on every guardrail.
PII_FLOOR = ("US_SOCIAL_SECURITY_NUMBER", "CREDIT_DEBIT_CARD_NUMBER")
#: BLOCK on a runtime's output only: a model must never say a credential. The detector
#: reads some random ids as a secret key (a case id alone on a line, or alone as a JSON
#: value), so scanning a runtime's input or the Gateway's tool data for them refused
#: real cases, every time, since a case id is derived from its document.
CREDENTIAL_FLOOR = ("AWS_ACCESS_KEY", "AWS_SECRET_KEY")


def output_only_block(entity_type: str) -> dict[str, Any]:
    """A PII entity blocked in what the model says, not screened in what it is given."""
    return {
        "Type": entity_type,
        "Action": "BLOCK",
        "InputAction": "NONE",
        "InputEnabled": False,
        "OutputAction": "BLOCK",
        "OutputEnabled": True,
    }


def baseline_policy(
    pii_entities: dict[str, str] | None = None,
    *,
    floor: bool = False,
    runtime: bool = False,
) -> dict[str, Any]:
    """``guardrail.egress-default`` with per-entity PII actions overridden.

    An override sets the action for its type, adding the type if the artifact
    lacks it; ``NONE`` leaves the type unscreened. With ``floor``, every
    ``PII_FLOOR`` type is BLOCK whatever the overrides say, and on a ``runtime``
    every ``CREDENTIAL_FLOOR`` type is blocked on output.
    """
    policy = load_control("guardrail.egress-default")
    pii = policy["SensitiveInformationPolicyConfig"]
    by_type = {e["Type"]: e for e in pii["PiiEntitiesConfig"]}
    overrides = dict(pii_entities or {})
    if floor:
        overrides.update(dict.fromkeys(PII_FLOOR, "BLOCK"))
    for entity_type, action in overrides.items():
        if action == "NONE":
            # Leave it out: Amazon Bedrock reports an entity whose Action is NONE as BLOCKED
            # (seen live with ApplyGuardrail), so "NONE" means not screened at all.
            by_type.pop(entity_type, None)
        else:
            by_type[entity_type] = {"Type": entity_type, "Action": action}
    if floor and runtime:
        by_type |= {t: output_only_block(t) for t in CREDENTIAL_FLOOR}
    pii["PiiEntitiesConfig"] = list(by_type.values())
    return policy


def floor_violations(policy: dict[str, Any]) -> list[str]:
    """What a guardrail policy (CloudFormation shape) lacks of the production floor."""
    missing = []
    filters = {
        f.get("Type"): f
        for f in policy.get("ContentPolicyConfig", {}).get("FiltersConfig", [])
    }
    attack = filters.get("PROMPT_ATTACK", {})
    if (
        attack.get("InputStrength") != "HIGH"
        or attack.get("InputEnabled") is False
        or attack.get("InputAction", "BLOCK") != "BLOCK"
    ):
        missing.append("a PROMPT_ATTACK filter at InputStrength HIGH")
    entities = {
        e.get("Type"): e
        for e in policy.get("SensitiveInformationPolicyConfig", {}).get(
            "PiiEntitiesConfig", []
        )
    }
    for entity_type in PII_FLOOR:
        e = entities.get(entity_type, {})
        if (
            e.get("Action") != "BLOCK"
            or e.get("InputEnabled") is False
            or e.get("OutputEnabled") is False
            or e.get("InputAction", "BLOCK") != "BLOCK"
            or e.get("OutputAction", "BLOCK") != "BLOCK"
        ):
            missing.append(f"{entity_type} at BLOCK")
    for entity_type in CREDENTIAL_FLOOR:
        e = entities.get(entity_type, {})
        if e.get("OutputEnabled") is False or (
            e.get("OutputAction", e.get("Action")) != "BLOCK"
        ):
            missing.append(f"{entity_type} at BLOCK on output")
    return missing


def published_version(guardrail, policy: Any = None):
    """A numbered version of ``guardrail``, for every consumer to name instead of DRAFT.

    DRAFT is what an update edits in place, and it refuses calls while it is
    UPDATING. A version is a snapshot, so its description carries a hash of the
    guardrail's properties and of ``policy`` (whatever ``add_property_override`` set):
    a policy change changes the description, and CloudFormation replaces the version,
    creating the new one before it repoints the consumers. The old one is retained, not
    deleted: a running runtime session, or a stack that reads the version from SSM and
    deploys later, still names it. Deleting the guardrail deletes every version.
    Amazon Bedrock keeps at most 20 versions per guardrail: past that, delete old ones by hand.
    Asking twice for one guardrail returns the same version.
    """
    import aws_cdk as cdk
    from aws_cdk import aws_bedrock as bedrock

    found = guardrail.node.try_find_child("Version")
    if found is not None:
        return found
    rendered = cdk.Stack.of(guardrail).resolve(guardrail._cfn_properties)
    digest = hashlib.sha256(
        json.dumps([rendered, policy], sort_keys=True, default=str).encode()
    ).hexdigest()
    version = bedrock.CfnGuardrailVersion(
        guardrail,
        "Version",
        guardrail_identifier=guardrail.attr_guardrail_id,
        description=f"Policy sha256:{digest[:16]}",
    )
    # Retained on stack delete too: deleting the guardrail deletes its versions.
    version.apply_removal_policy(cdk.RemovalPolicy.RETAIN)
    return version
