# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""A use case may extend a shared platform role only with grants on its own resources,
and may use a key it does not own only through kms:ViaService.

The Gateway role (and the interceptor role, when extensions are listed) is
published to SSM so a use case can let it reach the use case's own target
Lambdas, guardrail or tables. The same role serves every use case, so a grant
on anything else — a wildcard, a literal ARN, another stack's resource — would
hand that reach to every tenant. app.py runs this over each use case's
synthesized templates and fails the synth on any finding.

Pure Python over template dicts, so it is testable without aws_cdk.
"""

from __future__ import annotations

import json
import re

# Policy resource type -> the property that names the role(s) it attaches to.
_POLICY_TYPES = {
    "AWS::IAM::Policy": "Roles",
    "AWS::IAM::ManagedPolicy": "Roles",
    "AWS::IAM::RolePolicy": "RoleName",
}


def shared_role_grant_violations(
    templates: dict[str, dict],
    *,
    role_names: list[str],
    role_arn_parameters: list[str],
    key_arn_parameters: tuple[str, ...] = (),
) -> list[str]:
    """Findings for one use case's stacks ({stack_name: template}).

    A policy is a grant to a shared role when its role list mentions one of
    ``role_names`` or a template parameter whose default is one of
    ``role_arn_parameters`` (an SSM-resolved role ARN). Every Allow statement
    in such a policy must name only resources this use case defines: a Ref or
    GetAtt to a resource in the same template, an import from one of these
    stacks, or a Join/Sub built on one of those (``<own-arn>:*`` is fine).

    The one exception is a KMS-only statement on a key published at one of
    ``key_arn_parameters`` (the platform CMK) under kms:ViaService: the shared
    role then reads the use case's own encrypted tables and buckets, and the
    data grant beside it, held to the rule above, is the control.
    """
    findings: list[str] = []
    own_exports = tuple(f"{name}:" for name in templates)
    for stack_name, template in templates.items():
        resources = template.get("Resources", {})
        params = [
            pid
            for pid, p in template.get("Parameters", {}).items()
            if p.get("Default") in role_arn_parameters
        ]
        needles = [*role_names, *(f'"Ref": "{pid}"' for pid in params)]
        keys = [
            {"Ref": pid}
            for pid, p in template.get("Parameters", {}).items()
            if p.get("Default") in key_arn_parameters
        ]
        for logical_id, resource in resources.items():
            key = _POLICY_TYPES.get(resource.get("Type"))
            props = resource.get("Properties", {})
            if not key or not any(n in json.dumps(props.get(key)) for n in needles):
                continue
            statements = props.get("PolicyDocument", {}).get("Statement", [])
            for i, statement in enumerate(statements):
                where = f"{stack_name}/{logical_id} statement {statement.get('Sid', i)}"
                if statement.get("Effect") != "Allow":
                    continue
                if _key_via_service(statement, keys):
                    continue
                if "NotResource" in statement or "NotAction" in statement:
                    findings.append(f"{where}: NotResource/NotAction is not scoped")
                targets = statement.get("Resource", [])
                for target in targets if isinstance(targets, list) else [targets]:
                    if not _is_own(target, resources, own_exports):
                        findings.append(
                            f"{where}: {json.dumps(target)} is not a resource "
                            "this use case defines"
                        )
    return findings


def _is_own(value, resources: dict, own_exports: tuple[str, ...]) -> bool:
    if not isinstance(value, dict):
        return False  # a literal ARN cannot be shown to be the use case's own
    if "Ref" in value:
        return value["Ref"] in resources
    if "Fn::GetAtt" in value:
        att = value["Fn::GetAtt"]
        return (
            att[0] if isinstance(att, list) else str(att).split(".")[0]
        ) in resources
    if "Fn::ImportValue" in value:
        name = value["Fn::ImportValue"]
        return isinstance(name, str) and name.startswith(own_exports)
    if "Fn::Join" in value:
        parts = value["Fn::Join"][1]
        for part in parts if isinstance(parts, list) else []:
            if isinstance(part, str):
                if "*" in part:
                    return False  # a wildcard before any own resource
            elif _is_own(part, resources, own_exports):
                return True
        return False
    if "Fn::Sub" in value:
        sub = value["Fn::Sub"]
        text = sub if isinstance(sub, str) else sub[0]
        for ref in re.finditer(r"\$\{([A-Za-z0-9]+)[.}]", text):
            if ref.group(1) in resources:
                return "*" not in text[: ref.start()]
        return False
    return False


# Services a use case may route the platform CMK through. kms:ViaService pins a
# grant to requests those services make for the principal, so the key is never
# directly callable: a bare kms:Decrypt would decrypt anything the CMK sealed,
# including the platform's own Memory.
KEY_VIA_SERVICES = ("dynamodb", "s3", "logs", "sns", "sqs")
_VIA_SERVICE_RE = re.compile(
    rf"^({'|'.join(KEY_VIA_SERVICES)})\.[a-z0-9-]+\.amazonaws\.com$"
)
_VIA_OPERATORS = {
    "StringEquals",
    "StringLike",
    "ForAnyValue:StringEquals",
    "ForAnyValue:StringLike",
}


def foreign_key_grant_violations(templates: dict[str, dict]) -> list[str]:
    """Findings for one use case's stacks ({stack_name: template}).

    Any Allow statement with a KMS action on a key the use case does not define
    (the platform CMK from ``security/kms-key-arn``, a literal ARN, a wildcard)
    must carry ``kms:ViaService`` naming only KEY_VIA_SERVICES. A key the use
    case creates itself is its own business. Covers IAM policies attached to
    any role, not only shared ones: the CMK's key policy trusts the account.
    """
    findings: list[str] = []
    own_exports = tuple(f"{name}:" for name in templates)
    for stack_name, template in templates.items():
        resources = template.get("Resources", {})
        for logical_id, resource in resources.items():
            props = resource.get("Properties", {})
            if resource.get("Type") in _POLICY_TYPES:
                documents = [props.get("PolicyDocument", {})]
            elif resource.get("Type") == "AWS::IAM::Role":
                documents = [
                    p.get("PolicyDocument", {}) for p in props.get("Policies", [])
                ]
            else:
                continue
            for document in documents:
                for i, statement in enumerate(document.get("Statement", [])):
                    if statement.get("Effect") != "Allow" or not _has_kms_action(
                        statement
                    ):
                        continue
                    targets = statement.get("Resource", "*")
                    targets = targets if isinstance(targets, list) else [targets]
                    if "NotResource" not in statement and all(
                        _is_own(t, resources, own_exports) for t in targets
                    ):
                        continue
                    if not _via_service_only(statement.get("Condition", {})):
                        findings.append(
                            f"{stack_name}/{logical_id} statement "
                            f"{statement.get('Sid', i)}: a KMS grant on a key this "
                            "use case does not define needs a kms:ViaService "
                            f"condition naming only {', '.join(KEY_VIA_SERVICES)}"
                        )
    return findings


def _key_via_service(statement: dict, keys: list[dict]) -> bool:
    """A KMS-only grant on one of `keys`, usable only through KEY_VIA_SERVICES."""
    if not keys or "NotAction" in statement or "NotResource" in statement:
        return False
    actions = statement.get("Action", [])
    actions = actions if isinstance(actions, list) else [actions]
    targets = statement.get("Resource", [])
    targets = targets if isinstance(targets, list) else [targets]
    return (
        bool(actions)
        and all(str(a).lower().startswith("kms:") for a in actions)
        and bool(targets)
        and all(t in keys for t in targets)
        and _via_service_only(statement.get("Condition", {}))
    )


def _has_kms_action(statement: dict) -> bool:
    if "NotAction" in statement:
        return True
    actions = statement.get("Action", [])
    actions = actions if isinstance(actions, list) else [actions]
    return any(a == "*" or str(a).lower().startswith("kms:") for a in actions)


def _via_service_only(condition: dict) -> bool:
    for operator, clauses in condition.items():
        if operator not in _VIA_OPERATORS:
            continue
        for key, values in clauses.items():
            if key.lower() != "kms:viaservice":
                continue
            values = values if isinstance(values, list) else [values]
            return bool(values) and all(
                _VIA_SERVICE_RE.match(_render(v)) for v in values
            )
    return False


def _render(value) -> str:
    """A condition value as text; anything unresolvable renders as a wildcard."""
    pseudo = {"AWS::Region": "region", "AWS::URLSuffix": "amazonaws.com"}
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and "Ref" in value:
        return pseudo.get(value["Ref"], "*")
    if isinstance(value, dict) and "Fn::Join" in value:
        sep, parts = value["Fn::Join"]
        return sep.join(_render(p) for p in parts)
    if (
        isinstance(value, dict)
        and "Fn::Sub" in value
        and isinstance(value["Fn::Sub"], str)
    ):
        return re.sub(
            r"\$\{([^}]+)\}", lambda m: pseudo.get(m.group(1), "*"), value["Fn::Sub"]
        )
    return "*"


def inline_policy_name_collisions(templates: dict[str, dict]) -> list[str]:
    """Inline policies two stacks attach to one role under the same name.

    An inline policy's name is unique per role, so the second stack's deploy
    replaces the first's statements. CDK names the policy on an imported role after
    its construct path, which is the same in every stack that imports the role under
    the same id. Roles are told apart by the SSM parameter (or literal name) their
    reference resolves from.
    """
    owners: dict[tuple[str, str], list[str]] = {}
    for stack_name, template in templates.items():
        params = template.get("Parameters", {})
        for logical_id, resource in template.get("Resources", {}).items():
            if resource.get("Type") not in ("AWS::IAM::Policy", "AWS::IAM::RolePolicy"):
                continue
            props = resource.get("Properties", {})
            name = props.get("PolicyName")
            roles = props.get("Roles", props.get("RoleName"))
            text = json.dumps(roles)
            keys = [
                params[p]["Default"]
                for p in params
                if f'"Ref": "{p}"' in text and "Default" in params[p]
            ] or [
                r
                for r in (roles if isinstance(roles, list) else [roles])
                if isinstance(r, str)
            ]
            if not isinstance(name, str):
                continue
            for key in keys:
                owners.setdefault((key, name), []).append(f"{stack_name}/{logical_id}")
    return [
        f"{name} on {role}: " + ", ".join(where)
        for (role, name), where in sorted(owners.items())
        if len({w.split("/")[0] for w in where}) > 1
    ]
