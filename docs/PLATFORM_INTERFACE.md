# The platform interface

What a use case (or any external consumer) may rely on. Everything here is
discoverable at runtime with only `ssm:GetParameter` and standard OAuth — no
imports of this repo's stacks, no CloudFormation exports, no AWS Identity and Access Management (IAM) coupling on
the data plane. If it is not on this page, it is an internal and may change
without notice.

Interface version: **3**. Version 3 is version 2 with `ctx` as a typed `UseCaseContext`
instead of a dict, which was a breaking change. Additions are
non-breaking; renaming or removing a parameter listed here is a breaking change
and gets a version bump plus a deprecation window.

Each use case states the version it is written against as `platform_interface` in its
`manifest.yaml`. Loading `platform.yaml` refuses a use case that states no version or
another version. The platform keeps its version in
`infra_utils.platform_config.PLATFORM_INTERFACE_VERSION`, and a test checks that it
matches this page.

## Discovery — the SSM namespace

All parameters are in AWS Systems Manager (SSM) Parameter Store, under `/{project}/{environment}/` (default:
`/agentcore-workshop/dev/`). Verified against a live deployment, except three rows that
were only synthesized: `auth/idp-provider-name` (no external IdP federated),
`people-gateway/issuer/*` and `security/kms-key-arn` (no production-mode deployment):

| Parameter | Meaning |
|---|---|
| `auth/mode` | `brokered` (Cognito issues tokens — the default) or `direct` (the identity provider (IdP) does; no user pool). Absent on platforms deployed before it existed: read absent as `brokered` |
| `auth/issuer-url` | Token issuer (Cognito, or Entra's `.../v2.0` in direct mode); append `/.well-known/openid-configuration` for OpenID Connect (OIDC) discovery |
| `auth/user-pool-id` | The user pool everything authenticates against (brokered only) |
| `auth/app-client-id` | Authorization-code + Proof Key for Code Exchange (PKCE) client (humans; has a secret, readable via `cognito-idp describe-user-pool-client`) |
| `auth/domain-url` | The user pool's hosted domain (`https://<prefix>-<account>.auth.<region>.amazoncognito.com`): `/oauth2/authorize`, `/oauth2/token` and `/logout` are under it (brokered only) |
| `auth/idp-provider-name` | The brokered IdP's provider name on the pool (`EntraID`, `Okta` or `PingIdentity`). Present only when an external IdP is federated; absent means Cognito sign-in only. When present, a use case's own user client lists **only** this provider in `supported_identity_providers`, never `COGNITO`, which would reopen a password path around the IdP's multi-factor authentication (MFA) |
| `auth/web-client-id` | Authorization-code + PKCE client (browser single-page apps (SPAs); no secret; brokered only) |
| `auth/m2m-client-id` | client_credentials client (machines). Direct mode: the same app registration as `app-client-id` |
| `auth/m2m-scope` | Scope to request for a machine token: `agentcore/invoke` (brokered) or `<client_id>/.default` (direct) |
| `auth/m2m-client-secret-name` | Secrets Manager name of the machine-to-machine (M2M) client secret (generated Cognito secret in brokered mode; IdP app secret in direct mode) |
| `gateway/url` | Model Context Protocol (MCP) gateway endpoint — `tools/list` / `tools/call` with a Bearer JSON Web Token (JWT) |
| `gateway/id` | Gateway identifier, for a use case's own `CfnGatewayTarget` (`gateway_identifier`) |
| `gateway/role-arn` | The Gateway's service role. A use case may grant it access to **its own** resources only (its target Lambdas, its tables): the synth, when the AWS Cloud Development Kit (CDK) builds the templates, refuses any grant on it from a use-case stack that names a wildcard, a literal ARN, or a resource the use case does not define, because every use case shares this role |
| `gateway/use-case-clients/<name>/<client>/client-id`, `.../client-secret-name` | A client a use case declared under `gateway_clients` in its manifest: client_credentials on the platform pool, admitted by the Gateway, with scopes `uc-<name>/<scope>` on that use case's own resource server. Under `identity.mode: direct`, the operator's app registration named in `identity.use_case_clients` (no scopes; Cedar keys on `azp`). Present only while the use case is enabled |
| `gateway/interceptor-role-arn` | The Gateway interceptor's Lambda role, only when `gateway.interceptor_extensions` lists a check. Same rule as `gateway/role-arn`: grant it your own resources only. The interceptor's environment also contains `SSM_PREFIX` (`/{project}/{environment}`) for the check to find your parameters |
| `gateway/policy-engine-id` | Cedar policy engine attached to the Gateway, for a use case's own `CfnPolicy` statements. Only when `security.cedar.enabled` |
| `people-gateway/url`, `people-gateway/id` | The people Gateway, only when `gateway.people.enabled`: people sign in to it from a remote MCP client, and its URL is the audience of their tokens. Attach a people target with `gateway_identifier` set to the id ([gateway module](modules/gateway.md#people-gateway)) |
| `people-gateway/role-arn` | The people Gateway's role. Same rule as `gateway/role-arn`: grant it `lambda:InvokeFunction` on your own people target functions only, and name it as their only invoker |
| `people-gateway/remote-clients/<name>/client-id` | A remote client from `gateway.people.remote_clients` (`quick`, `copilot-studio`). The people interceptor puts its name in every call's `caller.client`; a target that runs shared handler code can allow these ids as its clients |
| `people-gateway/issuer/discovery-url`, `.../clients`, `.../audience` | Only with `gateway.people.issuer`, in place of `remote-clients/`: the issuer's discovery URL, its client ids (comma-separated) and the audience (`gateway` or a literal). Every call's `caller.client` is then `issuer`, and `caller.client_id` the matched client id ([gateway module](modules/gateway.md#an-organisations-own-issuer)) |
| `identity/gateway-credential-provider-name` | Token-vault provider agents use via `@requires_access_token` |
| `memory/memory-id`, `memory/memory-arn` | AgentCore Memory for this deployment |
| `runtimes/<component>/arn`, `runtimes/<component>/id` | Agent runtimes (`orchestrator`, plus `code-agent` / `research-agent` when Agent2Agent (A2A) is on) |
| `identity/<vendor>-provider-arn` | Three-legged OAuth (3LO) credential providers, only when configured |
| `ops-topic/arn` | The platform's Amazon Simple Notification Service (SNS) ops topic, published by its own `ops-topic` stack (which waits for no runtime), when `observability.alarms` is on or an enabled use case sets `alarm_topic: true` in its manifest. Point your alarm and OK actions at it. Under `deployment.mode: production` it is encrypted with the platform's customer managed key (CMK) in AWS Key Management Service (KMS), whose policy already lets CloudWatch publish; anything else that publishes to it needs `kms:GenerateDataKey*` and `kms:Decrypt` through SNS (below) |
| `security/kms-key-arn` | The platform CMK, only when `security.cloudtrail_alerting` deploys the security stack (always, under `deployment.mode: production`). Encrypt your own tables, buckets and queues with it. Not your log groups: its key policy has no statement for CloudWatch Logs, so a log group encrypted with it fails to deploy. Its key policy trusts the account, so the synth refuses any use-case KMS grant on a key the use case does not define unless it contains `kms:ViaService` naming only `dynamodb`, `s3`, `logs`, `sns` or `sqs` (`<service>.<region>.amazonaws.com`) |
| `networking/vpc-id` | The platform VPC, only when `security.networking` is on |
| `networking/private-subnet-ids` | Comma-separated private subnets with NAT egress, the only valid placement for AgentCore elastic network interfaces (ENIs). Only when `security.networking` is on |
| `networking/runtime-security-group-id` | The security group the platform's runtimes use. Only when `security.networking` is on |
| `use-cases/<name>/...` | Each use case publishes its own outputs here |

In CDK, resolve at deploy time, so your stack
synthesizes even when the platform is not deployed:

```python
gateway_url = ctx.ssm_value(self, "gateway/url")
```

`ctx.ssm_value` calls `ssm.StringParameter.value_for_string_parameter` under
`ctx.ssm_prefix`. Some properties are resolved in place by CloudFormation, such
as a runtime's environment. For those, use `ctx.ssm_ref("gateway/url")`, which
gives the `{{resolve:ssm:...}}` reference instead.

## The build() context

A use case's `build(app, ctx, config)` receives two values:

- `config`: the use case's own block from `platform.yaml`, unchanged.
- `ctx`: a frozen `UseCaseContext` (`infra_utils/use_case_context.py`) with the
  fields below. It also allows read-only dict access (`ctx["prefix"]`), so a use case
  written for the dict `ctx` of version 2 builds unchanged.

| Field | Meaning |
|---|---|
| `project`, `environment` | The deployment's identity |
| `prefix` | `{project}-{environment}`, the stack and resource name prefix |
| `ssm_prefix` | `/{project}/{environment}`, the root of the namespace above |
| `region`, `cdk_env` | Where to deploy; pass `env=ctx.cdk_env` to every stack |
| `platform` | The validated `PlatformConfig` (`infra_utils/platform_config.py`) after `deploy.sh`'s environment overrides. Read `identity`, `security` and `observability.alarms` from it; never change it |
| `production` | True under `deployment.mode: production`: keep data on delete and harden. Read it here, not from `platform`, so a use case agrees with the platform's own stacks |
| `log_retention_days` | The resolved `observability.log_retention_days`, for your log groups. A Lambda function needs none of its own: the platform gives every function's log group that retention |
| `model_id` | The resolved `agents.model_id` (context, environment or `platform.yaml`); empty when unset |
| `allowed_models`, `require_guardrails`, `cognito_issuer_url`, `network` | The resolved settings the platform's own runtimes were built with; `network` contains the VPC placement (`network_mode`, `subnet_ids`, `security_group_ids`) when `security.networking` is on |
| `runtime_kwargs()` | Those settings as `RuntimeStack` keyword arguments, with `guardrail_floor` and `retain_data` set from `production` and `log_retention_days` the retention on the runtime's own `/aws/bedrock-agentcore/runtimes/<id>-DEFAULT` log group (AgentCore creates it with none). Pass `**ctx.runtime_kwargs()` and your runtime follows the same allow-list, guardrail deny, retention and network |

Fields are only ever added, never renamed or removed.

## Building a runtime: `RuntimeStack`

`stacks.runtime_stack.RuntimeStack` is the one core class a use case may
instantiate. It gives a use-case runtime two things from the platform:

- the build pipeline: a remote arm64 CodeBuild build, with the image tagged by
  source hash;
- the controls: a scoped role, the model allow-list and the guardrail deny.

Build it as one of your `uc-` stacks:

```python
from stacks.runtime_stack import RuntimeStack

rt = RuntimeStack(
    app, f"{ctx.prefix}-uc-<name>-runtime",
    project_name=ctx.project, environment=ctx.environment,
    component_name="<name>-agent",
    source_dir="use-cases/<name>/agent",   # its own Dockerfile, the whole build context
    protocol="AGUI",                         # HTTP, MCP, A2A or AGUI
    cognito_allowed_clients=[...],           # your own clients
    extra_request_headers=["X-Amzn-Bedrock-AgentCore-Runtime-Custom-<Name>"],
    extra_env_vars={...},
    **ctx.runtime_kwargs(),
    env=ctx.cdk_env,
)
rt.runtime_role.add_to_policy(...)   # grants on your own tables and buckets
```

`credential_provider_names` lists the token-vault providers the runtime may get
tokens from. These are your own providers, and also
`identity/gateway-credential-provider-name` if the runtime calls the Gateway as
the platform's M2M client. The role names those providers and its own workload
identity, never `*`. So no runtime can create tokens for another runtime.

`guardrail_policy` replaces the policy of the baseline guardrail that
`require_guardrails` creates. The default is
`control-library/guardrails/egress-default.json`. It takes the same
CloudFormation shape as that artifact. So a use case can start from
`infra_utils.guardrail_policy.baseline_policy({"NAME": "NONE", ...})`, or write
its own. The `DenyUngovernedInference` statement stays in both cases.

With `guardrail_floor`, the synth fails unless the policy keeps the platform's
minimum settings:

- a `PROMPT_ATTACK` filter at `InputStrength: HIGH`;
- `US_SOCIAL_SECURITY_NUMBER` and `CREDIT_DEBIT_CARD_NUMBER` at `BLOCK`;
- `AWS_ACCESS_KEY` and `AWS_SECRET_KEY` at `BLOCK` on the model's output
  (`guardrail_policy.output_only_block`).

The key detector reads some random identifiers as a secret key. So it is not
applied to the input the model is given.

These are interface:

- the keyword arguments above, `credential_provider_names` and `guardrail_policy`;
- the properties `runtime_role`, `runtime` (the `CfnRuntime`), `runtime_arn` and
  `runtime_id`.

Every other argument, and the construct tree, are not interface. The stack
publishes `runtimes/<component_name>/arn` and `/id`, like the platform's own
runtimes.

Layers, resources several use cases share, are in [`layers/README.md`](../layers/README.md).

## Authentication — one issuer, two shapes

Read `auth/mode` first. Brokered (the default) is described below; in direct
mode the issuer is the customer's IdP and the same two shapes apply with
`auth/m2m-scope` and Entra's token endpoint (from OIDC discovery) — see
[Direct mode](ENTERPRISE_IDP.md#direct-mode-entra-id-issues-the-tokens).

- **Humans**: authorization-code + PKCE against the hosted UI using
  `auth/app-client-id`. With an enterprise IdP federated in (`IDP_TYPE=...`),
  the login happens at the IdP but the token is still ISSUED BY COGNITO —
  your use case verifies one issuer regardless of the customer's IdP
  (docs/ENTERPRISE_IDP.md). The platform's app and web clients register no
  callback. So a use case with its own front end creates its own user client,
  with its own callback URL. It also creates a branding style for that client
  (`CfnManagedLoginBranding` with `use_cognito_provided_values`). The domain
  serves managed login, which shows no page to a client without a branding style.
- **Machines**: client_credentials with `auth/m2m-client-id` against the
  Cognito domain's `/oauth2/token` (scripts/utils.py `get_m2m_token` is the
  reference).

Both produce a Bearer JWT accepted by the gateway (CUSTOM_JWT authorizer) and
the HTTP runtimes' `/invocations` endpoint. Two caveats that cost real
debugging time:

- The runtime authorizer validates signature, issuer, and client id, but
  **not scopes**. Enforce scopes in your handler if you need them.
- A2A runtimes are the exception to everything above: SigV4 only, no Bearer.

## Federation

The interface is identical in a federated deployment; only WHERE things live
changes. A workload account has its own memory/runtimes/identity parameters
but no local `auth/*` or `gateway/*` — those values come from the platform
account via the `deployment.federation` block in `platform.yaml`. This is why
a use case declares `requires:` in its manifest: the contract refuses to
enable it on a side of the federation that lacks its prerequisites.

## Outside the interface

These are deliberately NOT interface: core stack class names (except `RuntimeStack`, above), construct trees, CloudFormation outputs/exports,
resource names not published to SSM, and the internals of `deploy.sh`. Use
cases that reach past SSM into core stacks will break without warning, and
review will reject them anyway (CONTRIBUTING_USE_CASES.md).

## Known limitations

- **The guardrail floor is checked only inside `RuntimeStack`.** The check runs only when
  `guardrail_floor` is true, and `**ctx.runtime_kwargs()` sets it from production mode. A
  use case that builds its runtime without those keyword arguments, or creates its own
  guardrail outside `RuntimeStack`, gets no floor check. With `require_guardrails` off, no
  guardrail is created and `guardrail_policy` is ignored. Pass `**ctx.runtime_kwargs()`,
  and let `RuntimeStack` create the guardrail.
