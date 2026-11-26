<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Architecture

Terms are defined in the [glossary](glossary.md) and in [Concepts](../../../docs/start/concepts.md).

![The whole architecture: the use case's poller reads SAP and makes one case per SAP document (SAP can also push events through the SAP layer's bus, which is optional), the agent runs on the platform, and every tool call passes the Gateway's checks on its way back to SAP](../../../docs/start/architecture/architecture.light.drawio.png)

Follow the numbers in the diagram:

1. An SAP job, the drain job, sends an event to the SAP layer's `sap-events` bus.
2. This use case's rule delivers the event to the ingest queue. The queue's consumer reads SAP
   again and creates the case. The poller also runs on a schedule and creates any case that no
   event created.
3. The invoker runs the business process agent on AgentCore Runtime, once per case.
4. The agent calls every tool through the platform's shared AgentCore Gateway. Some tools are on
   this use case's own targets: cases, standard operating procedures (SOPs) and notifications.
   The others are on the SAP layer's `sap-<system>` targets.
5. Each `sap-<system>` target is an AWS for SAP MCP (Model Context Protocol) Server.
6. A person works in the operator console, or in an MCP client such as Kiro or Claude Code. The
   console starts cases and approves a write over the pack's limit. It shows each run as it
   happens, through an AppSync Events channel.

Two checks run inside the Gateway's interceptor, before Cedar:

- The SAP layer's check allows each caller only the SAP access that its own use case claimed
  (its SAP access claim).
- This use case's **run checks** allow a case tool or an SAP write only inside a run that the
  server started.

[Lifecycle](lifecycle.md) follows one case through these steps in order.

> **SAP API Policy.** Use of SAP APIs is subject to the
> [SAP API Policy](https://help.sap.com/doc/sap-api-policy/latest/en-US/API_Policy_latest.pdf)
> and your agreements with SAP. Review both before connecting this sample to an SAP system.

## Stacks

Five stacks, each `<project>-<env>-<name>`, deployed in this order by `deploy.sh deploy`:

| Stack | Contains |
|---|---|
| `uc-business-operations-console` | The console: a React single-page app (SPA) in a private S3 bucket, served by CloudFront with origin access control and a strict Content Security Policy (CSP). Publishes `ConsoleUrl` |
| `uc-business-operations` | Tables, buckets, the REST API, the Gateway targets, the pipeline, the packs and the path that publishes them, the run checks' registration in the interceptor, Cedar statements, alarms |
| `uc-business-operations-knowledge` | What the `knowledge:` flags turn on: Amazon Bedrock knowledge bases on S3 Vectors, and the optional Aurora agent-knowledge store. Only one parameter when every flag is off |
| `uc-business-operations-sap` | This use case's SAP access claim on the SAP layer and its Cedar permits on the SAP targets |
| `uc-business-operations-runtime` | The agent on AgentCore Runtime, the token-vault provider it calls the Gateway with, and the run-event channel |

It requires the platform's `auth`, `gateway`, `memory` and `ops-topic`, and the SAP layer
(`layers/sap/`). The SAP MCP runtimes and the `sap-<system>` targets are the layer's, not this
use case's.

## Components

### The operator console

Its pages:

- **Cases** (`/cases`): the cases in views, with a briefing on request.
- A **case** (`/cases/<case_id>`): what the agent read and did, and the decision it waits for.
- **Packs** (`/packs`): every pack. `/packs/<pack_id>` shows a pack by part, a procedure to read
  and edit, and rehearsal.
- **Deployment settings** (`/deployment`): the deployment's trigger mode and the contacts.
- **Tickets** (`/tickets`): only while `api.sample_ticketing` is on.

Every response includes a strict CSP: `script-src 'self'`, no framing, and `connect-src` limited
to this Region's API Gateway, Cognito and AppSync endpoints and the pool's sign-in domain. The
deploy builds the SPA with `npm` (or Docker, if `npm` is absent) and publishes it with a
`BucketDeployment`. The runtime stack, deployed last, writes the SPA's `aws-exports.json`. There
is no AWS WAF on the distribution, because it serves static files only. The responses also have
no `Strict-Transport-Security` (HSTS) header, because the console serves on the shared
`*.cloudfront.net` certificate. If you serve the console on your own domain, add HSTS there, and
a WAF web ACL in `us-east-1` if your policy requires one. How it is built:
[frontend/README.md](../frontend/README.md). Each screen, for the people who use it:
[The operator console](console.md).

### The REST API

One Amazon API Gateway REST API. Its Amazon Cognito authorizer accepts only the console client's
ID token. The routes:

- `/cases`, `/runs`, `/registry` and `/sops/sync`.
- `/packs`. Each pack's document, procedures, rules, controls, trigger mode, evaluations and test
  data are under `/packs/{pack_id}`.
- `/deployment`: the settings and the deployment's trigger mode.
- `/tickets`, while sample ticketing is on.

`POST /erp-events` is on a REST API of its own when `api.erp_events_endpoint` is on.
`openapi.yaml` is the contract. Under `deployment.mode: production`, a regional AWS WAF web ACL
is attached to every stage. It has a per-IP rate limit and the AWS common and known-bad-inputs
rules.

### Data

- DynamoDB tables for cases, config overrides, run grants and tickets.
- S3 buckets for the procedures, the pack registry and exemplars. An exemplar is a short worked
  example, built from the best-rated finished cases of one process type, that later runs read
  ([Closure and feedback](lifecycle.md#6-closure-and-feedback)).

Under `deployment.mode: production`, the tables and buckets are encrypted with the platform's
customer managed key in AWS Key Management Service (KMS), and they are kept when the stack is
deleted. The procedure and registry buckets are versioned, and no one may delete an object
version. As a result, each run's `sop_version_id` identifies the exact procedure file that the
run was given.

The cases table has `case_id` as its only key. Three indexes serve the readers that need a pack
or a status:

- `pack-status-index`: every read for one pack.
- `status-index`: the batch sweeper and the exemplar builder.
- `pack-status-updated-index`: the console's case list and the done summary. Its key is the pack
  and the status, in `updated_at` order. It projects only the fields that a list searches, sorts
  and sums on. So `GET /cases` reads about 1 KB for each case it passes over, and then reads the
  page's cases in full by id.

The views, the search, the exception filter and the sort all run on that last index
(`api/layer/python/case_query.py`). `GET /cases/counts` counts every view with the same filters.
With `by=exception_type`, it counts each view by exception type, for the facet on the Cases page.

### The pipeline

- The **SAP poller** reads each published pack's `source` on a schedule.
- SAP's events, through the SAP layer's `sap-events` bus, and **SAP push** (`/erp-events`) both
  send to the one **ingest queue**. Its consumer creates the same case for the same document as
  the poller does.
- A FIFO **agent queue** sends cases to the **invoker**. The invoker writes a run grant and
  invokes the runtime.
- The **batch sweeper** queues again the cases left `detected`.
- The **exemplar builder** turns finished cases into exemplars.

See [Lifecycle](lifecycle.md).

### The agent

An AgentCore runtime, built from `agent/` by the platform's `RuntimeStack`. Its authorizer
accepts only the delegating client, and only the run route and the invoker have that client's
credentials. The agent:

- Calls the Gateway as the `writer` client, through a token-vault provider.
- Sends the run id as `x-audit-correlation-id` on every call.
- Is given only tools on this use case's own targets and on the SAP targets. When the pack names
  an SAP system, it gets only that system's tools. Of these, it gets only the tools in the pack's
  `gateway_tools`.

Each run's AG-UI (Agent-User Interaction protocol) events are sent on the run-event channel
([runtime reference](reference/runtime.md)).

### Gateway targets

| Target | Tools | Cedar scope |
|---|---|---|
| `business-operations-case-management` | `get_case_state`, `update_case_state` | read, write |
| `business-operations-sops` | `load_sop` | read |
| `business-operations-notification` | `send_notification` | write |
| `business-operations-tickets` | `demo_create_ticket`, `demo_get_ticket` (sample ticketing only) | write, read |
| `business-operations-knowledge` | `search_sap_sops`, `search_sap_api_docs` (with their `knowledge:` flags) | read |
| `business-operations-agent-knowledge` | `get_precedent`, `check_vendor_risk` (with `agent_knowledge`) | read |
| `sap-<system>` (the SAP layer's) | `find_sap_services`, `get_metadata`, `get_service_hints`, `odata_read`, `odata_count`; the writes | read; write |

Scopes are `uc-business-operations/read` and `/write`. The manifest declares two Gateway clients.
The platform creates them on its user pool and admits them to the Gateway: `reader` (read) and
`writer` (read, write). The use case adds two more app clients on the pool. These are app
clients, which sign in to the platform, not SAP clients such as `100`.

- The **console client** uses the authorization code flow with PKCE (Proof Key for Code
  Exchange). It is the only client whose ID token the APIs accept. Under a brokered identity
  provider (IdP), it lists only that IdP.
- The **delegating client** uses client credentials, with the scope
  `business-operations-runtime/invoke`. Only the run route and the invoker have it. Because of
  that, the runtime can trust the acting person's `sub` that arrives with its token, and no other
  caller can send one.

No Gateway target writes procedures. The pack composer is the only writer of procedures. The
procedure routes (`/packs/{pack_id}/sops`) check each write, and then invoke the pack composer
with the author's identity. The pack composer also writes a pack's procedures when it publishes
the pack and when it imports a bundle.

## SAP access

The SAP layer owns the connection to SAP ([its walkthrough](../../../layers/sap/walkthrough.md)).
Each entry under `layers.sap.systems` is one AWS for SAP MCP Server and its Gateway target,
`sap-<system>`, in the stack `<project>-<env>-layer-sap`.

This use case's **access row**, under `layers.sap.access`, is the most SAP access it may use:

- `read` and `write`: what it may read and write through the Gateway.
- `bypass_gateway`: the systems that its own functions (the poller, the ingest consumer and the
  test-data seeder) call directly, outside the Gateway, with the system's service account. These
  functions run on a schedule or on an event, with no person and no run behind them, so they
  cannot use the Gateway's path. Listing each system keeps this direct access visible.

The `uc-business-operations-sap` stack adds two things:

- Its **SAP access claim**: which of its apps may use the systems and writes that its access row
  allows. The apps are `writer` and `reader`, and also the console app when a system uses `obo`
  (principal propagation) in direct mode. The SAP layer refuses SAP to every other client, the
  platform's own clients included, in every Cedar mode.
- Its **Cedar permits**: the SAP reads need the `read` scope, and the writes need `write`.

The operator sets the access row, and the use case author writes the SAP access claim. So an
author cannot give the use case more SAP access than the operator allowed. The claim can also
name the use case's apps, which the access row cannot, because their client ids exist only after
the use case deploys.

Writes are on by default on a `basic`, `m2m-sap` or `m2m-oidc` system, and off on an `obo`
system. To make a system read-only, set `writes: []` on it, or narrow `access.write`.
`odata_delete` is never available. What else a write must pass once it is on:
[Security](security.md). `verify.py` reads the first page of each claimed system's service
catalog as the reader client. Under `ENFORCE`, it fails if the reader is offered an SAP write.

### SAP access limits

Five limits decide whether one SAP call goes through, and a different person sets each one. In
SAP terms:

- The SAP access claim is like the authorizations that an SAP role requests.
- The access row is the most that an administrator will grant that role.
- The SAP user's own roles (PFCG, the SAP role maintenance transaction) still apply last.

![SAP access limits as nested boxes, widest outside: the access row the operator sets in platform.yaml, the SAP access claim the use case author writes in code, the Cedar permit the security engineer turns on, the run checks set by the pack's controls, and the SAP authorizations of the SAP user, set by SAP Basis. An SAP call goes through only when every box allows it](diagrams/sap-access-limits.png)

Which limits refuse a call in each Cedar mode (`security.cedar`):

| Limit | Set by | Cedar off | `LOG_ONLY` | `ENFORCE` |
|---|---|---|---|---|
| Gateway sign-in: a valid token from an allowed client | Platform | Refuses | Refuses | Refuses |
| Access row and SAP access claim (the SAP layer's check) | Operator, use case author | Refuses | Refuses | Refuses |
| Run checks | Pack admin, in the pack's controls | Refuses | Refuses | Refuses |
| Cedar permit | Security engineer | Not created | Logs, refuses nothing | Refuses, and `tools/list` hides the tool |
| SAP authorizations of the SAP user | SAP Basis | Refuses | Refuses | Refuses |

Four of the five limits refuse a call in every Cedar mode. Only the Cedar permit depends on the
mode. Without `ENFORCE`, any client the Gateway admits can call every tool that no other row
refuses, including another use case's tools
([Cedar on the shared Gateway](security.md#cedar-on-the-shared-gateway)). The quickstart sets
`ENFORCE`, and `deployment.mode: production` requires it.

## Names the synth checks

The synth (`cdk synth`) turns the AWS Cloud Development Kit (CDK) code into CloudFormation
templates before anything deploys. It fails, and names the setting to shorten, in two cases:

- `'<project>_<env>'` is longer than 28 characters. The runtime name
  `<project>_<env>_business_operations` must fit AgentCore's limit of 48.
- A tool name the model sees, `<target>___<tool>`, is longer than Amazon Bedrock's limit of 64.
  This limits an SAP system name to 16 characters.

Cedar policy names are the policy name plus a hash of the deployment (`read_cases_1a2b3c4d`),
because a name must be unique across every policy engine in the account and Region.
