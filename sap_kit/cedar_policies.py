# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Cedar statements on the platform's shared policy engine, and how to create them.

For a use case or layer that adds `CfnPolicy` statements on `gateway/policy-engine-id`.
The engine is shared, so every permit names its owner's target-prefixed actions and
a condition admitting only its owner's clients:

- **brokered** (Cognito): the token's `scope` tag, every granted scope
  space-delimited. The condition matches the whole token: a bare
  `*uc-x/read*` would also match `.../readonly`, or a scope prefix ending in
  this one.
- **direct** (Entra): a token carries no `scope` tag, so the condition is the
  calling app's `azp`, from a map of scope to the app ids that hold it.

Two things CreatePolicy decides at deploy, not at synth, shape `add_policies`:

- The Cedar schema comes from the Gateway's attached targets, so a policy naming
  an action on a target not yet attached fails. Pass the targets as `depends_on`.
- It is not idempotent on handler retry: a throttled call that succeeded is
  retried and conflicts with itself. Policies are created one at a time, each
  depending on the one before.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from aws_cdk import aws_bedrockagentcore as agentcore
from constructs import Construct

_APP_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: scope (or None) -> the app ids (`azp`) holding it; None means brokered mode.
Principals = dict[str | None, list[str]] | None


def principal_condition(
    scope_prefix: str, scope: str | None, principals: Principals = None
) -> str | None:
    """The `when` body admitting a holder of `<scope_prefix>/<scope>`, or None for none.

    Brokered with `scope` None: no condition. Direct with `scope` None: the apps
    `principals[None]` names (a person's console app, for example).
    """
    if principals is None:
        if scope is None:
            return None
        want = f"{scope_prefix}/{scope}"
        return (
            '  principal.hasTag("scope") &&\n'
            f'  (principal.getTag("scope") == "{want}" ||\n'
            f'   principal.getTag("scope") like "{want} *" ||\n'
            f'   principal.getTag("scope") like "* {want}" ||\n'
            f'   principal.getTag("scope") like "* {want} *")'
        )
    apps = principals.get(scope) or []
    if not apps:
        raise ValueError(f"no app holds {scope_prefix}/{scope}: nothing to permit")
    bad = [a for a in apps if not _APP_ID.match(a)]
    if bad:
        raise ValueError(f"not an app id, refused in a Cedar string: {bad}")
    alternatives = " ||\n   ".join(f'principal.getTag("azp") == "{a}"' for a in apps)
    return f'  principal.hasTag("azp") &&\n  ({alternatives})'


def for_mode(
    text: str,
    scope_prefix: str,
    principals: Principals,
    scopes: Iterable[str] = ("read", "write"),
) -> str:
    """A statement written with brokered conditions, rewritten for `principals`.

    The brokered condition text is exact and unique per scope, so replacing it is
    the whole translation; brokered statements pass through untouched.
    """
    if principals is None:
        return text
    for scope in scopes:
        brokered = principal_condition(scope_prefix, scope)
        if brokered in text:
            text = text.replace(
                brokered, principal_condition(scope_prefix, scope, principals)
            )
    return text


def scoped_permit(
    actions: str | list[str],
    scope: str | None,
    gateway_arn: str,
    *,
    scope_prefix: str,
    principals: Principals = None,
) -> str:
    """Permit `actions` on the Gateway to a holder of `<scope_prefix>/<scope>`.

    One action (a str) renders `action ==`, a list `action in [...]`.
    """
    if isinstance(actions, str):
        action = f'  action == AgentCore::Action::"{actions}",\n'
    else:
        listed = ",\n    ".join(f'AgentCore::Action::"{a}"' for a in actions)
        action = f"  action in [\n    {listed}\n  ],\n"
    head = (
        "permit(\n"
        "  principal is AgentCore::OAuthUser,\n"
        f"{action}"
        f'  resource == AgentCore::Gateway::"{gateway_arn}"\n'
        ")"
    )
    condition = principal_condition(scope_prefix, scope, principals)
    if condition is None:
        return head + ";"
    return head + "\nwhen {\n" + condition + "\n};"


@dataclass(frozen=True)
class PolicySpec:
    id: str  # the construct id is `Policy-<id>`; never rename a deployed one
    name: str  # sap_kit.names.policy_name
    description: str
    text: str


def add_policies(
    scope: Construct,
    specs: Iterable[PolicySpec],
    *,
    engine_id: str,
    depends_on: Iterable[Construct] = (),
    after: agentcore.CfnPolicy | None = None,
) -> list[agentcore.CfnPolicy]:
    """One CfnPolicy per spec, created one at a time after `depends_on` and `after`."""
    created: list[agentcore.CfnPolicy] = []
    previous = after
    depends_on = list(depends_on)
    for s in specs:
        policy = agentcore.CfnPolicy(
            scope,
            f"Policy-{s.id}",
            name=s.name,
            description=s.description,
            policy_engine_id=engine_id,
            definition=agentcore.CfnPolicy.PolicyDefinitionProperty(
                cedar=agentcore.CfnPolicy.CedarPolicyProperty(statement=s.text),
            ),
        )
        for d in depends_on:
            policy.add_resource_dependency(d)
        if previous is not None:
            policy.add_resource_dependency(previous)
        previous = policy
        created.append(policy)
    return created
