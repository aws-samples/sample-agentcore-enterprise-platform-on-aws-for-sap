# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""business-operations: SAP exception handling on the shared platform.

The use case's tables and buckets (business_operations/data.py), the
case-management tool behind the platform Gateway, scope-keyed Cedar statements
on the platform's policy engine, and the base group that gates every use-case
user.

Attaches to the platform by id, never by import (docs/PLATFORM_INTERFACE.md):
a target on the shared Gateway (`gateway/id`), an invoke grant on the shared
Gateway role for this use case's own Lambda only (`gateway/role-arn`), a group
on the shared user pool (`auth/user-pool-id`) and, when Cedar is on, statements
on the platform's engine (`gateway/policy-engine-id`) pinned to that Gateway.
"""

import functools
import json
import sys
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_iam as iam
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from infra_utils.use_case_context import UseCaseContext

HERE = Path(__file__).resolve().parent
# app.py loads this file by path, so its own package is not importable yet.
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from business_operations import (
    admin_user,
    audit_alerts,
    cedar,
    containment,
    data_contracts,
    gateway_run_checks,
    identity,
    names,
    notifications,
    people,
    policies,
)
from business_operations import alarms as uc_alarms
from business_operations import api as uc_api
from business_operations import config as uc_config
from business_operations import console as uc_console
from business_operations import data as uc_data
from business_operations import invoker as uc_invoker
from business_operations import observability as uc_observability
from business_operations import packs as uc_packs
from business_operations import persistence as uc_persistence
from business_operations import pipeline as uc_pipeline
from business_operations import runtime as uc_runtime
from business_operations import sap as uc_sap
from business_operations import sop_tools as uc_sop_tools
from business_operations import test_data as uc_test_data
from business_operations.knowledge import KnowledgeStack

from sap_kit.functions import gateway_lambda_target, uc_function

NAME = names.NAME
# Target names carry the use case's prefix: the interceptor check and every
# Cedar action match on it, so another tenant's same-named tool is untouched.
TARGET = names.CASE_TARGET
SCOPE = f"uc-{NAME}"
# Every user of the use case is in this group; per-pack groups restrict on top of it.
USERS_GROUP = uc_api.USERS_GROUP

# The case tools' tool_spec.json. update_case_state's description is the
# contract the handler's WRITABLE set enforces; a test keeps the two agreeing.
_CASE_ID = {
    "Type": "string",
    "Description": (
        "Case identity, exactly as given in the prompt: an opaque string, e.g. "
        "ap-bchzfkch6kxm6uwqvqe5hhlkvx. Do not construct or take it apart."
    ),
}
# What update_case_state accepts, in the schema's order. The Gateway's tool schema
# has no `enum`, so the list is spelled into the description; the handler refuses
# anything else, and a test keeps the two equal.
_CONTRACTS = data_contracts.constants()
AGENT_STATUSES = [
    s
    for s in _CONTRACTS["CASE_STATUSES"]
    if s not in _CONTRACTS["PLATFORM_CASE_STATUSES"]
]
TOOLS = [
    {
        "Name": "get_case_state",
        "Description": (
            "Get a case by its case_id. The case_id is given to you in the prompt. "
            "Treat it as opaque: pass it back exactly as given. The case's `key` "
            "field in the returned record is what identifies it in SAP; read the "
            "field names and values you need from there rather than from the id."
        ),
        "InputSchema": {
            "Type": "object",
            "Properties": {"case_id": _CASE_ID},
            "Required": ["case_id"],
        },
    },
    {
        "Name": "update_case_state",
        "Description": (
            "Record platform state on a case: status, title, description, priority, "
            "and `disposition`. Nothing else is writable here: the case's domain "
            "attributes come from the SAP record the poller read."
        ),
        "InputSchema": {
            "Type": "object",
            "Properties": {
                "case_id": _CASE_ID,
                "updates": {
                    "Type": "string",
                    "Description": (
                        'JSON string of fields to update (e.g. {"status": '
                        '"manual_review_required", "title": "Price variance above '
                        f'tolerance"}}). `status` is one of {", ".join(AGENT_STATUSES)}. '
                        "Leave `status` out of an update that records progress: "
                        f"{' and '.join(_CONTRACTS['PLATFORM_CASE_STATUSES'])} are set "
                        "by the platform and refused here. "
                        "Include `disposition` on the update that closes the case: a "
                        'short snake_case label for what was decided, e.g. "released" '
                        'or "rejected_duplicate". A pack that declares its labels '
                        "accepts only those, and the prompt names them. A case "
                        "settled as complete or sap_updated takes no further update. "
                        "`updated_at` "
                        "and `action_log` are recorded "
                        "automatically. When you set status awaiting_human_input "
                        "for a decision with more than one outcome, add "
                        "`decision_options`: 2 to 6 objects "
                        '{"id": snake_case, "label": up to 80 characters, "effect": '
                        'one line of up to 240, "write": the exact write as in '
                        "proposed_write, or null for an outcome that makes none}. A "
                        "person picks one, and only that option's write is approved. "
                        "Any status written without them removes the last set. Any "
                        "field other than status, title, description, priority, "
                        "disposition and decision_options is not applied and is "
                        "named back to you in the reply."
                    ),
                },
                "action": {
                    "Type": "string",
                    "Description": (
                        "Action name for history tracking (e.g. 'sap_updated', "
                        "'invoice_released', 'manual_review_required')"
                    ),
                },
            },
            "Required": ["case_id", "updates", "action"],
        },
    },
]


class BusinessOperationsStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        ctx: UseCaseContext,
        config: uc_config.BusinessOperationsConfig | None = None,
        **kw,
    ):
        super().__init__(scope, id, **kw)
        config = config or uc_config.load({})
        ssm_prefix = ctx.ssm_prefix
        project, environment = ctx.project, ctx.environment

        gateway_id = ctx.ssm_value(self, "gateway/id")
        gateway_arn = f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:gateway/{gateway_id}"

        data = uc_data.create(self, ctx)

        fn = uc_function(
            self,
            "CaseManagement",
            ctx,
            HERE / "case_management",
            handler="handler.handler",
            timeout=30,
            environment={"CASES_TABLE": data.cases.table_name},
        )
        data.grant_cases(fn, "GetItem", "UpdateItem")
        persist = uc_persistence.create(self, ctx)
        uc_persistence.grant(persist, fn)

        gateway_role = iam.Role.from_role_arn(
            self, "GatewayRole", ctx.ssm_value(self, "gateway/role-arn")
        )
        target = gateway_lambda_target(
            self,
            "CaseManagementTarget",
            name=TARGET,
            fn=fn,
            tools=TOOLS,
            gateway_id=gateway_id,
            gateway_role=gateway_role,
        )

        gateway_run_checks.attach(
            self,
            ctx,
            data,
            sap_callers=uc_sap.callers(ctx, identity.person_app(ctx, config), self),
            agent_callers=uc_sap.agent_callers(
                ctx, identity.person_app(ctx, config), self
            ),
        )

        # On the shared pool, so prefixed. No users are created here: an
        # operator adds an existing user to it after the deploy. Direct mode has
        # no pool: the group is an app role the operator creates (identity.py).
        direct = identity.direct(ctx)
        if not direct:
            cognito.CfnUserPoolGroup(
                self,
                "UsersGroup",
                user_pool_id=ctx.ssm_value(self, "auth/user-pool-id"),
                group_name=USERS_GROUP,
                description=f"{NAME}: every user of the Business Operations console",
            )

        console_url = uc_console.console_url(self, ctx)
        api = uc_api.create(self, ctx, config, data, cors_origins=[console_url])
        # case_key and the registry reader, for the case tool's id and disposition checks.
        fn.add_layers(api.layer)
        fn.add_environment("REGISTRY_BUCKET", data.registry.bucket_name)
        data.grant(fn, data.registry)
        uc_packs.create(
            self,
            ctx,
            config,
            data,
            api,
            case_tools=TOOLS,
            pool_id=None if direct else ctx.ssm_value(self, "auth/user-pool-id"),
        )
        # Before the first user, so they join the remote authors group too.
        people.create(
            self,
            ctx,
            config,
            data,
            api,
            pool_id=None if direct else ctx.ssm_value(self, "auth/user-pool-id"),
        )
        if config.admin_email and ctx.production:
            # One password user outside the IdP and its MFA, in every approver group:
            # a production stack gives access through the IdP's groups or add-user.
            raise ValueError(
                "use_cases.business-operations.admin_email is refused under "
                "deployment.mode: production: it makes one Cognito password user in "
                "every group, approvers included; map IdP groups or use add-user"
            )
        if config.admin_email and not direct:
            # After every group above exists, so the user joins each of them.
            admin_user.create(
                self, ctx.ssm_value(self, "auth/user-pool-id"), config.admin_email
            )
        audit_alerts.create(self, ctx, data, [api.functions["pack_composer"]])
        targets = {
            TARGET: target,
            **notifications.create(
                self,
                ctx,
                config,
                data,
                gateway_id=gateway_id,
                gateway_role=gateway_role,
                layer=api.layer,
                persist=persist,
            ),
            **uc_sop_tools.create(
                self,
                ctx,
                config,
                data,
                gateway_id=gateway_id,
                gateway_role=gateway_role,
                layer=api.layer,
            ),
        }

        if ctx.platform.security.cedar.enabled:
            policies.create(
                self,
                targets=targets,
                engine_id=ctx.ssm_value(self, "gateway/policy-engine-id"),
                gateway_arn=gateway_arn,
                project=project,
                environment=environment,
                prefix=ctx.prefix,
                principals=identity.cedar_principals(ctx, config),
            )

        reached = [
            n for n, s in config.sap.systems.items() if s.system_role == "production"
        ]
        if reached and not ctx.production:
            # Legitimate (reading production from development), but never by accident.
            cdk.Annotations.of(self).add_warning_v2(
                f"{NAME}:production-sap",
                "This deployment is not deployment.mode: production, but it reaches "
                f"SAP system(s) {reached}, whose system role is production.",
            )
        invoker = uc_invoker.create(self, ctx, data, api)
        functions, queue, dlq = uc_pipeline.create(
            self, ctx, config, data, api, invoker
        )
        watched = {"agent_invoker": invoker, **functions}
        seeder = uc_test_data.create(
            self,
            ctx,
            config,
            data,
            api,
            # The functions that open cases mark the ones the seeder's chains made.
            case_makers=[
                functions[k] for k in ("sap_poller", "sap_ingest") if k in functions
            ],
        )
        if seeder:
            watched["test_data"] = seeder
        # Built before the alarms, so the function-errors alarm watches it too.
        watched["containment"] = containment.function(self, ctx)
        alarms = uc_alarms.create(
            self,
            ctx,
            api.env["METRICS_NAMESPACE"],
            watched,
            config.alarms.cost_p90_usd,
            dlq,
        )
        containment.rule(self, ctx, watched["containment"], alarms)
        uc_observability.create(
            self, ctx, api.env["METRICS_NAMESPACE"], watched, queue, dlq
        )

        for key, value in (
            *data.outputs().items(),
            ("target-name", TARGET),
            ("tools", json.dumps([t["Name"] for t in TOOLS])),
            ("users-group", USERS_GROUP),
        ):
            ssm.StringParameter(
                self,
                f"Out-{key}",
                parameter_name=f"{ssm_prefix}/use-cases/{NAME}/{key}",
                string_value=value,
            )


def build(app, ctx: UseCaseContext, config: dict) -> None:
    from sap_layer import access

    sap = access.SapAccess(ctx, NAME)
    cfg = uc_config.load(
        config, sap=sap.config.model_copy(update={"systems": sap.systems})
    )
    identity.require(ctx, cfg)
    people.require(ctx, cfg)
    principals = identity.cedar_principals(ctx, cfg)
    # RuntimeStack builds the same name; checked here for the readable error.
    names.runtime_name(ctx.project, ctx.environment)
    gateway_run_checks.require_extension(ctx.platform)
    # The console's URL is the main stack's callback and CORS origin, read from SSM.
    uc_console.ConsoleStack(
        app, f"{ctx.prefix}-uc-{NAME}-console", ctx=ctx, env=ctx.cdk_env
    )
    BusinessOperationsStack(
        app, f"{ctx.prefix}-uc-{NAME}", ctx=ctx, config=cfg, env=ctx.cdk_env
    )
    KnowledgeStack(
        app,
        f"{ctx.prefix}-uc-{NAME}-knowledge",
        ctx=ctx,
        cfg=cfg.knowledge,
        contacts=cfg.contacts,
        sap_enabled=bool(cfg.sap.systems),
        cedar_statement=functools.partial(cedar.scoped_permit, principals=principals),
        env=ctx.cdk_env,
    )
    uc_sap.build(
        app,
        ctx,
        sap,
        console_app=identity.person_app(ctx, cfg),
        principals=principals,
    )
    uc_runtime.create(app, ctx, cfg)
