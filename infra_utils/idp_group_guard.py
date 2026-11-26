# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""No user may write custom:idp_groups for themselves.

The pre token generation trigger turns custom:idp_groups into cognito:groups,
and it reruns on every refresh, so whoever can write that attribute can grant
themselves a mapped group. Cognito needs write access to a mapped attribute on
every client a federated user signs in through (or the sign-in fails), so the
attribute cannot simply be unwritable. What makes it safe is that such a
client issues no token that can call UpdateUserAttributes: no
aws.cognito.signin.user.admin scope, and no InitiateAuth sign-in flow (those
tokens always carry that scope). Refresh stays allowed; it keeps the original
scopes.

Refresh also keeps the attribute: Cognito rewrites custom:idp_groups only at the
next federated sign-in, so a user the IdP removes from a group keeps the mapped
group until their refresh token expires. Such a client therefore refreshes for
MAX_REFRESH_MINUTES at most, not Cognito's default 30 days.

app.py runs this over every synthesized template, the platform's and each use
case's, and fails the synth on any finding. Pure Python over template dicts.
"""

from __future__ import annotations

ATTRIBUTE = "custom:idp_groups"
SELF_SERVICE_SCOPE = "aws.cognito.signin.user.admin"
SAFE_FLOWS = {"ALLOW_REFRESH_TOKEN_AUTH"}
#: How long an IdP group removal may take to reach a mapped Cognito group.
MAX_REFRESH_MINUTES = 12 * 60
_MINUTES = {"minutes": 1, "hours": 60, "days": 24 * 60}


def idp_group_write_violations(templates: dict[str, dict]) -> list[str]:
    findings: list[str] = []
    for stack_name, template in templates.items():
        for logical_id, resource in template.get("Resources", {}).items():
            if resource.get("Type") != "AWS::Cognito::UserPoolClient":
                continue
            props = resource.get("Properties", {})
            if ATTRIBUTE not in (props.get("WriteAttributes") or []):
                continue  # unset WriteAttributes = standard attributes only
            where = f"{stack_name}/{logical_id}"
            if SELF_SERVICE_SCOPE in (props.get("AllowedOAuthScopes") or []):
                findings.append(
                    f"{where} can write {ATTRIBUTE} and allows {SELF_SERVICE_SCOPE}"
                )
            # Unset ExplicitAuthFlows means Cognito's default, which includes SRP.
            flows = props.get("ExplicitAuthFlows")
            if flows is None or set(flows) - SAFE_FLOWS:
                findings.append(
                    f"{where} can write {ATTRIBUTE} and allows sign-in flows "
                    f"{sorted(set(flows or ['<Cognito default>']) - SAFE_FLOWS)}; "
                    "only ALLOW_REFRESH_TOKEN_AUTH is safe"
                )
            unit = (props.get("TokenValidityUnits") or {}).get("RefreshToken", "days")
            validity = props.get("RefreshTokenValidity", 30)
            if validity * _MINUTES.get(unit, _MINUTES["days"]) > MAX_REFRESH_MINUTES:
                findings.append(
                    f"{where} can write {ATTRIBUTE} and keeps a refresh token "
                    f"{validity} {unit}; at most {MAX_REFRESH_MINUTES} minutes, or a "
                    "user the IdP removed from a group keeps the mapped group that long"
                )
    return findings
