# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""{{name}} — {{summary}}

Scaffolded from sap_kit/templates/sap-use-case/. It reads SAP through the SAP
layer (layers/sap/) and writes nothing. Its one stack, `<prefix>-uc-{{name}}`,
holds what is this use case's own on the layer:

- its claim, `layers/sap/claims/{{name}}`: its `reader` client reads every system
  its `layers.sap.access.{{name}}` row allows, and writes nothing;
- with Cedar on, a read permit per system on that system's target, keyed on
  `uc-{{name}}/read`.

Add what the use case does beside them. A write needs more than a claim; see
sap_kit/skills/dev-add-sap-use-case/SKILL.md before adding one.
"""

import aws_cdk as cdk
from sap_layer import access

from infra_utils.use_case_context import UseCaseContext

NAME = "{{name}}"


def build(app, ctx: UseCaseContext, config: dict) -> None:
    """Entry point the platform calls for each enabled use case."""
    stack = cdk.Stack(app, f"{ctx.prefix}-uc-{NAME}", env=ctx.cdk_env)
    sap = access.SapAccess(ctx, NAME)
    reader = ctx.ssm_value(stack, f"gateway/use-case-clients/{NAME}/reader/client-id")
    sap.claim(stack, callers=[reader], read=sap.reads)
    if ctx.platform.security.cedar.enabled:
        # Brokered (Cognito) identity: the permit keys on the token's scope. Under
        # identity.mode direct, pass principals= as business-operations does.
        sap.permits(stack, read=sap.reads, scope_prefix=f"uc-{NAME}")
