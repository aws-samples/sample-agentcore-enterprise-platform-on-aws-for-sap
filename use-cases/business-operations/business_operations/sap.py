# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""This use case's SAP access, through the SAP layer (layers/sap/).

The layer builds the AWS for SAP MCP Server runtimes and their Gateway targets. This
module's stack, `<prefix>-uc-business-operations-sap`, holds only what is this use
case's own on them:

- its **claim** (`layers/sap/claims/business-operations`): the apps it calls SAP
  with, `writer` and `reader`, and under obo the console app a person's token comes
  from; every system and write its `layers.sap.access` row allows; governed by its
  run checks (`business_operations_check`), which keep the run grant, the amount gate
  and the approved write for these apps and pass every other caller untouched;
- its **Cedar permits** on the SAP targets, one read and one write per system, keyed
  on `uc-business-operations/<scope>` (or, in direct mode, on its apps' `azp`). Their
  names are the ones this stack has always given them, so they are not replaced;
- `use-cases/business-operations/sap-targets`, the system-to-target map verify reads.
"""

from __future__ import annotations

import json

import aws_cdk as cdk
from aws_cdk import aws_ssm as ssm
from sap_layer import names as sap_names
from sap_layer.access import SapAccess

from business_operations import cedar, names
from business_operations.gateway_run_checks import CHECK_PATH
from business_operations.runtime import AGENT_CLIENT
from infra_utils.use_case_context import UseCaseContext

#: The apps this use case calls SAP with (runtime.AGENT_CLIENT is the writer).
CLIENTS = ("writer", "reader")


def callers(
    ctx: UseCaseContext, console_app: str | None, scope: cdk.Stack
) -> list[str]:
    """The app ids the claim and the run checks name: tokens, read from SSM at deploy."""
    out = [
        ctx.ssm_value(scope, f"gateway/use-case-clients/{names.NAME}/{c}/client-id")
        for c in CLIENTS
    ]
    return out + ([console_app] if console_app else [])


def agent_callers(
    ctx: UseCaseContext, console_app: str | None, scope: cdk.Stack
) -> list[str]:
    """The subset of `callers` that runs the agent: its `writer` client, and the console
    app when a person's token reaches SAP. The `reader` is an operator's and is not."""
    out = [
        ctx.ssm_value(
            scope, f"gateway/use-case-clients/{names.NAME}/{AGENT_CLIENT}/client-id"
        )
    ]
    return out + ([console_app] if console_app else [])


def build(
    app,
    ctx: UseCaseContext,
    sap: SapAccess,
    *,
    console_app: str | None,
    principals: dict[str | None, list[str]] | None,
) -> cdk.Stack:
    """`console_app` is the console's app id when a person's token reaches SAP (obo)."""
    stack = cdk.Stack(app, f"{ctx.prefix}-uc-{names.NAME}-sap", env=ctx.cdk_env)
    if sap.reads:
        sap.claim(
            stack,
            callers=callers(ctx, console_app, stack),
            read=sap.reads,
            write=sap.writes,
            governed_by=CHECK_PATH.rsplit("/", 1)[1],
        )
        if ctx.platform.security.cedar.enabled:
            sap.permits(
                stack,
                read=sap.reads,
                write=sap.writes,
                scope_prefix=cedar.SCOPE_PREFIX,
                principals=principals,
            )
    # Always present, so the stack is never empty; verify.py reads it.
    ssm.StringParameter(
        stack,
        "Out-sap-targets",
        parameter_name=f"{ctx.ssm_prefix}/use-cases/{names.NAME}/sap-targets",
        string_value=json.dumps({s: sap_names.sap_target(s) for s in sap.reads}),
    )
    return stack
