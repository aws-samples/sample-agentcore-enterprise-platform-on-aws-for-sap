# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The agent runtime, the run-event channel, and what the runtime may touch.

The runtime is built by the platform's `RuntimeStack` (remote arm64 build of `agent/`,
the platform's scoped role, model allowlist and guardrail deny) as its own stack,
`<prefix>-uc-business-operations-runtime`, deployed after the main stack. It finds the
main stack's tables, buckets and clients through SSM, never by import, so the main
stack carries no export a runtime change could block.

Who calls it, and as whom:

- **Inbound.** The authorizer admits the delegating client alone (the run route and the
  invoker hold it). Only because of that is the delegated-identity header allowlisted:
  a caller holding that client may name the acting person, and `agent/utils/auth.py`
  re-checks both. Adding any other client here while the header is allowlisted lets
  that client spoof who acted.
- **Outbound.** The agent calls the shared Gateway as the use case's `writer` client,
  drawn from its own token-vault provider (`credential_provider_name`). Cedar keys on
  that token's `uc-business-operations/*` scopes, and the run checks on the run grant the
  invoker wrote for that client id.

The run-event channel is an AppSync Events API: the runtime publishes over IAM, the
console subscribes with a Cognito id token from the console client only, and every
subscribe is held to the users group and the channel's pack (`run_events_auth`). It
lives here because the runtime is its only publisher.

RuntimeStack has no hook to resolve SSM before it builds the runtime, so values the
runtime's environment and authorizer need are CloudFormation `{{resolve:ssm:...}}`
references; anything created after construction uses an SSM parameter as usual.
"""

from __future__ import annotations

import json

import aws_cdk as cdk
from aws_cdk import aws_appsync as appsync
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_iam as iam
from aws_cdk import aws_ssm as ssm

from business_operations import console, identity, names, persistence, run_events_auth
from business_operations.api import ADMINS_GROUP
from business_operations.config import BusinessOperationsConfig
from infra_utils.guardrail_policy import CREDENTIAL_FLOOR, output_only_block
from infra_utils.use_case_context import UseCaseContext

NAME = names.NAME
DELEGATED_IDENTITY_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Identity"  # publication-gate: allow wording
# The run grant's secret, invoker to agent (invoker/index.py RUN_GRANT_HEADER).
RUN_GRANT_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Run-Grant"  # publication-gate: allow wording
RUN_EVENTS_NAMESPACE = "runs"
#: The Gateway client the agent acts as (manifest `gateway_clients`).
AGENT_CLIENT = "writer"
GATEWAY_SCOPES = f"uc-{NAME}/read uc-{NAME}/write"
#: Used when platform.yaml sets no `agents.model_id`.
DEFAULT_SONNET = "us.anthropic.claude-sonnet-5-5"
DEFAULT_HAIKU = "us.anthropic.claude-haiku-5-5"
DEFAULT_OPUS = "us.anthropic.claude-opus-5-5"
#: Pack tier -> the runtime environment variable carrying its model. The tier set is
#: `packs/pack.schema.json`'s enum and the agent's `MODEL_TIERS`; a test pins all three.
TIER_ENV = {"sonnet": "MODEL_ID", "haiku": "MODEL_ID_HAIKU", "opus": "MODEL_ID_OPUS"}


def model_ids(ctx: UseCaseContext, cfg: BusinessOperationsConfig) -> dict[str, str]:
    """The model behind each tier this deployment serves, refused at synth when the
    allowlist omits a configured one.

    The allowlist scopes the runtime role's Amazon Bedrock grant, so a tier outside it would
    fail only at run time, and only for the packs that use it. `opus` is served only
    when its model is allowed (or set explicitly): an absent tier is a synth error for
    the pack naming it (`validate_packs`), never a silent run on another model.
    """
    allowed = list(ctx.allowed_models)
    sonnet = ctx.model_id or DEFAULT_SONNET
    haiku = cfg.runtime.haiku_model_id or (
        DEFAULT_HAIKU if not allowed or DEFAULT_HAIKU in allowed else sonnet
    )
    tiers = {"sonnet": sonnet, "haiku": haiku}
    opus = cfg.runtime.opus_model_id or (
        DEFAULT_OPUS if not allowed or DEFAULT_OPUS in allowed else None
    )
    if opus:
        tiers["opus"] = opus
    outside = sorted(
        f"{t}={m}" for t, m in tiers.items() if allowed and m not in allowed
    )
    if outside:
        raise ValueError(
            f"use_cases.{NAME}: model tier(s) {', '.join(outside)} are not in "
            "agents.allowed_models. Add them there, or set agents.model_id / "
            f"use_cases.{NAME}.runtime.haiku_model_id / opus_model_id to an allowed "
            "model."
        )
    return tiers


def unserved_tools(cfg: BusinessOperationsConfig) -> list[str]:
    """Tools a pack may name that this deployment does not serve, for the prompt gate."""
    k = cfg.knowledge
    return [
        *([] if k.sop_search else ["search_sap_sops"]),
        *([] if k.api_docs_search else ["search_sap_api_docs"]),
        *([] if k.agent_knowledge.enabled else ["get_precedent", "check_vendor_risk"]),
    ]


def guardrail_policy() -> dict:
    """The agent's guardrail, used when the platform requires one (`require_guardrails`).

    Not the platform baseline: that anonymizes names and addresses, and supplier names
    and remit-to addresses are the work. SSNs and card numbers are blocked, not
    anonymized, because the production floor requires it; bank and tax ids are
    anonymized because vendor master data carries them legitimately. AWS keys are
    blocked on output only: the detector reads some case ids as a secret key.

    No denied topic. Within the classic tier's 200 characters one blocked ordinary
    SOP-directed work ("release invoice ... and clear the payment block"), and what
    it stood for is enforced deterministically: the run checks, the amount gate,
    single-use approvals and the write guards.
    """
    return {
        "BlockedInputMessaging": "This request was blocked by the agent's guardrail policy.",
        "BlockedOutputsMessaging": "This response was blocked by the agent's guardrail policy.",
        "ContentPolicyConfig": {
            "FiltersConfig": [
                {
                    "Type": "PROMPT_ATTACK",
                    "InputStrength": "HIGH",
                    "OutputStrength": "NONE",
                },
                {
                    "Type": "MISCONDUCT",
                    "InputStrength": "HIGH",
                    "OutputStrength": "HIGH",
                },
                {
                    "Type": "INSULTS",
                    "InputStrength": "MEDIUM",
                    "OutputStrength": "MEDIUM",
                },
                {"Type": "HATE", "InputStrength": "HIGH", "OutputStrength": "HIGH"},
                {
                    "Type": "VIOLENCE",
                    "InputStrength": "MEDIUM",
                    "OutputStrength": "MEDIUM",
                },
                {"Type": "SEXUAL", "InputStrength": "HIGH", "OutputStrength": "HIGH"},
            ]
        },
        "SensitiveInformationPolicyConfig": {
            "PiiEntitiesConfig": [
                {"Type": "US_SOCIAL_SECURITY_NUMBER", "Action": "BLOCK"},
                {"Type": "CREDIT_DEBIT_CARD_NUMBER", "Action": "BLOCK"},
                {"Type": "US_BANK_ACCOUNT_NUMBER", "Action": "ANONYMIZE"},
                {"Type": "US_BANK_ROUTING_NUMBER", "Action": "ANONYMIZE"},
                {
                    "Type": "US_INDIVIDUAL_TAX_IDENTIFICATION_NUMBER",
                    "Action": "ANONYMIZE",
                },
                *(output_only_block(t) for t in CREDENTIAL_FLOOR),
            ]
        },
        "WordPolicyConfig": {"ManagedWordListsConfig": [{"Type": "PROFANITY"}]},
    }


def log_model_content(ctx: UseCaseContext, cfg: BusinessOperationsConfig) -> bool:
    """On outside production unless set; off in production unless set."""
    if cfg.runtime.log_model_content is not None:
        return cfg.runtime.log_model_content
    return not ctx.production


def span_env(content_on_spans: bool) -> dict[str, str]:
    """The runtime's span routing and content settings.

    Spans go to the shared `aws/spans` group, beside the platform's agents. A runtime
    created after 2026-07-20 with aws-opentelemetry-distro 0.18.0 or later defaults
    to its own log group instead. The judges' SDK reads both, but `evals-run` checks for
    `aws/spans` before judging, and the platform's other agents are there.

    Content: Strands always writes prompts, responses and tool payloads as span events
    under its default conventions, which is what the evaluators read, so "on" needs
    nothing. "Off" redacts them: an empty `gen_ai_unredacted_attributes=` allowlist
    redacts every message field, and the distro's own botocore capture is turned off.
    """
    env = {"UNIFIED_TRACES_DESTINATION_ENABLED": "false"}
    if not content_on_spans:
        env["OTEL_SEMCONV_STABILITY_OPT_IN"] = "gen_ai_unredacted_attributes="
        env["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "false"
    return env


def create(app: cdk.App, ctx: UseCaseContext, cfg: BusinessOperationsConfig):
    """Build the runtime stack. Imported here so a synth without the use case never
    loads the platform's RuntimeStack on its behalf."""
    from stacks.runtime_stack import RuntimeStack

    prefix, ssm_prefix = ctx.prefix, ctx.ssm_prefix
    own = f"{ssm_prefix}/use-cases/{NAME}"
    mine = f"use-cases/{NAME}"

    direct = identity.direct(ctx)
    forwards = identity.forwards_person_token(ctx, cfg)
    delegating = (
        cfg.entra.delegating_client_id
        if direct
        else ctx.ssm_ref(f"{mine}/delegating-client-id")
    )
    # Brokered: Cognito pins the client. Direct: an Entra token has no client_id, so
    # the authorizer pins the delegating app's own audience, and the container
    # re-checks `azp` on every call (agent/utils/auth.py, RUNTIME_DELEGATING_ONLY).
    authorizer = (
        {"cognito_allowed_clients": [], "allowed_audience": [delegating]}
        if direct
        else {"cognito_allowed_clients": [delegating]}
    )
    direct_env = (
        {
            "RUNTIME_DELEGATING_ONLY": "true",
            **(
                {"RUNTIME_USER_TOKEN_AUDIENCE": identity.platform_app_id(ctx)}
                if forwards
                else {}
            ),
        }
        if direct
        else {}
    )
    provider_name = names.credential_provider_name(prefix)
    tiers = model_ids(ctx, cfg)
    issuer = ctx.cognito_issuer_url
    cedar = ctx.platform.security.cedar
    namespace = f"{prefix}/{NAME}"
    content_on_spans = log_model_content(ctx, cfg)

    rt = RuntimeStack(
        app,
        f"{prefix}-uc-{NAME}-runtime",
        project_name=ctx.project,
        environment=ctx.environment,
        component_name=NAME,
        source_dir=f"use-cases/{NAME}/agent",
        protocol="AGUI",
        **authorizer,
        extra_request_headers=[
            DELEGATED_IDENTITY_HEADER,
            RUN_GRANT_HEADER,
            *([identity.USER_TOKEN_HEADER] if forwards else []),
        ],
        credential_provider_names=[provider_name],
        memory_actor_prefix=f"uc-{NAME}:",
        guardrail_policy=guardrail_policy(),
        extra_env_vars={
            "AWS_DEFAULT_REGION": ctx.region,
            **{TIER_ENV[t]: m for t, m in tiers.items()},
            "AGENT_MAX_TOKENS": str(cfg.runtime.max_tokens),
            "MEMORY_ID": ctx.ssm_ref("memory/memory-id"),
            "RUNTIME_JWT_DISCOVERY_URL": f"{issuer}/.well-known/openid-configuration",
            "RUNTIME_DELEGATING_CLIENTS": delegating,
            "GATEWAY_URL": ctx.ssm_ref("gateway/url"),
            "GATEWAY_CREDENTIAL_PROVIDER_NAME": provider_name,
            # Direct: the platform app's own `.default`, the Gateway's audience.
            "GATEWAY_SCOPES": f"{identity.platform_app_id(ctx)}/.default"
            if direct
            else GATEWAY_SCOPES,
            "CASES_TABLE": ctx.ssm_ref(f"{mine}/cases-table"),
            "CONFIG_TABLE": ctx.ssm_ref(f"{mine}/config-table"),
            "SOP_BUCKET": ctx.ssm_ref(f"{mine}/sops-bucket"),
            "EXEMPLAR_BUCKET": ctx.ssm_ref(f"{mine}/exemplars-bucket"),
            "REGISTRY_BUCKET": ctx.ssm_ref(f"{mine}/registry-bucket"),
            "RUN_EVENTS_NAMESPACE": RUN_EVENTS_NAMESPACE,
            "CEDAR_ENFORCEMENT_MODE": cedar.mode if cedar.enabled else "OFF",
            "METRICS_NAMESPACE": namespace,
            "CONTACTS_JSON": json.dumps(cfg.contacts),
            "UNSERVED_TOOLS": ",".join(unserved_tools(cfg)),
            # A pack naming SAP tools on a deployment with no SAP system maps no
            # process type, rather than plan reads the Gateway never lists.
            "SAP_MCP_ENABLED": "true" if cfg.sap.systems else "false",
            **(
                {"AGENT_KNOWLEDGE_ENABLED": "true"}
                if cfg.knowledge.agent_knowledge.enabled
                else {}
            ),
            **span_env(content_on_spans),
            **direct_env,
        },
        **ctx.runtime_kwargs(),
        env=ctx.cdk_env,
    )

    if tiers["haiku"] == tiers["sonnet"] and not cfg.runtime.haiku_model_id:
        cdk.Annotations.of(rt).add_warning_v2(
            f"{NAME}:haiku-tier-on-sonnet",
            f"agents.allowed_models leaves out {DEFAULT_HAIKU}, so the haiku tier runs "
            f"on {tiers['sonnet']} and a pack that sets model_tier: haiku bills at that "
            "model's rates. Allow a Haiku model and set "
            f"use_cases.{NAME}.runtime.haiku_model_id to it.",
        )
    if content_on_spans:
        cdk.Annotations.of(rt).add_warning_v2(
            f"{NAME}:log-model-content",
            "Model prompts and responses, including SAP data, are written in plaintext "
            "to the runtime's CloudWatch log group for observability.log_retention_days. "
            "Set use_cases.business-operations.runtime.log_model_content: "
            "false to redact them.",
        )

    if ctx.platform.agents.memory.strategies != []:
        cdk.Annotations.of(rt).add_warning_v2(
            f"{NAME}:memory-strategies",
            "agents.memory.strategies is not []: a Memory strategy extracts every stored "
            "event, and this runtime stores SAP document contents and vendor data in "
            "its short-term history. Set agents.memory.strategies: [] unless a strategy "
            "is wanted for that data.",
        )

    # ── The agent's Gateway client, in the token vault ──
    client = f"gateway/use-case-clients/{NAME}/{AGENT_CLIENT}"
    agentcore.CfnOAuth2CredentialProvider(
        rt,
        "AgentGatewayClient",
        name=provider_name,
        credential_provider_vendor="CustomOauth2",
        oauth2_provider_config_input=agentcore.CfnOAuth2CredentialProvider.Oauth2ProviderConfigInputProperty(
            custom_oauth2_provider_config=agentcore.CfnOAuth2CredentialProvider.CustomOauth2ProviderConfigInputProperty(
                oauth_discovery=agentcore.CfnOAuth2CredentialProvider.Oauth2DiscoveryProperty(
                    discovery_url=f"{issuer}/.well-known/openid-configuration"
                ),
                client_id=ctx.ssm_value(rt, f"{client}/client-id"),
                # A dynamic reference, resolved by CloudFormation: the secret never
                # appears in the template or cdk.out.
                client_secret=cdk.SecretValue.secrets_manager(
                    ctx.ssm_value(rt, f"{client}/client-secret-name")
                ).unsafe_unwrap(),
            )
        ),
    )

    # ── The run-event channel ──
    iam_auth = appsync.AppSyncAuthProvider(
        authorization_type=appsync.AppSyncAuthorizationType.IAM
    )
    if direct:
        # OIDC: AppSync checks the issuer and that `aud` is the platform app; the
        # console app (`azp`), the users role and the pack are run_events_auth's.
        user_mode = appsync.AppSyncAuthorizationType.OIDC
        user_auth = appsync.AppSyncAuthProvider(
            authorization_type=user_mode,
            open_id_connect_config=appsync.AppSyncOpenIdConnectConfig(
                oidc_provider=ctx.platform.identity.direct_issuer_url,
                client_id=identity.platform_app_id(ctx),
            ),
        )
    else:
        pool = cognito.UserPool.from_user_pool_id(
            rt, "SharedPool", ctx.ssm_value(rt, "auth/user-pool-id")
        )
        user_mode = appsync.AppSyncAuthorizationType.USER_POOL
        user_auth = appsync.AppSyncAuthProvider(
            authorization_type=user_mode,
            cognito_config=appsync.AppSyncCognitoConfig(
                user_pool=pool,
                # Pinned to the console client: the pool is shared. The group and the
                # pack are checked per subscribe (run_events_auth), not here.
                app_id_client_regex=ctx.ssm_value(rt, f"{mine}/console-client-id"),
            ),
        )
    events = appsync.EventApi(
        rt,
        "RunEvents",
        api_name=names.run_events_api_name(prefix),
        authorization_config=appsync.EventApiAuthConfig(
            auth_providers=[iam_auth, user_auth],
            connection_auth_mode_types=[user_mode],
            default_subscribe_auth_mode_types=[user_mode],
            default_publish_auth_mode_types=[appsync.AppSyncAuthorizationType.IAM],
        ),
    )
    channel = events.add_channel_namespace(
        "Runs",
        channel_namespace_name=RUN_EVENTS_NAMESPACE,
        subscribe_handler_config=run_events_auth.handler_config(
            rt,
            ctx,
            events,
            console_client_id=ctx.ssm_value(rt, f"{mine}/console-client-id"),
            registry_bucket=ctx.ssm_value(rt, f"{mine}/registry-bucket"),
            kms_key_arn=ctx.ssm_value(rt, "security/kms-key-arn")
            if ctx.production
            else None,
        ),
    )
    channel.grant_publish(rt.runtime_role)
    rt.runtime.add_property_override(
        "EnvironmentVariables.RUN_EVENTS_HTTP_DOMAIN", events.http_dns
    )

    # ── What the runtime may touch ──
    role = rt.runtime_role

    def table_arn(key: str) -> str:
        return rt.format_arn(
            service="dynamodb",
            resource="table",
            resource_name=ctx.ssm_value(rt, f"{mine}/{key}"),
        )

    def bucket_arn(key: str) -> str:
        return f"arn:{rt.partition}:s3:::{ctx.ssm_value(rt, f'{mine}/{key}')}"

    cases, config_table = table_arn("cases-table"), table_arn("config-table")
    role.add_to_policy(
        iam.PolicyStatement(
            sid="CasesReadWrite",
            # Routing, scope and amount-gate reads; the live snapshot and trace writes.
            actions=["dynamodb:GetItem", "dynamodb:UpdateItem"],
            resources=[cases],
        )
    )
    # Read only: SOP placeholders resolve against operator overrides; /config writes them.
    role.add_to_policy(
        iam.PolicyStatement(
            sid="ConfigRead",
            actions=["dynamodb:GetItem", "dynamodb:Query"],
            resources=[config_table],
        )
    )
    buckets = [
        bucket_arn(k) for k in ("sops-bucket", "exemplars-bucket", "registry-bucket")
    ]
    role.add_to_policy(
        iam.PolicyStatement(
            sid="PacksRead",
            actions=["s3:GetObject", "s3:ListBucket"],
            resources=[*buckets, *(f"{b}/*" for b in buckets)],
        )
    )
    if ctx.require_guardrails:
        # The main stack's persistence guardrail, for the trace write (persist_mask.py).
        rt.runtime.add_property_override(
            "EnvironmentVariables.PERSIST_GUARDRAIL_ID", ctx.ssm_ref(persistence.PARAM)
        )
        # A template parameter, not a dynamic reference: CloudFormation reads it again
        # on every update, so a version the main stack republished reaches the runtime.
        rt.runtime.add_property_override(
            "EnvironmentVariables.PERSIST_GUARDRAIL_VERSION",
            ctx.ssm_value(rt, persistence.VERSION_PARAM),
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="PersistGuardrail",
                actions=["bedrock:ApplyGuardrail"],
                resources=[
                    rt.format_arn(
                        service="bedrock",
                        resource="guardrail",
                        resource_name=ctx.ssm_value(rt, persistence.PARAM),
                    )
                ],
            )
        )
    role.add_to_policy(
        iam.PolicyStatement(
            sid="OwnMetrics",
            actions=["cloudwatch:PutMetricData"],
            resources=["*"],  # PutMetricData takes no resource; the namespace scopes it
            conditions={"StringEquals": {"cloudwatch:namespace": namespace}},
        )
    )
    if ctx.production:
        role.add_to_policy(
            iam.PolicyStatement(
                sid="DataKey",
                actions=[
                    "kms:Decrypt",
                    "kms:Encrypt",
                    "kms:GenerateDataKey*",
                    "kms:DescribeKey",
                ],
                resources=[ctx.ssm_value(rt, "security/kms-key-arn")],
                conditions={
                    "StringEquals": {
                        "kms:ViaService": [
                            f"dynamodb.{rt.region}.amazonaws.com",
                            f"s3.{rt.region}.amazonaws.com",
                        ]
                    }
                },
            )
        )

    # The console's aws-exports.json, written here because it names this stack's channel.
    console.publish_config(
        rt,
        ctx,
        client_id=ctx.ssm_value(rt, f"{mine}/console-client-id"),
        api_url=ctx.ssm_value(rt, f"{mine}/api-url"),
        ticketing=cfg.api.sample_ticketing,
        admins_group=ADMINS_GROUP,
        run_events=(events.http_dns, events.realtime_dns),
        api_scope=cfg.entra.api_scope if direct else None,
        dashboard_url=ctx.ssm_value(rt, f"{mine}/dashboard-url"),
        # `test_data` is not a console flag: it mirrors the setting that builds the
        # seeder and its routes, so the page shows only where the routes exist. Whether
        # the stack takes packs by promotion only is served after sign-in instead
        # (`GET /deployment/settings`), as the system roles are.
        features={
            **cfg.console.features.model_dump(),
            "test_data": cfg.test_data.enabled,
        },
        brand=cfg.console.brand.model_dump(exclude_none=True),
    )

    for key, value in (
        ("run-events-http-domain", events.http_dns),
        ("run-events-realtime-domain", events.realtime_dns),
        ("run-events-namespace", RUN_EVENTS_NAMESPACE),
        ("agent-client", AGENT_CLIENT),
        ("metrics-namespace", namespace),
    ):
        ssm.StringParameter(
            rt, f"Out-{key}", parameter_name=f"{own}/{key}", string_value=value
        )
    return rt
