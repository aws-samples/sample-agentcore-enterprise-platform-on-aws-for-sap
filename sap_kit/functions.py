# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""A use case's Lambda functions, and the Gateway targets they back, built one way.

A function gets the same runtime, architecture, asset excludes, X-Ray tracing and a
log group of its own; the caller adds only its grants. A target is one tool Lambda
behind the shared Gateway: the Gateway role may invoke that function and nothing else
of the use case's (app.py's shared-role guard refuses a wider grant).
"""

from __future__ import annotations

import shutil
import subprocess  # nosec B404 — fixed argv, no shell
from pathlib import Path

import aws_cdk as cdk
import jsii
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_logs as logs
from constructs import Construct

from infra_utils import log_retention
from infra_utils.use_case_context import UseCaseContext
from sap_kit.names import tool_name

# Untracked files a lane or a test run leaves beside a Lambda source; in an asset
# they change its hash, so every deploy republishes it unchanged.
ASSET_EXCLUDE = [
    "__pycache__",
    "*.pyc",
    ".DS_Store",
    ".ruff_cache",
    ".pytest_cache",
    ".mypy_cache",
]


def log_group(scope: Construct, id: str, ctx: UseCaseContext) -> logs.LogGroup:
    """A log group at the platform's retention, kept on delete under production."""
    return log_retention.log_group(scope, id, ctx.log_retention_days, ctx.production)


# pip's flags for wheels that run on the functions below: arm64, CPython 3.13.
_LAMBDA_WHEELS = [
    "--platform=manylinux2014_aarch64",
    "--implementation=cp",
    "--python-version=3.13",
    "--only-binary=:all:",
]


@jsii.implements(cdk.ILocalBundling)
class _PipWheels:
    """Installs `requirements.txt` with the local pip; the Docker image is the fallback."""

    def __init__(self, src: Path):
        self.src = src

    def try_bundle(self, output_dir: str, _options) -> bool:
        pip = shutil.which("pip3") or shutil.which("pip")
        if not pip:
            return False
        subprocess.run(  # nosec B603 — fixed argv
            [
                pip,
                "install",
                "--quiet",
                "--no-warn-conflicts",
                *_LAMBDA_WHEELS,
                "--target",
                output_dir,
            ]
            + ["-r", str(self.src / "requirements.txt")],
            check=True,
        )
        for f in self.src.glob("*.py"):
            shutil.copy(f, output_dir)
        return True


def code_with_wheels(src: Path) -> _lambda.Code:
    """`src`'s modules plus its pinned `requirements.txt`, as arm64 binary wheels."""
    return _lambda.Code.from_asset(
        str(src),
        exclude=ASSET_EXCLUDE,
        bundling=cdk.BundlingOptions(
            image=_lambda.Runtime.PYTHON_3_13.bundling_image,
            platform="linux/arm64",
            local=_PipWheels(src),
            command=[
                "bash",
                "-c",
                (
                    "pip install --only-binary=:all: -r requirements.txt"
                    " -t /asset-output && cp *.py /asset-output/"
                ),
            ],
        ),
    )


def uc_function(
    scope: Construct,
    id: str,
    ctx: UseCaseContext,
    code: str | Path | _lambda.Code,
    *,
    handler: str = "index.handler",
    layers: list[_lambda.ILayerVersion] | tuple = (),
    timeout: int = 15,
    **kw,
) -> _lambda.Function:
    """A Python 3.13 arm64 function; `code` is a source directory or a built asset."""
    if not isinstance(code, _lambda.Code):
        code = _lambda.Code.from_asset(str(code), exclude=ASSET_EXCLUDE)
    return _lambda.Function(
        scope,
        id,
        runtime=_lambda.Runtime.PYTHON_3_13,
        architecture=_lambda.Architecture.ARM_64,
        handler=handler,
        code=code,
        layers=list(layers) or None,
        timeout=cdk.Duration.seconds(timeout),
        log_group=log_group(scope, f"{id}Logs", ctx),
        tracing=_lambda.Tracing.ACTIVE,
        **kw,
    )


def gateway_lambda_target(
    scope: Construct,
    id: str,
    *,
    name: str,
    fn: _lambda.IFunction,
    tools: list[dict],
    gateway_id: str,
    gateway_role: iam.IRole,
) -> agentcore.CfnGatewayTarget:
    """The target `name` on the shared Gateway, serving `tools` from `fn`."""
    for tool in tools:
        tool_name(name, tool["Name"])
    gateway_role.add_to_principal_policy(
        iam.PolicyStatement(
            actions=["lambda:InvokeFunction"], resources=[fn.function_arn]
        )
    )
    target = agentcore.CfnGatewayTarget(
        scope,
        id,
        name=name,
        gateway_identifier=gateway_id,
        credential_provider_configurations=[
            {"credentialProviderType": "GATEWAY_IAM_ROLE"}
        ],
        target_configuration={"mcp": {}},
    )
    # The L1 mapping drops the `lambda` key (a Python keyword); set it raw, as the
    # platform's own targets do.
    target.add_property_override(
        "TargetConfiguration.Mcp.Lambda",
        {"LambdaArn": fn.function_arn, "ToolSchema": {"InlinePayload": tools}},
    )
    # The invoke grant must exist before the Gateway lists the target's tools.
    target.node.add_dependency(gateway_role)
    return target
