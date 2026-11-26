# AgentCore Security Controls

Reusable, opt-in security building blocks for the AgentCore accelerator. Every control is
defined once in a **control-library** that works with any infrastructure as code (IaC) tool. The AWS
Cloud Development Kit (CDK) uses it for account and workload scope, and Terraform uses it for
org scope. How to test everything: [`TESTING.md`](TESTING.md).

**Why one library, and why valid JSON/Cedar:** authoring each control once means a policy
fix is applied in both engines at the same time. There are no duplicated policy bodies that drift apart across
languages. Keeping artifacts as valid JSON (rather than templated `.tftpl`) means policy
linters (checkov, AWS Identity and Access Management (IAM) Access Analyzer, cfn-guard) can scan every
file in continuous integration (CI). That is a real trust signal for a security accelerator. Parameters use `<<sentinel>>` tokens injected at deploy
time, never template syntax that would break linting.

## The model (scope-split)

- **`control-library/`** — single source of truth. Valid JSON / Cedar with `<<sentinel>>`
  parameters, indexed by `catalog.yaml`. See [`control-library/README.md`](../control-library/README.md).
- **Terraform** (`control-library/terraform/org-guardrails/`) — org-scope guardrails (service control policies, SCPs).
- **CDK (Python)** — account/workload-scope controls, loaded via
  `infra_utils/policy_loader.py` and toggled by feature flags.

Every security control below is off by default. Turn them on with feature flags, or use the
`security-focused` profile, which enables the account/workload set together. That set includes
`enable_security`, the AWS Key Management Service (KMS) customer managed key (CMK) + CloudTrail
stack that item 7 depends on.

To compare what the workshop, secure and production postures turn on, see the generated
[`POSTURE_TIERS.md`](POSTURE_TIERS.md) table.

Two unrelated flags elsewhere in `app.py` *do* default on — `enable_transaction_search`
(an account- and region-level CloudWatch/X-Ray setting, on because tracing does not work
without it) and `enable_a2a`. On the Terraform side, `enable_scp_memory_enforce_cmk` and
`enable_gateway_scps` default to `false` — they deny operations the accelerator's own default
configuration performs (see the compatibility preflight under Quick start). Flags are matched
against the exact lowercase string `"true"`; `-c enable_cedar=True` does nothing and reports no error.

## Controls

| # | Control | Flag(s) | Where | Default |
|---|---|---|---|---|
| 1 | **SCP: CMK-for-Memory** | `enable_scp_memory_enforce_cmk` + vars | `control-library/terraform/org-guardrails/` ← `control-library/scp/` | off (opt-in, see preflight) |
| 1b | **SCP: Gateway configuration hardening** (CMK, no-auth, policy-engine=ENFORCE, approved IdP, protocol, private-endpoint targets, credential-provider, target-type) | `enable_gateway_scps` + vars | `control-library/terraform/org-guardrails/gateway.tf` ← `control-library/scp/gateway/` | off (opt-in, see preflight) |
| 2 | **VPC endpoint policy** — action-scoped; org restriction covers **SigV4 callers only** (OAuth/JSON Web Token (JWT) callers have no IAM principal and pass via `Principal: "*"` per AWS docs). Requires `org_id`: without it the endpoint is created with **no policy at all** | `enable_networking`, `org_id` | `stacks/networking_stack.py` ← `control-library/vpce/` | org-scoped (SigV4) when `org_id` set |
| 2b | **Least-privilege runtime IAM** — AWS Systems Manager (SSM) reads are path-scoped in `infra_utils/agentcore_role.py`; `control-library/iam/runtime-execution-least-privilege.json` is a **reference policy, not deployed** by any stack (the live role still grants ECR/X-Ray/`PutMetricData` on `Resource: "*"`) | — | `infra_utils/agentcore_role.py` | partial |
| 3 | **AgentCore Cedar policies** | `enable_cedar`, `cedar_mode` | `gateway_stack.py` ← `control-library/cedar/` | LOG_ONLY |
| 4 | **Resource-based policy: Memory in-account-only** | `enable_resource_policies`, `org_id` | `memory_stack.py` ← `control-library/resource-policies/` | enforce |
| 5+6 | **Amazon Bedrock Guardrails + egress Lambda interceptor** | `enable_egress_filter` | `gateway_stack.py`, `tools/egress_interceptor/` ← `control-library/guardrails/` | off; masks or blocks per entity (see below) |
| 6b | **Guardrailed-only Amazon Bedrock inference** — IAM `Null`-deny on the runtime roles for inference calls that have no `bedrock:GuardrailIdentifier`, plus a baseline guardrail per runtime (same `control-library` artifact as item 5+6) injected into the agents via `GUARDRAIL_ID`/`GUARDRAIL_VERSION`, with `bedrock:ApplyGuardrail` allowed only on that guardrail. Incompatible with the claude-sdk patterns (they cannot attach a guardrail; the config validator refuses the combination). Verify live with `scripts/check_guardrail_enforcement.py` | `require_guardrails` | `stacks/runtime_stack.py` ← `control-library/guardrails/` | off |
| 7 | **Observability: Amazon SNS + EventBridge alerting** | `enable_traceability` | `observability_stack.py` | off |
| 8 | **AgentCore Identity: deny unverified-userId workload tokens**, plus a scoped credential-provider IAM reference policy | `enable_scp_identity_deny_token_for_userid` (Terraform var) | `control-library/terraform/org-guardrails/identity.tf` ← `control-library/scp/identity/`, `control-library/iam/` | enforce (denies everyone) |

## Feature flags

Set via CDK context (`-c flag=value`) or environment variable (`FLAG=value`):

| Flag | Env | Purpose |
|---|---|---|
| `enable_resource_policies` | `ENABLE_RESOURCE_POLICIES` | Memory resource-based policy (item 4) |
| `enable_egress_filter` | `ENABLE_EGRESS_FILTER` | Guardrail + interceptor (items 5+6) |
| `require_guardrails` | `REQUIRE_GUARDRAILS` | Guardrailed-only Amazon Bedrock inference (item 6b) |
| `enable_cedar` | `ENABLE_CEDAR` | Cedar policy engine (item 3) |
| `cedar_mode` | `CEDAR_MODE` | `LOG_ONLY` (default) or `ENFORCE` |
| `enable_traceability` | `ENABLE_TRACEABILITY` | Alerting on sensitive API calls (item 7) |
| `enable_networking` | `ENABLE_NETWORKING` | VPC + AgentCore endpoint policy (item 2) |
| `enable_security` | `ENABLE_SECURITY` | KMS CMK + CloudTrail; **prerequisite for item 7** (needs management events) |
| `org_id` | `ORG_ID` | Org ID (o-xxxx) required by items 2 and 4 |

Item 6b and the `agents.allowed_models` allow-list apply to the AgentCore runtime roles only.
Use cases run their own roles and are outside both: a use case's own role can invoke the models
it grants itself, with a guardrail only where the use case applies one (each use case's README
says how). Until the platform passes these settings to use cases, keep each use case's model
list to the approved models and turn on its guardrail where 6b is required.

Item 4 fails without `org_id`. Item 2 instead creates an endpoint with no policy, and reports no error.
`scripts/deploy.sh` fails fast (or prompts) when resource policies are on and `ORG_ID` is empty.

## Quick start

```bash
# Everything account/workload-scope, in safe (log-only / masking) defaults:
export ORG_ID=o-yourorgid
./scripts/deploy.sh deploy --profile security-focused

# Or one control at a time:
ENABLE_CEDAR=true ./scripts/deploy.sh deploy --module 5

# Org guardrails (from the Organizations management account):
terraform -chdir=control-library/terraform/org-guardrails init
terraform -chdir=control-library/terraform/org-guardrails apply \
  -var 'target_ids=["ou-..."]'
```

### Org SCP compatibility preflight

The memory and gateway SCPs deny operations the accelerator's own default configuration
performs, which is why they are opt-in (`default = false`). **Attaching them over a default
deployment locks the platform out of its own control plane** — CreateMemory and gateway
create/update calls start failing org-wide. Before flipping either variable on, the covered
accounts must already run:

| SCP | The accelerator must have enabled first |
|---|---|
| `enable_scp_memory_enforce_cmk` | `enable_security` (the KMS CMK stack) with the CMK on **every** covered account's memory — the default memory stack creates Memory with no CMK |
| `enable_gateway_scps` | A gateway CMK plus Cedar in `ENFORCE` (`enable_cedar` + `cedar_mode=ENFORCE`) — the security-focused preset runs `LOG_ONLY`, which `require-policy-engine` denies |

`enable_scp_identity_deny_token_for_userid` stays on by default: it denies an API path the
accelerator never uses.

## Egress interceptor design (items 5+6)

- **Where it hooks:** on the Gateway target path as an interceptor Lambda
  (`tools/egress_interceptor/`), inspecting requests and responses before egress to
  tools/targets.
- **What it does:** masking or filtering of personally identifiable information (PII), and
  prompt-injection checks, through Amazon Bedrock Guardrails (`ApplyGuardrail`). Its configuration
  comes from `control-library/guardrails/`. The guardrail itself performs **no authorization**:
  Cedar (item 3) is a separate control behind a separate flag, and `enable_egress_filter` alone
  gives you none of it. A use case's `gateway.interceptor_extensions` checks run in the same
  Lambda, before the guardrail. They may refuse calls to that use case's own targets
  (`CONTRIBUTING_USE_CASES.md`).
- **Masking, or blocking per entity:** the handler raises an error only when an assessment comes
  back `action == "BLOCKED"`, and then the whole call or response is refused. Otherwise it
  substitutes anonymized text and the request proceeds.
- **What is scanned:** a payload's string leaves go in one `ApplyGuardrail` call per 50,000
  characters. A request's per-value blocks go in at most 50 text units a call. These parts are
  not scanned:
  - the answer to a `tools/list` request (decided by the request's method, not the response's
    shape)
  - the Model Context Protocol's (MCP's) own `params._meta` and `result._meta`
  - the response headers
  - the responses of the tools that `security.egress_unscanned_responses` lists (their requests
    are scanned)

  A `_meta` key inside tool arguments or a tool result is scanned.
- **Request action:** `security.egress_request_action` is `mask` (default) or `block`. With
  `block`, the whole `tools/call` request is refused if the guardrail would rewrite any of its
  strings. The reason says it was refused rather than rewritten, and the body is never logged.
  The target therefore receives only the body that a use case's checks approved. Responses are
  masked either way.
- **Entities:** `security.egress_pii_entities` in `platform.yaml` sets the action
  (`BLOCK`, `ANONYMIZE`, or `NONE`) per PII entity type over the artifact's own. `NONE` leaves
  the entity out of the policy. The Gateway's guardrail is shared, so `NONE` stops masking
  that entity in every use case's tool traffic, not only one use case's. Under
  `deployment.mode: production`, `US_SOCIAL_SECURITY_NUMBER` and `CREDIT_DEBIT_CARD_NUMBER`
  are always `BLOCK`, on the Gateway and in every runtime's baseline guardrail. Validation
  refuses a configuration that lowers one. Each runtime also blocks `AWS_ACCESS_KEY` and
  `AWS_SECRET_KEY` in the model's output. Neither is screened in the model's input or the
  Gateway's tool data, because the detector reads some random identifiers (a case id on its
  own) as a secret key (`infra_utils/guardrail_policy.py`).
- **Caveats:** the `gatewayRequest`/`gatewayResponse` shape is unvalidated, so the handler
  scans every string leaf generically. When a guardrail is configured, it refuses a payload
  shape it does not recognise, because it cannot scan a `tools/call` it cannot read. It passes
  through an unrecognised shape only when the payload names another method. When
  `ApplyGuardrail` cannot be applied at all (an Amazon Bedrock throttle, a quota, a validation
  error), the call is refused with the same tool error a block gives. An unscanned call never
  goes through. Validate against live Gateway traces before relying on it in
  production.
- **Alerting:** with `security.traceability`, a code, configuration or permission change to
  the `<prefix>-egress-interceptor` function alerts the security topic (item 7), because every
  use case's checks run in it.
- **Toggle:** `enable_egress_filter`, deployed by `gateway_stack.py` like every other flag.

## Cedar authorization (item 3)

Cedar is implicit-deny: anything not explicitly permitted is denied. The repo previously
shipped a blanket `forbid` alongside the read permit, and it was removed. In Cedar a matching
`forbid` overrides every `permit`, so it made the permit dead code and denied everything.

What that means in practice, before you describe this as a default-deny gateway:

1. `enable_cedar` defaults off, so no policy engine is attached and no Cedar evaluation happens.
2. `cedar_mode` defaults to `LOG_ONLY`, so decisions are logged, not enforced. Nothing is
   actually denied until `cedar_mode=ENFORCE`.
3. The one shipped policy is unconstrained on principal and restricted to its own Gateway
   (`permit(principal, action in [AgentCore::Action::"<<read_action>>"], resource ==
   AgentCore::Gateway::"<<gateway_arn>>");`; CreatePolicy rejects an open `resource`), so any
   authenticated caller may invoke the sample tool. Narrow it before enforcing.

## Adding a control

1. Add the JSON/Cedar file under `control-library/<type>/` and register it in `catalog.yaml`.
2. Load it in a stack via `policy_loader.load_control[_json|_text]` behind a feature flag,
   or reference it from a Terraform module with `replace()`.
3. `make validate-controls && make test-controls` — then add a synth check to `TESTING.md`.

## AgentCore Identity controls (item 8)

Two controls covering the token path into the credential vault.

**SCP: deny `GetWorkloadAccessTokenForUserId`.** That API takes the user identifier as an
unverified string. Any principal that has the action can create a workload access token for any
user and read that user's stored credentials out of the token vault. The call needs no JWT and
no proof of identity. Agents behind Runtime or Gateway inbound auth never need it:
the caller's verified token arrives with the request, and `GetWorkloadAccessTokenForJWT` is
the path that checks it.

The exemption parameter defaults to a role ARN that cannot exist, so the control denies
everyone until an operator supplies a real pattern. Narrow it only for a genuine break-glass
or migration path, and prefer removing the need over widening the pattern:

```bash
terraform -chdir=control-library/terraform/org-guardrails apply \
  -var 'target_ids=["ou-example-11111111"]' \
  -var 'identity_approved_principal_arn_pattern=arn:aws:iam::111122223333:role/break-glass'
```

You may need the userId path for a specific service, and want something narrower than an ARN
exemption. The action also supports the `bedrock-agentcore:userid` condition key, so the deny
can be scoped by user identifier instead. The blanket form is the safer default.

Quota note: this is a standalone SCP, bringing the module to 3 attachments per target against
4 usable slots (the 5-per-target limit minus `FullAWSAccess`). A fourth standalone SCP is the
last one that fits — beyond that, merge statements the way `gateway.tf` does.

**IAM: `iam.identity-credential-provider-scoped`.** A reference policy for an agent's own
execution role. AgentCore [does not enforce any binding][scope-cp] between a workload identity
and the credential providers it may read. IAM is therefore the only boundary: use one workload
identity and one role per trust boundary, each naming exactly one provider. A shared execution role hands
every agent every provider's credentials.

Note each `Allow` lists the parent resources as well as the leaf. `GetResourceOauth2Token` and
the `GetWorkloadAccessToken*` actions declare several **required** resource types: the
directory and the token vault, as well as the workload identity and the provider. A
statement naming only the provider ARN therefore authorises nothing. The resulting `AccessDenied` is
easy to "fix" by widening `Resource` to `"*"`, which defeats the control entirely. `Deny`
statements need only the specific ARN they target.

Like `iam.runtime-execution-least-privilege`, this is a template. The deployed runtime roles
follow the same shape without loading it. `RuntimeStack` grants:

- the `GetWorkloadAccessToken*` actions on the runtime's own workload identity
  (`<runtime name>-*`) only
- `GetResourceOauth2Token` and the vault secret only for the providers the runtime declares in
  `credential_provider_names`

None of them is granted on `*`.

Retrieving a token is also not the same as retrieving credentials. What a workload gets back
is scoped to the user identity in its workload access token. For three-legged OAuth (3LO)
providers, the end user must complete authorization before any credentials exist to return.

[scope-cp]: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/scope-credential-provider-access.html

## Gateway configuration hardening (control-plane SCPs)

Enterprise guardrails built on the launched AgentCore **Gateway configuration** condition keys.
They constrain what admins can create/update (control plane), enforced via
`control-library/terraform/org-guardrails/`:

| SCP | Condition key | Effect |
|---|---|---|
| `require-cmk` | `KmsKeyArn` | Gateway must use a (approved) CMK |
| `deny-no-auth` | `GatewayAuthorizerType` | Block `NONE` (no unauthenticated gateways) |
| `require-policy-engine` | `PolicyEngineArn` / `PolicyEngineMode` | Require a Cedar policy engine in `ENFORCE` |
| `enforce-approved-idp` | `DiscoveryUrl` | JWT gateways must use an approved IdP |
| `restrict-protocol` | `ProtocolType` | Restrict explicitly-set protocols to `MCP` (an omitted protocol falls back to the service default, unconstrained) |
| `targets-require-private-endpoint` | `PrivateEndpointType` | Targets must use a private endpoint (private egress) |
| `targets-restrict-credential-provider` | `CredentialProviderType` | Deny `API_KEY` / `JWT_PASSTHROUGH` |
| `targets-restrict-type` | `McpTargetConfigurationType` | Allow-list target types (lambda, mcpServer) |

**They deploy as one policy, not eight.** Organizations allows only 5 SCPs per target (4 usable
after `FullAWSAccess`), so `gateway.tf` flattens all eight documents into a single consolidated
SCP, `${name_prefix}-scp-gateway-guardrails`. Consequences: `enable_gateway_scps` is
all-or-nothing (subsetting means editing the map in `gateway.tf`), and a `lifecycle`
precondition enforces the 5,120-character SCP limit plus Sid uniqueness — a ninth control can
fail at plan time. The `for_each` is on the *attachment*, over `var.target_ids`.

**"Fully-private Gateway" bundle:** `deny-no-auth` + `require-policy-engine` +
`targets-require-private-endpoint`. These are control-plane controls: they constrain how a
gateway may be configured, not who may invoke one.

For invoke-time restrictions, per-gateway resource-based policies attached with the native
`AWS::BedrockAgentCore::ResourcePolicy` (as used for Memory in item 4) are the mechanism this
repo uses today.

## Safe rollout

- Cedar ships `LOG_ONLY`; validate decision logs before `cedar_mode=ENFORCE`, and narrow the
  shipped permit's principal first. Its resource is already restricted to the Gateway.
- SCPs are additive-deny. Attach them to a sandbox organizational unit (OU) first.
- Every guardrail the stacks create is published as a numbered version
  (`infra_utils/guardrail_policy.py`), and the interceptor and runtimes name that version, never
  `DRAFT`.
- Subscribe an endpoint to the item-7 SNS topic to actually receive alerts. The topic is not
  KMS-encrypted — add a CMK if alert contents are sensitive in your environment.
- Item 7 needs CloudTrail management events, so enable it together with `enable_security`.
