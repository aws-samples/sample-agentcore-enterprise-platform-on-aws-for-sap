<!-- markdownlint-disable MD041 -->

# Memory module

The memory module (`stacks/memory_stack.py`) creates the platform's AgentCore
Memory store: conversation history that survives across sessions, partitioned
by caller. The partition key (`actor_id`) is the verified `sub` claim from the
caller's JSON Web Token (JWT). The memory store is where the platform's
identity verification matters most: an unverified identity would mix different
callers' history under one actor ([Verify caller identity](../IDENTITY.md)). Short-term memory
(raw conversation events) is always on; long-term memory (semantic fact
extraction) is opt-in because it incurs additional inference cost.

## When it deploys

| Configuration | Deployed? |
|---|---|
| Centralized / distributed (the default) | Always |
| Federated, platform account | No — the platform account runs no agents, so it needs no memory |
| Federated, workload account | Yes — **each workload account gets its own memory**, deliberately: conversation history is the boundary between workloads, and account isolation is the strongest boundary available |

Source: `expected_stacks()` in `infra_utils/platform_config.py` adds
`{prefix}-memory` when the federated role is not `platform`; `app.py` mirrors
the condition.

## What it creates

| Resource | Name pattern | Purpose |
|---|---|---|
| `AWS::BedrockAgentCore::Memory` | `{project}_{environment}_memory` (hyphens become underscores) | The memory store; events expire after 30 days |
| Memory strategy: `user_preference` | `{name}_user_pref`, namespace `USER_ID` | Unless `agents.memory.strategies` is set — user preference tracking |
| Memory strategy: `semantic` (optional) | `{name}_semantic`, namespace `AGENT_ID` | With `use_long_term_memory=true`, or when listed in `agents.memory.strategies` — semantic fact extraction |
| `AWS::BedrockAgentCore::ResourcePolicy` (optional) | attached to the memory ARN | Only with `enable_resource_policies=true` — in-account-only access (see Security notes) |
| 2 × `AWS::SSM::Parameter` (AWS Systems Manager) | `/{project}/{environment}/memory/*` | `memory-id`, `memory-arn` |

## Configuration

Precedence at synth (when `app.py` builds the templates): AWS Cloud Development Kit (CDK) context > env var > `platform.yaml` > default.
`deploy.sh` reads env vars, then `platform.yaml`, then `workshop.env`.

| cdk context | Env var | `platform.yaml` | Default | Effect |
|---|---|---|---|---|
| `use_long_term_memory` | `USE_LONG_TERM_MEMORY` | `agents.memory.long_term` | `false` | Adds the semantic strategy |
| `enable_resource_policies` | `ENABLE_RESOURCE_POLICIES` | `security.resource_policies` | `false` | Attaches the in-account-only resource policy |
| `org_id` | `ORG_ID` | `security.org_id` | `""` | Required when resource policies are on; synth fails without it |
| `enable_security` | `ENABLE_SECURITY` | `security.cloudtrail_alerting` | `false` | Indirect: when the security stack exists, its AWS Key Management Service (KMS) customer managed key (CMK) becomes the memory's `encryption_key_arn` |

## Interfaces

SSM parameters under `/{project}/{environment}/memory/` (part of
[the platform interface](../PLATFORM_INTERFACE.md)):

| Parameter | Value |
|---|---|
| `memory-id` | The AgentCore Memory id |
| `memory-arn` | The memory ARN |

Stack outputs `MemoryId` / `MemoryArn` are also **exported** as
`{project}-{environment}-memory-id` / `-memory-arn` — the only module of the
three with CloudFormation exports. External consumers should still use the SSM
parameters; exports are not part of the platform interface. `app.py` injects
`MEMORY_ID` (plus `USE_LONG_TERM_MEMORY`)
into the orchestrator runtime's environment.

## Security notes

- **Encryption at rest with a CMK is opt-in**, and depends on the security stack
  (`security.cloudtrail_alerting` / `ENABLE_SECURITY`): when that stack
  exists, its KMS key ARN is passed as `encryption_key_arn`; otherwise the
  memory uses the service default. Enforcing CMK-only memory across an
  organization is a service control policy (SCP) (`control-library/scp/memory/enforce-cmk.json`,
  applied via `control-library/terraform/org-guardrails/`), not something this stack does —
  see [Security controls](../SECURITY_CONTROLS.md) and
  [TESTING.md](../TESTING.md) items B2/B4.
- The optional resource policy
  (`control-library/resource-policies/memory/in-account-only.json`) allows
  `bedrock-agentcore:*` on the memory for the account root and denies any
  principal outside the AWS Organization (`aws:PrincipalOrgID`), with a
  `aws:ViaAWSService: false` guard so service-linked access keeps working.
- Data-plane access comes from the **runtime role**
  (`stacks/runtime_stack.py`): `CreateEvent`, `GetEvent`, `ListEvents`,
  `DeleteEvent`, `ListSessions` and `RetrieveMemoryRecords`. Each is restricted
  to this deployment's Memory (`memory/<project>_<environment>_memory-*`), so a
  runtime never reaches another deployment's Memory in the same account.
- Isolation between users inside one memory is `actor_id`. It is
  application-level, and relies on the JWT verification in
  `agent-code/shared/auth.py`.
- Isolation between runtimes is AWS Identity and Access Management (IAM), on
  the `bedrock-agentcore:actorId` condition key. It keeps a use case's
  conversations out of reach of the platform's agents and of other use cases:
  - Actor ids starting `uc-` are reserved for use cases.
  - A runtime built with `memory_actor_prefix="uc-<name>:"` may use only actor
    ids under that prefix. It receives the prefix as `MEMORY_ACTOR_PREFIX`, and
    must prepend it.
  - Every other runtime is refused all of `uc-*`.
- `RetrieveMemoryRecords` names a namespace, not an actor, so IAM restricts it
  to the same partition in a different way:
  - on `bedrock-agentcore:namespace` and `namespacePath`, whose strategy
    templates contain the actor (`/strategies/{memoryStrategyId}/actors/{actorId}/`);
  - and on this deployment's Memory ARN.

  A platform runtime's `namespace` and `namespacePath` must each name an actor,
  and the `u` and `uc` actor stubs are refused. So a platform runtime cannot
  search above the actor level or across the `uc-` actors.
- Account isolation in a federation is the stronger boundary.

## Verification

`scripts/verify.py` runs `scripts/test_memory.py` whenever memory is in the
footprint. It runs five checks against the deployed data plane, and exits
non-zero on failure: it creates real conversation events, lists them, fetches
one by id, exercises pagination, and confirms an invalid memory id is rejected. The orchestrator
invoke (`scripts/invoke.py`) then exercises the same memory through the agent
path.

## Known issues

- **Memory is per-workload in a federation — by design, not omission.** There
  is no shared memory in the platform account. If two workload accounts need
  shared history, that is an architecture decision to make explicitly, not a
  flag to change ([Multi-account federation](../MULTI_ACCOUNT.md)).
- Framework memory integrations need more than `CreateEvent`: LangGraph's
  checkpointer lists events to rehydrate a thread, which is why the runtime
  role includes `ListEvents`/`ListSessions`/`DeleteEvent`. A pattern that
  worked on one framework can fail at invoke on another — the
  [pattern matrix](../TESTING.md) exists to catch exactly this.
- The memory **name** uses underscores (`{project}_{environment}_memory`)
  while every other resource uses hyphens — AgentCore Memory naming rules,
  worth knowing before you grep for it.
- Enabling `enable_resource_policies` without `org_id` fails at synth with a
  clear message; a placeholder org id (`o-REPLACEME`) is rejected by
  `platform.yaml` validation before it can render into a policy that matches
  nothing.
- Long-term memory extraction runs additional inference — the flag is off by
  default for cost, not capability, reasons.
- **Switching `agents.memory.strategies` on a live stack leaves the old records behind.**
  Unset, the strategies use the literal namespaces `USER_ID` and `AGENT_ID`. A list uses
  per-actor templates (`/strategies/{memoryStrategyId}/actors/{actorId}/`). The change
  updates the Memory in place, but records already extracted under the old namespaces are
  not moved, and the new strategies do not read them. This in-place update is not tested
  live. Choose the strategies before the first deploy, or accept that long-term records
  start again.
