# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_ssm as ssm
from sap_layer import access


def build(app, ctx, config):
    stack = cdk.Stack(app, f"{ctx.prefix}-uc-vendor-lookup", env=ctx.cdk_env)
    sap = access.SapAccess(ctx, "vendor-lookup")
    p = lambda n: ssm.StringParameter.value_for_string_parameter(
        stack, f"{ctx.ssm_prefix}/{n}"
    )
    client_id = p("gateway/use-case-clients/vendor-lookup/agent/client-id")
    sap.claim(stack, callers=[client_id], read=["s4"], governed_by=None)  # reads only
    if ctx.platform.security.cedar.enabled:
        sap.permits(
            stack, read=["s4"], scope_prefix="uc-vendor-lookup", read_scope="read"
        )
    _lambda.Function(
        stack,
        "Lookup",
        runtime=_lambda.Runtime.PYTHON_3_13,
        handler="index.handler",
        code=_lambda.Code.from_asset(str(Path(__file__).with_name("fn"))),
        environment={
            "SSM_PREFIX": ctx.ssm_prefix,
            "TOOL": sap.tool("s4", "odata_read"),
        },
    )
    # + grant the function its client secret and gateway/url; it calls tools/call with its token
    if config.get("ingest"):  # tests only: SAP's events for this use case, to a queue
        key = kms.Key(stack, "IngestKey") if config.get("cmk") else None
        crypt = sqs.QueueEncryption.KMS if key else sqs.QueueEncryption.SQS_MANAGED
        dlq = sqs.Queue(stack, "IngestDlq", encryption=crypt, encryption_master_key=key)
        queue = sqs.Queue(
            stack, "IngestQueue", encryption=crypt, encryption_master_key=key
        )
        sap.subscribe(stack, queue=queue, dlq=dlq, systems=config["ingest"])
