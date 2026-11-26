# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The `business-operations-sops` Gateway target: `load_sop`.

In the main stack beside the SOP and registry buckets it reads, so it exists whether
or not the knowledge stack deploys SOP search. Cedar permits it to the read scope
(policies.py); the interceptor check keeps a run inside its grant's packs, because a
process type names a pack.
"""

from __future__ import annotations

import json
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda

from business_operations import names
from business_operations.config import BusinessOperationsConfig
from business_operations.data import Data
from infra_utils.use_case_context import UseCaseContext
from sap_kit.functions import gateway_lambda_target, uc_function

UC = Path(__file__).resolve().parents[1]
TARGET = names.SOPS_TARGET
TOOLS = [
    {
        "Name": "load_sop",
        "Description": (
            "Load the complete focused SOP for a known exception type, verbatim, with "
            "tolerances and contacts resolved. Use this instead of search_sap_sops once "
            "the exception type is classified: search returns fragments, this returns "
            "the whole procedure. The first line names the document and its version."
        ),
        "InputSchema": {
            "Type": "object",
            "Properties": {
                "process_type": {
                    "Type": "string",
                    "Description": (
                        "The classified exception type the SOP names, e.g. "
                        "duplicate_invoice, missing_goods_receipt, short_pay"
                    ),
                }
            },
            "Required": ["process_type"],
        },
    }
]


def create(
    stack: cdk.Stack,
    ctx: UseCaseContext,
    config: BusinessOperationsConfig,
    data: Data,
    *,
    gateway_id: str,
    gateway_role: iam.IRole,
    layer: _lambda.ILayerVersion,
) -> dict[str, agentcore.CfnGatewayTarget]:
    """The target, by name, for policies.create."""
    fn = uc_function(
        stack,
        "SopTools",
        ctx,
        # registry.py and config_overrides.py come from the layer, as the agent's.
        UC / "sop_tools",
        handler="handler.handler",
        layers=[layer],
        timeout=30,
        memory_size=512,
        environment={
            "SOPS_BUCKET": data.sops.bucket_name,
            "REGISTRY_BUCKET": data.registry.bucket_name,
            "CONFIG_TABLE": data.config.table_name,
            "CONTACTS_JSON": json.dumps(config.contacts),
            # As the runtime sets it, so both see the same process types.
            "SAP_MCP_ENABLED": "true" if config.sap.systems else "false",
        },
    )
    data.grant(fn, data.sops, data.registry, data.config)
    target = gateway_lambda_target(
        stack,
        "SopToolsTarget",
        name=TARGET,
        fn=fn,
        tools=TOOLS,
        gateway_id=gateway_id,
        gateway_role=gateway_role,
    )
    return {TARGET: target}
