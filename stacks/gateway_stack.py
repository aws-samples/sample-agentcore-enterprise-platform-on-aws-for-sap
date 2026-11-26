# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Gateway Stack — AgentCore Gateway with Lambda tool targets."""

import json
import os
import shutil
import tempfile

import aws_cdk as cdk
from aws_cdk import (
    aws_bedrock as bedrock,
)
from aws_cdk import (
    aws_bedrockagentcore as agentcore,
)
from aws_cdk import (
    aws_cognito as cognito,
)
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    aws_lambda as _lambda,
)
from aws_cdk import (
    aws_secretsmanager as secretsmanager,
)
from aws_cdk import (
    aws_ssm as ssm,
)
from constructs import Construct

from infra_utils.guardrail_policy import baseline_policy, published_version
from infra_utils.jwt_authorizer import custom_jwt_authorizer
from infra_utils.policy_loader import load_control_text


class GatewayStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        project_name: str,
        environment: str,
        cognito_issuer_url: str,
        cognito_allowed_clients: list[str],
        allowed_audience: list[str] | None = None,
        tool_configs: dict | None = None,
        enable_web_search: bool = False,
        enable_egress_filter: bool = False,
        # PII action overrides for the egress guardrail, and whether the
        # production floor holds (infra_utils/guardrail_policy.py).
        egress_pii_entities: dict[str, str] | None = None,
        guardrail_floor: bool = False,
        egress_skip_tools: list[str] | None = None,
        egress_request_action: str = "mask",
        enable_cedar: bool = False,
        cedar_mode: str = "LOG_ONLY",
        debug_exceptions: bool = True,
        kms_key_arn: str = "",
        user_pool_id: str = "",
        use_case_clients: dict[str, dict[str, list[str]]] | None = None,
        use_case_client_apps: dict[str, dict[str, dict[str, str]]] | None = None,
        token_exchange: bool = False,
        interceptor_extensions: list[dict] | None = None,
        **kwargs,
    ):
        super().__init__(scope, id, **kwargs)
        cognito_allowed_clients = list(cognito_allowed_clients)

        prefix = f"{project_name}-{environment}"
        gw_name = f"{project_name}-{environment}-gateway"

        # Gateway IAM role. Trust only this deployment's Gateway (confused deputy):
        # without the condition, anyone who may PassRole it to CreateGateway or
        # CreateAgentRuntime acts as the Gateway, and so as the one principal a
        # Gateway-only runtime resource policy admits. The Gateway does not exist
        # yet, so match on its name: its id is "{name}-{random}".
        gw_role = iam.Role(
            self,
            "GatewayRole",
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnLike": {
                        "aws:SourceArn": f"arn:aws:bedrock-agentcore:{self.region}:"
                        f"{self.account}:gateway/{gw_name}-*"
                    },
                },
            ),
            role_name=f"{prefix}-gateway-role",
        )
        gw_role.add_to_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[
                    f"arn:aws:lambda:{self.region}:{self.account}:function:{prefix}-tool-*"
                ],
            )
        )
        if token_exchange:
            # A target with outbound grantType TOKEN_EXCHANGE trades the caller's
            # IdP token for one addressed to the target, through a provider in the
            # account's one token vault, as this Gateway's workload identity. The
            # Gateway's workload identity is service-created and unnamed, and the
            # vault keeps an inline provider secret under its own name, hence the
            # vault-wide resources; the trust policy above keeps the role to this
            # Gateway. Measured: without the directory itself on
            # GetResourceOauth2Token the exchange fails as "insufficient permissions".
            def arn(resource: str) -> str:
                return (
                    f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:{resource}"
                )

            directory = [
                arn("workload-identity-directory/default"),
                arn("workload-identity-directory/default/workload-identity/*"),
            ]
            gw_role.add_to_policy(
                iam.PolicyStatement(
                    sid="TokenExchangeWorkloadToken",
                    actions=[
                        "bedrock-agentcore:GetWorkloadAccessToken",
                        "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                    ],
                    resources=directory,
                )
            )
            gw_role.add_to_policy(
                iam.PolicyStatement(
                    sid="TokenExchangeVault",
                    actions=["bedrock-agentcore:GetResourceOauth2Token"],
                    resources=[
                        arn("token-vault/default"),
                        arn("token-vault/default/oauth2credentialprovider/*"),
                        *directory,
                    ],
                )
            )
            gw_role.add_to_policy(
                iam.PolicyStatement(
                    sid="TokenExchangeProviderSecrets",
                    actions=["secretsmanager:GetSecretValue"],
                    resources=[
                        (
                            f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:"
                            "bedrock-agentcore-identity!default/oauth2/*"
                        )
                    ],
                )
            )
        if enable_web_search:
            # The Web Search built-in connector authorizes per request against a
            # service-owned tool ARN (account id is literally "aws"). Both actions
            # are documented requirements for the gateway service role.
            gw_role.add_to_policy(
                iam.PolicyStatement(
                    sid="InvokeGateway",
                    actions=["bedrock-agentcore:InvokeGateway"],
                    resources=[
                        f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:gateway/*"
                    ],
                )
            )
            gw_role.add_to_policy(
                iam.PolicyStatement(
                    sid="InvokeWebSearch",
                    actions=["bedrock-agentcore:InvokeWebSearch"],
                    resources=[
                        f"arn:aws:bedrock-agentcore:{self.region}:aws:tool/web-search.v1"
                    ],
                )
            )

        # ── Optional: egress Lambda interceptor + Bedrock Guardrail (control-library) ──
        # Applies a Bedrock Guardrail to Gateway REQUEST/RESPONSE for PII masking and
        # prompt-injection blocking. Built before the Gateway so it can reference the ARN.
        # Use-case check modules (gateway.interceptor_extensions) ride in the
        # same function and run before the guardrail, so the interceptor exists
        # when either is configured.
        self._interceptor_fn = None
        interceptor_configurations = None
        extensions = interceptor_extensions or []
        guardrail = None
        if enable_egress_filter:
            guardrail_cfg = baseline_policy(egress_pii_entities, floor=guardrail_floor)
            guardrail = bedrock.CfnGuardrail(
                self,
                "EgressGuardrail",
                name=f"{prefix}-egress-guardrail",
                description="Egress guardrail for AgentCore Gateway (PII + prompt injection).",
                blocked_input_messaging=guardrail_cfg["BlockedInputMessaging"],
                blocked_outputs_messaging=guardrail_cfg["BlockedOutputsMessaging"],
            )
            # Policy blocks come straight from the control-library artifact (CFN shape).
            for cfn_key in (
                "ContentPolicyConfig",
                "SensitiveInformationPolicyConfig",
                "TopicPolicyConfig",
                "WordPolicyConfig",
                "ContextualGroundingPolicyConfig",
            ):
                if cfn_key in guardrail_cfg:
                    guardrail.add_property_override(cfn_key, guardrail_cfg[cfn_key])

        if enable_egress_filter or extensions:
            interceptor_role = iam.Role(
                self,
                "InterceptorRole",
                assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
                managed_policies=[
                    iam.ManagedPolicy.from_aws_managed_policy_name(
                        "service-role/AWSLambdaBasicExecutionRole"
                    ),
                ],
            )
            interceptor_env = {}
            if guardrail is not None:
                interceptor_role.add_to_policy(
                    iam.PolicyStatement(
                        actions=["bedrock:ApplyGuardrail"],
                        resources=[guardrail.attr_guardrail_arn],
                    )
                )
                interceptor_env = {
                    "GUARDRAIL_ID": guardrail.attr_guardrail_id,
                    "GUARDRAIL_VERSION": published_version(
                        guardrail, guardrail_cfg
                    ).attr_version,
                }
                if egress_skip_tools:
                    interceptor_env["EGRESS_SKIP_TOOLS"] = json.dumps(egress_skip_tools)
                if egress_request_action != "mask":
                    interceptor_env["EGRESS_REQUEST_ACTION"] = egress_request_action
            code_dir = "tools/egress_interceptor"
            if extensions:
                code_dir = self._bundle_extensions(code_dir, extensions)
                interceptor_env["INTERCEPTOR_EXTENSIONS"] = json.dumps(
                    [
                        {
                            "package": os.path.basename(e["path"].rstrip("/")),
                            "target_prefixes": e["target_prefixes"],
                            "request_headers": e.get("request_headers", False),
                        }
                        for e in extensions
                    ],
                    separators=(",", ":"),
                )
                interceptor_env["SSM_PREFIX"] = f"/{project_name}/{environment}"
                # The role CloudFormation deploys this app's stacks as, so an
                # extension can tell a stack-written parameter from a hand-written one.
                exec_role = getattr(
                    self.synthesizer, "cloud_formation_execution_role_arn", None
                )
                if exec_role:
                    interceptor_env["CFN_EXEC_ROLE_ARN"] = cdk.Fn.sub(exec_role)

            self._interceptor_fn = _lambda.Function(
                self,
                "EgressInterceptor",
                function_name=f"{prefix}-egress-interceptor",
                runtime=_lambda.Runtime.PYTHON_3_13,
                handler="handler.handler",
                code=_lambda.Code.from_asset(code_dir),
                architecture=_lambda.Architecture.ARM_64,
                timeout=cdk.Duration.seconds(30),
                role=interceptor_role,
                environment=interceptor_env,
            )
            # The Gateway invokes the interceptor as its own role; the resource
            # permission added below does not stand in for this grant.
            gw_role.add_to_policy(
                iam.PolicyStatement(
                    actions=["lambda:InvokeFunction"],
                    resources=[self._interceptor_fn.function_arn],
                )
            )
            if extensions:
                # Use cases grant this role access to their own tables and
                # parameters by ARN; app.py holds those grants to the same rule
                # as the Gateway role's.
                ssm.StringParameter(
                    self,
                    "SSMGateway-interceptor-role-arn",
                    parameter_name=f"/{project_name}/{environment}/gateway/interceptor-role-arn",
                    string_value=interceptor_role.role_arn,
                )

            interceptor_configurations = [
                agentcore.CfnGateway.GatewayInterceptorConfigurationProperty(
                    # Extensions check requests only; responses need the guardrail.
                    interception_points=(
                        ["REQUEST", "RESPONSE"]
                        if guardrail is not None
                        else ["REQUEST"]
                    ),
                    interceptor=agentcore.CfnGateway.InterceptorConfigurationProperty(
                        lambda_=agentcore.CfnGateway.LambdaInterceptorConfigurationProperty(
                            arn=self._interceptor_fn.function_arn,
                        ),
                    ),
                    input_configuration=agentcore.CfnGateway.InterceptorInputConfigurationProperty(
                        pass_request_headers=any(
                            e.get("request_headers") for e in extensions
                        ),
                    ),
                ),
            ]

        # ── Optional: AgentCore Cedar policy engine (control-library) ──
        # Deterministic, identity-aware authorization of tool calls. Cedar is implicit
        # default-deny (no permit means deny), so only explicit read permits are shipped —
        # a blanket forbid would override every permit. Runs in LOG_ONLY by default
        # (evaluate + log, no block); set cedar_mode=ENFORCE once decision logs are validated.
        policy_engine_configuration = None
        policy_engine = None
        if enable_cedar:
            policy_engine = agentcore.CfnPolicyEngine(
                self,
                "PolicyEngine",
                name=f"{project_name}_{environment}_policy_engine".replace("-", "_"),
                description=f"Cedar policy engine for {project_name}/{environment} gateway.",
            )
            # Attaching the engine makes the Gateway read it as itself: the
            # update calls GetPolicyEngine and probes AuthorizeAction with the
            # Gateway's role, and fails outright if either is denied. The
            # Gateway half cannot name this Gateway's ARN without a cycle.
            gw_role.add_to_policy(
                iam.PolicyStatement(
                    actions=["bedrock-agentcore:GetPolicyEngine"],
                    resources=[policy_engine.attr_policy_engine_arn],
                )
            )
            gw_role.add_to_policy(
                iam.PolicyStatement(
                    actions=[
                        "bedrock-agentcore:AuthorizeAction",
                        "bedrock-agentcore:PartiallyAuthorizeActions",
                    ],
                    resources=[
                        policy_engine.attr_policy_engine_arn,
                        f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:gateway/*",
                    ],
                )
            )
            policy_engine_configuration = (
                agentcore.CfnGateway.GatewayPolicyEngineConfigurationProperty(
                    arn=policy_engine.attr_policy_engine_arn,
                    mode=cedar_mode,
                )
            )

        # ── Use-case clients (client_credentials) ──
        # Created here rather than in the auth stack so that enabling or
        # disabling a use case changes its clients and this allow-list in one
        # stack update: no export for a removed client to block, and no SSM
        # value for the Gateway to read before it exists. Each use case gets
        # its own resource server, so its tokens never carry agentcore/invoke.
        for uc_name, clients in sorted((use_case_clients or {}).items()):
            if not user_pool_id:
                raise ValueError("use-case gateway clients need the user pool id")
            pool = cognito.UserPool.from_user_pool_id(
                self, f"UserPool-{uc_name}", user_pool_id
            )
            scope_names = sorted({s for scopes in clients.values() for s in scopes})
            resource_server = cognito.UserPoolResourceServer(
                self,
                f"UseCaseRS-{uc_name}",
                user_pool=pool,
                identifier=f"uc-{uc_name}",
                scopes=[
                    cognito.ResourceServerScope(
                        scope_name=s, scope_description=f"{uc_name} {s}"
                    )
                    for s in scope_names
                ],
            )
            for client_name, scopes in sorted(clients.items()):
                client = pool.add_client(
                    f"UseCaseClient-{uc_name}-{client_name}",
                    user_pool_client_name=f"{prefix}-uc-{uc_name}-{client_name}",
                    generate_secret=True,
                    access_token_validity=cdk.Duration.minutes(60),
                    auth_flows=cognito.AuthFlow(user_password=False, user_srp=False),
                    o_auth=cognito.OAuthSettings(
                        flows=cognito.OAuthFlows(client_credentials=True),
                        scopes=[
                            cognito.OAuthScope.custom(f"uc-{uc_name}/{s}")
                            for s in scopes
                        ],
                    ),
                )
                client.node.add_dependency(resource_server)
                secret = secretsmanager.Secret(
                    self,
                    f"UseCaseClientSecret-{uc_name}-{client_name}",
                    description=f"{uc_name} {client_name} client secret",
                    secret_string_value=client.user_pool_client_secret,
                )
                base = f"/{project_name}/{environment}/gateway/use-case-clients/{uc_name}/{client_name}"
                for key, value in (
                    ("client-id", client.user_pool_client_id),
                    ("client-secret-name", secret.secret_name),
                ):
                    ssm.StringParameter(
                        self,
                        f"SSMUseCaseClient-{uc_name}-{client_name}-{key}",
                        parameter_name=f"{base}/{key}",
                        string_value=value,
                    )
                cognito_allowed_clients.append(client.user_pool_client_id)

        # Direct mode: the same parameters for app registrations the operator
        # created in the IdP. The Gateway already admits them by audience, so
        # nothing is added to its authorizer; Cedar tells them apart.
        for uc_name, apps in sorted((use_case_client_apps or {}).items()):
            for client_name, app in sorted(apps.items()):
                base = f"/{project_name}/{environment}/gateway/use-case-clients/{uc_name}/{client_name}"
                for key, value in (
                    ("client-id", app["client_id"]),
                    ("client-secret-name", app["client_secret_name"]),
                ):
                    ssm.StringParameter(
                        self,
                        f"SSMUseCaseClient-{uc_name}-{client_name}-{key}",
                        parameter_name=f"{base}/{key}",
                        string_value=value,
                    )

        # Gateway
        gateway_props = {
            "name": gw_name,
            "role_arn": gw_role.role_arn,
            "authorizer_type": "CUSTOM_JWT",
            "protocol_type": "MCP",
            "authorizer_configuration": custom_jwt_authorizer(
                cognito_issuer_url, cognito_allowed_clients, allowed_audience
            ),
            "protocol_configuration": {
                "mcp": {"supportedVersions": ["2025-03-26", "2025-06-18"]},
            },
            "interceptor_configurations": interceptor_configurations,
            "policy_engine_configuration": policy_engine_configuration,
        }
        if debug_exceptions:
            gateway_props["exception_level"] = "DEBUG"
        if kms_key_arn:
            gateway_props["kms_key_arn"] = kms_key_arn
            gw_role.add_to_policy(
                iam.PolicyStatement(
                    actions=["kms:Decrypt", "kms:DescribeKey", "kms:GenerateDataKey"],
                    resources=[kms_key_arn],
                )
            )
        self._gateway = agentcore.CfnGateway(self, "Gateway", **gateway_props)
        # role_arn only orders the Gateway after the Role, not after its
        # DefaultPolicy (KMS / Lambda / web-search grants). A construct-level
        # dependency covers every CfnResource under the role, including policies
        # added later, so the Gateway is never created before its permissions.
        # The engine grants are checked when the engine attaches, and the
        # interceptor grant on the first request: both land before the Gateway.
        self._gateway.node.add_dependency(gw_role)

        # Allow the Gateway to invoke the interceptor Lambda (after gateway ARN is known).
        if self._interceptor_fn is not None:
            self._interceptor_fn.add_permission(
                "AgentCoreInvokeInterceptor",
                principal=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
                source_arn=self._gateway.attr_gateway_arn,
            )

        # Lambda tool role
        lambda_role = iam.Role(
            self,
            "LambdaToolRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
            ],
        )

        # Deploy Lambda tools and register as gateway targets
        for tool_name, config in (tool_configs or {}).items():
            fn = _lambda.Function(
                self,
                f"Tool-{tool_name}",
                function_name=f"{prefix}-tool-{tool_name}",
                runtime=_lambda.Runtime.PYTHON_3_13,
                handler="handler.handler",
                code=_lambda.Code.from_asset(config["source_dir"]),
                architecture=_lambda.Architecture.ARM_64,
                timeout=cdk.Duration.seconds(30),
                role=lambda_role,
                environment=config.get("env_vars", {}),
            )
            fn.add_permission(
                f"AgentCoreInvoke-{tool_name}",
                principal=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
                source_arn=self._gateway.attr_gateway_arn,
            )

            target = agentcore.CfnGatewayTarget(
                self,
                f"Target-{tool_name}",
                name=tool_name,
                gateway_identifier=self._gateway.attr_gateway_identifier,
                credential_provider_configurations=[
                    {"credentialProviderType": "GATEWAY_IAM_ROLE"},
                ],
                target_configuration={
                    "mcp": {},
                },
            )
            # Override the target configuration directly — CDK strips the "lambda" key
            # because it's a Python reserved word and the L1 property mapping doesn't handle it
            target.add_property_override(
                "TargetConfiguration.Mcp.Lambda",
                {
                    "LambdaArn": fn.function_arn,
                    "ToolSchema": {
                        "InlinePayload": config.get("tool_schema", []),
                    },
                },
            )

        # Web Search built-in connector target: Amazon-operated search index
        # exposed as a standard MCP tool. No Lambda, no API key, and queries
        # never leave AWS. Region-gated in app.py (us-east-1 / eu-west-1 /
        # ap-northeast-1 at launch).
        if enable_web_search:
            web_search = agentcore.CfnGatewayTarget(
                self,
                "Target-web-search",
                name="web-search",
                gateway_identifier=self._gateway.attr_gateway_identifier,
                credential_provider_configurations=[
                    {"credentialProviderType": "GATEWAY_IAM_ROLE"},
                ],
                target_configuration={"mcp": {}},
            )
            # Same override trick as the Lambda targets: the L1 property mapping
            # predates the connector target type, so set it directly. No version
            # pin — the connector's default version tracks Amazon's improvements
            # (and dated pins rot; see the model-ID lesson).
            web_search.add_property_override(
                "TargetConfiguration.Mcp.Connector",
                {
                    "Source": {"ConnectorId": "web-search"},
                    "Configurations": [{"Name": "WebSearch", "ParameterValues": {}}],
                },
            )

        # One CfnPolicy per Cedar artifact in the control-library, created after
        # the Gateway and its targets: CreatePolicy needs the statement pinned to
        # the Gateway's ARN, and validates actions against the attached targets.
        if policy_engine is not None:
            cedar_controls = {
                "PermitReadTools": "cedar.gateway-default.permit-read",
            }
            targets = [
                c
                for c in self.node.children
                if isinstance(c, agentcore.CfnGatewayTarget)
            ]
            for construct_id, control_id in cedar_controls.items():
                statement = load_control_text(
                    control_id, {"gateway_arn": self._gateway.attr_gateway_arn}
                )
                policy = agentcore.CfnPolicy(
                    self,
                    construct_id,
                    # Unique per account and Region, not per engine.
                    name=f"{project_name}_{environment}_{construct_id}".replace(
                        "-", "_"
                    ),
                    policy_engine_id=policy_engine.attr_policy_engine_id,
                    definition=agentcore.CfnPolicy.PolicyDefinitionProperty(
                        cedar=agentcore.CfnPolicy.CedarPolicyProperty(
                            statement=statement
                        ),
                    ),
                )
                for target in targets:
                    policy.add_resource_dependency(target)

        # SSM
        ssm.StringParameter(
            self,
            "SSMGatewayUrl",
            parameter_name=f"/{project_name}/{environment}/gateway/url",
            string_value=self._gateway.attr_gateway_url,
        )
        # What a use case needs to attach to this Gateway from its own stack:
        # targets (id), grants on its own resources (role ARN; app.py refuses
        # any other grant), and Cedar policies (engine id, only when enabled).
        published = {
            "id": self._gateway.attr_gateway_identifier,
            "role-arn": gw_role.role_arn,
        }
        if policy_engine is not None:
            published["policy-engine-id"] = policy_engine.attr_policy_engine_id
        for key, value in published.items():
            ssm.StringParameter(
                self,
                f"SSMGateway-{key}",
                parameter_name=f"/{project_name}/{environment}/gateway/{key}",
                string_value=value,
            )

        cdk.CfnOutput(self, "GatewayUrl", value=self._gateway.attr_gateway_url)
        cdk.CfnOutput(self, "GatewayArn", value=self._gateway.attr_gateway_arn)
        cdk.CfnOutput(self, "GatewayId", value=self._gateway.attr_gateway_identifier)

    @staticmethod
    def _bundle_extensions(handler_dir: str, extensions: list[dict]) -> str:
        """The interceptor asset with each extension package under extensions/."""
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        staged = tempfile.mkdtemp(prefix="interceptor-")
        # A local __pycache__ or tests/ would ship and change the asset hash.
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "tests")
        shutil.copytree(
            os.path.join(repo_root, handler_dir),
            staged,
            dirs_exist_ok=True,
            ignore=ignore,
        )
        os.makedirs(os.path.join(staged, "extensions"))
        open(os.path.join(staged, "extensions", "__init__.py"), "w").close()
        for ext in extensions:
            src = os.path.join(repo_root, ext["path"])
            if not os.path.isfile(os.path.join(src, "__init__.py")):
                raise ValueError(
                    f"interceptor extension {ext['path']!r} is not a Python package "
                    "(a directory with __init__.py exposing check(body, headers))"
                )
            shutil.copytree(
                src,
                os.path.join(staged, "extensions", os.path.basename(src.rstrip("/"))),
                ignore=ignore,
            )
        return staged

    @property
    def gateway_url(self) -> str:
        return self._gateway.attr_gateway_url

    @property
    def gateway_arn(self) -> str:
        return self._gateway.attr_gateway_arn
