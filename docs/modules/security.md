<!-- markdownlint-disable MD041 -->

# Security module

The `security` stack (`stacks/security_stack.py`) is small on purpose: an AWS Key
Management Service (KMS) customer managed key (CMK) and a CloudTrail audit trail. Most of the platform's
security surface is deliberately *inside the stacks it protects*, such as an
AWS Identity and Access Management (IAM) deny in the runtime stack and a policy
engine on the gateway. Each part is turned on by its own flag. This page maps that surface. The control-by-control reference,
including the org-scope service control policies (SCPs) and their caveats, is
[Security controls](../SECURITY_CONTROLS.md) — read that before enforcing
anything; this page does not duplicate it.

## When it deploys

| Path | Setting |
|---|---|
| `platform.yaml` | `security.cloudtrail_alerting: true` |
| Env var / AWS Cloud Development Kit (CDK) context | `ENABLE_SECURITY=true` / `-c enable_security=true` |
| Guided workshop | Module E (Security Automation) |
| Preset | `security-focused` (with the rest of the account-scope set) and `platform-team` enable it |

Off by default. In a federated deployment `app.py` does not limit it to one role —
it deploys wherever the flag is on.

## What it creates

| Resource | Details |
|---|---|
| KMS CMK | Alias `alias/<project>-<env>-agentcore`, key rotation enabled, `RemovalPolicy.DESTROY` |
| S3 bucket | `<project>-<env>-cloudtrail-<account>`, S3-managed encryption, auto-delete on destroy |
| CloudTrail trail | `<project>-<env>-agentcore-trail`, single-region, logging to that bucket |

## Configuration

| Key (platform.yaml) | Env var | Default | Effect |
|---|---|---|---|
| `security.cloudtrail_alerting` | `ENABLE_SECURITY` | `false` | Creates this stack |

The stack's own `enable_kms` / `enable_cloudtrail` constructor parameters are
both hardwired to `true` by `app.py` — there is no flag to deploy one without
the other.

## Interfaces

- `KmsKeyArn` CloudFormation output.
- The `memory` stack consumes `kms_key.key_arn` directly when both stacks are
  enabled, encrypting AgentCore Memory with the CMK (and takes a stack
  dependency on `security`). Without this stack, Memory uses service-managed
  encryption.
- The `enable_traceability` alerting in the `observability` stack only fires
  on CloudTrail management events, so it *requires* this stack — the
  `platform.yaml` validator refuses `security.traceability: true` without
  `security.cloudtrail_alerting: true`.

## The security surface lives mostly elsewhere

These controls are part of the platform's security posture but deploy inside
other stacks, each behind its own flag:

| Control | Flag | Lives in | Page |
|---|---|---|---|
| Memory resource policy (in-account-only, org deny guard) | `enable_resource_policies` + `org_id` | `memory` stack | [memory.md](memory.md) |
| Egress filter (Amazon Bedrock Guardrail + interceptor Lambda, personally identifiable information (PII) masking or blocking) | `enable_egress_filter` | `gateway` stack | [gateway.md](gateway.md) |
| Cedar policy engine (`LOG_ONLY`/`ENFORCE`) | `enable_cedar`, `cedar_mode` | `gateway` stack | [gateway.md](gateway.md) |
| Guardrailed-only inference (IAM `Null`-deny + baseline guardrail per runtime) | `require_guardrails` | `runtime-*` stacks | [runtime.md](runtime.md) |
| Model allow-list (runtime role Amazon Bedrock IAM scoped to `agents.allowed_models`) | `allowed_models` | `runtime-*` stacks | [runtime.md](runtime.md) |
| Traceability (SNS + EventBridge alerting on sensitive API calls) | `enable_traceability` | `observability` stack | [observability.md](observability.md) |
| VPC endpoint policy (org-scoped, SigV4 callers only) | `enable_networking` + `org_id` | `networking` stack | [networking.md](networking.md) |
| Org-level SCPs (Memory CMK, gateway hardening, identity token path) | Terraform variables | `control-library/terraform/org-guardrails/` | [Security controls](../SECURITY_CONTROLS.md) |

All of them source their policy bodies from `control-library/` (indexed by
`catalog.yaml`), authored once as valid JSON/Cedar and consumed by CDK
(account/workload scope) or Terraform (org scope).

## Security notes

- Removal policies are `DESTROY` and the trail bucket auto-deletes: destroying
  the stack deletes the audit history. That is the workshop posture — retain
  the bucket for production.
- The trail records management events only. It is single-Region, except in
  production mode, where it is multi-Region.
- The trail bucket uses S3-managed encryption, except in production mode, where
  it and the trail use the stack's CMK.
- The CMK encrypts Memory and the Gateways. In production mode it also encrypts
  the trail and its bucket and the ops topic, and use cases encrypt their own
  tables, buckets and queues with it (`security/kms-key-arn`). Log groups do not
  use it (see [Known issues](#known-issues)).

## Verification

There is no dedicated live check for this stack itself; `scripts/verify.py`
composes checks from the deployment contract, and the controls this page maps
carry their own:

- `scripts/check_guardrail_enforcement.py` — IAM simulation that inference
  without a guardrail is denied on every runtime role (runs when `require_guardrails` is on).
- `scripts/check_network.py` — endpoint/VPC placement (networking).
- Synth-time checks for each control: [`TESTING.md`](../TESTING.md) part A.

## Known issues

- **Traceability needs this stack.** `security.traceability` without
  `security.cloudtrail_alerting` is a validation error, because the alert rule
  would never fire, and no error would say so.
- **The org SCPs can lock the platform out.** `enable_scp_memory_enforce_cmk`
  and `enable_gateway_scps` deny operations the platform's own *default*
  configuration performs. Attach them only after every covered account runs
  the prerequisites (see the compatibility preflight in
  [Security controls](../SECURITY_CONTROLS.md)).
- **Flags match the exact string `"true"`.** `-c enable_security=True`
  does nothing, without an error.
- **Cedar and the egress filter are separate controls.** Enabling the egress
  filter gives you masking, not authorization; Cedar ships `LOG_ONLY` and
  enforces nothing until `cedar_mode=ENFORCE`.
- **Log groups do not use the platform CMK.** CloudWatch Logs encrypts every log group at
  rest with keys that the service holds, and reading a group needs an IAM grant. The CMK
  would add a key-policy check on top, but nothing wires it: that needs `KmsKeyId` on each
  log group and a `logs.<region>.amazonaws.com` statement in the key policy. The scanner
  finding `CKV_AWS_158` is accepted until 2027-05-01. If your policy requires a customer key
  on logs, add both before production.
