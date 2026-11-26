# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The SAP layer: one AWS for SAP MCP Server runtime and Gateway target per SAP system.

Its own stack, `<prefix>-layer-sap`, deployed after the platform and before any use
case, so a second SAP use case builds on it instead of on the first. Built here
rather than from AWS's published CloudFormation template: the image is pinned, the
role is scoped to that system's own secret and provider, and the Gateway reaches the
runtime with its IAM role, so no Cognito pool or client secret sits between them.

Attaches to the platform by id (docs/PLATFORM_INTERFACE.md): a target on the shared
Gateway, an invoke grant on the Gateway role for this stack's own runtimes only, and,
when `security.networking` is on, the platform's private subnets. It holds no Cedar
statement: each use case permits its own clients (`sap_layer.access`).

What it publishes under `layers/sap/` is the interface use cases read at run time:
`systems` (target, flow, type, writes, tools, runtime_arn per system),
`systems/<system>/connection` for the systems some use case may call directly,
`egress-security-group-id` when the platform VPC is on, and `check`, the settings of
the layer's interceptor check (`sap_check/`), which refuses any SAP call no use case
has claimed. When some system has `senders`: `bus-arn`, the `sap-events` bus SAP puts
its events on, `systems/<system>/sender-role-arn`, and `ingest`, what verify checks.

A system on flow `obo` is the exception to SigV4: its runtime trusts Entra, and its
target exchanges the caller's Entra token on every call (two OBO hops, the Gateway's
and the server's), so SAP sees the person. It needs the platform's direct Entra mode,
since only then is the Gateway's inbound token an Entra one.

The runtime, role, resource policy, target, providers and security group were first
built by the business-operations use case and moved here by CloudFormation stack
refactor, which moves a resource only with its logical id and properties unchanged:
keep both, descriptions included (`names.LEGACY_LABEL`).
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote, urlparse

import aws_cdk as cdk
import sap_layer
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_ssm as ssm
from constructs import Construct
from sap_layer import access, names
from sap_layer.config import (
    MCP_FLOW,
    SapConfig,
    SapSystem,
    image_registry_account,
)
from sap_layer.config import load as load_config

from infra_utils import log_retention
from infra_utils.use_case_context import UseCaseContext

# tools/list of the pinned image, for an obo target: the Gateway cannot list it
# itself, since listing needs a user token and target creation has none.
SAP_TOOLS_FILE = Path(sap_layer.__file__).with_name("sap_tools.json")
# Forwarded to the server; a use case's interceptor check may tie a write to its run
# by the first.
AUDIT_HEADERS = ["x-audit-correlation-id", "x-audit-initiator", "x-audit-trigger"]


#: Every data-plane action a SigV4 runtime takes. Only the Gateway's role may use one.
RUNTIME_INVOKE_ACTIONS = [
    "bedrock-agentcore:InvokeAgentRuntime",
    "bedrock-agentcore:InvokeAgentRuntimeForUser",
    "bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream",
    "bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStreamForUser",
    "bedrock-agentcore:InvokeAgentRuntimeCommand",
    "bedrock-agentcore:InvokeAgentRuntimeCommandShell",
    "bedrock-agentcore:StopRuntimeSession",
]


def gateway_only_policy(runtime_arn: str, gateway_role_arn: str) -> dict:
    """The SAP runtime's resource policy: the Gateway's role, and nobody else.

    AgentCore's documented pattern for a SigV4 runtime fronted by a Gateway. The
    explicit deny overrides any identity-based allow in the account.
    """
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowGatewayRole",
                "Effect": "Allow",
                "Principal": {"AWS": gateway_role_arn},
                "Action": "bedrock-agentcore:InvokeAgentRuntime",
                "Resource": runtime_arn,
            },
            {
                "Sid": "DenyEveryoneElse",
                "Effect": "Deny",
                "Principal": {"AWS": "*"},
                "Action": RUNTIME_INVOKE_ACTIONS,
                "Resource": runtime_arn,
                "Condition": {"ArnNotEquals": {"aws:PrincipalArn": gateway_role_arn}},
            },
        ],
    }


def bus_policy(bus_arn: str, senders: list[tuple[str, str, list[str]]]) -> dict:
    """The `sap-events` bus's resource policy, from each sender's (role ARN, source,
    topics): nobody but the sender roles may put, and each only its own source and its
    use cases' topics.

    Denies only, never an allow. In the bus's own account an allow here would grant
    `PutEvents` by itself and bypass the conditions on the sender role's identity
    policy (seen live: such an allow let the role put any source and any topic). The
    role's own policy is what allows; these denies hold over any allow in the account,
    including one attached to the role later.
    """
    roles = [arn for arn, _, _ in senders]
    statements = [
        {
            "Sid": "DenyEveryoneElse",
            "Effect": "Deny",
            "Principal": "*",
            "Action": "events:PutEvents",
            "Resource": bus_arn,
            "Condition": {"ArnNotEquals": {"aws:PrincipalArn": roles}},
        }
    ]
    for n, (arn, source, topics) in enumerate(senders):
        mine = {"ArnEquals": {"aws:PrincipalArn": arn}}
        if not topics:  # no use case ingests this system: it may put nothing
            statements.append(
                {
                    "Sid": f"DenySender{n}",
                    "Effect": "Deny",
                    "Principal": "*",
                    "Action": "events:PutEvents",
                    "Resource": bus_arn,
                    "Condition": mine,
                }
            )
            continue
        for sid, key, values in (
            (f"DenySender{n}OtherSource", "events:source", [source]),
            (f"DenySender{n}OtherTopic", "events:detail-type", topics),
        ):
            statements.append(
                {
                    "Sid": sid,
                    "Effect": "Deny",
                    "Principal": "*",
                    "Action": "events:PutEvents",
                    "Resource": bus_arn,
                    "Condition": {**mine, "ForAnyValue:StringNotEquals": {key: values}},
                }
            )
    return {"Version": "2012-10-17", "Statement": statements}


def sender_statement(
    bus_arn: str, system: str, topics: list[str]
) -> iam.PolicyStatement:
    """A sender role's one permission: its own `source`, its use cases' `detail-type`s.

    Both keys are arrays on `PutEvents`, and `ForAllValues` alone passes an empty
    set, so each is also required present (`Null: false`).
    """
    return iam.PolicyStatement(
        actions=["events:PutEvents"],
        resources=[bus_arn],
        conditions={
            "ForAllValues:StringEquals": {
                "events:source": [names.event_source(system)],
                "events:detail-type": topics,
            },
            "Null": {"events:source": "false", "events:detail-type": "false"},
        },
    )


def runtime_env(
    system: SapSystem, *, region: str, provider_name: str | None
) -> dict[str, str]:
    """The server's environment for one system: the contract this layer owns.

    A test pins every name.
    Delete is always off, whatever `writes` says.
    """
    env = {
        "MCP_SERVER_SAP_BASE_URL": system.base_url,
        "MCP_SERVER_SAP_SYSTEM": system.type,
        "MCP_SERVER_SAP_OAUTH_FLOW": MCP_FLOW[system.flow],
        "MCP_SERVER_REGION": region,
        "MCP_SERVER_LOG_LEVEL": "INFO",
        "MCP_SERVER_READ_ENABLED": "true",
        # The server refuses WRITE_ENABLED=true with no operation on.
        "MCP_SERVER_WRITE_ENABLED": str(bool(system.writes)).lower(),
        "MCP_SERVER_CREATE_ENABLED": str("create" in system.writes).lower(),
        "MCP_SERVER_UPDATE_ENABLED": str("update" in system.writes).lower(),
        "MCP_SERVER_FUNCTION_IMPORT_ENABLED": str(
            "function_import" in system.writes
        ).lower(),
        "MCP_SERVER_DELETE_ENABLED": "false",
        # Verify SAP's certificate: its chain and the host in base_url.
        "MCP_SERVER_SESSION_VERIFY": "true",
    }
    if system.client:
        env["MCP_SERVER_SAP_CLIENT_NUMBER"] = system.client
    if system.flow == "basic":
        env["MCP_SERVER_BASIC_AUTH_SECRET_NAME"] = system.secret_name
    else:
        # The SAP_ infix: the Reference's table says MCP_SERVER_OAUTH_PROVIDER, its
        # own validation rule 3 and the template say this.
        env["MCP_SERVER_SAP_OAUTH_PROVIDER"] = provider_name
        env["MCP_SERVER_SAP_OAUTH_SCOPES"] = ",".join(system.scopes)
    if system.service_prefixes:
        env["MCP_SERVER_ALLOWED_SERVICE_PREFIXES"] = ",".join(system.service_prefixes)
    # Only off the default (V2), so a V2 system's template is unchanged.
    if system.odata_version == "v4":
        env["MCP_SERVER_ODATA_VERSION"] = "V4"
    if system.catalog_bucket:
        env["MCP_SERVER_CUSTOM_CATALOG_BUCKET"] = system.catalog_bucket
        if system.odata_version != "v2":
            env["MCP_SERVER_CUSTOM_CATALOG_ODATA_VERSION"] = (
                system.odata_version.upper()
            )
    return env


def static_tools(cfg: SapSystem) -> list[dict]:
    """The obo target's tool list: the reads and this system's enabled writes."""
    wanted = {*names.LISTED_READ_TOOLS, *(names.WRITE_TOOLS[w] for w in cfg.writes)}
    tools = [t for t in json.loads(SAP_TOOLS_FILE.read_text()) if t["name"] in wanted]
    missing = wanted - {t["name"] for t in tools}
    if missing:
        raise ValueError(f"{SAP_TOOLS_FILE.name} lacks {sorted(missing)}")
    return tools


class SapLayerStack(cdk.Stack):
    def __init__(
        self, scope: Construct, id: str, *, ctx: UseCaseContext, sap: SapConfig, **kw
    ):
        super().__init__(scope, id, **kw)
        security = ctx.platform.security

        self._ctx = ctx
        self._sap = sap
        # Read even with no system: a stack refactor moving a runtime and target in
        # cannot add the parameters they reference.
        gateway_id = ctx.ssm_value(self, "gateway/id")
        gateway_role = iam.Role.from_role_arn(
            # Not "GatewayRole" or "SapGatewayRole": the policy CDK attaches is named
            # after this id, and other policies on the same shared role have those.
            self,
            "LayerGatewayRole",
            ctx.ssm_value(self, "gateway/role-arn"),
        )
        self._network = (
            self._network_config() if security.networking and sap.systems else None
        )
        for system in sap.obo_direct_systems():
            cdk.Annotations.of(self).add_warning_v2(
                "sap-layer:obo-unattended",
                f"SAP system {system} signs people in on behalf of themselves (flow "
                "obo), and an unattended block lets a use case call it directly as a "
                "service account. What that account reads reaches whoever the use "
                "case lets see it, not who SAP would authorize: give the account a "
                "read-only SAP role.",
            )

        for system, cfg in sap.systems.items():
            target_name = names.sap_target(system)
            credentials = [
                {
                    "credentialProviderType": "GATEWAY_IAM_ROLE",
                    "credentialProvider": {
                        "iamCredentialProvider": {"service": "bedrock-agentcore"}
                    },
                }
            ]
            mcp_server: dict = {"listingMode": "DEFAULT"}
            if cfg.flow == "obo":
                identity = ctx.platform.identity
                if (identity.idp, identity.mode) != ("entra_id", "direct"):
                    raise ValueError(
                        f"SAP system {system}: flow obo needs identity.mode direct "
                        "with idp entra_id; the Gateway exchanges the caller's "
                        "Entra token"
                    )
                runtime = self._runtime(system, cfg)
                provider = self._gateway_provider(system)
                credentials = [
                    {
                        "credentialProviderType": "OAUTH",
                        "credentialProvider": {
                            "oauthCredentialProvider": {
                                "providerArn": provider.attr_credential_provider_arn,
                                "grantType": "TOKEN_EXCHANGE",
                                "scopes": cfg.gateway_scopes,
                            }
                        },
                    }
                ]
                mcp_server = {
                    "mcpToolSchema": {
                        "inlinePayload": json.dumps({"tools": static_tools(cfg)})
                    }
                }
                endpoint = _invocation_url(self.region, self._encoded_arn(runtime))
                depends = [runtime, provider, gateway_role]
            elif cfg.runtime_arn:
                # Bring your own: its owner lets this Gateway's role invoke it. The
                # shared-role guard refuses a grant here on an ARN we do not define.
                endpoint = _invocation_url(self.region, quote(cfg.runtime_arn, safe=""))
                depends: list[Construct] = []
            else:
                runtime = self._runtime(system, cfg)
                # The deny is what holds: an IAM allow on "*" anywhere in the account
                # (a runtime role, an admin) would otherwise reach SAP past the
                # Gateway, its Cedar policies and the interceptor checks.
                gateway_only = agentcore.CfnResourcePolicy(
                    self,
                    f"RuntimePolicy-{system}",
                    resource_arn=runtime.attr_agent_runtime_arn,
                    policy=self.to_json_string(
                        gateway_only_policy(
                            runtime.attr_agent_runtime_arn, gateway_role.role_arn
                        )
                    ),
                )
                gateway_role.add_to_principal_policy(
                    iam.PolicyStatement(
                        actions=["bedrock-agentcore:InvokeAgentRuntime"],
                        resources=[
                            runtime.attr_agent_runtime_arn,
                            cdk.Fn.join("", [runtime.attr_agent_runtime_arn, "/*"]),
                        ],
                    )
                )
                endpoint = _invocation_url(self.region, self._encoded_arn(runtime))
                depends = [runtime, gateway_role, gateway_only]

            target = agentcore.CfnGatewayTarget(
                self,
                f"Target-{system}",
                name=target_name,
                description=f"SAP layer: SAP system {system}",
                gateway_identifier=gateway_id,
                credential_provider_configurations=credentials,
                target_configuration={
                    "mcp": {"mcpServer": {"endpoint": endpoint, **mcp_server}}
                },
                metadata_configuration={"allowedRequestHeaders": AUDIT_HEADERS},
            )
            # The Gateway lists the runtime's tools when the target is created.
            for d in depends:
                target.node.add_dependency(d)

        # app.py tags each stack's resources Component=<its suffix>; the moved ones keep
        # the value their first stack gave them, so a stack refactor finds them unchanged.
        for child in self.node.children:
            if child.node.id.startswith(MOVED):
                cdk.Tags.of(child).add(
                    "Component", names.LEGACY_COMPONENT, priority=200
                )

        self._publish(sap, ctx)

    def _publish(self, sap: SapConfig, ctx: UseCaseContext) -> None:
        """The layer's interface under `layers/sap/`, and its check's settings."""
        root = f"{ctx.ssm_prefix}/layers/{names.NAME}"
        cedar = ctx.platform.security.cedar
        # Always present, so the stack is never empty.
        ssm.StringParameter(
            self,
            "Out-systems",
            parameter_name=f"{root}/systems",
            string_value=self.to_json_string(
                {
                    system: {
                        "target": names.sap_target(system),
                        "flow": cfg.flow,
                        "type": cfg.type,
                        "writes": cfg.writes,
                        "tools": access.target_tools(system, cfg),
                        "runtime_arn": cfg.runtime_arn,
                    }
                    for system, cfg in sap.systems.items()
                }
            ),
        )
        for system in sap.direct_systems():
            cfg = sap.systems[system]
            ssm.StringParameter(
                self,
                f"Out-connection-{system}",
                parameter_name=f"{root}/systems/{system}/connection",
                string_value=json.dumps(
                    {
                        "base_url": cfg.base_url,
                        "client": cfg.client,
                        "secret_name": cfg.direct_secret_name,
                        **cfg.direct_sign_in(),
                    }
                ),
            )
        if self._network and sap.direct_systems():
            ssm.StringParameter(
                self,
                "Out-egress-security-group-id",
                parameter_name=f"{root}/egress-security-group-id",
                string_value=self._network["networkModeConfig"]["securityGroups"][0],
            )
        if not sap.systems:
            return
        settings = ssm.StringParameter(
            self,
            "Out-check",
            parameter_name=f"{root}/check",
            description=access.trusted_description(self),
            string_value=json.dumps(
                {
                    "cedar_enforced": cedar.enabled and cedar.mode == "ENFORCE",
                    "targets": {names.sap_target(s): s for s in sap.systems},
                    # Only these use cases' claims are read, each capped by its row.
                    "ceilings": {
                        uc: {"read": sap.reads(uc), "write": sap.writes(uc)}
                        for uc in sorted(sap.access)
                    },
                },
                sort_keys=True,
            ),
        )
        settings.grant_read(access.interceptor_role(self, ctx))
        self._alert(root, ctx)
        self._events(sap, root)

    def _events(self, sap: SapConfig, root: str) -> None:
        """The `sap-events` bus and one sender role per system with `senders`.

        Each role may put only its system's `source` and, as `detail-type`, only the
        use cases whose `ingest` names that system; the bus policy refuses everyone
        else in the account, administrators included, so only a SAP system's own
        sender can put a `sap.<system>` event. Nothing is built without `senders`.
        """
        systems = sap.sender_systems()
        if not systems:
            return
        name = names.event_bus_name(self._ctx.prefix)
        # Built, not read off the bus: the bus's policy names the roles.
        bus_arn = self.format_arn(
            service="events", resource="event-bus", resource_name=name
        )
        roles = []
        senders = []
        for system in systems:
            topics = sap.topics(system)
            role = iam.Role(
                self,
                f"SapSender-{system}",
                description=f"{self._ctx.prefix} SAP layer: SAP system {system} puts "
                "its events on the sap-events bus",
                assumed_by=iam.CompositePrincipal(
                    *(iam.ArnPrincipal(p) for p in sap.systems[system].senders)
                ),
                max_session_duration=cdk.Duration.hours(1),
                # No use case ingests it yet: the role can put nothing.
                inline_policies={
                    "PutSapEvents": iam.PolicyDocument(
                        statements=[sender_statement(bus_arn, system, topics)]
                    )
                }
                if topics
                else None,
            )
            roles.append(role.role_arn)
            senders.append((role.role_arn, names.event_source(system), topics))
            ssm.StringParameter(
                self,
                f"Out-sender-role-arn-{system}",
                parameter_name=f"{root}/systems/{system}/sender-role-arn",
                string_value=role.role_arn,
            )
        bus = events.CfnEventBus(
            self,
            "SapEvents",
            name=name,
            description="SAP layer: events SAP systems put, routed to use cases by "
            "detail-type",
            policy=bus_policy(bus_arn, senders),
        )
        ssm.StringParameter(
            self,
            "Out-bus-arn",
            parameter_name=f"{root}/bus-arn",
            string_value=bus.attr_arn,
        )
        # What verify checks the live bus, roles and rules against.
        ssm.StringParameter(
            self,
            "Out-ingest",
            parameter_name=f"{root}/ingest",
            string_value=json.dumps(
                {
                    "systems": {
                        s: {"senders": sap.systems[s].senders, "topics": sap.topics(s)}
                        for s in systems
                    },
                    "ceilings": {
                        uc: sap.ingest(uc)
                        for uc in sorted(sap.access)
                        if sap.ingest(uc)
                    },
                },
                sort_keys=True,
            ),
        )

    def _alert(self, root: str, ctx: UseCaseContext) -> None:
        """Tell the platform's ops topic when anyone but CloudFormation writes under `root`.

        CloudTrail management events reach EventBridge only while a trail logs them
        (the platform's security stack, or an organization trail). The message names
        the parameter, the call and the principal, never a value.
        """
        topic = ctx.ssm_value(self, "ops-topic/arn")
        # An alert the function fails to deliver after Lambda's retries lands here, and
        # the alarm says so on the same topic. Not also the rule target's DLQ: under
        # production the queue is on the platform CMK, which EventBridge could use only
        # through a key-policy statement this layer cannot add; EventBridge itself
        # retries an invoke for 24 hours.
        dlq = sqs.Queue(
            self,
            "WriteAlertDlq",
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            retention_period=cdk.Duration.days(14),
        )
        alarm = cloudwatch.Alarm(
            self,
            "WriteAlertUndelivered",
            alarm_description="SAP layer: a write alert could not be delivered; "
            "read the WriteAlertDlq queue and CloudTrail",
            metric=dlq.metric_approximate_number_of_messages_visible(
                period=cdk.Duration.minutes(5)
            ),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        alarm.add_alarm_action(
            cw_actions.SnsAction(sns.Topic.from_topic_arn(self, "OpsTopic", topic))
        )
        fn = _lambda.Function(
            self,
            "WriteAlert",
            dead_letter_queue=dlq,
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="index.handler",
            code=_lambda.Code.from_asset(
                str(Path(__file__).with_name("write_alert")), exclude=["__pycache__"]
            ),
            timeout=cdk.Duration.seconds(10),
            environment={"TOPIC_ARN": topic},
            log_group=log_retention.log_group(
                self, "WriteAlertLogs", ctx.log_retention_days, ctx.production
            ),
        )
        fn.add_to_role_policy(
            iam.PolicyStatement(actions=["sns:Publish"], resources=[topic])
        )
        if ctx.production:
            # The topic and the DLQ are encrypted with the platform CMK there.
            key_arn = ctx.ssm_value(self, "security/kms-key-arn")
            cfn_dlq = dlq.node.default_child
            cfn_dlq.sqs_managed_sse_enabled = None
            cfn_dlq.kms_master_key_id = key_arn
            fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["kms:GenerateDataKey*", "kms:Decrypt"],
                    resources=[key_arn],
                    conditions={
                        "StringEquals": {
                            "kms:ViaService": [
                                f"sns.{self.region}.amazonaws.com",
                                f"sqs.{self.region}.amazonaws.com",
                            ]
                        }
                    },
                )
            )
        events.Rule(
            self,
            "WriteOutsideCloudFormation",
            description="SAP layer: a claim or setting written by anyone but CloudFormation",
            event_pattern=events.EventPattern(
                **write_alert_pattern(root, cloudformation_writer(self))
            ),
            targets=[targets.LambdaFunction(fn)],
        )

    def _encoded_arn(self, runtime: agentcore.CfnRuntime) -> str:
        """The runtime ARN, URL-encoded. Each token is its own element: quote()
        on a string holding one would encode the token's placeholder."""
        return cdk.Fn.join(
            "",
            [
                "arn%3A",
                self.partition,
                "%3Abedrock-agentcore%3A",
                self.region,
                "%3A",
                self.account,
                "%3Aruntime%2F",
                runtime.attr_agent_runtime_id,
            ],
        )

    def _gateway_provider(self, system: str) -> agentcore.CfnOAuth2CredentialProvider:
        """Hop one of obo: the platform's own Entra app, exchanging the caller's
        token for one addressed to this system's server. The secret is the one
        the platform's direct mode already holds (a plain string, so inline)."""
        project, environment = self._ctx.project, self._ctx.environment
        identity = self._ctx.platform.identity
        name = names.sap_gateway_provider_name(project, environment, system)
        provider = agentcore.CfnOAuth2CredentialProvider(
            self,
            f"GatewayProvider-{system}",
            name=name,
            credential_provider_vendor="MicrosoftOauth2",
            oauth2_provider_config_input={
                "microsoftOauth2ProviderConfig": {
                    "clientId": identity.client_id,
                    "tenantId": identity.tenant_id,
                    "clientSecret": cdk.SecretValue.secrets_manager(
                        identity.client_secret_name
                    ).unsafe_unwrap(),
                }
            },
        )

        # The Gateway role's grants for the exchange are the platform's (its
        # gateway stack, under identity.mode direct): that role serves every use
        # case and layer, so app.py refuses a vault-wide grant on it.
        return provider

    def _network_config(self) -> dict:
        """Platform private subnets, and a group of ours that reaches SAP's ports only."""
        sg = ec2.CfnSecurityGroup(
            self,
            "SapSecurityGroup",
            group_description=f"{names.LEGACY_LABEL}: AWS for SAP MCP server egress to SAP",
            vpc_id=self._ctx.ssm_value(self, "networking/vpc-id"),
            security_group_egress=[
                {
                    "ipProtocol": "tcp",
                    "fromPort": port,
                    "toPort": port,
                    "cidrIp": "0.0.0.0/0",
                    "description": f"HTTPS to SAP on port {port}",
                }
                for port in sorted(
                    {443}
                    | {
                        # A direct caller fetches a token flow's token itself, with
                        # its own (`unattended`) settings or the server's.
                        urlparse(url).port or 443
                        for s in self._sap.systems.values()
                        for url in (
                            s.base_url,
                            s.token_url,
                            s.discovery_url,
                            s.direct_sign_in().get("token_url"),
                            s.direct_sign_in().get("discovery_url"),
                        )
                        if url
                    }
                )
            ],
        )
        return {
            "networkMode": "VPC",
            "networkModeConfig": {
                "subnets": cdk.Fn.split(
                    ",", self._ctx.ssm_value(self, "networking/private-subnet-ids")
                ),
                "securityGroups": [sg.attr_group_id],
            },
        }

    def _runtime(self, system: str, cfg: SapSystem) -> agentcore.CfnRuntime:
        project, environment = self._ctx.project, self._ctx.environment
        runtime_name = names.sap_runtime_name(project, environment, system)

        def arn(service: str, resource: str, account: str | None = None) -> str:
            acct = self.account if account is None else account
            return f"arn:{self.partition}:{service}:{self.region}:{acct}:{resource}"

        registry = image_registry_account(self.region)
        role = iam.Role(
            self,
            f"Role-{system}",
            # The image's repository admits a pull only by a role with this name.
            role_name=names.sap_role_name(project, environment, system),
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnLike": {"aws:SourceArn": arn("bedrock-agentcore", "*")},
                },
            ),
            description=f"{names.LEGACY_LABEL}: AWS for SAP MCP server for {system}",
        )
        secret = arn("secretsmanager", f"secret:{cfg.secret_name}-??????")
        workload = [
            arn("bedrock-agentcore", "workload-identity-directory/default"),
            arn(
                "bedrock-agentcore",
                f"workload-identity-directory/default/workload-identity/{runtime_name}-*",
            ),
        ]
        statements = [
            iam.PolicyStatement(
                actions=["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
                resources=[arn("ecr", "repository/aws-sap-mcp", registry)],
            ),
            # These take no resource ARN.
            iam.PolicyStatement(
                actions=[
                    "ecr:GetAuthorizationToken",
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords",
                ],
                resources=["*"],
            ),
            iam.PolicyStatement(
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={
                    "StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}
                },
            ),
            iam.PolicyStatement(
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                resources=[
                    arn(
                        "logs",
                        f"log-group:/aws/bedrock-agentcore/runtimes/{runtime_name}-*",
                    )
                ],
            ),
            iam.PolicyStatement(
                actions=["logs:DescribeLogGroups", "logs:DescribeLogStreams"],
                resources=[arn("logs", "log-group:*")],
            ),
            iam.PolicyStatement(
                actions=[
                    "bedrock-agentcore:GetWorkloadAccessToken",
                    "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                ],
                resources=workload,
            ),
            iam.PolicyStatement(
                actions=[
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:DescribeSecret",
                ],
                resources=[secret],
            ),
        ]

        if cfg.catalog_bucket:
            statements.append(
                iam.PolicyStatement(
                    actions=["s3:GetObject"],
                    resources=[
                        f"arn:{self.partition}:s3:::{cfg.catalog_bucket}/catalog.json"
                    ],
                )
            )

        provider_name = None
        provider = None
        if cfg.flow != "basic":
            provider_name = names.sap_provider_name(project, environment, system)
            provider = self._provider(system, cfg, provider_name)
            statements.append(
                iam.PolicyStatement(
                    actions=[
                        "bedrock-agentcore:GetResourceOauth2Token",
                        "bedrock-agentcore:GetOauth2CredentialProvider",
                    ],
                    resources=[
                        arn("bedrock-agentcore", "token-vault/default"),
                        provider.attr_credential_provider_arn,
                        *workload,
                    ],
                )
            )

        # Not "RolePolicy-<system>": the policy's name on the role is its logical id,
        # and the copy the use case's stack left attached (retained) has that one.
        policy = iam.Policy(self, f"LayerRolePolicy-{system}", statements=statements)
        policy.attach_to_role(role)

        runtime = agentcore.CfnRuntime(
            self,
            f"Runtime-{system}",
            agent_runtime_name=runtime_name,
            description=f"{names.LEGACY_LABEL}: AWS for SAP MCP server, SAP system {system}",
            agent_runtime_artifact={
                "containerConfiguration": {
                    "containerUri": f"{registry}.dkr.ecr.{self.region}"
                    f".amazonaws.com/aws-sap-mcp{self._sap.image_ref}"
                }
            },
            role_arn=role.role_arn,
            protocol_configuration="MCP",
            # No JWT authorizer: the runtime takes SigV4, and its resource policy
            # (gateway_only_policy) refuses every principal but the Gateway's role.
            # Under obo it takes an Entra token addressed to its own app, which
            # only the Gateway's exchange produces (when the apps are distinct).
            authorizer_configuration=self._entra_authorizer(cfg)
            if cfg.flow == "obo"
            else None,
            network_configuration=self._network or {"networkMode": "PUBLIC"},
            environment_variables=runtime_env(
                cfg, region=self.region, provider_name=provider_name
            ),
        )
        # The server checks its secret and provider at start; the role must work first.
        runtime.node.add_dependency(policy)
        if provider is not None:
            runtime.node.add_dependency(provider)
        # Not a RuntimeStack, so its log group needs the same retention set here.
        from stacks.runtime_stack import runtime_log_retention

        runtime_log_retention(
            self,
            f"RuntimeLogRetention-{system}",
            runtime.attr_agent_runtime_id,
            self._ctx.log_retention_days,
            self._ctx.production,
        )
        return runtime

    def _entra_authorizer(self, cfg: SapSystem) -> dict:
        """Tokens for this server's app that the Gateway's exchange (hop one) minted.

        The audience alone admits any tenant client consented to the server app's
        scope, straight past the Gateway, Cedar and both checks. Entra puts the
        exchanging app in `azp` (v2 tokens; the v2 issuer refuses v1 ones), and hop
        one is the platform's own app, so `azp` is pinned to it. Entra tokens carry
        no `client_id`, which is all `allowedClients` reads."""
        identity = self._ctx.platform.identity
        return {
            "customJwtAuthorizer": {
                "discoveryUrl": f"https://login.microsoftonline.com/{identity.tenant_id}"
                "/v2.0/.well-known/openid-configuration",
                "allowedAudience": [_secret_field(cfg.secret_name, "clientId")],
                "customClaims": [
                    {
                        "inboundTokenClaimName": "azp",
                        "inboundTokenClaimValueType": "STRING",
                        "authorizingClaimMatchValue": {
                            "claimMatchOperator": "EQUALS",
                            "claimMatchValue": {"matchValueString": identity.client_id},
                        },
                    }
                ],
            }
        }

    def _provider(
        self, system: str, cfg: SapSystem, name: str
    ) -> agentcore.CfnOAuth2CredentialProvider:
        """The token-vault provider for an m2m or obo flow, reading the system's own secret."""
        secret = {
            "clientId": _secret_field(cfg.secret_name, "clientId"),
            "clientSecretSource": "EXTERNAL",
            "clientSecretConfig": {
                "secretId": cfg.secret_name,
                "jsonKey": "clientSecret",
            },
        }
        if cfg.flow == "obo":
            # Hop two: the server's app exchanges the Gateway's token for SAP's.
            return agentcore.CfnOAuth2CredentialProvider(
                self,
                f"Provider-{system}",
                name=name,
                credential_provider_vendor="MicrosoftOauth2",
                oauth2_provider_config_input={
                    "microsoftOauth2ProviderConfig": {
                        **secret,
                        "tenantId": self._ctx.platform.identity.tenant_id,
                    }
                },
            )
        if cfg.flow == "m2m-sap":
            discovery = {
                "authorizationServerMetadata": {
                    "issuer": cfg.token_url,
                    "tokenEndpoint": cfg.token_url,
                    # Required by the shape; client credentials never visits it.
                    "authorizationEndpoint": cfg.token_url,
                }
            }
        else:
            discovery = {"discoveryUrl": cfg.discovery_url}
        return agentcore.CfnOAuth2CredentialProvider(
            self,
            f"Provider-{system}",
            name=name,
            credential_provider_vendor="CustomOauth2",
            oauth2_provider_config_input={
                "customOauth2ProviderConfig": {
                    "oauthDiscovery": discovery,
                    **secret,
                    # SAP SOAUTH2 wants the client in the Authorization header.
                    "clientAuthenticationMethod": "CLIENT_SECRET_BASIC",
                }
            },
        )


def _secret_field(secret_name: str, key: str) -> str:
    # A client id is not a secret; a dynamic reference keeps one source of truth.
    return f"{{{{resolve:secretsmanager:{secret_name}:SecretString:{key}}}}}"


def _invocation_url(region: str, encoded_arn: str) -> str:
    return cdk.Fn.join(
        "",
        [
            f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/",
            encoded_arn,
            "/invocations?qualifier=DEFAULT",
        ],
    )


#: The calls that change a parameter, and who may make them under the layer's prefix.
WRITE_CALLS = [
    "PutParameter",
    "DeleteParameter",
    "DeleteParameters",
    "LabelParameterVersion",
    "UnlabelParameterVersion",
]


def cloudformation_writer(stack: cdk.Stack) -> str:
    """The session a stack deploy writes as: this app's bootstrap execution role in
    session `AWSCloudFormation`. The check trusts exactly this writer (the platform
    hands the interceptor the same role as CFN_EXEC_ROLE_ARN)."""
    role = stack.synthesizer.cloud_formation_execution_role_arn
    return cdk.Fn.sub(
        role.replace(":iam::", ":sts::", 1).replace(":role/", ":assumed-role/", 1)
        + "/AWSCloudFormation"
    )


def write_alert_pattern(root: str, writer: str) -> dict:
    """The EventBridge pattern: a write call under `root/` by anyone but `writer`."""
    prefix = [{"prefix": f"{root}/"}]
    return {
        "source": ["aws.ssm"],
        "detail_type": ["AWS API Call via CloudTrail"],
        "detail": {
            "eventSource": ["ssm.amazonaws.com"],
            "eventName": WRITE_CALLS,
            "userIdentity": {"arn": [{"anything-but": [writer]}]},
            "$or": [
                {"requestParameters": {"name": prefix}},
                {"requestParameters": {"names": prefix}},
            ],
        },
    }


#: Construct ids of the resources the layer took over from business-operations.
MOVED = ("Role-", "Runtime-", "Provider-", "GatewayProvider-", "SapSecurityGroup")


def build(app, ctx: UseCaseContext, config: dict) -> SapLayerStack:
    sap = load_config(config)
    access.require_check(ctx.platform, sap)
    if ctx.production and sap.systems and sap.image_tag == "latest":
        # The runtime holds the SAP credentials, and its certificate check rests on
        # the server reading MCP_SERVER_SESSION_VERIFY: production runs a reviewed one.
        raise ValueError(
            "layers.sap.image_tag is latest, which deployment.mode: production refuses: "
            "pin a version tag (read it from a runtime's startup banner) or a sha256: "
            "digest, so every runtime runs the server you reviewed"
        )
    stack = SapLayerStack(
        app, f"{ctx.prefix}-layer-{names.NAME}", ctx=ctx, sap=sap, env=ctx.cdk_env
    )
    return stack
