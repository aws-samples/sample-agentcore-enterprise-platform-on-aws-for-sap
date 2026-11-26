# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Deploy order for use-case and layer stacks, and for the stacks that create Cedar policies.

A use case or layer reads the platform through SSM, which gives CDK no edge. So
`add_read_edges` gives each of its stacks an edge to every stack in the app that
publishes a parameter it reads, either as a `value_for_string_parameter` template
parameter or as a `{{resolve:ssm:...}}` string, and to nothing else. A stack that
reads only `auth/` then deploys as soon as auth is up, not after the runtime chain.

A read the walk cannot see is what the manifest's `requires` is for: a deploy-time
custom resource calling GetParameter, a name built from a token, or an order with no
read behind it at all. A `requires` entry that no stack of the build was found to read
becomes an edge on every stack of the build, which is what every entry used to be.

`chain_policy_stacks` puts the stacks that create Cedar policies one behind the other.
CreatePolicy is not idempotent on handler retry, so two stacks creating policies on the
shared engine at once can conflict, and `cdk deploy --concurrency` would otherwise allow it.
"""

from __future__ import annotations

import graphlib
import itertools
import json
import re

import aws_cdk as cdk
from aws_cdk import aws_ssm as ssm

_RESOLVE = re.compile(r"\{\{resolve:ssm:([^}:]+)")
POLICY = "AWS::BedrockAgentCore::Policy"


def _stacks(app: cdk.App) -> list[cdk.Stack]:
    return [c for c in app.node.children if isinstance(c, cdk.Stack)]


def _own(stack: cdk.Stack):
    return (c for c in stack.node.find_all() if cdk.Stack.of(c) is stack)


def publishers(app: cdk.App) -> dict[str, cdk.Stack]:
    """Parameter name -> the stack that creates it, for every literal name in the app."""
    found = {}
    for stack in _stacks(app):
        for c in _own(stack):
            if (
                isinstance(c, ssm.CfnParameter)
                and c.name
                and not cdk.Token.is_unresolved(c.name)
            ):
                found[c.name] = stack
    return found


def reads(stack: cdk.Stack) -> set[str]:
    """The SSM parameter names `stack` reads at deploy."""
    names = set()
    for c in _own(stack):
        if isinstance(c, cdk.CfnParameter):
            if c.type.startswith("AWS::SSM::Parameter::Value") and isinstance(
                c.default, str
            ):
                names.add(c.default)
        elif isinstance(c, cdk.CfnResource):
            # ponytail: resolves each resource's properties before synth. Safe while no
            # construct mutates state behind a cached Lazy after this pass (app.py runs it
            # last); tests/test_stack_edges.py pins templates byte-equal with and without it.
            props = json.dumps(stack.resolve(c._cfn_properties), default=str)
            names.update(_RESOLVE.findall(props))
    return names


def add_read_edges(
    stacks: list[cdk.Stack],
    required: list[cdk.Stack],
    published: dict[str, cdk.Stack],
    owner: str,
) -> None:
    """Edge each of one build's `stacks` to the publishers of what it reads; a
    `required` stack none of them reads becomes an edge on all of them."""
    read_from = set()
    for stack in stacks:
        for name in sorted(reads(stack)):
            source = published.get(name)
            if source is None or source.node.path == stack.node.path:
                continue
            stack.add_stack_dependency(source, f"{owner} reads {name}")
            read_from.add(source.node.path)
    for core in required:
        if core.node.path in read_from:
            continue
        for stack in stacks:
            stack.add_stack_dependency(core, f"{owner} requires it")


def _ancestors(stack: cdk.Stack, seen: set[str] | None = None) -> set[str]:
    seen = set() if seen is None else seen
    for dep in stack.dependencies:
        if dep.node.path not in seen:
            seen.add(dep.node.path)
            _ancestors(dep, seen)
    return seen


def chain_policy_stacks(app: cdk.App) -> list[str]:
    """Order every stack that creates a Cedar policy behind the one before it, in an
    order the existing edges allow. Returns the chain, by construct path."""
    policy_stacks = {
        s.node.path: s
        for s in _stacks(app)
        if any(
            isinstance(c, cdk.CfnResource) and c.cfn_resource_type == POLICY
            for c in _own(s)
        )
    }
    graph = {
        path: _ancestors(stack) & policy_stacks.keys()
        for path, stack in policy_stacks.items()
    }
    chain = list(graphlib.TopologicalSorter(graph).static_order())
    for before, after in itertools.pairwise(chain):
        policy_stacks[after].add_stack_dependency(
            policy_stacks[before], "Cedar policies: one stack at a time"
        )
    return chain
