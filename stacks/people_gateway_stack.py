# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""People Gateway Stack: a second AgentCore Gateway, for people signed in as themselves.

`gateway.people`. The agent Gateway carries agents; this one carries people from a
remote MCP client (AWS Quick, Copilot Studio), each with their own token:

* The **remote clients**, one Cognito authorization-code client per
  `gateway.people.remote_clients` entry, with short tokens and its login style.
* The **Gateway**, whose custom JWT authorizer admits only those clients (gate 1).
* The **resource server** named the Gateway's URL, scope `people`, so a client that
  sends RFC 8707 `resource=<url>` gets a token whose `aud` is this Gateway.
* The **people interceptor** on REQUEST (gate 2,
  `tools/people_interceptor/handler.py`): checks the claims and hands each target
  the verified caller, never the token.

With `gateway.people.issuer`, the organisation's own OpenID Connect issuer signs people
in instead: no Cognito client and no resource server; the authorizer takes the
issuer's discovery URL with its clients or audience, and the interceptor checks the
issuer's tokens. A Gateway authorizer takes one discovery URL, so it is one or the
other.

Generic: it names no use case. A use case attaches its own people targets by the ids
published under `people-gateway/`, and grants this Gateway's role its own functions.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404 - fixed argv, no shell
from pathlib import Path

import aws_cdk as cdk
import jsii
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_logs as logs
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from infra_utils import log_retention
from infra_utils.jwt_authorizer import custom_jwt_authorizer, jwt_authorizer_at

INTERCEPTOR_SRC = Path(__file__).resolve().parents[1] / "tools" / "people_interceptor"
#: The token lifetimes on every remote client: a group removal reaches a person at
#: their next access token, and a sign-out ends their refresh token at once.
ACCESS_MINUTES = 15
REFRESH_HOURS = 8
#: Where the interceptor reads the caller's claims. None: it verifies the bearer token
#: from the request headers itself. A dotted path into the interceptor's event (never
#: under `body` or `headers`): the claims the Gateway validated, and no headers are
#: passed. Which the Gateway offers is a live test (design, open question 1).
CLAIMS_PATH: str | None = None
#: The scope on the resource server named the Gateway's URL.
SCOPE = "people"
METRIC_NAMESPACE_SUFFIX = "people-gateway"

_WHEELS = [
    "--platform=manylinux2014_aarch64",
    "--implementation=cp",
    "--python-version=3.13",
    "--only-binary=:all:",
]


@jsii.implements(cdk.ILocalBundling)
class _LocalWheels:
    """The interceptor and its pinned requirements, with the local pip."""

    def try_bundle(self, output_dir: str, _options) -> bool:
        pip = shutil.which("pip3") or shutil.which("pip")
        if not pip:
            return False
        subprocess.run(  # nosec B603 - fixed argv
            [
                pip,
                "install",
                "--quiet",
                "--no-warn-conflicts",
                *_WHEELS,
                "--target",
                output_dir,
                "-r",
                str(INTERCEPTOR_SRC / "requirements.txt"),
            ],
            check=True,
        )
        shutil.copy(INTERCEPTOR_SRC / "handler.py", output_dir)
        return True


def _issuer_authorizer(issuer: dict) -> dict:
    """Gate 1 for an issuer. A literal audience is pinned there; `gateway`, this
    Gateway's own URL, cannot be named in its own authorizer (a cycle), so the clients
    are pinned instead, which AgentCore matches against `client_id`, and the
    interceptor checks the audience."""
    if issuer["audience"] == "gateway":
        return jwt_authorizer_at(issuer["discovery_url"], list(issuer["clients"]))
    return jwt_authorizer_at(
        issuer["discovery_url"], allowed_audience=[issuer["audience"]]
    )


def _interceptor_code() -> _lambda.Code:
    return _lambda.Code.from_asset(
        str(INTERCEPTOR_SRC),
        exclude=["__pycache__", "*.pyc", "tests"],
        bundling=cdk.BundlingOptions(
            image=_lambda.Runtime.PYTHON_3_13.bundling_image,
            platform="linux/arm64",
            local=_LocalWheels(),
            command=[
                "bash",
                "-c",
                (
                    "pip install --only-binary=:all: -r requirements.txt"
                    " -t /asset-output && cp handler.py /asset-output/"
                ),
            ],
        ),
    )


class PeopleGatewayStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        project_name: str,
        environment: str,
        cognito_issuer_url: str,
        user_pool_id: str,
        # [{"name", "callback_urls"}], one Cognito client each.
        remote_clients: list[dict],
        # The identity providers the pool's own user clients list.
        remote_client_providers: list[str] | None = None,
        kms_key_arn: str = "",
        log_retention_days: int = 30,
        retain_data: bool = False,
        claims_path: str | None = CLAIMS_PATH,
        # gateway.people.issuer, as PeopleIssuer.model_dump(by_alias=True), or None.
        issuer: dict | None = None,
        # identity.group_map merged over the enabled use cases: an issuer's group
        # claim values become these groups, as the pool's hook maps a console token's.
        group_map: dict[str, list[str]] | None = None,
        **kwargs,
    ):
        super().__init__(scope, id, **kwargs)
        if bool(issuer) == bool(remote_clients):
            raise ValueError(
                "the people Gateway needs remote clients or an issuer, not both"
            )
        from_pool = bool(issuer) and issuer["groups"]["from"] == "pool"
        if (remote_clients or from_pool) and not user_pool_id:
            raise ValueError("the people Gateway needs the user pool id")
        if claims_path and {"body", "headers"} & set(claims_path.split(".")):
            raise ValueError("claims_path may not name a part the client writes")
        prefix = f"{project_name}-{environment}"
        name = f"{prefix}-people-gateway"
        base = f"/{project_name}/{environment}/people-gateway"
        url_parameter = f"{base}/url"

        # ── Remote clients: created before the Gateway, whose allow-list names them ──
        pool = (
            cognito.UserPool.from_user_pool_id(self, "UserPool", user_pool_id)
            if user_pool_id
            else None
        )
        clients: dict[str, str] = {}
        for rc in remote_clients or ():
            client_name = rc["name"]
            client = pool.add_client(
                f"RemoteClient-{client_name}",
                user_pool_client_name=f"{prefix}-remote-{client_name}",
                generate_secret=True,
                access_token_validity=cdk.Duration.minutes(ACCESS_MINUTES),
                id_token_validity=cdk.Duration.minutes(ACCESS_MINUTES),
                refresh_token_validity=cdk.Duration.hours(REFRESH_HOURS),
                # A used refresh token is replaced, and the old one dies after this.
                refresh_token_rotation_grace_period=cdk.Duration.seconds(10),
                enable_token_revocation=True,
                prevent_user_existence_errors=True,
                auth_flows=cognito.AuthFlow(user_password=False, user_srp=False),
                supported_identity_providers=[
                    cognito.UserPoolClientIdentityProvider.custom(p)
                    for p in (remote_client_providers or ["COGNITO"])
                ],
                # A client cannot be allowed the people scope here: its resource
                # server is named the Gateway's URL, which needs this client first.
                # The token's `aud` binds it to the Gateway instead.
                o_auth=cognito.OAuthSettings(
                    flows=cognito.OAuthFlows(authorization_code_grant=True),
                    scopes=[
                        cognito.OAuthScope.OPENID,
                        cognito.OAuthScope.EMAIL,
                        cognito.OAuthScope.PROFILE,
                    ],
                    callback_urls=rc["callback_urls"],
                ),
            )
            # Managed login (version 2) serves no page to a client without a style.
            cognito.CfnManagedLoginBranding(
                self,
                f"RemoteClientBranding-{client_name}",
                user_pool_id=user_pool_id,
                client_id=client.user_pool_client_id,
                use_cognito_provided_values=True,
            )
            ssm.StringParameter(
                self,
                f"SSMRemoteClient-{client_name}-client-id",
                parameter_name=f"{base}/remote-clients/{client_name}/client-id",
                string_value=client.user_pool_client_id,
            )
            clients[client_name] = client.user_pool_client_id

        # ── The role: this Gateway alone may assume it (confused deputy) ──
        role = iam.Role(
            self,
            "PeopleGatewayRole",
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnLike": {
                        "aws:SourceArn": f"arn:aws:bedrock-agentcore:{self.region}:"
                        f"{self.account}:gateway/{name}-*"
                    },
                },
            ),
            role_name=f"{prefix}-people-gateway-role",
        )

        # ── The people interceptor (gate 2) ──
        interceptor_role = iam.Role(
            self,
            "PeopleInterceptorRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
        )
        interceptor_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter"],
                resources=[
                    self.format_arn(
                        service="ssm",
                        resource="parameter",
                        resource_name=url_parameter.lstrip("/"),
                    )
                ],
            )
        )
        if issuer:
            groups = issuer["groups"]
            env = {
                "DISCOVERY_URL": issuer["discovery_url"],
                "RESOURCE_PARAMETER": url_parameter,
                "ALLOWED_CLIENTS": ",".join(issuer["clients"]),
                "AUDIENCE": issuer["audience"],
                "SUBJECT_CLAIM": issuer["subject_claim"],
                "DISPLAY_CLAIM": issuer["display_claim"],
                "EMAIL_CLAIM": issuer["email_claim"],
                "GROUPS_FROM": groups["from"],
            }
            if from_pool:
                env["USER_POOL_ID"] = user_pool_id
                # The one lookup `from: pool` needs: who has this email, and their
                # groups, on this pool only.
                interceptor_role.add_to_policy(
                    iam.PolicyStatement(
                        actions=[
                            "cognito-idp:ListUsers",
                            "cognito-idp:AdminListGroupsForUser",
                        ],
                        resources=[pool.user_pool_arn],
                    )
                )
            else:
                env["GROUPS_CLAIM"] = groups["claim"]
                env["GROUP_MAP"] = json.dumps(group_map or {}, separators=(",", ":"))
        else:
            env = {
                "ISSUER": cognito_issuer_url.rstrip("/"),
                "RESOURCE_PARAMETER": url_parameter,
                # The remote clients' ids, and their names in the same order, which a
                # target records as the door a change came through.
                "ALLOWED_CLIENTS": cdk.Fn.join(",", list(clients.values())),
                "CLIENT_NAMES": ",".join(clients),
            }
        if claims_path:
            env["CLAIMS_PATH"] = claims_path
        log_group = log_retention.log_group(
            self, "PeopleInterceptorLogs", log_retention_days, retain_data
        )
        interceptor = _lambda.Function(
            self,
            "PeopleInterceptor",
            function_name=f"{prefix}-people-interceptor",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="handler.handler",
            code=_interceptor_code(),
            timeout=cdk.Duration.seconds(10),
            role=interceptor_role,
            environment=env,
            log_group=log_group,
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[interceptor.function_arn],
            )
        )
        # Every refusal the interceptor logs, for a use case's alarm.
        logs.MetricFilter(
            self,
            "PeopleInterceptorRefusals",
            log_group=log_group,
            metric_namespace=f"{prefix}/{METRIC_NAMESPACE_SUFFIX}",
            metric_name="Refusals",
            filter_pattern=logs.FilterPattern.all_terms(
                "people_interceptor", "refused"
            ),
            metric_value="1",
        )

        # ── The Gateway (gate 1) ──
        props = {
            "name": name,
            "role_arn": role.role_arn,
            "authorizer_type": "CUSTOM_JWT",
            "protocol_type": "MCP",
            "authorizer_configuration": _issuer_authorizer(issuer)
            if issuer
            else custom_jwt_authorizer(cognito_issuer_url, list(clients.values())),
            "protocol_configuration": {
                "mcp": {"supportedVersions": ["2025-03-26", "2025-06-18"]},
            },
            "interceptor_configurations": [
                agentcore.CfnGateway.GatewayInterceptorConfigurationProperty(
                    interception_points=["REQUEST"],
                    interceptor=agentcore.CfnGateway.InterceptorConfigurationProperty(
                        lambda_=agentcore.CfnGateway.LambdaInterceptorConfigurationProperty(
                            arn=interceptor.function_arn
                        )
                    ),
                    input_configuration=agentcore.CfnGateway.InterceptorInputConfigurationProperty(
                        # The token, unless the claims come in the Gateway's input.
                        pass_request_headers=not claims_path,
                    ),
                )
            ],
        }
        if kms_key_arn:
            props["kms_key_arn"] = kms_key_arn
            role.add_to_policy(
                iam.PolicyStatement(
                    actions=["kms:Decrypt", "kms:DescribeKey", "kms:GenerateDataKey"],
                    resources=[kms_key_arn],
                )
            )
        self._gateway = agentcore.CfnGateway(self, "PeopleGateway", **props)
        self._gateway.node.add_dependency(role)
        interceptor.add_permission(
            "PeopleGatewayInvokesInterceptor",
            principal=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            source_arn=self._gateway.attr_gateway_arn,
        )

        if issuer:
            # An issuer binds its tokens to the Gateway itself (`aud`): no resource
            # server. What an operator needs to set the connector up is published.
            for key, value in (
                ("discovery-url", issuer["discovery_url"]),
                ("clients", ",".join(issuer["clients"])),
                ("audience", issuer["audience"]),
            ):
                ssm.StringParameter(
                    self,
                    f"SSMPeopleIssuer-{key}",
                    parameter_name=f"{base}/issuer/{key}",
                    string_value=value,
                )
        else:
            # A token for `resource=<url>` is issued only when a resource server has
            # exactly that identifier.
            cognito.UserPoolResourceServer(
                self,
                "PeopleGatewayResourceServer",
                user_pool=pool,
                identifier=self._gateway.attr_gateway_url,
                user_pool_resource_server_name=f"{prefix}-people-gateway",
                scopes=[
                    cognito.ResourceServerScope(
                        scope_name=SCOPE, scope_description="Use the people Gateway"
                    )
                ],
            )

        for key, value in (
            ("url", self._gateway.attr_gateway_url),
            ("id", self._gateway.attr_gateway_identifier),
            ("role-arn", role.role_arn),
        ):
            ssm.StringParameter(
                self,
                f"SSMPeopleGateway-{key}",
                parameter_name=f"{base}/{key}",
                string_value=value,
            )
        cdk.CfnOutput(self, "PeopleGatewayUrl", value=self._gateway.attr_gateway_url)

    @property
    def gateway_url(self) -> str:
        return self._gateway.attr_gateway_url
