# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""A use case's declared public outputs must not carry a secret.

A manifest's ``public_outputs`` lets the deploy summary and export print those
CloudFormation outputs. An output whose value is a Secrets Manager or SSM
SecureString dynamic reference would print the secret, so app.py runs this
over each use case's synthesized templates and fails the synth on a finding.

Pure Python over template dicts, so it is testable without aws_cdk.
"""

from __future__ import annotations

import json

# CDK renders SecretValue and secure-string reads as these, often split
# across an Fn::Join, so the search runs over the serialized value.
SECRET_REFERENCES = ("{{resolve:secretsmanager:", "{{resolve:ssm-secure:")


def secret_public_outputs(
    templates: dict[str, dict], public_outputs: list[str]
) -> list[str]:
    """``<stack>/<OutputKey>`` for each declared output that resolves a secret."""
    findings = []
    for stack_name, template in templates.items():
        outputs = template.get("Outputs", {})
        for key in public_outputs:
            value = json.dumps(outputs.get(key, {}).get("Value"))
            if any(ref in value for ref in SECRET_REFERENCES):
                findings.append(f"{stack_name}/{key}")
    return findings
