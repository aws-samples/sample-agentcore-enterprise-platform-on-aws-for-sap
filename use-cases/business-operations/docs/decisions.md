# Design decisions

Terms are defined in the [glossary](glossary.md) and in [Concepts](../../../docs/start/concepts.md).

Why this use case is built the way it is. Most decisions come from the earlier sample that this
use case grew from. Where this repository changed a decision, the entry describes it as it is now.
You do not need this page to deploy. Paths are relative to `use-cases/business-operations/`
unless they start with `layers/` or `infra_utils/`. A standard operating procedure (SOP) is
called a procedure below. "The agent" means the business process agent, and "the Gateway" means
the AgentCore Gateway.

## SAP access

> **SAP API Policy.** Use of SAP APIs is subject to the
> [SAP API Policy](https://help.sap.com/doc/sap-api-policy/latest/en-US/API_Policy_latest.pdf)
> and your agreements with SAP. Review both before connecting this sample to an SAP system.

### The agent reaches SAP only through the AWS for SAP MCP Server, behind the Gateway

The agent's SAP reads, writes and service discovery all go through the AWS for SAP MCP (Model
Context Protocol) Server. There is one server runtime per SAP system, attached to the platform's
shared Gateway as the target `sap-<system>`. The SAP layer deploys AWS's image itself, fixed to
one image tag. The Gateway calls the server with its own IAM (AWS Identity and Access Management)
role. A server that you deployed yourself is attached through `runtime_arn`.

The agent learns a service's fields from the SAP system itself (`find_sap_services`,
`get_metadata`, `get_service_hints`), not from specs kept here. An optional knowledge base of
example API specs (`packs/_seed/api-docs/`, `api_docs_search`) is off by default.

**Why:** with one path to SAP, there is one place to authorize, log and refuse calls. AWS
maintains the protocol layer, the inbound authentication and the OData (Open Data Protocol)
handling, so this repository does not have to write them. `$metadata` is the system's own
description of its services, custom fields included.
**Where:** `layers/sap/stack.py`, `layers/sap/lib/sap_layer/config.py`,
`layers/sap/walkthrough.md`, `business_operations/knowledge.py`.

### Deletes are never available; writes are on by default and set per system

Nothing can call `odata_delete`. The SAP layer's check refuses it for every caller, and no Cedar
permit names it. The image that the layer deploys also starts with deletes turned off. A server
that you attach through `runtime_arn` keeps whatever its owner set, and the check still refuses
the call.

`odata_create`, `odata_update` and `odata_function_import` are on by default, so a use case that
posts to SAP works on its first deploy. The exception is an `obo` system (principal propagation:
SAP sees the signed-in person), whose `writes` defaults to `[]`. `writes: []` makes any system
read-only.

**Why:** a delete erases a record that an auditor may need, and no shipped procedure uses one.
The write default follows this repository's rule: defaults that make adoption easy, with the risk
stated. It replaces the earlier sample's read-only setting for production. Under `obo`, no Cedar
statement can grant a write yet. **Where:** `layers/sap/sap_check/decide.py`,
`layers/sap/lib/sap_layer/config.py` (`_complete`), `layers/sap/stack.py`.

### Three functions read SAP directly, as the system's service account

Three functions call SAP outside the Gateway:

- the SAP poller;
- the ingest consumer, which reads a pack's query again when SAP sends a short notice (a nudge)
  that something changed;
- the test-data seeder.

They use the system's service-account secret: Basic authentication, or a client-credentials
token on `m2m-sap` and `m2m-oidc`. They call only the systems that the operator lists under
`access.<use case>.bypass_gateway`. The seeder also writes: it posts test documents, and the
evaluations read back through it. The seeder is refused under `deployment.mode: production`.

**Why:** a scheduled or queued function has no person and no Gateway session. A direct read of
one filtered entity set also costs less than an agent turn. Each direct system must be listed,
so the operator can see every path that does not go through the Gateway. **Where:** `business_operations/pipeline.py` (`_grant_sap`),
`pipeline/sap_poller.py`, `pipeline/sap_ingest.py`, `test_data/index.py`,
`business_operations/test_data.py`, `layers/sap/lib/sap_layer/access.py` (`grant_direct`),
`platform.example.yaml`.

### Surveying SAP and scoping a use case are done in other repositories

These are done outside this repository: the capability survey, the generation of CDS (Core Data
Services) views, the activation of OData services, and ABAP writes. This repository only uses
services that are already published. The analyst interview that scopes a use case also runs from
the analyst's own LLM client. Its first draft of a pack arrives here as a document, through the
create route or a bundle.

**Why:** the boundary is which system the code connects to. Neither job needs this stack's
credentials, but authoring a pack does, so authoring stays here. On S/4HANA the shipped pack reads
`API_SUPPLIERINVOICE_PROCESS_SRV`, which SAP publishes, so a first deploy needs no survey. The
resolver is for SAP ECC (ERP Central Component), or for a system that lacks a service. **Where:**
`packs/finance_ops/pack.json`, `skills/dev-new-sap-process/SKILL.md`, `packs/pack.schema.json`
(the one contract the repositories share).

## Run checks and autonomy

### Deterministic checks at the Gateway, not instructions in the prompt

The run checks are a check in the Gateway interceptor. They run before Cedar, in every Cedar
mode, for this use case's checked tools: SAP writes, the case, ticket, process-type and pack
tools, and the agent's own SAP reads. (The operator's `reader` app client reads without a grant.)
For a checked call, the run checks:

- require the run grant that the invoker wrote (the invoker deletes the grant when the run ends);
- refuse anything that changes data during a rehearsal;
- refuse an SAP write that does not name the case's own document (`case_document_check`);
- make a write wait for a person's approval when it is above `AUTO_WRITE_LIMIT_USD`, or when it
  includes an amount above that limit or above the case's amount (`write_amount_field`);
- keep case and pack tools inside the run's packs.

The agent repeats none of these checks. It keeps only the checks that need SAP reads or a model:

- the write guards (`write_guards`), which read SAP again before a payment write;
- the verifier, a second model that must agree with every payment write before it runs.

**Why:** a prompt gives probable results, not certain ones, and text in the input can change
what the model does (prompt injection). The Gateway is outside the model's reach. The earlier
sample relied on Cedar role checks, which protect only while Cedar is in `ENFORCE`. **Where:**
`business_operations_check/run_checks.py`, `business_operations/gateway_run_checks.py`,
`agent/utils/write_gate.py`.

### Cedar is the platform's engine, and it starts permissive

Each use case has its own Cedar statements on the platform's one policy engine. They match this
use case's scopes (brokered identity), or its apps' `azp` (direct Entra ID). The engine is off
unless `security.cedar.enabled` is set, and it starts in `LOG_ONLY`. `security.cedar.mode` sets
`ENFORCE`. `ops.py production-check` fails until Cedar is in `ENFORCE`. Statements are created
one at a time, because `CreatePolicy` is not idempotent on retry.

**Why:** this replaces the earlier sample's `ENFORCE` default, to make adoption easier. The run
checks and the SAP layer's check run in every mode, but some controls work only under `ENFORCE`:

- The SAP check lets a write through when its SAP access claim names no governing check only while
  Cedar is in `ENFORCE`.
- Without Cedar, use cases are not separated from each other.
- Without Cedar, a reader client can call write tools that the run checks do not check.

`production-check` names that gap. The table in
[SAP access limits](architecture.md#sap-access-limits) shows which controls apply in each Cedar
mode. **Where:** `infra_utils/platform_config.py`
(`CedarConfig`), `sap_kit/cedar_policies.py` (`add_policies`),
`business_operations/policies.py`, `business_operations/cedar.py`,
`business_operations/production_check.py`.

### The trigger mode starts at `manual`

Cases are created without a person. But no run starts without a person until an admin sets two
trigger modes to `auto`: the deployment's and the pack's. The commands are
`PUT /deployment/trigger-mode`, `PUT /packs/{pack_id}/trigger-mode` for a pack, or
`ops.py trigger-mode set auto [--pack <pack_id>]`. A pack with no trigger mode set is `manual`,
so a new pack starts on `manual` while another pack runs on `auto`.

**Why:** `auto` lets runs make SAP writes up to `AUTO_WRITE_LIMIT_USD` with no one watching.
`manual` decides when a run starts, not what it writes. The run checks decide what it writes.
The pack setting exists so that one new pack can stay `manual` while the others run on `auto`.
**Where:** `business_operations/pipeline.py`, `pipeline/case_store.py`
(`Gate`), `api/trigger_mode/index.py`.

### The agent has no tool that rewrites its own instructions

Procedure and pack writes go through `/packs/{pack_id}/sops` and `/packs/{pack_id}`, made by a
person in the authors group. The agent is denied the procedure management tools, even on a
discovery run, which has no pack allowlist.

**Why:** an agent that can edit its procedures can rewrite the rules that it is judged against. **Where:** `agent/utils/mcp_topology.py` (`_AGENT_DENIED_TOOLS`),
`api/sops/index.py`, `api/layer/python/sop_store.py`.

### SOP citations are verified by quoted text

When the agent cites a procedure in a case update or notification, it quotes the sentence that
applies. The runtime checks that quote against the procedure text that this run actually
received. That text is the procedure in its prompt, with values filled in, plus every procedure
lookup in the same run. A quote must be at least 6 words, and at most two sentences and 320 characters.
`verified: false` means the quote was not in that text. It does not mean the procedure was not
followed.

**Why:** an agent can write a real clause number next to a rule it made up, and many procedures
are not numbered. An exact text match always gives the same result, and an auditor can follow
it. A verified quote also has `section`: the number of the clause that the quote starts in. The
runtime reads it from the same text, from the nearest clause number above the quote, so the
console can show "Price variance 2.2". `section` is null for a quote that is not verified and for
a procedure without numbers. The clause number that the agent writes is never used. **Where:** `agent/utils/evidence.py`.

### Case audit fields are written by the server

The case Lambda function sets `updated_at` and adds entries to `action_log`. When a case enters
`awaiting_human_input`, it sets `inquiry_sent_at`. That value stays while the case is in that
status, and is removed when the case leaves it. Values that the model sends for these fields are
ignored. Because every wait sets `inquiry_sent_at`, it does not say what the case waits for. The
ticket tool records that: when it links a ticket to a case, it writes the ticket's `response_type`
on the case as `ticket_response_type`. The console shows **Needs approval** for `approval` and
**Needs a reply** for `free_text`. A case with no ticket is shown as needing approval when it offers
outcomes or a proposed change, and as needing a reply otherwise.

**Why:** the record of how long a case has waited for a person must not depend on the model. **Where:**
`case_management/handler.py`, `tickets_tool/handler.py`.

## Packs and SOPs

### Domain expertise is configuration: a pack

A use case's knowledge is a pack under `packs/<pack_id>/`. It has `pack.json` (tools, model
tier, SAP source, fields, constants, entitlement), `base_prompt.txt`, and one procedure per
`process_type`. The router maps a case's `process_type` to its pack and procedure. Adding a
process type is a pack edit, not an agent change.

**Why:** procedures change far more often than code, and the people who own them do not release
code. **Where:**
`agent/utils/skill_router.py`, `packs/pack.schema.json`, `packs/finance_ops/`.

### The whole SOP goes into the prompt, and tunables stay out of the prose

The router puts the complete procedure for the case's process type into the prompt, not
fragments found by search. Thresholds are `{{SYMBOL}}` constants from `pack.json`. Addresses are
`{{CONTACT_<NAME>}}` values from the use case's `contacts`. Both are filled in the same way in the
prompt and in `load_sop`. Search across procedures (`search_sap_sops`) is a knowledge base on S3
Vectors that ingests with `NONE` chunking, so there is one vector per procedure.

**Why:** a known procedure should run the same way every time, and changing a tolerance should
not mean rewriting a procedure. A search result is a whole procedure, never a mix of two. S3
Vectors has no minimum compute cost while idle. **Where:** `agent/utils/skill_router.py`,
`agent/utils/config_overrides.py`, `sop_tools/handler.py`,
`business_operations/knowledge.py`.

### One registry object is the trust boundary, with one writer

Every runtime reads all packs from one `registry.json` when it starts: the procedure index, the
entitlement map, the `process_type` map and the constants allowlist. The registry is in its own
versioned bucket. A bucket policy denies writes to every principal except the pack composer. If
the registry is missing or cannot be read, the runtime refuses to work (it fails closed).

**Why:** whoever can write the registry can give themselves a pack. So it has one writer, which
IAM cannot override. It is one object, so no reader ever sees only half of the packs. **Where:** `agent/utils/registry.py`,
`business_operations/packs.py`, `pack_composer/index.py`.

### Packs move between environments as bundles

The development stage creates pack versions. The quality and production stages (QAS and PRD in
SAP terms) only import them. The operator uses `ops.py bundle export`, `import` (dry run, then
apply), `rollback` and `retire`. These invoke the stack's `pack_composer` over IAM, and the
composer enforces the stack's `promotion.role`.

The pack import at deploy brings checkout packs only into a development stack. It takes a pack
with no version, or one that changed since the last deploy imported it. Every stack adopts its
active packs as version 0. Outside development, the whole pack, structure and content (prompt,
procedure text) alike, arrives only by import, with a change reference. Only current settings
are changed in place there:

- `PUT /deployment/settings`: contacts;
- `PUT /packs/{pack_id}/controls`: write limits lowered below the pack's value.

**Why:** a pack is instructions that move money. It is reviewed and tested once, then promoted
like an SAP transport, in the same way that SAP customizing arrives in production by transport.
`gateway_tools` is a real grant, because Cedar permits are scoped to actions. This replaces the
earlier sample's command that published from a checkout. **Where:** `business_operations/bundle_ops.py`,
`api/layer/python/bundle.py`, `api/layer/python/pack_tiers.py`, `pack_composer/index.py`.

### Creating a pack is a spec, and the pack is born incomplete

`POST /packs` with a `create` body accepts a fixed set of spec keys, and builds the pack
document itself. Its `gateway_tools` is a set that the stack decides: reads, case state and a
ticket, never an SAP write or `send_notification`. Every SAP value is an `<AWAITING_SURVEY>`
placeholder. So the poller never sees the pack, and no case routes to it, until a pack admin
completes it with SAP values. Creates are refused outside development.

**Why:** a create form that accepted a tool list would let a browser form issue an SAP write
grant.
**Where:** `api/layer/python/pack_compose.py`, `agent/utils/registry.py` (`is_incomplete`).

## Runtime

### Cost is set per pack: model tier, turn limit, caching, exemplars

Each pack declares `model_tier`, `max_turns` and `run_budget`. A hook cancels a run that goes
past its turn limit, or its token or cost budget, through the same `agent.cancel()`. The model
runs with prompt caching (`CacheConfig(strategy="auto")`), because the system prompt is large and
is the same across cases of one process type.

A daily job, the exemplar builder, takes the best-rated completed cases of each process type. It
shortens them into exemplars: worked examples of the steps that resolved a case. The agent reads
them when a run starts. Agent knowledge (`knowledge.agent_knowledge`) is off by default. When it
is on, the same job also writes two more kinds of record:

- precedents: what happened on comparable cases of the same supplier;
- lessons: a pack author's comment on a case that they rated down.

The agent reads both through `get_precedent`. A team needs agent knowledge only when its procedures use that supplier history
or links between suppliers, because it adds a standing database cost
([Configuration](configuration.md#knowledge)).

**Why:** most turns are routine tool calls, so the cheapest model tier that passes the
evaluations should run them. The earlier sample's split into an orchestrator agent and specialist
agents was removed. **Where:**
`agent/basic_agent.py`, `agent/utils/hooks.py` (`MaxTurnsHook`),
`pipeline/exemplar_builder.py`.

### A case id is derived from the SAP natural key

`case_id` is `{short_id}-{base32(sha256(pack_id, key))[:26]}`. Here `key` is the ordered map of
SAP fields that identifies the exception, with any number of fields. The record keeps `key` and
`title`, and logs print both. `GET /cases?key=<pack_id>:<json>` finds a case from its SAP key.
Creating the id refuses a blank key field.

**Why:** the id is used in DynamoDB, SQS, URLs, AgentCore Memory and AppSync channels at the same
time. It must work in all of them, whatever characters an SAP key contains. Because the id is
derived from the key, every producer can detect a duplicate without an index.
**Where:** `agent/utils/case_key.py`, `types/case-id-vectors.json`, `api/cases/index.py`.

### A sweeper recovers cases left in `detected`

A scheduled batch sweeper queues again the cases that are still `detected` after a minimum age
(15 minutes by default). It queues at most 100 per sweep, on the same queue and invoker as every
other run, as the use case's machine client. Under `manual` it does nothing.

**Why:** the poller and push queue a case only when they create it. A case created under
`manual`, or one whose queueing failed, has no other way to be queued. A message can be delivered
more than once. The invoker records that the case has a run in progress before it starts, so a second delivery
does not run the case twice. **Where:** `pipeline/batch_runner.py`, `business_operations/pipeline.py`.

### Evaluations run before promotion

`ops.py evals-run` creates each evaluation case in SAP, runs it through the deployed invoker,
and grades it. It grades the stored case, SAP's own record and built-in judges, plus the
`SAPActionAccuracy` judge from `evals-setup`. Evaluation cases are stored with the pack
(`packs/<pack_id>/evals.json`).

**Why:** an agent that writes to financial systems is judged on the actions it took, not on its
answer. A model or procedure change that breaks a case must be found before it is promoted. **Where:**
`evals/run_regression.py`, `evals/setup_evaluations.py`, `evals/oracle.py`.

Two `expected` keys test the controls, not the agent:

- `gateway` reads the interceptor's verdict lines for the case's run. `seen` lists
  `{tool, verdict, reason_contains}` entries that must appear. With `if_called: true`, no attempt
  passes as well. `never_allowed` lists tools that no line may allow. A verdict line names no
  run. The SAP layer's check and the run checks log `interceptor run {"run_id": ...}` in the same
  invocation, and the runner links the two by its request id.
- `guardrail` passes when two things are true. A guardrail intervened (a `blocked` verdict, or a
  guardrail's refusal in the trace). And no `absent` value appears anywhere in the case record,
  except in the attributes that the case put there.

Both are pure functions, so a code-based evaluator can run them on a session's spans (the
OpenTelemetry trace records of a session). `guardrail_stop_ok: true` also passes a run that the
runtime guardrail stopped, without its `required_tool_calls`. In such a run, the blocked message
is the agent's own text and the case is `manual_review_required`. The run reports it as
`PASS (guardrail stopped the run)`. `forbidden_tool_calls` and `sap_state` still apply.

## Identity

### SAP sees a service account unless a system uses `obo`

Under the platform's brokered identity (Amazon Cognito, optionally federating an enterprise
identity provider (IdP)), SAP sees one service account: flow `basic`, `m2m-sap` or `m2m-oidc`.
Flow `obo` (on behalf of) calls SAP as the signed-in person, which SAP calls principal
propagation. It needs the platform's direct Entra ID mode. Queued runs, and runs that start with
no person, have no person to propagate. So they can use only the service-account flows, and a
system configured only for `obo` gives them no SAP path.

Who started a run is sent beside who authenticated it. Gateway calls include
`x-audit-correlation-id` (the run), `x-audit-initiator` and `x-audit-trigger`, and the SAP
targets forward these headers to the server.

**Why:** most SAP systems can trust a technical user on the first day. Per-person SAP identity
depends on how SAP trusts your IdP. With a service account, the run and its case record link a
call to the person or schedule behind it. **Where:**
`layers/sap/lib/sap_layer/config.py`, `business_operations/identity.py`,
`agent/utils/mcp_topology.py` (`audit_headers`), `layers/sap/stack.py` (`AUDIT_HEADERS`).

## Console

### Text never takes an alpha modifier

A `text-*` class never takes an alpha modifier. Less prominent text uses its own measured
color token, never a `/70` on an existing token. A drift test fails on any `text-*/<alpha>`
class.

**Why:** before the rule existed, an alpha on muted text lowered its contrast to about 2.7:1,
under WCAG AA, in 29 places. **Where:** `frontend/src/styles/globals.css`,
`frontend/test/drift/config.test.ts`.
