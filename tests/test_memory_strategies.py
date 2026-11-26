# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""agents.memory.strategies: unset is the historical set, a list replaces it."""

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from pydantic import ValidationError

from infra_utils.platform_config import MemoryConfig
from stacks.memory_stack import MemoryStack


def strategies(**kwargs) -> list:
    app = cdk.App()
    stack = MemoryStack(app, "m", project_name="p", environment="dev", **kwargs)
    (memory,) = (
        Template.from_stack(stack)
        .find_resources("AWS::BedrockAgentCore::Memory")
        .values()
    )
    return memory["Properties"]["MemoryStrategies"]


def test_unset_keeps_the_historical_literals():
    # An existing deployment must synthesize the same Memory it already has.
    assert strategies(use_long_term_memory=True) == [
        {
            "SemanticMemoryStrategy": {
                "Name": "p_dev_memory_semantic",
                "Description": "Semantic fact extraction and override strategy",
                "Namespaces": ["AGENT_ID"],
            }
        },
        {
            "UserPreferenceMemoryStrategy": {
                "Name": "p_dev_memory_user_pref",
                "Description": "User preference tracking strategy",
                "Namespaces": ["USER_ID"],
            }
        },
    ]


def test_empty_list_is_short_term_only():
    assert strategies(memory_strategies=[]) == []


def test_listed_strategies_are_namespaced_per_actor():
    out = strategies(memory_strategies=["semantic", "summary", "user_preference"])
    templates = [next(iter(s.values()))["NamespaceTemplates"][0] for s in out]
    assert all("{actorId}" in t for t in templates)
    assert "{sessionId}" in templates[1]


def test_config_rejects_duplicates_and_long_term_together():
    assert MemoryConfig().strategies is None
    assert MemoryConfig(strategies=[]).strategies == []
    with pytest.raises(ValidationError):
        MemoryConfig(strategies=["semantic", "semantic"])
    with pytest.raises(ValidationError):
        MemoryConfig(long_term=True, strategies=["semantic"])
    with pytest.raises(ValidationError):
        MemoryConfig(strategies=["episodic"])
