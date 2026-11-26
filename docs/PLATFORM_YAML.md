# platform.yaml reference

`platform.yaml` is the one file that describes a deployment: project identity,
account strategy, IdP, agent pattern, gateway tools, security controls,
observability, and (for a migration) the agent you are bringing. Everything
else — the stack list, verification, the dashboard, destroy — is derived from
it, so a typo'd key is an error and every failure in the file is reported at
once.

You rarely write it from scratch: `deploy.sh design --profile <name>` writes it
from the matching `presets/<name>.yaml` (the presets are embedded verbatim at
the end of this page). Edit the result, then validate without touching AWS:

```bash
python -m infra_utils.platform_config platform.yaml
```

Precedence when a value is set in more than one place:
`cdk context > env var > platform.yaml > defaults`. The **Env override**
column is the name `to_env()` exports for each key — the same name `deploy.sh`
and `app.py` read, so an exported variable wins over the file for that run
only. Keys marked ✦ change **which stacks** are deployed
(`expected_stacks()`), not just how a stack is configured; a plan is printed
before anything is created.

✦ changes the deployment footprint (the set of stacks), see `expected_stacks()`.

## Top level

| Key | Type | Default | Env override | Description |
|---|---|---|---|---|
| `project` | str (matches `^[a-z][a-z0-9-]{2,32}$`) | `'agentcore-workshop'` | `PROJECT_NAME` | Name prefix for every stack, role and SSM parameter (`{project}-{environment}-…`). Changing it is a new deployment, not a rename. |
| `environment` | str (matches `^[a-z][a-z0-9]{1,15}$`) | `'dev'` | `ENVIRONMENT` | Second half of the stack prefix; lets one account hold `dev` and `prod` side by side. |
| `region` | str (matches `^[a-z]{2}(-[a-z]+)+-\d$`) | `'us-east-1'` | `AWS_REGION` | Where everything deploys. Also decides whether `gateway.web_search: auto` turns on (launch regions only). |
| `use_cases` ✦ | map of str → object | `{}` | — | Opt-in use cases: name → that use case's own config block, passed to its `build()` untouched (`{}` enables with defaults). Names must exist under `use-cases/`, so a typo is an error, not a silent no-op. Their stacks ride at the end of the footprint. |
| `layers` ✦ | map of str → object | `{}` | — | Opt-in layers: name → that layer's own config block, passed to its `build()` and validated by the layer. Names must exist under `layers/`. A layer's stacks deploy after the core stacks and before any use case; a use case that lists `layer-<name>` in its manifest's `requires` fails at config load unless the layer is enabled here. |

## `deployment:`

Deployment posture and multi-account strategy. See docs/MULTI_ACCOUNT.md.

| Key | Type | Default | Env override | Description |
|---|---|---|---|---|
| `deployment.mode` | one of: `workshop`, `production` | `'workshop'` | `DEPLOYMENT_MODE` | `workshop` keeps disposable lifecycle defaults. `production` retains stateful resources and fails validation unless enterprise identity, networking, audit, authorization, guardrails, model allow-listing and monitored alarms are all configured. |
| `deployment.strategy` ✦ | one of: `centralized`, `distributed`, `federated` | `'centralized'` | `DEPLOYMENT_STRATEGY` | `centralized` puts everything in one account. `distributed` means each team deploys its own copy of this file. `federated` splits shared services (auth, gateway) into `platform_account` from agent runtimes in `workload_accounts`; the account you deploy into decides the role, the same file works in both. |
| `deployment.platform_account` | str | `""` | `PLATFORM_ACCOUNT` | 12-digit account this deployment may modify. Required for production and for centralized/distributed enterprise-IdP deployments; in a federated deployment it hosts the shared services. |
| `deployment.workload_accounts` | list of str | `[]` | — | 12-digit accounts that run agent runtimes in a `federated` deployment. Deploying a federated file from an account in neither list is a hard error. |
| `deployment.federation.gateway_url` | str | `""` | — | Platform-account gateway MCP endpoint a workload account calls (an output of the platform gateway stack). |
| `deployment.federation.issuer_url` | str | `""` | — | Platform Cognito issuer; the OIDC discovery URL is derived from it. |
| `deployment.federation.m2m_client_id` | str | `""` | — | Platform Cognito M2M client id the workload's token vault exchanges for a JWT. Not secret. |
| `deployment.federation.m2m_client_secret_name` | str | `""` | — | Secrets Manager NAME in the workload account holding the M2M client secret. The secret itself never goes in this file. |

## `identity:`

Who issues the tokens the platform trusts.

| Key | Type | Default | Env override | Description |
|---|---|---|---|---|
| `identity.idp` | one of: `cognito`, `entra_id`, `okta`, `ping` | `'cognito'` | `IDP_TYPE` | Who authenticates users. `cognito` is self-contained; the others federate Cognito to your enterprise IdP (docs/ENTERPRISE_IDP.md). |
| `identity.mode` | one of: `brokered`, `direct` | `'brokered'` | `IDP_MODE` | Who issues the tokens. `brokered`: Cognito issues them and the IdP only signs users in — works with no IdP at all. `direct`: the IdP issues them and no Cognito user pool is deployed; `entra_id` only, and the app registration needs a service principal and v2 access tokens (docs/ENTERPRISE_IDP.md, Direct mode). |
| `identity.tenant_id` | str | `""` | `IDP_TENANT_ID` | Entra ID tenant. Required when `idp: entra_id`. |
| `identity.client_id` | str | `""` | `IDP_CLIENT_ID` | App registration / OIDC client id at your IdP. |
| `identity.issuer_url` | str | `""` | `IDP_ISSUER_URL` | OIDC issuer of your IdP. Required for `okta` and `ping` (Entra derives it from the tenant). |
| `identity.client_secret_name` | str | `""` | `IDP_CLIENT_SECRET_NAME` | Secrets Manager NAME holding the IdP client secret. Required for any non-Cognito IdP; the value never goes in this file. |
| `identity.self_sign_up` | bool | `False` | `SELF_SIGN_UP` | Off by default, so only users an administrator creates (`aws cognito-idp admin-create-user`) or the IdP federates can sign in. `true` opens Cognito self sign-up, and only when no external IdP is configured. It is pool-wide, so it applies to every use case: anyone who can reach the hosted UI could then create an account that is a signed-in user to all of them. Ignored in `direct` mode, which has no user pool. |
| `identity.group_claim` | str (matches `^([A-Za-z][A-Za-z0-9_.:-]{0,63})?$`) | `""` | — | The brokered IdP's claim that carries group values (`groups` for Entra security groups, `roles` for app roles). Setting it adds the custom attribute `custom:idp_groups` to the user pool and maps the claim into it at every sign-in; the platform's user clients then write only the mapped attributes, which federated sign-in requires, and the synth refuses any client that could let a user write it. Cognito never deletes a custom attribute, so this is a platform setting, not a per-use-case one. Needs `idp` other than `cognito` and `mode: brokered`. |
| `identity.group_map` | map of str → map of str → list of str | `{}` | — | `{use case: {IdP value: [Cognito group, ...]}}`. A pre token generation trigger adds those groups to `cognito:groups` in a federated user's ID and access tokens. It only adds groups named here, keeps existing ones, and ignores unmapped values and users who did not sign in through the IdP. Each use case may name only groups under its own prefix (`<use case>-…`), and only enabled use cases count. No enabled entry means no trigger. Needs `group_claim`. |
| `identity.use_case_clients` | map of str → map of str → block | `{}` | — | `direct` mode only. `{use case: {client: {client_id, client_secret_name}}}`: one IdP app registration for each client an enabled use case declares under `gateway_clients` in its manifest, no more and no fewer. Brokered mode creates those clients on the user pool; CloudFormation cannot create IdP app registrations, so in direct mode the operator creates them (client_credentials for the platform app's `.default`, so the Gateway's audience pin admits them) and the platform publishes them at the same `gateway/use-case-clients/<use case>/<client>/` parameters. The secret holds the client secret as a plain string. Under Cedar, a use case's statements tell these apps apart by the token's `azp`. |

## `agents:`

| Key | Type | Default | Env override | Description |
|---|---|---|---|---|
| `agents.pattern` | one of: `orchestrator`, `strands-agent`, `langgraph-agent`, `claude-sdk-agent`, `claude-sdk-multi-agent`, `agui-strands-agent`, `agui-langgraph-agent`, `none` | `'orchestrator'` | `AGENT_PATTERN` | Which agent container the runtime stack builds (docs/ARCHITECTURE.md). `none` builds no orchestrator runtime, for a deployment whose use cases bring their own agent. Ignored for the image once `migration:` is set, but still exported so verify and invoke behave. |
| `agents.model_id` | str | `""` | `MODEL_ID` | Amazon Bedrock model or cross-Region inference profile injected as `MODEL_ID` into every agent. Empty = each pattern's baked-in default. |
| `agents.orchestrator_runtime_generation` | int (1–99) | `1` | `ORCHESTRATOR_RUNTIME_GENERATION` | Controlled replacement generation for the orchestrator AgentCore Runtime. Increase only when an existing runtime is unreadable or another immutable runtime property requires replacement; healthy A2A runtimes are unaffected. |
| `agents.allowed_models` | list of str | `[]` | `ALLOWED_MODELS` | Model allow-list. When set, the runtime roles' Amazon Bedrock IAM permissions are scoped to exactly these and `model_id` must be one of them (otherwise the containers' defaults would bypass the list). Empty = unrestricted, today's behaviour. |
| `agents.a2a` ✦ | bool | `False` | `ENABLE_A2A` | Deploy the `code-agent` and `research-agent` sub-agent runtimes next to the orchestrator (A2A protocol). |
| `agents.memory.long_term` | bool | `False` | `USE_LONG_TERM_MEMORY` | Add the semantic long-term strategy to the AgentCore Memory store (fact extraction across sessions). |
| `agents.memory.strategies` | list of one of: `semantic`, `summary`, `user_preference`, optional | absent | — | Which long-term strategies extract records from stored events. Unset keeps the historical set: `user_preference`, plus `semantic` when `long_term` is true. A list replaces that set and cannot be combined with `long_term`; each listed strategy gets a per-actor namespace (`summary` is per session too). `[]` is short-term memory only, for a use case whose events must not be extracted into records. |
| `agents.memory.event_expiry_days` | int (1–365) | `30` | `MEMORY_EVENT_EXPIRY_DAYS` | How long AgentCore Memory events remain available. Production requires an explicit bounded value and retains the Memory resource itself on stack deletion. |

## `gateway:`

| Key | Type | Default | Env override | Description |
|---|---|---|---|---|
| `gateway.web_search` | one of: `auto`, `on`, `off` | `'auto'` | `ENABLE_WEB_SEARCH` | Built-in web-search connector on the gateway. `auto` turns it on only in regions where the connector exists. |
| `gateway.tools` | list of str | `['sample-tool']` | — | Lambda tool names the gateway exposes. Schema-validated today; the deployed list still comes from `tool_configs` in `app.py` (docs/modules/gateway.md). |
| `gateway.interceptor_extensions` | list of block | `[]` | — | Use-case check modules the Gateway interceptor runs, in order, before the egress guardrail; the first refusal answers the call and the target is never invoked. Listing one creates the interceptor even with `security.egress_filter` off (REQUEST only then), and publishes `gateway/interceptor-role-arn` so the use case can grant that role its own tables. Each entry: `path`, a package directory relative to the repo root exposing `check(body, headers)` that returns `None` to allow or a reason string to refuse (anything else refuses; nothing it returns or mutates is forwarded); `target_prefixes`, the target names it governs (it never sees another use case's calls, and fails closed only for its own); `request_headers`, whether this check alone receives the request headers, which are never scanned, logged or forwarded (CONTRIBUTING_USE_CASES.md). |
| `gateway.people.enabled` | bool | `False` | — | A second Gateway for people signed in as themselves from a remote MCP client, apart from the agent Gateway; off by default, and on, the `people-gateway` stack (docs/modules/gateway.md#people-gateway). It creates the people Gateway: its custom JWT authorizer admits only `remote_clients`; the resource server named its URL (scope `people`) binds their tokens to it; the people interceptor verifies each request's token again and hands every target the verified `caller` (`sub`, `username`, `client`, `client_id`, `groups`) in place of anything the client sent, never the token. Publishes `people-gateway/{url,id,role-arn}` for a use case's own people targets. Needs at least one remote client, or `issuer`; remote clients need `identity.mode: brokered`. Cedar is not required: the people Gateway carries only people targets. |
| `gateway.people.remote_clients` | list of block | `[]` | — | The clients people sign in through (Cognito authorization code, one person per token). Each entry: `name`, `quick` (AWS Quick) or `copilot-studio`; `regions` for `quick`, the Regions people use Quick from (the callback is `https://<region>.quicksight.aws.amazon.com/sn/oauthcallback`); `callbacks` for `copilot-studio`, each connector's redirect under `https://global.consent.azure-apim.net/redirect/` (the bare URL until the connector exists); `allow_in_production`, which `deployment.mode: production` requires. Each gets an app client (a secret that never leaves Cognito, 15-minute access tokens, 8-hour refresh tokens with rotation, revocation on) and its managed-login style, and is published at `people-gateway/remote-clients/<name>/client-id`. The agent Gateway admits none of them. |
| `gateway.people.issuer` | block, optional | absent | — | An OpenID Connect issuer of the organisation's own that signs people in to the people Gateway, instead of the Cognito `remote_clients`: for an organisation whose remote MCP client must sign in through its own identity provider. A Gateway's authorizer trusts one discovery URL, so this and `remote_clients` are refused together. No Cognito client and no resource server are created; the authorizer takes the issuer's discovery URL, and the people interceptor checks the issuer's tokens itself (docs/modules/gateway.md#an-organisations-own-issuer). Allowed under `identity.mode: direct`. |
| `gateway.people.issuer.discovery_url` | str (matches `^https://[^\s?#/]+(/[^\s?#]*)?/\.well-known/(openid-configuration|oauth-authorization-server)(/[^\s?#]+)?$`) | **required** | — | The issuer's metadata document: https, ending `/.well-known/openid-configuration` (OpenID Connect Discovery) or with `/.well-known/oauth-authorization-server` (RFC 8414). Its `issuer` must be the one this URL names, or every request is refused. |
| `gateway.people.issuer.clients` | list of Annotated (at least 1) | **required** | — | The client ids, at the issuer, of the remote MCP clients' connectors. A token must name one in `azp`, `client_id`, `act.client_id` (an RFC 8693 on-behalf-of token) or `aud`. |
| `gateway.people.issuer.audience` | str (at least 1) | **required** | — | What the issuer puts in the token's `aud`: a literal value, which the authorizer pins, or `gateway` for the people Gateway's own URL (RFC 8707 `resource`). With `gateway` the authorizer pins `clients` instead, matched against the token's `client_id`, and the interceptor checks the audience; an issuer that names the client only in `azp` needs a literal audience. |
| `gateway.people.issuer.subject_claim` | str (at least 1) | `'sub'` | — | The claim that is the person's identity of record, recorded against every change. |
| `gateway.people.issuer.display_claim` | str (at least 1) | `'email'` | — | The claim shown as the person's name; the subject when absent. |
| `gateway.people.issuer.email_claim` | str (at least 1) | `'email'` | — | The claim holding the person's email, as the issuer emits it; read with `groups.from: pool`. |
| `gateway.people.issuer.groups.from` | one of: `claim`, `pool` | `'claim'` | — | `claim`: the person's groups are a claim of the token, through `identity.group_map` when it has entries (only mapped groups then), else as written. `pool`: the groups of the one enabled user pool user whose verified email equals the token's email claim, read with `cognito-idp:ListUsers` and `AdminListGroupsForUser` on this pool alone and cached for a minute; brokered only. |
| `gateway.people.issuer.groups.claim` | str | `""` | — | With `from: claim`, the claim carrying the groups (a list, or space-separated text). |
| `gateway.people.issuer.groups.email_is_verified_by_issuer` | bool | `False` | — | Whether the issuer only ever puts an address its owner proved in the email claim. `from: pool` is refused without it, since anyone who could set their own email at the issuer would otherwise take that pool user's groups. A token saying `email_verified: false` is refused either way. |
| `gateway.people.issuer.allow_in_production` | bool | `False` | — | `deployment.mode: production` refuses the issuer unless this is set, as for a remote client. |

## `security:`

| Key | Type | Default | Env override | Description |
|---|---|---|---|---|
| `security.networking` ✦ | bool | `False` | `ENABLE_NETWORKING` | VPC mode: runtimes in private subnets behind a NAT gateway and VPC endpoints. Adds the `networking` stack; required by migration private dependencies. |
| `security.vpc` | block, optional | absent | — | Where the VPC comes from. Absent: create `10.0.0.0/16` with one NAT. Needs `networking: true`; choose at first deploy, since changing it replaces the VPC (docs/modules/networking.md). |
| `security.vpc.cidr` | str | `""` | — | Created VPC: its range, `/16` to `/22`. Imported VPC: its existing range, which the endpoint security group admits on 443 (required unless `endpoints: none`). |
| `security.vpc.egress` | one of: `nat`, `nat_per_az` | `'nat'` | — | Created VPC only. `nat_per_az` puts a NAT gateway in each AZ, so one AZ failing leaves the other's egress up. |
| `security.vpc.endpoints` | one of: `core`, `none` | `'core'` | — | `core`: the platform's interface endpoints. `none` (imported VPC only): your network team's central endpoints serve them. |
| `security.vpc.vpc_id` | str (matches `^(?:|vpc-[0-9a-f]{8,17})$`) | `""` | — | Deploy into this existing VPC: no VPC, NAT, route or gateway endpoint is created, and its routes stay yours. Give `private_subnet_ids` with it. |
| `security.vpc.private_subnet_ids` | list of str | `[]` | — | With `vpc_id`: at least two private subnets in different AZs AgentCore supports (`preflight` checks the AZ IDs). |
| `security.cloudtrail_alerting` ✦ | bool | `False` | `ENABLE_SECURITY` | The `security` stack: a CloudTrail trail plus alerting. Needed for `traceability` to fire at all. |
| `security.resource_policies` | bool | `False` | `ENABLE_RESOURCE_POLICIES` | In-account-only resource policy on the Memory store, with an org deny guard. Needs `org_id`. |
| `security.egress_filter` | bool | `False` | `ENABLE_EGRESS_FILTER` | An Amazon Bedrock Guardrail plus an interceptor Lambda on the gateway, over every tool call's arguments and response: it masks the `ANONYMIZE` entities and refuses a call or response carrying a `BLOCK` one whole (docs/SECURITY_CONTROLS.md). |
| `security.egress_pii_entities` | map of one of: `ADDRESS`, `AGE`, `AWS_ACCESS_KEY`, `AWS_SECRET_KEY`, `CA_HEALTH_NUMBER`, `CA_SOCIAL_INSURANCE_NUMBER`, `CREDIT_DEBIT_CARD_CVV`, `CREDIT_DEBIT_CARD_EXPIRY`, `CREDIT_DEBIT_CARD_NUMBER`, `DRIVER_ID`, `EMAIL`, `INTERNATIONAL_BANK_ACCOUNT_NUMBER`, `IP_ADDRESS`, `LICENSE_PLATE`, `MAC_ADDRESS`, `NAME`, `PASSWORD`, `PHONE`, `PIN`, `SWIFT_CODE`, `UK_NATIONAL_HEALTH_SERVICE_NUMBER`, `UK_NATIONAL_INSURANCE_NUMBER`, `UK_UNIQUE_TAXPAYER_REFERENCE_NUMBER`, `URL`, `USERNAME`, `US_BANK_ACCOUNT_NUMBER`, `US_BANK_ROUTING_NUMBER`, `US_INDIVIDUAL_TAX_IDENTIFICATION_NUMBER`, `US_PASSPORT_NUMBER`, `US_SOCIAL_SECURITY_NUMBER`, `VEHICLE_IDENTIFICATION_NUMBER` → one of: `BLOCK`, `ANONYMIZE`, `NONE` | `{}` | — | Per-entity PII actions on the egress guardrail (`BLOCK`, `ANONYMIZE`, or `NONE` to detect only), over the artifact's own: NAME, ADDRESS, EMAIL and PHONE anonymized, SSNs and card numbers blocked. Keys are Amazon Bedrock Guardrails personally identifiable information (PII) entity types. Under `production`, `US_SOCIAL_SECURITY_NUMBER`, `CREDIT_DEBIT_CARD_NUMBER`, `AWS_ACCESS_KEY` and `AWS_SECRET_KEY` always block, here and in every runtime's baseline guardrail, and validation refuses a lower action. Example: `{NAME: NONE, ADDRESS: NONE}` keeps supplier names and addresses intact in tool traffic. |
| `security.egress_unscanned_responses` | list of str | `[]` | — | Tools whose responses the egress guardrail does not scan, each `<target>___<tool>` or a prefix of one that names its target in full (`<target>___` covers every tool on it). Their requests are still scanned. List only tools that return content this deployment authored, such as its own SOPs and their search, never SAP or other external data: the SAP layer refuses an entry on an SAP target. Use it when the Amazon Bedrock Guardrails detector misreads authored content, such as a general ledger (G/L) account number that it reads as a card number. |
| `security.egress_request_action` | one of: `mask`, `block` | `'mask'` | — | What the egress guardrail does to a tool-call request it would rewrite: `mask` (default) substitutes the anonymized text and forwards it, `block` refuses the call instead. Use `block` when a use case's checks approve the request body, so the target never receives a body other than the one approved. Responses are always masked. |
| `security.require_guardrails` | bool | `False` | `REQUIRE_GUARDRAILS` | IAM-denies Amazon Bedrock inference without a Guardrail on the runtime roles, creates a baseline guardrail per runtime and injects it into the agents. Incompatible with the `claude-sdk-*` patterns, which cannot attach one. |
| `security.cedar.enabled` | bool | `False` | `ENABLE_CEDAR` | Cedar policy engine on the gateway (tool-level authorization). |
| `security.cedar.mode` | one of: `LOG_ONLY`, `ENFORCE` | `'LOG_ONLY'` | `CEDAR_MODE` | `LOG_ONLY` records decisions without enforcing them. Change to `ENFORCE` only after you read the logs of real traffic. With more than one use case enabled, `deploy.sh design` and the synth warn while Cedar is off or `LOG_ONLY`: one use case's Gateway clients can then call another's tools that only Cedar protects. |
| `security.traceability` | bool | `False` | `ENABLE_TRACEABILITY` | SNS + EventBridge alerts on sensitive API calls. Requires `cloudtrail_alerting`, the rule only matches CloudTrail management events. |
| `security.org_id` | str | `""` | `ORG_ID` | AWS Organizations id (`o-…`) the VPC endpoint and Memory policies are scoped to. Empty is fine (deploy.sh prompts); a placeholder is rejected. |

## `observability:`

| Key | Type | Default | Env override | Description |
|---|---|---|---|---|
| `observability.transaction_search` | bool | `True` | `ENABLE_TRANSACTION_SEARCH` | Configure CloudWatch Transaction Search so runtime traces are searchable. Account-scoped, not per stack. |
| `observability.alarms` | bool | `False` | `ENABLE_ALARMS` | CloudWatch alarms per deployed resource, an SNS ops topic and the platform dashboard. |
| `observability.alarm_email` | str | `""` | `ALARM_EMAIL` | Inbox subscribed to the alarm topic (SNS sends a confirmation first). Empty = topic without subscription; placeholders are rejected. |
| `observability.log_retention_days` | int | `30` | `LOG_RETENTION_DAYS` | CloudWatch log retention for platform-managed log groups. Supported values map to native CloudWatch periods: 30, 90, 180, or 365 days. |
| `observability.span_retention_days` | int, optional | absent | `SPAN_RETENTION_DAYS` | Retention for the account-wide Transaction Search groups `aws/spans` and `/aws/application-signals/data`, which hold span content. Empty leaves the account's setting alone; any CloudWatch Logs period is accepted. Needs `transaction_search`, and is never reverted on destroy because other workloads may share the groups. |

## `migration:`

Migrate an existing agent onto the platform.

| Key | Type | Default | Env override | Description |
|---|---|---|---|---|
| `migration` | block, optional | absent | `MIGRATION_ENABLED` | Move an existing agent onto the platform. Absent = no migration, everything behaves as before. Stack names do not change: the migrated agent deploys as `…-runtime-orchestrator` on both targets. |
| `migration.source.platform` | one of: `openshift`, `kubernetes`, `ec2`, `ecs`, `lambda`, `on-prem`, `other` | **required** | `MIGRATION_SOURCE_PLATFORM` | Where the agent runs today. Shapes the docs and the plan, not the deploy. |
| `migration.source.image` | str | `""` | `MIGRATION_SOURCE_IMAGE` | Pre-built image reference. Exactly one of `image` or `build`. AgentCore Runtime is arm64-only and a pre-built image cannot be checked for that before deploy, so the plan warns. |
| `migration.source.build` | block, optional | absent | — | Build the image here from the customer's Dockerfile. Yields arm64 by construction, which is why the shipped preset uses it. Exactly one of `image` or `build`. |
| `migration.source.build.context` | str | **required** | `MIGRATION_BUILD_CONTEXT` | Docker build context, relative to the repo root. |
| `migration.source.build.dockerfile` | str | `'Dockerfile'` | `MIGRATION_BUILD_DOCKERFILE` | Dockerfile path, relative to `context`. |
| `migration.source.registry_secret_name` | str | `""` | `MIGRATION_REGISTRY_SECRET_NAME` | Secrets Manager NAME holding docker-login credentials for a private registry. |
| `migration.source.port` | int (1–65535), optional | absent | `MIGRATION_PORT` | What the container listens on. Required by the supported `adapter` mode. |
| `migration.source.invoke_path` | str | `""` | `MIGRATION_INVOKE_PATH` | Where the container takes a request. Required by the supported adapter mode, which forwards `POST /invocations` there. |
| `migration.source.health_path` | str | `'/'` | `MIGRATION_HEALTH_PATH` | What the adapter polls to answer `GET /ping`. |
| `migration.source.trigger` | one of: `webhook`, `schedule`, `http`, `queue` | `'http'` | `MIGRATION_TRIGGER` | How work arrives today. Discovery metadata only: trigger and cutover infrastructure are external and are not deployed. |
| `migration.source.env` | map of str → str | `{}` | `MIGRATION_ENV_JSON` | Plain configuration for the container. Secret-shaped keys (`*_SECRET`, `*_TOKEN`, `*_KEY`, …) are refused because env values render in clear. |
| `migration.source.secrets` | list of str | `[]` | `MIGRATION_SECRETS` | Environment variable NAMES only. Each value lives in Secrets Manager under `<project>/<environment>/migration/<NAME>`; the plan lists what to create. |
| `migration.target.runtime` | one of: `agentcore`, `ec2` | `'agentcore'` | `MIGRATION_TARGET_RUNTIME` | Only `agentcore` is currently deployable (arm64, the 8080 `/invocations` contract). `ec2` is reserved for a future ECS-on-EC2 implementation and fails validation. |
| `migration.target.mode` | one of: `adapter`, `native` | `'adapter'` | `MIGRATION_TARGET_MODE` | Only `adapter` is currently deployable; it wraps the container so it speaks the AgentCore contract. `native` is reserved for a future implementation and fails validation. |
| `migration.network.private_dependencies` | list of str | `[]` | `MIGRATION_PRIVATE_DEPENDENCIES` | Up to 25 unique customer-side hostnames (3,000 encoded bytes) the agent must still reach. Needs `security.networking` and a `connectivity` other than `none`; verification runs a fixed allow-list TLS probe from the runtime VPC. |
| `migration.network.connectivity` | one of: `vpn`, `transit-gateway`, `none` | `'none'` | `MIGRATION_CONNECTIVITY` | Records the existing external path from the platform VPC to the customer network. The accelerator does not provision VPN or Transit Gateway resources. |
| `migration.network.dns_forwarders` | list of str | `[]` | `MIGRATION_DNS_FORWARDERS` | Records externally managed IPv4 resolvers for private hostnames. The accelerator does not create resolver endpoints or rules. |
| `migration.network.ca_bundle_secret_name` | str | `""` | `MIGRATION_CA_BUNDLE_SECRET_NAME` | Secrets Manager NAME of a private CA bundle used by the network probe. The bundle is read in memory and is not injected into the customer image or returned as evidence. |
| `migration.network.receipt.vpc_id` | str (matches `^(?:|vpc-[0-9a-f]{8,17})$`) | `""` | — | Deployed target VPC ID containing the runtime and fixed dependency probe. Required before private-network approval. |
| `migration.network.receipt.private_subnet_ids` | list of str | `[]` | — | Deployed private subnet IDs used by the dependency probe; the digest sorts them because order has no meaning. |
| `migration.network.receipt.security_group_ids` | list of str | `[]` | — | Deployed security group IDs used by the dependency probe; the digest sorts them because order has no meaning. |
| `migration.network.receipt.ca_bundle_version_id` | str | `""` | — | Exact Secrets Manager version ID of the tested private CA bundle. Required when `ca_bundle_secret_name` is set, so rotation invalidates approval. |
| `migration.network.receipt.probe_verification_reference` | str | `""` | — | Customer evidence reference for the successful fixed allow-list DNS and hostname-verified TLS probe from the deployed VPC. |
| `migration.network.gate.owner` | str | `""` | — | Team or alias accountable for executing and reversing this stage. |
| `migration.network.gate.approver` | str | `""` | — | Separate team or alias that accepted the referenced evidence. |
| `migration.network.gate.approved_at` | datetime, optional | absent | — | Non-future timestamp with UTC offset. Supplying it asserts final approval and requires every other gate field; traffic approval must not predate any required prerequisite. |
| `migration.network.gate.expires_at` | datetime, optional | absent | — | Required UTC-offset timestamp after approval and no more than seven days later. An expired gate blocks readiness. |
| `migration.network.gate.evidence` | list of str | `[]` | — | One or more references in the customer's approved evidence system; do not paste payloads or secrets. Runtime, network, data, trigger, and traffic gates also require their exact printed plan digest. |
| `migration.network.gate.rollback` | str | `""` | — | Concise reversal procedure or reference. Required before the gate can pass. |
| `migration.stages.runtime.account_id` | str (matches `^(?:|\d{12})$`) | `""` | — | 12-digit AWS account containing the verified AgentCore Runtime and its VPC. Required for cutover; must match the Runtime ARN and deployment account intent. |
| `migration.stages.runtime.runtime_arn` | str | `""` | — | AgentCore Runtime ARN captured from the deployed runtime stack. Required for cutover and checked against the configured account and Region. |
| `migration.stages.runtime.source_hash` | str (matches `^(?:|[a-f0-9]{16})$`) | `""` | — | The 16-character `SourceHash` output from the deployed runtime stack, binding approval to the reviewed source and migration adapter build. |
| `migration.stages.runtime.image_digest` | str (matches `^(?:|sha256:[a-f0-9]{64})$`) | `""` | — | Immutable ECR image digest (`sha256:…`) resolved after deployment; a mutable image tag is not sufficient cutover evidence. |
| `migration.stages.runtime.verification_reference` | str | `""` | — | Customer evidence reference for the successful live `deploy.sh verify` run against this exact deployed target. |
| `migration.stages.runtime.gate.owner` | str | `""` | — | Team or alias accountable for executing and reversing this stage. |
| `migration.stages.runtime.gate.approver` | str | `""` | — | Separate team or alias that accepted the referenced evidence. |
| `migration.stages.runtime.gate.approved_at` | datetime, optional | absent | — | Non-future timestamp with UTC offset. Supplying it asserts final approval and requires every other gate field; traffic approval must not predate any required prerequisite. |
| `migration.stages.runtime.gate.expires_at` | datetime, optional | absent | — | Required UTC-offset timestamp after approval and no more than seven days later. An expired gate blocks readiness. |
| `migration.stages.runtime.gate.evidence` | list of str | `[]` | — | One or more references in the customer's approved evidence system; do not paste payloads or secrets. Runtime, network, data, trigger, and traffic gates also require their exact printed plan digest. |
| `migration.stages.runtime.gate.rollback` | str | `""` | — | Concise reversal procedure or reference. Required before the gate can pass. |
| `migration.stages.data.strategy` | one of: `none`, `retain-source`, `external-copy` | `'none'` | — | `none` keeps data movement out of scope. `retain-source` uses versioned retain-source dataset contracts. `external-copy` records and gates a separately designed customer procedure; neither strategy runs a copy during deployment. |
| `migration.stages.data.datasets` | list of block | `[]` | — | Up to 25 versioned retain-source dataset contracts. Each names a declared private dependency plus customer-approved classification, retention, identity-mapping, and validation references. |
| `migration.stages.data.gate.owner` | str | `""` | — | Team or alias accountable for executing and reversing this stage. |
| `migration.stages.data.gate.approver` | str | `""` | — | Separate team or alias that accepted the referenced evidence. |
| `migration.stages.data.gate.approved_at` | datetime, optional | absent | — | Non-future timestamp with UTC offset. Supplying it asserts final approval and requires every other gate field; traffic approval must not predate any required prerequisite. |
| `migration.stages.data.gate.expires_at` | datetime, optional | absent | — | Required UTC-offset timestamp after approval and no more than seven days later. An expired gate blocks readiness. |
| `migration.stages.data.gate.evidence` | list of str | `[]` | — | One or more references in the customer's approved evidence system; do not paste payloads or secrets. Runtime, network, data, trigger, and traffic gates also require their exact printed plan digest. |
| `migration.stages.data.gate.rollback` | str | `""` | — | Concise reversal procedure or reference. Required before the gate can pass. |
| `migration.stages.triggers.strategy` | one of: `none`, `external-shadow` | `'none'` | — | `none` leaves the source trigger unchanged. `external-shadow` records and gates a customer-operated shadow rehearsal; the accelerator does not create or change the event source. |
| `migration.stages.triggers.source_reference` | str | `""` | — | Reference to the current event source or route. Must differ from `shadow_reference`; use an identifier, not a payload or credential. |
| `migration.stages.triggers.shadow_reference` | str | `""` | — | Reference to the separate disabled, dry-run, idempotent, or dual-published shadow target used for rehearsal. |
| `migration.stages.triggers.safety_mode` | one of: `read-only`, `dry-run`, `idempotent`, `dual-publish`, optional | absent | — | How shadow work avoids customer impact. HTTP/webhook: `read-only` or `idempotent`; schedule: `dry-run` or `idempotent`; queue: `dual-publish` to a separate destination. |
| `migration.stages.triggers.idempotency_reference` | str | `""` | — | Customer evidence for duplicate detection/idempotency. Required for `idempotent` and `dual-publish` safety modes. |
| `migration.stages.triggers.signature_validation_reference` | str | `""` | — | Customer evidence that webhook signatures are validated before processing. Required for webhook shadowing. |
| `migration.stages.triggers.validation_reference` | str | `""` | — | Customer test or result reference proving the shadow path accepted representative events without unsafe effects. |
| `migration.stages.triggers.gate.owner` | str | `""` | — | Team or alias accountable for executing and reversing this stage. |
| `migration.stages.triggers.gate.approver` | str | `""` | — | Separate team or alias that accepted the referenced evidence. |
| `migration.stages.triggers.gate.approved_at` | datetime, optional | absent | — | Non-future timestamp with UTC offset. Supplying it asserts final approval and requires every other gate field; traffic approval must not predate any required prerequisite. |
| `migration.stages.triggers.gate.expires_at` | datetime, optional | absent | — | Required UTC-offset timestamp after approval and no more than seven days later. An expired gate blocks readiness. |
| `migration.stages.triggers.gate.evidence` | list of str | `[]` | — | One or more references in the customer's approved evidence system; do not paste payloads or secrets. Runtime, network, data, trigger, and traffic gates also require their exact printed plan digest. |
| `migration.stages.triggers.gate.rollback` | str | `""` | — | Concise reversal procedure or reference. Required before the gate can pass. |
| `migration.stages.traffic.strategy` | one of: `none`, `external-canary`, `external-switch` | `'none'` | — | `none` means safe target-only deployment. `external-canary` records percentage steps for HTTP/webhook routing. `external-switch` records an atomic customer-operated switch and is required for schedule/queue cutover. |
| `migration.stages.traffic.router_reference` | str | `""` | — | Reference to the customer-owned router, event-source configuration, or approved change that will perform the shift. |
| `migration.stages.traffic.source_reference` | str | `""` | — | Reference to the recorded rollback destination. Must differ from `target_reference`. |
| `migration.stages.traffic.target_reference` | str | `""` | — | Reference to the reviewed AgentCore target route or event-source destination. |
| `migration.stages.traffic.metrics_reference` | str | `""` | — | Dashboard or query reference used to evaluate every step against the abort thresholds. |
| `migration.stages.traffic.canary_steps_percent` | list of int | `[]` | — | For `external-canary`, one to ten unique increasing percentages from 1 through 100, ending at 100. Forbidden for an atomic switch. |
| `migration.stages.traffic.abort` | block, optional | absent | — | Required customer-approved stop conditions evaluated during every canary step or atomic-switch observation window. |
| `migration.stages.traffic.abort.max_error_rate_percent` | float (0–100) | **required** | — | Maximum acceptable error rate from 0 through 100 before rollback. |
| `migration.stages.traffic.abort.max_p95_latency_ms` | int (1–3600000) | **required** | — | Maximum acceptable p95 latency in milliseconds before rollback. |
| `migration.stages.traffic.abort.max_failed_events` | int (0–) | **required** | — | Maximum acceptable failed event count before rollback. |
| `migration.stages.traffic.abort.observation_minutes` | int (1–1440) | **required** | — | How long to observe each step before proceeding, from 1 minute through 24 hours. |
| `migration.stages.traffic.gate.owner` | str | `""` | — | Team or alias accountable for executing and reversing this stage. |
| `migration.stages.traffic.gate.approver` | str | `""` | — | Separate team or alias that accepted the referenced evidence. |
| `migration.stages.traffic.gate.approved_at` | datetime, optional | absent | — | Non-future timestamp with UTC offset. Supplying it asserts final approval and requires every other gate field; traffic approval must not predate any required prerequisite. |
| `migration.stages.traffic.gate.expires_at` | datetime, optional | absent | — | Required UTC-offset timestamp after approval and no more than seven days later. An expired gate blocks readiness. |
| `migration.stages.traffic.gate.evidence` | list of str | `[]` | — | One or more references in the customer's approved evidence system; do not paste payloads or secrets. Runtime, network, data, trigger, and traffic gates also require their exact printed plan digest. |
| `migration.stages.traffic.gate.rollback` | str | `""` | — | Concise reversal procedure or reference. Required before the gate can pass. |

## Presets

Each preset is a complete, validated `platform.yaml`; `--profile <name>` copies it for you.

<details>
<summary><code>presets/distributed.yaml</code></summary>

```yaml
# Distributed: every team or workload account runs its own full copy of the
# platform from this one file — auth, gateway, runtimes, all of it. Nothing is
# shared between accounts; organisation-wide guardrails (control-library/terraform/org-guardrails)
# are what keep the copies consistent. Pick this when teams must not depend on
# each other's uptime or change windows. See docs/MULTI_ACCOUNT.md.
# Modules: 3 4 5 6 9
project: agentcore-workshop
environment: dev
region: us-east-1
deployment:
  strategy: distributed
  platform_account: "000000000000"  # REPLACE: aws sts get-caller-identity
identity:
  idp: cognito
agents:
  pattern: orchestrator
gateway:
  web_search: auto
  tools: [sample-tool]
security: {}
observability:
  transaction_search: true
```

</details>

<details>
<summary><code>presets/federated.yaml</code></summary>

```yaml
# Federated: shared services in a platform account, agents in workload accounts.
# The SAME file deploys into both accounts; the account you deploy into decides
# the role (platform: auth + identity + gateway + observability; workload:
# identity + memory + runtimes + observability). Trust between them is pure
# OAuth — no cross-account IAM on the data plane. See docs/MULTI_ACCOUNT.md.
# Modules: 3 4 5 6 9 (platform side) / 4 6 9 + A (workload side)
#
# Order of operations: deploy in the platform account first, copy the four
# federation values from its outputs into `deployment.federation`, then deploy
# the same file in each workload account. `deploy.sh design` shows which side
# the current credentials will produce.
project: agentcore-workshop
environment: dev
region: us-east-1
deployment:
  strategy: federated
  # Sentinel account ids: `deploy.sh design` refuses to deploy until replaced.
  platform_account: "000000000000"
  workload_accounts: ["123456789012"]
  federation:                      # sentinels — copy from the platform account's `deploy.sh export`
    gateway_url: "https://REPLACE_ME.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
    issuer_url: "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_REPLACEME"
    m2m_client_id: "REPLACE_ME"
    m2m_client_secret_name: "agentcore/platform-m2m"   # pragma: allowlist secret — a NAME in the workload account
identity:
  idp: entra_id
  tenant_id: "00000000-0000-0000-0000-000000000000"   # sentinel — your Entra tenant id
  client_id: "REPLACE_ME"                             # sentinel — the app registration's client id
  client_secret_name: "agentcore/idp-client-secret"   # pragma: allowlist secret — a NAME, not a value
agents:
  pattern: strands-agent
gateway:
  web_search: auto
  tools: [sample-tool]
security: {}                     # every control opt-in; enable deliberately
observability:
  transaction_search: true
```

</details>

<details>
<summary><code>presets/greenfield.yaml</code></summary>

```yaml
# Greenfield: first agent platform, one account, defaults everywhere.
# Modules: 3 4 5 6 9
project: agentcore-workshop
environment: dev
region: us-east-1

deployment:
  strategy: centralized

identity:
  idp: cognito

agents:
  pattern: orchestrator

gateway:
  web_search: auto
  tools: [sample-tool]

security: {}          # every control opt-in; enable deliberately

observability:
  transaction_search: true
```

</details>

<details>
<summary><code>presets/migration.yaml</code></summary>

```yaml
# Migration: an existing agent moves onto the platform; enterprise IdP.
# Modules: 3 4 6 7 9
# Fill in the identity block from your IdP's app registration; the client
# secret goes in Secrets Manager, only its NAME goes here.
# Preview the migration before touching AWS:  deploy.sh migrate plan
project: agentcore-workshop
environment: dev
region: us-east-1
deployment:
  strategy: centralized
  platform_account: "000000000000"  # replace with the account this migration may modify
identity:
  idp: entra_id
  tenant_id: "00000000-0000-0000-0000-000000000000"   # your Entra tenant
  client_id: "REPLACE_ME"
  client_secret_name: "agentcore/idp-client-secret"  # pragma: allowlist secret — Secrets Manager NAME, not a value
agents:
  pattern: langgraph-agent    # ignored for the image once migration: is set; still selects verify/invoke behaviour
gateway:
  web_search: auto
  tools: [sample-tool]
security: {}                  # private_dependencies need networking: true
observability:
  transaction_search: true
migration:
  source:
    platform: openshift       # where it runs today (shapes the docs, not the deploy)
    build:                    # build here so the image is arm64 (AgentCore Runtime is arm64-only);
      context: ./migration/simulation/existing-ec2-agent   # use image: for a registry reference instead
      dockerfile: Dockerfile
    port: 8000                # what the container listens on
    invoke_path: /run         # the adapter forwards POST /invocations here
    health_path: /healthz     # ...and GET /ping here
    trigger: webhook          # discovery metadata; trigger/cutover stays external and is not deployed
    env: {}                   # plain config only; secret-looking keys are refused
    secrets: [JIRA_TOKEN, GIT_TOKEN]   # ENV NAMES; values in Secrets Manager under <project>/<env>/migration/<NAME>
  target:
    runtime: agentcore        # only deployable runtime today; ec2 fails closed
    mode: adapter             # only deployable mode today; native fails closed
  network:
    private_dependencies: []  # customer-side hostnames the agent must reach (needs networking + connectivity)
    connectivity: none        # vpn|transit-gateway once private_dependencies is filled
    # receipt: {}              # after verify: VPC/subnet/SG IDs, probe evidence, tested CA version
  stages:
    # Runtime deployment is already in scope. These surrounding stages are
    # explicit opt-ins and remain customer-operated; `migrate readiness`
    # blocks cutover until each enabled stage has owner/approver/evidence/
    # rollback/approved_at/expires_at recorded under its gate.
    runtime: {}               # after verify: account, RuntimeArn, SourceHash, ECR digest, evidence, gate
    data:
      strategy: none          # retain-source or external-copy after a source-specific plan is approved
    triggers:
      strategy: none          # external-shadow; source-specific safe rehearsal, never deployed here
      # external-shadow also requires source_reference, shadow_reference,
      # safety_mode, validation_reference, and source-specific evidence.
    traffic:
      strategy: none          # external-canary (HTTP/webhook) or external-switch (schedule/queue)
      # A traffic plan also records router/source/target/metrics references,
      # canary steps where supported, abort thresholds, and an approval gate.
```

</details>

<details>
<summary><code>presets/multi-agent.yaml</code></summary>

```yaml
# Multi-agent: orchestrator + A2A sub-agents, long-term memory.
# Modules: 3 4 5 6 7 8 9
project: agentcore-workshop
environment: dev
region: us-east-1

deployment:
  strategy: centralized

identity:
  idp: cognito

agents:
  pattern: claude-sdk-multi-agent
  a2a: true
  memory:
    long_term: true

gateway:
  web_search: auto
  tools: [sample-tool]

security: {}

observability:
  transaction_search: true
```

</details>

<details>
<summary><code>presets/platform-team.yaml</code></summary>

```yaml
# Platform team: the full build — every module, as shared infrastructure.
# Modules: 3 4 5 A 6 7 8 9 C E
# Federated strategy: one shared platform account runs auth and the gateway
# for the organisation; workload accounts run their own runtimes against it.
# Replace the sentinel account ids below with real ones before deploying.
# See docs/MULTI_ACCOUNT.md.
project: agentcore-workshop
environment: dev
region: us-east-1

deployment:
  strategy: federated
  platform_account: "000000000000"      # REPLACE: aws sts get-caller-identity
  workload_accounts: ["123456789012"]   # REPLACE: each workload account id
  federation:                      # sentinels — copy from the platform account's `deploy.sh export`
    gateway_url: "https://REPLACE_ME.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
    issuer_url: "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_REPLACEME"
    m2m_client_id: "REPLACE_ME"
    m2m_client_secret_name: "agentcore/platform-m2m"   # pragma: allowlist secret — a NAME in the workload account

identity:
  idp: cognito

agents:
  pattern: orchestrator
  a2a: true
  memory:
    long_term: true

gateway:
  web_search: auto
  tools: [sample-tool]

security:
  networking: true            # VPC mode
  cloudtrail_alerting: true
  traceability: true

observability:
  transaction_search: true
  alarms: true                # CloudWatch alarms + the platform dashboard
```

</details>

<details>
<summary><code>presets/production.yaml</code></summary>

```yaml
# Production starting point: secure controls are mandatory and stateful
# resources are retained. Replace every sentinel before deploying; synthesis
# with placeholders is only a CI parity check, not production approval.
project: agentcore-production
environment: prod
region: us-east-1

deployment:
  mode: production
  strategy: centralized
  platform_account: "000000000000"

identity:
  idp: entra_id
  mode: brokered
  tenant_id: 00000000-0000-0000-0000-000000000000
  client_id: REPLACE_ME
  client_secret_name: agentcore/idp-client-secret

agents:
  pattern: strands-agent
  model_id: us.anthropic.claude-sonnet-4-6
  allowed_models:
    - us.anthropic.claude-sonnet-4-6
  memory:
    event_expiry_days: 30

gateway:
  # Production requires an explicit decision instead of region-dependent auto.
  web_search: "off"
  tools: [sample-tool]

security:
  networking: true
  cloudtrail_alerting: true
  resource_policies: true
  egress_filter: true
  require_guardrails: true
  cedar:
    enabled: true
    mode: ENFORCE
  traceability: true
  org_id: o-example1234

observability:
  transaction_search: true
  alarms: true
  alarm_email: ops@REPLACE_ME.invalid
  log_retention_days: 90
```

</details>

<details>
<summary><code>presets/security-focused.yaml</code></summary>

```yaml
# Security-focused: every opt-in control on, Cedar in LOG_ONLY first.
# Modules: 3 4 5 6 9 E
# Flip cedar.mode to ENFORCE only after inspecting decision logs on live
# traffic (docs/SECURITY_CONTROLS.md).
project: agentcore-workshop
environment: dev
region: us-east-1

deployment:
  strategy: centralized

identity:
  idp: cognito

agents:
  pattern: orchestrator
  # Fixed model + allow-list: the runtime roles' Amazon Bedrock IAM is scoped to
  # exactly this model, and every agent (including A2A sub-agents) runs it.
  # model_id is required alongside allowed_models — without it the containers'
  # baked-in defaults would silently bypass the list.
  model_id: us.anthropic.claude-sonnet-4-6
  allowed_models:
    - us.anthropic.claude-sonnet-4-6

gateway:
  web_search: auto
  tools: [sample-tool]

security:
  networking: true
  cloudtrail_alerting: true
  resource_policies: true
  egress_filter: true
  require_guardrails: true
  cedar:
    enabled: true
    mode: LOG_ONLY
  traceability: true
  # deploy.sh prompts for the org id, or export ORG_ID — find it with:
  #   aws organizations describe-organization --query Organization.Id
  org_id: ""

observability:
  transaction_search: true
```

</details>

<!-- generated by scripts/gen_platform_reference.py — do not edit by hand -->
