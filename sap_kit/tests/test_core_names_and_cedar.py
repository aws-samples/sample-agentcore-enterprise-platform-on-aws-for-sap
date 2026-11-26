# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The shared name checks and Cedar helpers (sap_kit/names.py, cedar_policies.py).

Each use case's own tests pin the exact text and names they produce for it;
these pin what holds for any owner.
"""

from __future__ import annotations

import aws_cdk as cdk
import aws_cdk.assertions
import pytest

from sap_kit import cedar_policies, names


def test_the_hash_is_per_owner_and_deployment():
    a = names.policy_name("p", "dev", "owner-a", "read")
    assert a == names.policy_name("p", "dev", "owner-a", "read")
    assert a != names.policy_name("p", "dev", "owner-b", "read")
    assert a != names.policy_name("p", "prod", "owner-a", "read")


def test_a_scope_condition_matches_the_whole_scope_token():
    text = cedar_policies.principal_condition("uc-x", "read")
    assert '== "uc-x/read"' in text and 'like "* uc-x/read *"' in text
    assert cedar_policies.principal_condition("uc-x", None) is None


def test_direct_mode_keys_on_azp_and_refuses_what_is_not_an_app_id():
    text = cedar_policies.principal_condition("uc-x", "read", {"read": ["app-1"]})
    assert 'principal.getTag("azp") == "app-1"' in text
    with pytest.raises(ValueError, match="not an app id"):
        cedar_policies.principal_condition("uc-x", "read", {"read": ['a" || true']})
    with pytest.raises(ValueError, match="nothing to permit"):
        cedar_policies.principal_condition("uc-x", "write", {"read": ["app-1"]})


def test_policies_are_chained_and_written_as_plain_cedar():
    stack = cdk.Stack(cdk.App(), "S")
    spec = cedar_policies.PolicySpec(
        "a", "a_1", "d", "permit(principal, action, resource);"
    )
    first, second = cedar_policies.add_policies(
        stack,
        [spec, cedar_policies.PolicySpec("b", "b_1", "d", spec.text)],
        engine_id="e",
    )
    resources = cdk.assertions.Template.from_stack(stack).to_json()["Resources"]
    assert resources[stack.resolve(second.logical_id)]["DependsOn"] == [
        stack.resolve(first.logical_id)
    ]
    definition = resources[stack.resolve(first.logical_id)]["Properties"]["Definition"]
    assert definition == {"Cedar": {"Statement": spec.text}}
