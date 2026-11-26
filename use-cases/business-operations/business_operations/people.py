# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""This use case's tools on the platform's people Gateway (`people_tools`).

The platform's `gateway.people` is a second Gateway for people signed in as themselves
from a remote MCP client; its interceptor hands every target the verified `caller` and
never the token (docs/modules/gateway.md#people-gateway). This module attaches one
target to it, `business-operations-people`, a Lambda on the console's own handler code
(`api/people/index.py`), with the tools of the tiers `people_tools` names:

* `read`: packs, SOPs, the lint, the base prompt, rules and versions.
* `author`: SOP text, pack content and rule values, and creating a pack. Development
  only: synth refuses it under `deployment.mode: production` or another promotion role,
  and the handlers refuse every write outside development anyway.

Every tool needs `business-operations-remote-authors` (created here, mappable with
`identity.group_map`) on top of the console's groups. The function holds the read
grants the console's pack, SOP and config functions hold for these routes, and the
composer invoke the writes need; nothing on the cases, the trigger mode, the stop or runs.
Only the people Gateway's role may invoke it.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_logs as logs
from aws_cdk import aws_ssm as ssm

from business_operations import ASSET_EXCLUDE, names
from business_operations import alarms as uc_alarms
from business_operations import api as uc_api
from business_operations.config import BusinessOperationsConfig, promotion_role
from business_operations.data import Data
from infra_utils.use_case_context import UseCaseContext
from sap_kit.functions import gateway_lambda_target, uc_function

API_SRC = Path(__file__).resolve().parents[1] / "api"
#: The route groups the people target runs; every other directory of `api/` stays out
#: of its code.
ROUTE_GROUPS = ("people", "packs", "sops", "config")
TARGET = f"{names.NAME}-people"
AUTHOR = "author"
#: The metric the target's refusals (gate 3) are counted under, in the use case's
#: namespace; the interceptor's (gate 2) are the platform's.
REFUSALS_METRIC = "PeopleRefusals"


def handler_module():
    """`api/people/index.py`, whose `TOOLS` is the target's fixed set (stdlib only)."""
    spec = importlib.util.spec_from_file_location(
        "bo_people_handler", API_SRC / "people" / "index.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def tools(cfg: BusinessOperationsConfig) -> list[dict]:
    """The target's tool schemas: those of the tiers `people_tools` names."""
    mod = handler_module()
    return [t.schema() for t in mod.TOOLS if t.tier in cfg.people_tools]


def require(ctx: UseCaseContext, cfg: BusinessOperationsConfig) -> None:
    """Refuse at synth what `people_tools` cannot deploy, or must not."""
    if not cfg.people_tools:
        return
    if not ctx.platform.gateway.people.enabled:
        raise ValueError(
            f"use_cases.{names.NAME}.people_tools needs the platform's people Gateway: "
            "set gateway.people.enabled true with a remote client"
        )
    if AUTHOR in cfg.people_tools:
        role = promotion_role(ctx, cfg.promotion)
        if ctx.production or role != "development":
            raise ValueError(
                f"use_cases.{names.NAME}.people_tools: author is development only "
                f"(this stack: deployment.mode {'production' if ctx.production else 'workshop'}, "
                f"promotion.role {role}); a pack reaches quality and production by import"
            )


def create(
    stack: cdk.Stack,
    ctx: UseCaseContext,
    cfg: BusinessOperationsConfig,
    data: Data,
    api: uc_api.Api,
    *,
    pool_id: str | None,
) -> _lambda.Function | None:
    if not cfg.people_tools:
        return None
    if pool_id:
        cognito.CfnUserPoolGroup(
            stack,
            "RemoteAuthorsGroup",
            user_pool_id=pool_id,
            group_name=names.REMOTE_AUTHORS_GROUP,
            description=f"{names.NAME}: may author from a remote MCP client",
        )
    from business_operations import packs as uc_packs

    own = f"{ctx.ssm_prefix}/use-cases/{names.NAME}"
    issuer = ctx.platform.gateway.people.issuer
    # The clients `admit` takes: the issuer's own client ids, or the remote clients'.
    client_ids = (
        list(issuer.clients)
        if issuer
        else [
            ctx.ssm_value(stack, f"people-gateway/remote-clients/{c.name}/client-id")
            for c in ctx.platform.gateway.people.remote_clients
        ]
    )
    composer = api.functions["pack_composer"]
    code = _lambda.Code.from_asset(
        str(API_SRC),
        exclude=[
            *ASSET_EXCLUDE,
            *(
                p.name
                for p in API_SRC.iterdir()
                if p.is_dir() and p.name not in ROUTE_GROUPS
            ),
        ],
    )
    fn = uc_function(
        stack,
        "PeopleTarget",
        ctx,
        code,
        handler="people/index.handler",
        layers=[api.layer],
        timeout=30,
        memory_size=512,
        environment={
            **api.env,
            # `admit` takes the remote clients as this function's clients.
            "API_ALLOWED_CLIENTS": cdk.Fn.join(",", client_ids),
            "PEOPLE_TARGET": TARGET,
            "PEOPLE_TIERS": ",".join(cfg.people_tools),
            "REMOTE_AUTHORS_GROUP": names.REMOTE_AUTHORS_GROUP,
            "PROMOTION_ROLE": promotion_role(ctx, cfg.promotion),
            "PACK_COMPOSER_FUNCTION": composer.function_name,
            "SOPS_BUCKET": data.sops.bucket_name,
            # What `GET /registry` and `GET /packs/{pack_id}/rules` read, as the
            # console's config function holds them (business_operations/api.py).
            "CONFIG_TABLE": data.config.table_name,
            "CONTACTS_JSON": json.dumps(cfg.contacts),
            "SAP_SYSTEM_ROLES": json.dumps(
                {name: s.system_role for name, s in cfg.sap.systems.items()}
            ),
            "DEPLOYMENT_MODE": "production" if ctx.production else "workshop",
            **uc_packs._kb_env(own),
        },
    )
    # One person's AI cannot take the composer's single slot from everyone.
    fn.node.default_child.reserved_concurrent_executions = 2
    data.grant(fn, data.registry)
    data.grant(fn, data.sops)
    data.grant(fn, data.config)  # read only: no override is written here
    composer.grant_invoke(fn)
    # The SOP listing's search state; never an ingestion start.
    fn.add_to_role_policy(
        iam.PolicyStatement(
            actions=["ssm:GetParameter"],
            resources=[
                stack.format_arn(
                    service="ssm",
                    resource="parameter",
                    resource_name=f"{own.lstrip('/')}/sops-{p}",
                )
                for p in ("kb-id", "data-source-id")
            ],
        )
    )
    fn.add_to_role_policy(
        iam.PolicyStatement(
            actions=["bedrock:ListIngestionJobs"],
            resources=[
                stack.format_arn(
                    service="bedrock", resource="knowledge-base", resource_name="*"
                )
            ],
        )
    )

    role_arn = ctx.ssm_value(stack, "people-gateway/role-arn")
    role = iam.Role.from_role_arn(stack, "PeopleGatewayRole", role_arn)
    # The people Gateway's role is the function's only invoker by resource policy.
    fn.add_permission("PeopleGatewayInvokes", principal=iam.ArnPrincipal(role_arn))
    gateway_lambda_target(
        stack,
        "PeopleTargetOnGateway",
        name=TARGET,
        fn=fn,
        tools=tools(cfg),
        gateway_id=ctx.ssm_value(stack, "people-gateway/id"),
        gateway_role=role,
    )

    # `verify.py` checks the function's invokers and its refusals.
    ssm.StringParameter(
        stack,
        "Out-people-target-function",
        parameter_name=f"{own}/people-target-function",
        string_value=fn.function_name,
    )

    namespace = api.env["METRICS_NAMESPACE"]
    logs.MetricFilter(
        stack,
        "PeopleTargetRefusals",
        log_group=fn.log_group,
        metric_namespace=namespace,
        metric_name=REFUSALS_METRIC,
        filter_pattern=logs.FilterPattern.all_terms("people_refused"),
        metric_value="1",
    )
    topic = uc_alarms.topic(stack, ctx)
    for id_, metric, where in (
        (
            "PeopleGatewayRefusalsAlarm",
            cloudwatch.Metric(
                namespace=f"{ctx.prefix}/people-gateway",
                metric_name="Refusals",
                statistic="Sum",
                period=cdk.Duration.minutes(5),
            ),
            "the people Gateway's interceptor (a token it could not verify)",
        ),
        (
            "PeopleTargetRefusalsAlarm",
            cloudwatch.Metric(
                namespace=namespace,
                metric_name=REFUSALS_METRIC,
                statistic="Sum",
                period=cdk.Duration.minutes(5),
            ),
            f"{TARGET} (a tool, caller or argument outside its set)",
        ),
    ):
        alarm = cloudwatch.Alarm(
            stack,
            id_,
            alarm_description=f"{ctx.prefix} {names.NAME}: calls refused by {where}. "
            "A client probing what it is not offered, or a broken connector.",
            metric=metric,
            threshold=0,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            evaluation_periods=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        alarm.add_alarm_action(cw_actions.SnsAction(topic))
        alarm.add_ok_action(cw_actions.SnsAction(topic))
    return fn
