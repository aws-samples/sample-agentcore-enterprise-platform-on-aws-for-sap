# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Trust a parameter only when CloudFormation wrote its latest version.

Stdlib only, and no package-relative import, so a use case's interceptor check can
link this file into its own package (the interceptor bundles each check's directory
alone) and read its settings the way the layer's check reads its own.
"""

from __future__ import annotations

import os


# Who CloudFormation is when a CDK stack deploy writes a parameter: this app's bootstrap
# execution role (the platform hands its ARN to every extension as CFN_EXEC_ROLE_ARN, so
# its account, qualifier and Region are all pinned), in its own session.
def cloudformation() -> str:
    role = os.environ["CFN_EXEC_ROLE_ARN"]  # arn:<partition>:iam::<account>:role/<name>
    return (
        role.replace(":iam::", ":sts::", 1).replace(":role/", ":assumed-role/", 1)
        + "/AWSCloudFormation"
    )


class Untrusted(ValueError):
    """A parameter's latest version was not written by a stack deploy."""


def cloudformation_wrote(ssm, name: str) -> str:
    """The parameter's value, if CloudFormation wrote its latest version; else raises.

    Anyone with ssm:PutParameter in the account could rewrite a parameter a check
    trusts. Only a stack deploy is trusted: CloudFormation writes as the CDK
    bootstrap execution role in session `AWSCloudFormation`, which GetParameterHistory
    records as the version's LastModifiedUser. A hand-written version has the
    writer's own identity there.
    """
    entries, token = [], None
    while True:
        page = ssm.get_parameter_history(
            Name=name, MaxResults=50, **({"NextToken": token} if token else {})
        )
        entries += page["Parameters"]
        token = page.get("NextToken")
        if not token:
            break
    latest = max(entries, key=lambda e: e["Version"])
    writer = latest.get("LastModifiedUser") or ""
    if writer != cloudformation():
        raise Untrusted(f"{name} was last written by {writer or 'nobody known'}")
    return latest["Value"]
