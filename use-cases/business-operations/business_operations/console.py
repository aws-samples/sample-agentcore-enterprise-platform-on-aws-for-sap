# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The operator console: the React SPA on private S3 behind CloudFront.

Two halves, because the console URL and the API URL each have to exist before
the other's consumer deploys:

- `ConsoleStack` (deployed first) holds the bucket, the distribution and the
  built SPA. It publishes the console URL to SSM, where the main stack reads it
  for the console client's callback URL and the APIs' CORS origin.
- `publish_config` runs in the runtime stack, which deploys last: it reads the API
  URLs and the console client id the main stack publishes, adds the run-event
  channel it creates itself, and writes `aws-exports.json` into the console's bucket.

The SPA is built at asset staging (`npm ci` once, `npm run build`), and only
for a stack the CDK CLI is bundling: `cdk ls` and a deploy `--exclusively` of
another stack skip it.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess  # nosec B404 — fixed argv, no shell
from pathlib import Path

import aws_cdk as cdk
import jsii
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from business_operations import names
from infra_utils.use_case_context import UseCaseContext

UC_DIR = Path(__file__).resolve().parents[1]
FRONTEND = UC_DIR / "frontend"
# The SPA compiles the run-client's TypeScript through a path alias.
SOURCES = (FRONTEND, UC_DIR / "packages")
BUILD_EXCLUDE = ["node_modules", "build", ".vite", "*.log"]
CONFIG_FILE = "aws-exports.json"
INDEX_FILE = "index.html"
NAME = names.NAME


#: Where the console stack publishes, under the deployment's ssm_prefix.
_OWN = f"use-cases/{NAME}"


ENTRA_LOGIN = "https://login.microsoftonline.com"


def csp(region: str, auth_domain_url: str) -> str:
    """The Content-Security-Policy every object is served with.

    `script-src 'self'` with no inline script is the point: an injected
    `<script>` or `onerror=` does not run, so nothing can read the token store
    (`public/theme-init.js` exists so the one pre-paint script is a file).
    `style-src` keeps `'unsafe-inline'` because Radix appends a scroll-lock
    `<style>` at runtime. `connect-src` names each service by Region rather than
    `*.amazonaws.com`; the hosted sign-in domain is the platform's own. `wss:` is
    spelled out for the run-events channel, since not every browser lets an
    `https:` source match a WebSocket.
    """
    connect = [
        "'self'",
        f"https://*.execute-api.{region}.amazonaws.com",
        f"https://cognito-idp.{region}.amazonaws.com",
        auth_domain_url,
        f"https://*.appsync-api.{region}.amazonaws.com",
        f"wss://*.appsync-realtime-api.{region}.amazonaws.com",
    ]
    return "; ".join(
        [
            "default-src 'self'",
            "script-src 'self'",
            "style-src 'self' 'unsafe-inline'",
            "font-src 'self'",
            "img-src 'self' data:",
            "connect-src " + " ".join(connect),
            "frame-ancestors 'none'",
            "frame-src 'none'",
            "object-src 'none'",
            "base-uri 'self'",
            "form-action 'self'",
            "upgrade-insecure-requests",
        ]
    )


# A client-side route has no file extension; anything with one is an object in
# the bucket. Rewriting only the former keeps a missing hashed chunk a 403
# instead of an HTML page served as JavaScript.
SPA_REWRITE = """\
function handler(event) {
  var request = event.request;
  var last = request.uri.split('/').pop();
  if (last.indexOf('.') === -1) {
    request.uri = '/index.html';
  }
  return request;
}
"""


def source_hash() -> str:
    """Fingerprint of everything the build reads, so an unchanged SPA is not rebuilt."""
    h = hashlib.sha256()
    for path in SOURCES:
        h.update(cdk.FileSystem.fingerprint(str(path), exclude=BUILD_EXCLUDE).encode())
    return h.hexdigest()


@jsii.implements(cdk.ILocalBundling)
class _NpmBuild:
    """Builds the SPA with the local npm; the Docker image is the fallback."""

    def try_bundle(self, output_dir: str, _options) -> bool:
        npm = shutil.which("npm")
        if not npm:
            return False
        if not (FRONTEND / "node_modules").is_dir():
            subprocess.run([npm, "ci"], cwd=FRONTEND, check=True)  # nosec B603
        subprocess.run([npm, "run", "build"], cwd=FRONTEND, check=True)  # nosec B603
        shutil.copytree(FRONTEND / "build", output_dir, dirs_exist_ok=True)
        return True


def _spa_source() -> s3deploy.ISource:
    return s3deploy.Source.asset(
        str(UC_DIR),
        asset_hash=source_hash(),
        asset_hash_type=cdk.AssetHashType.CUSTOM,
        bundling=cdk.BundlingOptions(
            image=cdk.DockerImage.from_registry(
                "public.ecr.aws/docker/library/node:22"
            ),
            local=_NpmBuild(),
            working_directory="/asset-input/frontend",
            command=[
                "bash",
                "-c",
                "npm ci && npm run build && cp -r build/. /asset-output/",
            ],
        ),
    )


class ConsoleStack(cdk.Stack):
    def __init__(self, scope: Construct, id: str, *, ctx: UseCaseContext, **kw):
        super().__init__(scope, id, **kw)
        ssm_prefix = ctx.ssm_prefix
        retain = ctx.production
        # Direct mode signs in at Entra, and there is no Cognito domain to read.
        auth_domain_url = (
            ENTRA_LOGIN
            if ctx.platform.identity.mode == "direct"
            else ctx.ssm_value(self, "auth/domain-url")
        )

        access_logs = s3.Bucket(
            self,
            "HostingBucketAccessLogs",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            # S3 server access logging delivers only to an SSE-S3 bucket.
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            lifecycle_rules=[s3.LifecycleRule(expiration=cdk.Duration.days(90))],
            removal_policy=cdk.RemovalPolicy.RETAIN
            if retain
            else cdk.RemovalPolicy.DESTROY,
            auto_delete_objects=not retain,
        )
        # Unversioned: the bundle is rebuilt from source on every deploy, and
        # prune removes the previous one.
        bucket = s3.Bucket(
            self,
            "HostingBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            server_access_logs_bucket=access_logs,
            server_access_logs_prefix="hosting-bucket-access-logs/",
            removal_policy=cdk.RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        headers = cloudfront.ResponseHeadersPolicy(
            self,
            "SecurityHeaders",
            comment=f"{NAME} console: CSP and security headers",
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                content_security_policy=cloudfront.ResponseHeadersContentSecurityPolicy(
                    content_security_policy=csp(self.region, auth_domain_url),
                    override=True,
                ),
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(
                    override=True
                ),
                # What an older browser honours in place of frame-ancestors.
                frame_options=cloudfront.ResponseHeadersFrameOptions(
                    frame_option=cloudfront.HeadersFrameOption.DENY, override=True
                ),
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                    referrer_policy=cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN,
                    override=True,
                ),
                # No HSTS: this serves on the shared *.cloudfront.net certificate;
                # HSTS belongs with the custom domain an adopter brings.
            ),
        )

        self.distribution = cloudfront.Distribution(
            self,
            "Distribution",
            comment=f"{ctx.prefix} {NAME} console",
            default_root_object=INDEX_FILE,
            # The access-log bucket is ACL-enabled (ObjectWriter), which CloudFront
            # standard logging needs.
            enable_logging=True,
            log_bucket=access_logs,
            log_file_prefix="cloudfront/",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                response_headers_policy=headers,
                function_associations=[
                    cloudfront.FunctionAssociation(
                        event_type=cloudfront.FunctionEventType.VIEWER_REQUEST,
                        function=cloudfront.Function(
                            self,
                            "SpaRewrite",
                            code=cloudfront.FunctionCode.from_inline(SPA_REWRITE),
                            runtime=cloudfront.FunctionRuntime.JS_2_0,
                        ),
                    )
                ],
            ),
        )
        self.distribution.node.default_child.add_metadata(
            "checkov",
            {
                "skip": [
                    {
                        "id": "CKV_AWS_174",
                        "comment": "No aliases, so this serves on the default *.cloudfront.net "
                        "certificate, which pins TLSv1; CloudFront refuses a higher minimum "
                        "without a custom domain.",
                    },
                    {
                        "id": "CKV_AWS_68",
                        "comment": "A CloudFront WebACL must live in us-east-1. This serves only "
                        "the static bundle; every data call goes to the API, which has its own "
                        "WebACL under production.",
                    },
                ]
            },
        )
        # On a stack delete, emptying the log bucket must come after its writers are gone:
        # depending on the emptying custom resource makes the hosting bucket and the
        # distribution delete first. S3 and CloudFront still deliver some logs late, which
        # `deploy.sh destroy` handles by emptying the bucket and retrying.
        empty_logs = access_logs.node.try_find_child("AutoDeleteObjectsCustomResource")
        if empty_logs is not None:
            bucket.node.add_dependency(empty_logs)
            self.distribution.node.add_dependency(empty_logs)
        self.url = f"https://{self.distribution.distribution_domain_name}"

        # aws-exports.json is the runtime stack's (publish_config), so prune leaves it.
        # index.html is published after the chunks it names, with no-cache: prune
        # deletes the old chunks, which a browser's cached index.html would request.
        source = _spa_source()
        site = s3deploy.BucketDeployment(
            self,
            "Site",
            sources=[source],
            destination_bucket=bucket,
            exclude=[CONFIG_FILE, INDEX_FILE],
            prune=True,
            distribution=self.distribution,
            memory_limit=512,
        )
        index = s3deploy.BucketDeployment(
            self,
            "SiteIndex",
            sources=[source],
            destination_bucket=bucket,
            exclude=["*"],
            include=[INDEX_FILE],
            prune=False,
            cache_control=[s3deploy.CacheControl.no_cache()],
            distribution=self.distribution,
            memory_limit=512,
        )
        index.node.add_dependency(site)

        for key, value in (
            ("console-url", self.url),
            ("console-bucket", bucket.bucket_name),
            ("console-distribution-id", self.distribution.distribution_id),
        ):
            ssm.StringParameter(
                self,
                f"Out-{key}",
                parameter_name=f"{ssm_prefix}/{_OWN}/{key}",
                string_value=value,
            )
        cdk.CfnOutput(self, "ConsoleUrl", value=self.url)


def console_url(stack: cdk.Stack, ctx: UseCaseContext) -> str:
    """The console's URL, from the console stack's SSM output."""
    return ctx.ssm_value(stack, f"{_OWN}/console-url")


def exports(
    *,
    issuer_url: str,
    client_id: str,
    url: str,
    region: str,
    api_url: str,
    ticketing: bool,
    admins_group: str,
    run_events: tuple[str, str] | None = None,
    api_scope: str | None = None,
    dashboard_url: str | None = None,
    features: dict[str, bool] | None = None,
    brand: dict[str, str] | None = None,
) -> dict:
    """The SPA's runtime configuration (frontend/src/lib/config.ts reads it).

    `run_events` is the channel's (http, realtime) domains. Without both the console
    opens no channel, and a run renders only once its trace lands on the case.
    `api_scope` (direct mode): the platform app's delegated scope; the console then
    sends its access token, not its id token, to the API and the channel.
    `dashboard_url` is the use case's CloudWatch dashboard; the rail links it, and
    AgentCore's agent observability view, for admins.
    `features` is `console.features` from the config, every flag present, so the console
    never supplies a default of its own; `brand` is `console.brand`, both strings, for the
    same reason, and its `monogram` when one is set.
    """
    return {
        "authority": issuer_url,
        "client_id": client_id,
        "redirect_uri": url,
        "post_logout_redirect_uri": url,
        "response_type": "code",
        "scope": f"openid email profile offline_access {api_scope}"
        if api_scope
        else "openid email profile",
        **({"apiToken": "access"} if api_scope else {}),
        "automaticSilentRenew": True,
        "awsRegion": region,
        "apiUrl": api_url,
        # /tickets is on the console API, so this flag is all the console needs.
        "ticketingEnabled": ticketing,
        "adminsGroup": admins_group,
        "features": dict(features or {}),
        "brand": dict(brand or {}),
        **({"dashboardUrl": dashboard_url} if dashboard_url else {}),
        "agentObservabilityUrl": f"https://{region}.console.aws.amazon.com/cloudwatch/home"
        f"?region={region}#gen-ai-observability/agent-core",
        **(
            {
                "runEventsHttpDomain": run_events[0],
                "runEventsRealtimeDomain": run_events[1],
            }
            if run_events
            else {}
        ),
    }


def publish_config(
    stack: cdk.Stack,
    ctx: UseCaseContext,
    *,
    client_id: str,
    api_url: str,
    ticketing: bool,
    admins_group: str,
    run_events: tuple[str, str] | None = None,
    api_scope: str | None = None,
    dashboard_url: str | None = None,
    features: dict[str, bool] | None = None,
    brand: dict[str, str] | None = None,
) -> None:
    """Write aws-exports.json into the console bucket.

    The runtime stack calls this, not the main stack: it deploys last, and the run-event
    channel the file names is its own.
    """

    def read(key: str) -> str:
        return ctx.ssm_value(stack, f"{_OWN}/{key}")

    url = read("console-url")
    bucket = s3.Bucket.from_bucket_name(stack, "ConsoleBucket", read("console-bucket"))
    distribution = cloudfront.Distribution.from_distribution_attributes(
        stack,
        "ConsoleDistribution",
        distribution_id=read("console-distribution-id"),
        domain_name=cdk.Fn.select(2, cdk.Fn.split("/", url)),
    )
    s3deploy.BucketDeployment(
        stack,
        "ConsoleConfig",
        sources=[
            s3deploy.Source.json_data(
                CONFIG_FILE,
                exports(
                    issuer_url=ctx.ssm_value(stack, "auth/issuer-url"),
                    client_id=client_id,
                    url=url,
                    region=stack.region,
                    api_url=api_url,
                    ticketing=ticketing,
                    admins_group=admins_group,
                    run_events=run_events,
                    api_scope=api_scope,
                    dashboard_url=dashboard_url,
                    features=features,
                    brand=brand,
                ),
            )
        ],
        destination_bucket=bucket,
        prune=False,
        cache_control=[s3deploy.CacheControl.no_cache()],
        distribution=distribution,
        distribution_paths=[f"/{CONFIG_FILE}"],
    )
