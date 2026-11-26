# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The REST surface: the console API, with the sample ticketing on it, and SAP push ingest.

Every resource is created with the stack as its scope, so a logical id never moves when this module does.

Two RestApis, each with its own stage, access log and authorizer, because each is
something an adopter changes on its own: the console API (cases, runs, trigger mode,
config, and `/tickets` while `api.sample_ticketing` is on) and `POST /erp-events`
(`api.erp_events_endpoint`, which a Direct Connect customer may want private). The sample
ticketing is a stand-in for a real ITSM; turning the flag off removes its routes and
function whole, which is all a second RestApi used to buy.

**Who gets in.** API Gateway's Cognito authorizer proves only that an id token came
from the shared pool, and any client on it can mint one. Each handler's first call,
`entitlement.admit`, refuses a token from any client but the console client, and any
caller outside the `business-operations-users` group; `PUT /deployment/trigger-mode` and a pack's also need
`business-operations-admins`. Pack-level groups restrict on top (declare to allow).

**The account's API Gateway CloudWatch role** is one setting per account and Region
(`AWS::ApiGateway::Account`). No RestApi here creates it unless `api.set_account_cloudwatch_role`
says so, and then only the console API does.

**Wiring.** The agent queue, the invoker and `POST /erp-events`'s handler are wired
through `Api.functions` by invoker.py and pipeline.py (an environment value and a
grant); a route whose dependency is absent answers 503 rather than accepting work it
cannot deliver.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_ssm as ssm
from aws_cdk import aws_wafv2 as wafv2

from business_operations import ASSET_EXCLUDE, data_contracts, identity, names
from business_operations.config import BusinessOperationsConfig
from business_operations.data import Data
from infra_utils.idp_group_guard import MAX_REFRESH_MINUTES
from infra_utils.log_retention import service_log_retention
from infra_utils.use_case_context import UseCaseContext
from sap_kit.functions import code_with_wheels, log_group, uc_function

API_SRC = Path(__file__).resolve().parents[1] / "api"
AUTHORIZER_SRC = API_SRC.parent / "api_authorizer"
NAME = names.NAME
USERS_GROUP = f"{NAME}-users"
ADMINS_GROUP = f"{NAME}-admins"
AUTHORS_GROUP = f"{NAME}-authors"
LIMIT_OWNERS_GROUP = names.LIMIT_OWNERS_GROUP
CASE_WORKERS_GROUP = names.CASE_WORKERS_GROUP
#: The console's access and id tokens carry the user's groups, so this is how long a
#: group change takes to reach a signed-in user (the browser renews before expiry).
#: 5 minutes is Cognito's minimum.
CONSOLE_TOKEN_MINUTES = 5
# The delegating client's resource server: one scope, for calls to this use case's
# own runtime. Prefixed, like every name on the shared pool.
RUNTIME_RESOURCE_SERVER = f"{NAME}-runtime"
RUNTIME_SCOPE = f"{RUNTIME_RESOURCE_SERVER}/invoke"
# The ingest client's: `POST /erp-events` for a sender with OAuth client credentials
# (an Event Mesh webhook, a BTP iFlow). Its own server, since `uc-<name>` is the
# platform's and every token on it reaches the Gateway.
INGEST_RESOURCE_SERVER = f"{NAME}-ingest"
INGEST_SCOPE = f"{INGEST_RESOURCE_SERVER}/send"
# The authoring MCP server's loopback redirect (tools/mcp-authoring/mcp_authoring.py
# REDIRECT_URI, which a test pins to this): a PKCE public client, so only the process
# that started the flow can redeem the code.
AUTHORING_REDIRECT_URI = "http://localhost:12800/callback"


@dataclass
class Api:
    url: str
    console_client_id: str
    delegating_secret: secretsmanager.ISecret
    # DELEGATING_CLIENT_ID, _SECRET_ARN, _SCOPE and where to get its token, for the
    # run route and the invoker (layer/python/delegating_token.py).
    delegating_env: dict[str, str]
    # case_key and the other shared modules; the Gateway tools use it too.
    layer: _lambda.ILayerVersion
    functions: dict[str, _lambda.Function] = field(default_factory=dict)
    # For routes another module mounts on the console API (packs.py).
    rest_api: apigw.RestApi | None = None
    authorizer: apigw.IAuthorizer | None = None
    env: dict[str, str] = field(default_factory=dict)
    pool: cognito.IUserPool | None = None
    authorization_type: apigw.AuthorizationType = apigw.AuthorizationType.COGNITO


def _group(stack: cdk.Stack, id: str, pool_id: str, name: str, what: str) -> None:
    cognito.CfnUserPoolGroup(
        stack, id, user_pool_id=pool_id, group_name=name, description=f"{NAME}: {what}"
    )


def _clients(
    stack: cdk.Stack, ctx: UseCaseContext, pool: cognito.IUserPool, origins: list[str]
):
    """The console's user client and the delegating machine client, on the shared pool."""
    identity = ctx.platform.identity
    brokered_idp = identity.idp != "cognito"
    # With a brokered IdP the platform lists only that IdP on its own clients, so a
    # password form never bypasses corporate MFA. This client does the same.
    provider = (
        cognito.UserPoolClientIdentityProvider.custom(
            ctx.ssm_value(stack, "auth/idp-provider-name")
        )
        if brokered_idp
        else cognito.UserPoolClientIdentityProvider.COGNITO
    )
    console = cognito.UserPoolClient(
        stack,
        "ConsoleClient",
        user_pool=pool,
        user_pool_client_name=f"{ctx.prefix}-{NAME}-console",
        generate_secret=False,
        # SRP only for Cognito's own users; never a flow that issues a token able to
        # write custom:idp_groups (app.py's group guard).
        auth_flows=cognito.AuthFlow(user_srp=not brokered_idp),
        access_token_validity=cdk.Duration.minutes(CONSOLE_TOKEN_MINUTES),
        id_token_validity=cdk.Duration.minutes(CONSOLE_TOKEN_MINUTES),
        supported_identity_providers=[provider],
        o_auth=cognito.OAuthSettings(
            flows=cognito.OAuthFlows(authorization_code_grant=True),
            scopes=[
                cognito.OAuthScope.OPENID,
                cognito.OAuthScope.EMAIL,
                cognito.OAuthScope.PROFILE,
            ],
            # The console signs in and out at its own origin (console.py).
            callback_urls=[*origins, AUTHORING_REDIRECT_URI],
            logout_urls=[*origins, AUTHORING_REDIRECT_URI],
        ),
        prevent_user_existence_errors=True,
        # A federated sign-in fails unless the client may write every mapped
        # attribute; mirrors the platform's own user clients.
        write_attributes=(
            cognito.ClientAttributes()
            .with_standard_attributes(email=True, fullname=True)
            .with_custom_attributes("idp_groups")
            if identity.group_claim
            else None
        ),
        # A mapped group outlives its IdP removal until the refresh token expires
        # (infra_utils/idp_group_guard.py, which refuses longer).
        refresh_token_validity=(
            cdk.Duration.minutes(MAX_REFRESH_MINUTES) if identity.group_claim else None
        ),
    )

    # The shared domain serves managed login, which shows no page to a client
    # without a branding style. This one is Cognito's default look.
    cognito.CfnManagedLoginBranding(
        stack,
        "ConsoleBranding",
        user_pool_id=pool.user_pool_id,
        client_id=console.user_pool_client_id,
        use_cognito_provided_values=True,
    )

    server = cognito.UserPoolResourceServer(
        stack,
        "RuntimeResourceServer",
        user_pool=pool,
        identifier=RUNTIME_RESOURCE_SERVER,
        scopes=[
            cognito.ResourceServerScope(
                scope_name="invoke",
                scope_description="Start or stop a run on this use case's agent runtime",
            )
        ],
    )
    # Held only by the run route and the invoker: the runtime accepts an acting human's
    # `sub` beside this client's token and no other's.
    delegating = cognito.UserPoolClient(
        stack,
        "DelegatingClient",
        user_pool=pool,
        user_pool_client_name=f"{ctx.prefix}-{NAME}-delegating",
        generate_secret=True,
        auth_flows=cognito.AuthFlow(user_password=False, user_srp=False),
        o_auth=cognito.OAuthSettings(
            flows=cognito.OAuthFlows(client_credentials=True),
            scopes=[cognito.OAuthScope.custom(RUNTIME_SCOPE)],
        ),
        access_token_validity=cdk.Duration.minutes(60),
    )
    delegating.node.add_dependency(server)
    secret = secretsmanager.Secret(
        stack,
        "DelegatingClientSecret",
        description=f"{ctx.prefix} {NAME}: delegating client secret",
        secret_string_value=delegating.user_pool_client_secret,
    )
    return console, delegating, secret


def _ingest_client(stack: cdk.Stack, ctx: UseCaseContext, pool: cognito.IUserPool):
    """The machine client a sender mints `INGEST_SCOPE` tokens with: its id and the
    secret holding its client secret."""
    server = cognito.UserPoolResourceServer(
        stack,
        "IngestResourceServer",
        user_pool=pool,
        identifier=INGEST_RESOURCE_SERVER,
        scopes=[
            cognito.ResourceServerScope(
                scope_name="send",
                scope_description="Send SAP exceptions to POST /erp-events",
            )
        ],
    )
    client = cognito.UserPoolClient(
        stack,
        "IngestClient",
        user_pool=pool,
        user_pool_client_name=f"{ctx.prefix}-{NAME}-ingest",
        generate_secret=True,
        auth_flows=cognito.AuthFlow(user_password=False, user_srp=False),
        o_auth=cognito.OAuthSettings(
            flows=cognito.OAuthFlows(client_credentials=True),
            scopes=[cognito.OAuthScope.custom(INGEST_SCOPE)],
        ),
        access_token_validity=cdk.Duration.minutes(60),
    )
    client.node.add_dependency(server)
    secret = secretsmanager.Secret(
        stack,
        "IngestClientSecret",
        description=f"{ctx.prefix} {NAME}: the ingest client's secret",
        secret_string_value=client.user_pool_client_secret,
    )
    return client.user_pool_client_id, secret


def _web_acl(stack: cdk.Stack, prefix: str) -> wafv2.CfnWebACL:
    """A per-IP rate limit plus AWS's common and known-bad-inputs rule groups."""

    def visibility(metric: str):
        return wafv2.CfnWebACL.VisibilityConfigProperty(
            cloud_watch_metrics_enabled=True,
            metric_name=f"{prefix}-{NAME}-{metric}",
            sampled_requests_enabled=True,
        )

    def managed(name: str, priority: int, metric: str):
        return wafv2.CfnWebACL.RuleProperty(
            name=name,
            priority=priority,
            override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
            statement=wafv2.CfnWebACL.StatementProperty(
                managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                    vendor_name="AWS", name=name
                )
            ),
            visibility_config=visibility(metric),
        )

    return wafv2.CfnWebACL(
        stack,
        "ApiWebAcl",
        scope="REGIONAL",
        default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
        visibility_config=visibility("api-waf"),
        rules=[
            wafv2.CfnWebACL.RuleProperty(
                name="RateLimitPerIp",
                priority=0,
                action=wafv2.CfnWebACL.RuleActionProperty(block={}),
                statement=wafv2.CfnWebACL.StatementProperty(
                    rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                        limit=2000, aggregate_key_type="IP"
                    )
                ),
                visibility_config=visibility("waf-rate-limit"),
            ),
            managed("AWSManagedRulesCommonRuleSet", 1, "waf-common"),
            managed("AWSManagedRulesKnownBadInputsRuleSet", 2, "waf-bad-inputs"),
        ],
    )


def _rest_api(
    stack: cdk.Stack,
    id: str,
    ctx: UseCaseContext,
    description: str,
    *,
    cloudwatch_role: bool,
    method_options: dict | None = None,
    cors_origins: list[str] | None = None,
) -> apigw.RestApi:
    api = apigw.RestApi(
        stack,
        id,
        description=description,
        cloud_watch_role=cloudwatch_role,
        # On every resource, not just the root: a browser preflights each path.
        default_cors_preflight_options=apigw.CorsOptions(
            allow_origins=cors_origins,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Content-Type", "Authorization"],
        )
        if cors_origins
        else None,
        deploy_options=apigw.StageOptions(
            stage_name="prod",
            throttling_rate_limit=100,
            throttling_burst_limit=200,
            # A stage cache is keyed on the request and shared across callers, so it
            # would serve one operator's entitled cases to the next.
            caching_enabled=False,
            cache_cluster_enabled=False,
            logging_level=apigw.MethodLoggingLevel.INFO,
            # Data tracing writes whole bodies (case data, SAP records) to CloudWatch.
            # The access log records every call without them.
            data_trace_enabled=False,
            metrics_enabled=True,
            access_log_destination=apigw.LogGroupLogDestination(
                log_group(stack, f"{id}AccessLogs", ctx)
            ),
            access_log_format=apigw.AccessLogFormat.json_with_standard_fields(
                caller=True,
                http_method=True,
                ip=True,
                protocol=True,
                request_time=True,
                resource_path=True,
                response_length=True,
                status=True,
                user=True,
            ),
            tracing_enabled=True,
            method_options=method_options,
        ),
    )
    if cors_origins:
        # The authorizer's 401, an unknown route's 403 and an integration timeout never
        # reach a handler, so without these they carry no CORS header and the browser
        # reads an expired token as a network failure. One console, so one origin.
        cors = {
            "Access-Control-Allow-Origin": f"'{cors_origins[0]}'",
            "Vary": "'Origin'",
        }
        for kind, response_type in (
            ("4xx", apigw.ResponseType.DEFAULT_4_XX),
            ("5xx", apigw.ResponseType.DEFAULT_5_XX),
        ):
            api.add_gateway_response(
                f"{id}Default{kind}", type=response_type, response_headers=cors
            )
    # API Gateway creates the stage's execution log group itself, with no retention.
    service_log_retention(
        stack,
        f"{id}ExecutionLogRetention",
        f"API-Gateway-Execution-Logs_{api.rest_api_id}/prod",
        ctx.log_retention_days,
        ctx.production,
    )
    return api


def _associate(stack: cdk.Stack, id: str, acl: wafv2.CfnWebACL, api: apigw.RestApi):
    wafv2.CfnWebACLAssociation(
        stack,
        id,
        resource_arn=(
            f"arn:{stack.partition}:apigateway:{stack.region}::/restapis/"
            f"{api.rest_api_id}/stages/{api.deployment_stage.stage_name}"
        ),
        web_acl_arn=acl.attr_arn,
    )


def _authorizer(
    stack: cdk.Stack,
    id: str,
    ctx: UseCaseContext,
    pool: cognito.IUserPool | None,
    console_id: str,
) -> apigw.IAuthorizer:
    """The pool's Cognito authorizer, or in direct mode the Entra token authorizer.

    One function backs every direct-mode authorizer; each RestApi needs its own
    authorizer resource.
    """
    if pool is not None:
        return apigw.CognitoUserPoolsAuthorizer(stack, id, cognito_user_pools=[pool])
    fn = stack.node.try_find_child("ApiAuthorizerFunction")
    if fn is None:
        fn = uc_function(
            stack,
            "ApiAuthorizerFunction",
            ctx,
            code_with_wheels(AUTHORIZER_SRC),
            timeout=10,
            environment={
                "DISCOVERY_URL": f"{ctx.platform.identity.direct_issuer_url}"
                "/.well-known/openid-configuration",
                "AUDIENCE": identity.platform_app_id(ctx),
                "ALLOWED_CLIENTS": console_id,
            },
        )
    return apigw.TokenAuthorizer(stack, id, handler=fn)


def create(
    stack: cdk.Stack,
    ctx: UseCaseContext,
    config: BusinessOperationsConfig,
    data: Data,
    *,
    cors_origins: list[str] | None = None,
) -> Api:
    """Build the APIs, their functions, the use case's groups and clients."""
    prefix, ssm_prefix = ctx.prefix, ctx.ssm_prefix
    stop_parameter = names.trigger_mode_parameters(ssm_prefix)["stop"]

    def param_arn(name: str) -> str:
        return stack.format_arn(
            service="ssm", resource="parameter", resource_name=name.lstrip("/")
        )

    origins = cors_origins or []
    direct = identity.direct(ctx)

    if direct:
        # Entra app registrations the operator created; the groups are app roles.
        entra = config.entra
        pool = None
        console_id = entra.console_client_id
        secret = secretsmanager.Secret.from_secret_name_v2(
            stack, "DelegatingClientSecret", entra.delegating_secret_name
        )
        delegating_env = {
            "DELEGATING_CLIENT_ID": entra.delegating_client_id,
            "DELEGATING_SECRET_ARN": secret.secret_arn,
            # Its own audience, so the token is good at this runtime and never at the
            # Gateway, whose audience is the platform app.
            "DELEGATING_SCOPE": f"{entra.delegating_client_id}/.default",
            "DELEGATING_TOKEN_URL": identity.token_url(ctx),
        }
    else:
        pool_id = ctx.ssm_value(stack, "auth/user-pool-id")
        pool = cognito.UserPool.from_user_pool_id(stack, "SharedPool", pool_id)
        _group(
            stack, "AdminsGroup", pool_id, ADMINS_GROUP, "may change the trigger mode"
        )
        _group(
            stack, "AuthorsGroup", pool_id, AUTHORS_GROUP, "may author packs and SOPs"
        )
        _group(
            stack,
            "LimitOwnersGroup",
            pool_id,
            LIMIT_OWNERS_GROUP,
            "may lower a pack's write limits",
        )
        _group(
            stack,
            "CaseWorkersGroup",
            pool_id,
            CASE_WORKERS_GROUP,
            "may start the agent on a case (writes under the auto-write limit)",
        )
        console, delegating, secret = _clients(stack, ctx, pool, origins)
        console_id = console.user_pool_client_id
        delegating_env = {
            "DELEGATING_CLIENT_ID": delegating.user_pool_client_id,
            "DELEGATING_SECRET_ARN": secret.secret_arn,
            "DELEGATING_SCOPE": RUNTIME_SCOPE,
            "AUTH_DOMAIN_URL": ctx.ssm_value(stack, "auth/domain-url"),
        }

    layer = _lambda.LayerVersion(
        stack,
        "ApiLayer",
        # registry.py there is a symlink to the agent's copy: one file, two assets.
        code=_lambda.Code.from_asset(
            str(API_SRC / "layer"),
            follow_symlinks=cdk.SymlinkFollowMode.ALWAYS,
            exclude=ASSET_EXCLUDE,
        ),
        compatible_runtimes=[_lambda.Runtime.PYTHON_3_13],
        description=f"{NAME}: entitlement, case identity and the registry reader",
    )
    common = {
        "API_ALLOWED_CLIENTS": console_id,
        "USERS_GROUP": USERS_GROUP,
        "ADMINS_GROUP": ADMINS_GROUP,
        "SOP_AUTHOR_GROUPS": AUTHORS_GROUP,
        "LIMIT_OWNER_GROUPS": LIMIT_OWNERS_GROUP,
        "CASE_WORKER_GROUPS": CASE_WORKERS_GROUP,
        # Every route that starts a run refuses while an emergency stop stands.
        "EMERGENCY_STOP_PARAMETER": stop_parameter,
        "CORS_ALLOWED_ORIGINS": ",".join(origins),
        "REGISTRY_BUCKET": data.registry.bucket_name,
        "STACK_NAME": stack.stack_name,
        # Per stack, so two stacks in one account never read each other's metrics.
        "METRICS_NAMESPACE": f"{prefix}/{NAME}",
        # An Entra person's groups are the platform app's app roles.
        **({"GROUP_CLAIMS": identity.GROUP_CLAIMS_DIRECT} if direct else {}),
    }

    def fn(
        id: str,
        source: str,
        env: dict[str, str],
        timeout: int = 15,
        handler: str = "index.handler",
    ):
        f = uc_function(
            stack,
            id,
            ctx,
            API_SRC / source,
            layers=[layer],
            timeout=timeout,
            environment={**common, **env},
            handler=handler,
        )
        data.grant(f, data.registry)  # every route reads the entitlement map
        f.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter"], resources=[param_arn(stop_parameter)]
            )
        )
        return f

    # CONFIG_TABLE: an approval reads the pack's dual-approval limit, overrides applied.
    cases = fn(
        "CasesFunction",
        "cases",
        {"TABLE_NAME": data.cases.table_name, "CONFIG_TABLE": data.config.table_name},
    )
    # BatchGetItem: a list page reads its cases whole by id after the query index.
    data.grant_cases(cases, "GetItem", "BatchGetItem", "Query", "UpdateItem")
    data.grant(cases, data.config)

    trigger_mode_names = names.trigger_mode_parameters(ssm_prefix)
    trigger_mode_prefix = trigger_mode_names["prefix"]
    trigger_mode = fn(
        "TriggerModeFunction",
        "trigger_mode",
        {
            "TRIGGER_MODE_PREFIX": trigger_mode_prefix,
            "PACK_MODE_PREFIX": trigger_mode_names["packs"],
            "PACK_ACTOR_PREFIX": trigger_mode_names["pack_actors"],
            "AUTONOMOUS_CAPABLE": "false",
        },
        timeout=10,
    )
    trigger_mode.add_to_role_policy(
        iam.PolicyStatement(
            # GetParameters reads the packs' modes ten at a time for GET /deployment/trigger-mode.
            actions=["ssm:GetParameter", "ssm:GetParameters", "ssm:PutParameter"],
            resources=[
                stack.format_arn(
                    service="ssm",
                    resource="parameter",
                    resource_name=f"{trigger_mode_prefix.lstrip('/')}/*",
                )
            ],
        )
    )

    config_fn = fn(
        "ConfigFunction",
        "config",
        {
            "CONFIG_TABLE": data.config.table_name,
            "CONTACTS_JSON": json.dumps(config.contacts),
            # What `GET /registry` and `GET /deployment/settings` say about where the agent writes:
            # every SAP system this use case reads, with its system role, and the mode.
            "SAP_SYSTEM_ROLES": json.dumps(
                {
                    name: system.system_role
                    for name, system in config.sap.systems.items()
                }
            ),
            "DEPLOYMENT_MODE": "production" if ctx.production else "workshop",
        },
        timeout=10,
    )
    data.grant(config_fn, data.config, write=True)
    # All-or-nothing multi-key writes; not in CDK's read-write action set.
    config_fn.add_to_role_policy(
        iam.PolicyStatement(
            actions=["dynamodb:TransactWriteItems"], resources=[data.config.table_arn]
        )
    )

    runs = fn(
        "RunsFunction",
        "runs",
        {
            "CASES_TABLE": data.cases.table_name,
            **delegating_env,
            # The caller's token rides to the Gateway, for an obo SAP system.
            **(
                {"FORWARD_USER_TOKEN": "true"}
                if identity.forwards_person_token(ctx, config)
                else {}
            ),
        },
        timeout=30,
    )
    data.grant_cases(runs, "GetItem", "UpdateItem")
    secret.grant_read(runs)

    # The emergency stop: the one route that holds the switch and the right to end runs.
    # `invoker.create` adds the grants table and the runtime, as it does for `runs`.
    stop = fn(
        "StopFunction",
        "stop",
        {
            "TRIGGER_MODE_PREFIX": trigger_mode_prefix,
            "CASES_TABLE": data.cases.table_name,
            "CONFIG_TABLE": data.config.table_name,
            **delegating_env,
        },
        timeout=30,
        # The routes, and `ops.py emergency-stop` invoking it over IAM.
        handler="index.dispatch",
    )
    stop.add_to_role_policy(
        iam.PolicyStatement(
            actions=["ssm:PutParameter"],
            resources=[
                param_arn(f"{trigger_mode_prefix}/{name}")
                for name in (
                    names.DEPLOYMENT_TRIGGER_MODE,
                    f"{names.DEPLOYMENT_TRIGGER_MODE}-last-changed-by",
                    names.EMERGENCY_STOP,
                )
            ],
        )
    )
    # Releases a run's claim and parks its case, nothing else on the cases.
    data.grant_cases(stop, "UpdateItem")
    secret.grant_read(stop)
    # The stop's record only: its own namespace of the config table, never an override.
    stop.add_to_role_policy(
        iam.PolicyStatement(
            actions=["dynamodb:PutItem", "dynamodb:UpdateItem"],
            resources=[data.config.table_arn],
            conditions={
                "ForAllValues:StringEquals": {"dynamodb:LeadingKeys": ["deployment"]}
            },
        )
    )

    waf = ctx.production
    acl = _web_acl(stack, prefix) if waf else None

    # ── The console API ──
    api = _rest_api(
        stack,
        "Api",
        ctx,
        f"{prefix} {NAME}: cases, runs, trigger mode and config",
        cloudwatch_role=config.api.set_account_cloudwatch_role,
        cors_origins=origins,
    )
    if acl:
        _associate(stack, "ApiWebAclAssociation", acl, api)
    authorization_type = (
        apigw.AuthorizationType.CUSTOM if direct else apigw.AuthorizationType.COGNITO
    )
    authorizer = _authorizer(stack, "ApiAuthorizer", ctx, pool, console_id)
    authed = {
        "authorizer": authorizer,
        "authorization_type": authorization_type,
    }

    def route(resource: apigw.IResource, methods: list[str], target, **extra):
        # No test-invoke permission: it only serves API Gateway's console Test button,
        # and one per method was a tenth of this stack's 500-resource limit.
        integration = apigw.LambdaIntegration(target, allow_test_invoke=False)
        for method in methods:
            resource.add_method(method, integration, **authed, **extra)

    cases_r = api.root.add_resource("cases")
    route(cases_r, ["GET"], cases)
    route(cases_r.add_resource("counts"), ["GET"], cases)
    route(cases_r.add_resource("summary"), ["GET"], cases)
    case_r = cases_r.add_resource("{case_id}")
    route(case_r, ["GET"], cases)
    route(case_r.add_resource("rating"), ["PUT"], cases)
    route(case_r.add_resource("reply"), ["POST"], cases)
    # SQS caps MessageGroupId at 128 characters; a case id outside the codec's shape
    # is a 400 here rather than a botocore 500 later.
    enqueue_model = api.add_model(
        "EnqueueCaseModel",
        content_type="application/json",
        schema=apigw.JsonSchema(
            schema=apigw.JsonSchemaVersion.DRAFT4,
            type=apigw.JsonSchemaType.OBJECT,
            required=["case_id"],
            additional_properties=False,
            properties={
                "case_id": apigw.JsonSchema(
                    type=apigw.JsonSchemaType.STRING,
                    pattern=data_contracts.constants()["CASE_ID_PATTERN"],
                )
            },
        ),
    )
    route(
        cases_r.add_resource("enqueue"),
        ["POST"],
        cases,
        request_validator=apigw.RequestValidator(
            stack, "ApiRequestValidator", rest_api=api, validate_request_body=True
        ),
        request_models={"application/json": enqueue_model},
    )
    # Paths by pack part: what applies to one pack is under it, what applies to every
    # pack is under /deployment. Each Lambda serves the paths its old ones did.
    route(api.root.add_resource("registry"), ["GET"], config_fn)
    deployment_r = api.root.add_resource("deployment")
    route(deployment_r.add_resource("settings"), ["GET", "PUT"], config_fn)
    route(deployment_r.add_resource("trigger-mode"), ["GET", "PUT"], trigger_mode)
    stop_r = deployment_r.add_resource("stop")
    route(stop_r, ["POST"], stop)
    route(stop_r.add_resource("clear"), ["POST"], stop)
    pack_r = pack_resource(api)
    route(pack_r.add_resource("rules"), ["GET"], config_fn)
    route(pack_r.add_resource("controls"), ["GET", "PUT"], config_fn)
    route(pack_r.add_resource("trigger-mode"), ["GET", "PUT"], trigger_mode)
    runs_r = api.root.add_resource("runs")
    route(runs_r, ["POST"], runs)
    route(runs_r.add_resource("stop"), ["POST"], runs)

    functions = {
        "cases": cases,
        "trigger_mode": trigger_mode,
        "config": config_fn,
        "runs": runs,
        "stop": stop,
    }
    outputs = {
        "api-url": api.url,
        "console-client-id": console_id,
        "delegating-client-id": delegating_env["DELEGATING_CLIENT_ID"],
        "delegating-client-secret-arn": secret.secret_arn,
        "delegating-scope": delegating_env["DELEGATING_SCOPE"],
        "admins-group": ADMINS_GROUP,
        "authors-group": AUTHORS_GROUP,
        # `ops.py emergency-stop` invokes it over IAM.
        "stop-function": stop.function_name,
        # The knowledge stack's search Lambda reads it (knowledge.API_LAYER_PARAM).
        "api-layer-arn": layer.layer_version_arn,
    }
    cdk.CfnOutput(stack, "ApiUrl", value=api.url)

    # ── The sample ticketing, on the console API ──
    if config.api.sample_ticketing:
        tickets = fn(
            "TicketsFunction",
            "tickets",
            {
                "TICKETS_TABLE_NAME": data.tickets.table_name,
                "CASES_TABLE_NAME": data.cases.table_name,
                "CONFIG_TABLE": data.config.table_name,
            },
        )
        data.grant(tickets, data.tickets, write=True)
        data.grant(tickets, data.config)
        # Reads a case's pack for every decision, and records the decision on it.
        data.cases.grant(
            tickets, "dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:UpdateItem"
        )
        data.grant_key(tickets)
        tickets_r = api.root.add_resource("tickets")
        route(tickets_r, ["GET", "POST"], tickets)
        ticket_r = tickets_r.add_resource("{id}")
        route(ticket_r, ["GET", "PUT"], tickets)
        route(ticket_r.add_resource("action"), ["POST"], tickets)
        functions["tickets"] = tickets
        cdk.CfnOutput(
            stack,
            "SampleTicketing",
            value=(
                "on: /tickets on the console API and the agent's ticket tools are a "
                "stand-in for a real ITSM; replace them before production"
            ),
        )
        if ctx.production:
            cdk.Annotations.of(stack).add_warning_v2(
                f"{NAME}:sample-ticketing",
                "Sample ticketing is on under deployment.mode: production. It is a "
                "stand-in for a real ITSM; set use_cases.business-operations.api."
                "sample_ticketing: false once one is wired.",
            )

    # ── SAP push ingest ──
    if config.api.erp_events_endpoint:
        from business_operations.pipeline import SRC as PIPELINE_SRC
        from business_operations.pipeline import code as pipeline_code

        # Its environment and grants are the pipeline's (pipeline.py), which also
        # creates the token it checks and the queue it fills. PyJWT checks the
        # ingest client's tokens, which exist only on the pool.
        ingest = uc_function(
            stack,
            "ErpEventsFunction",
            ctx,
            code_with_wheels(PIPELINE_SRC) if pool else pipeline_code(),
            handler="erp_events.handler",
            layers=[layer],
            timeout=29,
        )
        if pool is not None:
            client_id, client_secret = _ingest_client(stack, ctx, pool)
            for k, v in {
                "INGEST_ISSUER": f"https://cognito-idp.{stack.region}.amazonaws.com/"
                f"{pool.user_pool_id}",
                "INGEST_CLIENT_ID": client_id,
                "INGEST_SCOPE": INGEST_SCOPE,
            }.items():
                ingest.add_environment(k, v)
            outputs["ingest-client-id"] = client_id
            outputs["ingest-client-secret-arn"] = client_secret.secret_arn
            outputs["ingest-scope"] = INGEST_SCOPE
        ingest_api = _rest_api(
            stack,
            "ErpEventsApi",
            ctx,
            f"{prefix} {NAME}: SAP pushes exceptions here",
            cloudwatch_role=False,
            method_options={
                "/erp-events/POST": apigw.MethodDeploymentOptions(
                    throttling_rate_limit=50, throttling_burst_limit=100
                )
            },
        )
        _after_account(api, ingest_api)
        if acl:
            _associate(stack, "ErpEventsApiWebAclAssociation", acl, ingest_api)
        # No authorizer: the caller is an SM59 destination with no user session, or
        # a machine client. The handler checks the shared token, constant-time, or
        # the ingest client's access token.
        ingest_api.root.add_resource("erp-events").add_method(
            "POST",
            apigw.LambdaIntegration(ingest),
            authorization_type=apigw.AuthorizationType.NONE,
        )
        functions["erp_events"] = ingest
        outputs["erp-events-url"] = f"{ingest_api.url}erp-events"

    for key, value in outputs.items():
        ssm.StringParameter(
            stack,
            f"Out-{key}",
            parameter_name=f"{ssm_prefix}/use-cases/{NAME}/{key}",
            string_value=value,
        )
    return Api(
        api.url,
        console_id,
        secret,
        delegating_env,
        layer,
        functions,
        rest_api=api,
        authorizer=authorizer,
        env=common,
        pool=pool,
        authorization_type=authorization_type,
    )


def pack_resource(api: apigw.RestApi) -> apigw.IResource:
    """`/packs/{pack_id}`, made by whichever module mounts on it first."""
    packs = api.root.get_resource("packs") or api.root.add_resource("packs")
    return packs.get_resource("{pack_id}") or packs.add_resource("{pack_id}")


def _after_account(main: apigw.RestApi, other: apigw.RestApi) -> None:
    """Stage logging needs the account's CloudWatch role; when the console API sets
    it, every other stage waits for it."""
    account = main.node.try_find_child("Account")
    if account:
        other.deployment_stage.node.add_dependency(account)
