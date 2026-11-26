# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Every guardrail the synthesized app defines fits Amazon Bedrock's limits.

CloudFormation only finds an oversized field when it creates the guardrail, so
a topic definition one sentence too long fails a deploy. This synthesizes the
app with every guardrail on (the egress filter, each runtime's baseline, the
notification and persistence guardrails) and checks each against the limits in
https://docs.aws.amazon.com/bedrock/latest/APIReference/API_CreateGuardrail.html,
https://docs.aws.amazon.com/bedrock/latest/APIReference/API_GuardrailTopicConfig.html
and https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-tiers.html.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from app_synth import synth_app

UC = Path(__file__).resolve().parents[1]

NAME = re.compile(r"[0-9a-zA-Z-_]{1,50}")
TOPIC_NAME = re.compile(r"[0-9a-zA-Z-_ !?.]{1,100}")
# A definition's cap is the tier's: 200 characters on CLASSIC (the default), 1,000 on STANDARD.
DEFINITION_MAX = {"CLASSIC": 200, "STANDARD": 1000}


@pytest.fixture(scope="module")
def guardrails() -> dict[str, dict]:
    config = yaml.safe_load(
        (UC / "fixtures/contract/business-operations.yaml").read_text()
    )
    config["security"] |= {"require_guardrails": True, "egress_filter": True}
    result, out = synth_app(yaml.safe_dump(config))
    assert result.returncode == 0, result.stderr[-2000:]
    return {
        f"{template.stem}/{logical_id}": resource["Properties"]
        for template in out.glob("*.template.json")
        for logical_id, resource in json.loads(template.read_text())[
            "Resources"
        ].items()
        if resource["Type"] == "AWS::Bedrock::Guardrail"  # publication-gate: allow wording
    }


def test_every_kind_of_guardrail_is_checked(guardrails):
    ids = [key.split("/")[1] for key in guardrails]
    for kind in (
        "EgressGuardrail",
        "BaselineGuardrail",
        "NotificationGuardrail",
        "PersistGuardrail",
    ):
        assert any(i.startswith(kind) for i in ids), sorted(guardrails)


def _problems(props: dict) -> list[str]:
    found = []
    name = props["Name"]
    if isinstance(name, str) and not NAME.fullmatch(name):
        found.append(f"name {name!r} is not 1-50 of [0-9a-zA-Z-_]")
    if len(props.get("Description", "x")) > 200:
        found.append("description is over 200 characters")
    for key in ("BlockedInputMessaging", "BlockedOutputsMessaging"):
        if not 1 <= len(props[key]) <= 500:
            found.append(f"{key} is not 1-500 characters")
    topics = props.get("TopicPolicyConfig", {})
    tier = topics.get("TierConfig", {}).get("TierName", "CLASSIC")
    configs = topics.get("TopicsConfig", [])
    if len(configs) > 30:
        found.append(f"{len(configs)} topics, over 30")
    for topic in configs:
        label = f"topic {topic['Name']!r}"
        if not TOPIC_NAME.fullmatch(topic["Name"]):
            found.append(f"{label}: name is not 1-100 of [0-9a-zA-Z-_ !?.]")
        if not 1 <= len(topic["Definition"]) <= DEFINITION_MAX[tier]:
            found.append(
                f"{label}: definition is {len(topic['Definition'])} characters, "
                f"over {DEFINITION_MAX[tier]} on the {tier} tier"
            )
        examples = topic.get("Examples", [])
        if len(examples) > 5:
            found.append(f"{label}: {len(examples)} examples, over 5")
        found += [
            f"{label}: example {e[:30]!r}... is {len(e)} characters, over 100"
            for e in examples
            if not 1 <= len(e) <= 100
        ]
    if tier == "STANDARD" and "CrossRegionConfig" not in props:
        found.append("the STANDARD tier needs a CrossRegionConfig")
    return found


def test_every_guardrail_fits_bedrock_limits(guardrails):
    problems = {key: p for key, props in guardrails.items() if (p := _problems(props))}
    assert not problems, json.dumps(problems, indent=2)
