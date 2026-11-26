# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The checks app.py runs over the synthesized templates, and what each one sees.

A guard is a pure function from ``{stack_name: template}`` to a list of findings. Its
scope decides which templates it gets: every stack in the app, or one owner's stacks
at a time, where an owner is each enabled layer and use case, in turn. ``run`` runs
every guard and fails the synth with all of their findings at once.

A core patch adds a guard by appending to ``GUARDS``, not by editing app.py.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import infra_utils.platform_config as pc
from infra_utils.idp_group_guard import idp_group_write_violations
from infra_utils.public_outputs import secret_public_outputs
from infra_utils.shared_role_grants import (
    foreign_key_grant_violations,
    inline_policy_name_collisions,
    shared_role_grant_violations,
)


@dataclass(frozen=True)
class Scope:
    """What a guard is checking: the deployment's names, and the owner if it has one."""

    prefix: str  # "<project>-<environment>"
    ssm_prefix: str  # "/<project>/<environment>"
    manifest: Any = None  # the owner's manifest; None when the guard sees every stack
    extensions: tuple = ()  # gateway.interceptor_extensions


Check = Callable[[dict[str, dict], Scope], list[str]]


@dataclass(frozen=True)
class Guard:
    title: str  # the heading of the synth error
    check: Check
    per_owner: bool = False  # each owner's stacks in turn, instead of every stack


def _shared_role(templates: dict[str, dict], s: Scope) -> list[str]:
    # The Gateway and interceptor roles, and the people Gateway's, serve every tenant,
    # so a grant on anything an owner does not define reaches every tenant.
    return shared_role_grant_violations(
        templates,
        role_names=[f"{s.prefix}-gateway-role", f"{s.prefix}-people-gateway-role"],
        role_arn_parameters=[
            f"{s.ssm_prefix}/gateway/role-arn",
            f"{s.ssm_prefix}/gateway/interceptor-role-arn",
            f"{s.ssm_prefix}/people-gateway/role-arn",
        ],
        key_arn_parameters=(f"{s.ssm_prefix}/security/kms-key-arn",),
    )


_REPO = Path(__file__).resolve().parents[1]


def _foreign_prefix_targets(templates: dict[str, dict], s: Scope) -> list[str]:
    # An interceptor check decides every target its prefixes match, so a target named
    # with another owner's prefix is refused by, or decided by, that owner's check.
    root = (
        pc.LAYERS_DIR if isinstance(s.manifest, pc.LayerManifest) else pc.USE_CASES_DIR
    )
    home = (root / s.manifest.name).resolve()

    def prefixes(own: bool) -> set[str]:
        return {
            p
            for e in s.extensions
            if (_REPO / e.path).resolve().is_relative_to(home) == own
            for p in e.target_prefixes
        }

    foreign = prefixes(False) - prefixes(True)
    return [
        f"{stack}/{rid}: target {name!r} starts with {p!r}, which another "
        "owner's interceptor check governs"
        for stack, t in templates.items()
        for rid, r in t.get("Resources", {}).items()
        if r.get("Type") == "AWS::BedrockAgentCore::GatewayTarget"
        and isinstance(name := r.get("Properties", {}).get("Name"), str)
        for p in sorted(foreign)
        if name.startswith(p)
    ]


GUARDS: list[Guard] = [
    # custom:idp_groups becomes cognito:groups at every token issue, so no client
    # on the pool, the platform's or a use case's, may let a user write it.
    Guard(
        "A user pool client would let a user write custom:idp_groups and so "
        "grant themselves a mapped group",
        lambda templates, _: idp_group_write_violations(templates),
    ),
    # An imported role's default policy is named after its construct path, so two
    # stacks importing one role under one id write one inline policy in turn.
    Guard(
        "Two stacks attach an inline policy of the same name to one role, so each "
        "deploy replaces the other's grants; import the role under another id",
        lambda templates, _: inline_policy_name_collisions(templates),
    ),
    Guard(
        "A use case grants a shared platform role access to something it does not own",
        _shared_role,
        per_owner=True,
    ),
    # The deploy summary and export print every public output.
    Guard(
        "A use case declares a public output that resolves a secret; the deploy "
        "summary and export would print it",
        lambda templates, s: secret_public_outputs(
            templates, s.manifest.public_outputs
        ),
        per_owner=True,
    ),
    # security/kms-key-arn's key policy trusts the account, so the IAM grant is the
    # whole control: only through a service (kms:ViaService), never a bare Decrypt.
    Guard(
        "A use case grants direct use of a KMS key it does not own",
        lambda templates, _: foreign_key_grant_violations(templates),
        per_owner=True,
    ),
    Guard(
        "A Gateway target is named with a prefix another owner's interceptor check "
        "governs; name it with your own",
        _foreign_prefix_targets,
        per_owner=True,
    ),
]


def run(
    templates: dict[str, dict],
    *,
    prefix: str,
    ssm_prefix: str,
    owners: Iterable[Any] = (),
    extensions: Iterable[Any] = (),
) -> None:
    """Run every guard; raise one ValueError listing every finding.

    `owners` are the enabled layers' and use cases' manifests; each one's stacks are
    `<prefix>-<suffix>` for every suffix in its `stacks`. `extensions` are
    gateway.interceptor_extensions.
    """
    extensions = tuple(extensions)
    everything = Scope(prefix, ssm_prefix, extensions=extensions)
    scoped = [
        (
            Scope(prefix, ssm_prefix, m, extensions),
            {
                name: t
                for name, t in templates.items()
                if name in {f"{prefix}-{s}" for s in m.stacks}
            },
        )
        for m in owners
    ]
    errors = []
    for guard in GUARDS:
        findings = (
            [f for scope, own in scoped for f in guard.check(own, scope)]
            if guard.per_owner
            else guard.check(templates, everything)
        )
        if findings:
            errors.append(f"{guard.title}:\n  " + "\n  ".join(findings))
    if errors:
        raise ValueError("\n".join(errors))
