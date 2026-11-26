<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Configuration reference

Terms are defined in the [glossary](glossary.md) and in [Concepts](../../../docs/start/concepts.md).

The use case's block in `platform.yaml` takes these keys: `knowledge`, `api`, `runtime`,
`contacts`, `notification`, `pipeline`, `alarms`, `test_data`, `promotion`, `entra`, `console`,
`people_tools` and `admin_email`. Any other key fails the synth (`cdk synth`, which builds the
CloudFormation templates before a deploy). The pydantic model, `business_operations/config.py`,
is authoritative for each default and limit. SAP systems belong to the SAP layer, under
`layers.sap` ([Walkthrough](../walkthrough.md#sap)). `promotion` is described in
[Packs](packs.md#promotion-between-stacks), and `entra` in [Direct mode](direct-mode.md).

This page says "standard operating procedure (SOP)" once, and "procedure" after that.

## Knowledge

```yaml
use_cases:
  business-operations:
    knowledge:
      sop_search: true         # search_sap_sops over the SOP bucket
      api_docs_search: false   # search_sap_api_docs over example OpenAPI specs
      agent_knowledge:
        enabled: false         # get_precedent and check_vendor_risk on Aurora
        min_acu: 0             # 0 pauses when idle; the first call after a pause waits for it to resume
        max_acu: 4
        seconds_until_auto_pause: 3600
```

- `sop_search` and `api_docs_search` each add an Amazon Bedrock knowledge base on S3 Vectors and
  a tool on `business-operations-knowledge`. Both need Amazon Bedrock model access to **Titan
  Text Embeddings V2** in the Region. Without it, the deploy succeeds and the ingestion job fails.
  When idle, they cost only S3 Vectors storage. Each deploy of the knowledge stack starts an
  ingestion job for each corpus whose files changed. `ops.py kb-sync` starts one on every
  knowledge base.
- `api_docs_search` is off, because its corpus, `packs/_seed/api-docs/`, is example S/4HANA
  specifications, not your system's. The AWS for SAP MCP (Model Context Protocol) Server's
  `get_metadata` reads the real field list. The deploy copies that directory into the bucket, so
  edit the directory, not the bucket.
- `agent_knowledge` is off because it has a **standing cost**: an Aurora Serverless v2 cluster in
  an isolated VPC (no NAT, no route out), its secret and a flow log. You pay for these even while
  the cluster is paused: storage and the secret, but no compute. When awake, it runs at 0.5 ACU
  (Aurora capacity unit) or more until `seconds_until_auto_pause` passes. The daily exemplar
  builder wakes it. Under `deployment.mode: production`, the cluster uses the platform's customer
  managed key in AWS Key Management Service (KMS), which cannot change after the first deploy.

Agent knowledge gives the business process agent what earlier cases taught. A team needs it when
the right action depends on a supplier's history, such as a duplicate invoice from a supplier
that shares a bank account with another. It stores four kinds of row:

- A **precedent** is an earlier finished case of the same process type, and of the same supplier
  when the call names one, with its rating score. `get_precedent` returns precedents.
- A **lesson** is a pack author's comment on a negative rating, kept for that case's supplier.
- A **vendor** is one supplier, and a **vendor edge** is a link between two suppliers, such as a
  shared bank account. `check_vendor_risk` reads them.

**What agent knowledge does not do yet.** The exemplar builder fills `precedent`, and fills
`lesson` from an author's comment on a negative rating
([Lifecycle](lifecycle.md#6-closure-and-feedback)). `vendor` and `vendor_edge` have no automatic
writer. The poller reads supplier invoices, not the vendor master. So nothing links suppliers by
bank account or tax ID until you load a file:

```bash
./scripts/deploy.sh usecase run business-operations knowledge-load vendors.json
# {"vendors": [{"supplier_number": "100", "name": "Acme"}],
#  "vendor_edges": [{"from_vendor": "100", "to_vendor": "200",
#                    "edge_type": "shared_bank_account", "evidence": "same IBAN"}],
#  "lessons": [{"process_type": "duplicate_invoice", "lesson": "...", "authored_by": "..."}]}
```

Until then, `check_vendor_risk` returns no related vendors. That means "no recorded edges", not
"no risk", and no shipped pack lists the tool. Until the builder's first run, `get_precedent`
returns nothing. A pack without a `supplier_number` field never gets precedents or lessons. The
vector buckets use S3-managed keys even under production. An S3 Vectors bucket on the platform's
KMS key needs a key-policy statement that a use case cannot add.

## API, contacts, test data, alarms

```yaml
use_cases:
  business-operations:
    contacts: {ap_team: ap-team@example.com}   # {{CONTACT_AP_TEAM}}; keys are lower snake-case
    api:
      sample_ticketing: true                # SAMPLE ITSM: replace it before production
      erp_events_endpoint: false            # POST /erp-events on its own API
      set_account_cloudwatch_role: false    # see "API Gateway logging"
    test_data:
      enabled: false           # the seeder ops.py seed invokes posts real SAP documents
    alarms:
      cost_p90_usd: 0.10       # the cost alarm's p90 per run
```

**`admin_email`** creates the first console user with the stack. The user is in every group the
stack creates except pack-admins. Cognito emails the user a temporary password, valid for seven
days. Set it only on a new stack: a user pool that already has that email fails the create, and
removing the key later deletes the user. Add everyone else with `ops.py add-user`.
`deployment.mode: production` refuses `admin_email` at synth. The user is a Cognito password user
outside your identity provider (IdP) and its multi-factor authentication (MFA), and it can approve
every pack's writes.

**Contacts** fill the procedures' `{{CONTACT_<NAME>}}` values. The synth warns about any contact
that a procedure cites and this block leaves unset. `PUT /deployment/settings` overrides contacts
for one stack. Contacts belong to the stack only: no pack declares a contact. A pack's rule
constants belong to the pack only. `PUT /packs/{pack_id}/controls` can only lower the pack's two
write limits ([where each value lives](packs/sop-values.md)).

**Sample ticketing is a stand-in for a real IT service management (ITSM) system**, such as
ServiceNow or Jira. It provides both `/tickets` on the console API and the agent's ticket tools.
The deploy summary prints `SampleTicketing`, and a `production` synth warns while it is on.
Replace it, then set `sample_ticketing: false`. That removes the routes, their function and the
ticket tools completely. The empty `Tickets` table stays, because the stack creates it in every
deployment. An empty on-demand table costs nothing.

**API Gateway logging.** Stage logs need API Gateway's CloudWatch role. That role is one setting
for each account and Region (`AWS::ApiGateway::Account`), so by default this stack never sets it.
Check before the first deploy with `aws apigateway get-account --query cloudwatchRoleArn`. If it
prints `null`, the stage fails to create. In that case, set
`api.set_account_cloudwatch_role: true` in exactly one stack in the account and Region.

**Test data** (off by default) deploys the seeder that `ops.py seed` invokes, for workshops and
demos. It posts **real SAP documents**. For `finance_ops`, these are a purchase order, a goods
receipt and a supplier invoice with a payment block, on the S/4HANA sample data in company code
1710. An ECC (SAP ERP Central Component) system or a customer's own configuration rejects them,
and SAP's message is shown. Two callers may invoke the seeder:

- An IAM principal with `lambda:InvokeFunction` on it (`ops.py seed`). CloudTrail names this
  caller only on a trail that logs Lambda data events, and the platform's trail does not.
- The console's test data page, through `/packs/{pack_id}/test-data`. It is for an operator
  (`business-operations-admins`) who may see the pack, and only for a pack whose system is a
  sandbox. That route posts nothing itself. It passes each copy to the seeder asynchronously,
  with no retry, and the seeder records each seed before its first SAP call.

A case opened for a seeded document includes that record as `test_data`. So the page lists what
the seed was expected to do beside what it did. `test_data.enabled` also turns on the console's
`features.test_data`.

```bash
./scripts/deploy.sh usecase run business-operations seed finance_ops --list   # inputs and presets
./scripts/deploy.sh usecase run business-operations seed finance_ops --preset 2 --yes
./scripts/deploy.sh usecase run business-operations seed finance_ops invoice_amount=26250 --yes
```

Inputs start at the pack's `seed.inputs` defaults. A `--preset` overrides them, and then each
`<input>=<value>` overrides those. The command prints the documents SAP created, and the case the
poller will open. When a later document fails, it also prints the documents that SAP had already
created.

With no `posting_date`, the seeder posts on the latest date that SAP has open for the company
code. If it cannot find one, it fails and names `posting_date`. It reads that date from SAP's
period tables through ADT (ABAP Development Tools). ADT accepts Basic authentication only, and
the user needs ADT data preview. So when the system's direct callers sign in with `m2m-sap` or
`m2m-oidc`, a blank `posting_date` fails before any SAP call. `--list` says so, and you pass
`posting_date=<YYYY-MM-DD>`. SAP publishes no released OData (Open Data Protocol) API for the
open periods that an OAuth scope could cover.

The seeder writes to SAP **outside the Gateway, Cedar and the run checks**, with the system's
service-account secret. A seed has no model and no run, and its amounts come from an operator.
These limits apply to it instead:

- It is off by default.
- It is reached only over IAM, or through the operator-only route above.
- It posts only the documents that the pack's `seed.steps` declare (structure).
- It logs every document number.
- It seeds only a sandbox.

A pack with a `seed` block needs an SAP system in
`layers.sap.access.business-operations.bypass_gateway` whose `system_role` is `sandbox`, or the
synth fails. The seeder has IAM access to no other system's secret. At run time, it refuses a
pack imported later that resolves to another system. `seed <pack>` and `evals-run` first ask the
seeder to describe the pack. It reports the system and its system role after one GET to SAP, so
an SAP system that does not answer fails before anything is posted.

## Console

```yaml
use_cases:
  business-operations:
    console:
      brand:
        name: Business Operations          # what the console calls itself, 1 to 40 characters
        subtitle: SAP exception handling   # the line under it, 1 to 80 characters
        monogram: BO                       # optional: the console's mark, 1 to 3 letters or digits
```

`console.brand` renames the console without a code change. The runtime stack writes it into
`aws-exports.json` as `brand`, with both strings always present. Each is one line of plain text.
It must not be blank, and must have no leading or trailing space, no `<` or `>`, and no control
character. Otherwise the synth fails and names the key. `monogram` is optional: 1 to 3 letters or
digits in any script, written only when set. Without it, the console draws its own mark. Anyone
can fetch the file, so the brand is public, as the sign-in page is.

`console.features` contains the console's feature flags. None is defined today, so the block
takes no key, and an unknown flag fails the synth. The runtime stack writes `features` into the
console's `aws-exports.json`. Its one entry there is not a flag anyone sets: `test_data` copies
`test_data.enabled`, so the test data page shows only where its routes exist. The console only
reads it. A flag shows or hides a view and grants nothing, because every route still decides for
itself.

The console learns where the agent writes after sign-in, never from `aws-exports.json`, which
anyone can fetch:

- `GET /registry` gives each pack's SAP system and its `system_role`.
- `GET /deployment/settings` gives `deployment`, which contains:
  - the platform's `deployment.mode`;
  - this stack's `promotion.role`;
  - `promotion_only`, true when that role is `quality` or `production`, so every pack and
    procedure write over HTTP is refused;
  - every SAP system this use case reaches, with its system_role;
  - `non_production`, true when none of them is `production`.

The console marks what is not production from these values. The system role is still only what
the operator says it is.

## Remote MCP client tools

```yaml
use_cases:
  business-operations:
    people_tools: [read, author]   # default []: nothing on the people Gateway
```

`people_tools` attaches this use case's tools to the platform's people Gateway (`gateway.people`,
[gateway module](../../../docs/modules/gateway.md#people-gateway)). It is for people signed in
from a remote MCP client.

- `read` lists packs, and reads a pack, its procedures, its base prompt, its rules and its
  versions. It also lints a procedure without saving it.
- `author` replaces a procedure's text, the base prompt, the display name and description, and
  rule values. It also creates a pack.

Every tool needs `business-operations-remote-authors`, as well as what its console route needs
([Security](security.md#people-gateway)). Grant it with
`ops.py add-user <email> --remote-author`, or map it with `identity.group_map`.

The synth refuses `people_tools` without `gateway.people.enabled`. It refuses `author` under
`deployment.mode: production`, or under a `promotion.role` other than `development`. An empty
list creates nothing: no target, no function and no group.

## Notification

```yaml
use_cases:
  business-operations:
    notification:
      channel: ses                        # or tickets (needs api.sample_ticketing)
      ses_sender_email: agent@example.com # a verified SES identity
      recipient_domains: [example.com]    # empty: the sender's own domain only
```

Without `channel`, the channel is `ses` once `ses_sender_email` is set. Until then, it is
`tickets` while sample ticketing is on, so a new deployment completes cases before anyone sets up
Amazon SES. With the channel `ses` and no sender, every send fails as a channel failure and
raises the `NotificationFailure` alarm, and the synth warns. A new account's SES is in the SES
sandbox, and can email only verified identities until production access is granted. Why
recipients are restricted: [Security](security.md#notifications).

The stack does not verify the sender, and no check reports an unverified one. Until you verify
it, every send fails as a channel failure. Verify the address or its domain in SES before the
first run, for example with `aws sesv2 create-email-identity --email-identity
agent@example.com`, and follow the link in the email that SES sends.

## Pipeline

```yaml
use_cases:
  business-operations:
    pipeline:
      max_concurrency: 5            # runs at once: Amazon Bedrock and SAP load
      warm_delay_seconds: 15        # a burst for one pack waits behind its first case
      poller_schedule: rate(15 minutes)   # each poll re-reads every open exception
      sweep_schedule: rate(1 hour)  # polls only the packs with source.poll: sweep
      batch_schedule: rate(1 hour)
      exemplar_schedule: rate(1 day)
      poll_created_since: 2026-12-07   # lab or demo stack: only records created since
      ingest:
        max_concurrency: 2          # batches the ingest consumer drains at once: bounds SAP reads
```

`poll_created_since` is for a stack on a shared sandbox. Without it, the first poll creates a case
for every open exception in SAP (about 2,900 blocked invoices on the demo system). With it, the
poller and the ingest consumer read only records created on or after that date. This applies to
every pack whose `source` names a `created_field` (`finance_ops`: `CreationDate`). A pack that
names none still polls its whole backlog.

**Trigger modes** are not in this block. They are deployment settings that an admin changes while
the stack runs ([Trigger mode](console.md#trigger-mode)). There are two: one for the deployment,
and one for each pack. A new case starts a run by itself only when both are `auto`, and both
start at `manual`. The pack's setting lets a new pack start at `manual` while another pack runs
on `auto`. The deployment's setting lets an admin return every pack to `manual` at once.

SAP's events reach the one ingest queue through the SAP layer's bus. Who may send, and which
systems this use case subscribes to, are the layer's settings, not this block's:

```yaml
layers:
  sap:
    systems:
      s4:
        senders: [arn:aws:iam::123456789012:role/sap-host-role]   # may assume s4's sender role
    access:
      business-operations:
        bypass_gateway: [s4]        # the consumer answers a nudge by reading SAP
        ingest: [s4]                # one rule routes s4's events to the ingest queue
```

`pipeline.ingest.senders`, the per-system queues of the first version of this use case, is refused
with a message that names these keys. What each does to a case: [Lifecycle](lifecycle.md#1-detection).

## Runtime

| Key | Default | What it does |
|---|---|---|
| `haiku_model_id` | Claude Haiku, or the `sonnet` tier's model when `agents.allowed_models` leaves Haiku out | The `haiku` tier a pack's `model_tier` names; `sonnet` is the platform's `agents.model_id` |
| `opus_model_id` | Claude Opus when the allowlist allows it; otherwise not served | The `opus` tier. A pack naming a tier the deployment does not serve fails the synth |
| `max_tokens` | 8192 | The most output tokens per model call. If it is below what the closing answer needs, the final `update_case_state` is never written and the case stops at `manual_review_required` |
| `log_model_content` | on outside `deployment.mode: production`, off in it | Writes prompts, responses and tool payloads in plaintext to the runtime's log group, where the evaluators read them ([Operations](operating/observability.md)) |

A run is limited by its pack's `max_turns` (15 at most) and by its `run_budget`. By default, the
budget is 2,000,000 tokens or $0.50 of estimated model cost, whichever comes first. A typical run
is far below both, so the default stops only a run that does not end. A run over budget:

- stops before its next tool call;
- ends with `RUN_BUDGET_EXCEEDED`;
- counts one `RunBudgetExceeded` metric;
- leaves its case at `manual_review_required`.

The budget is structure, like `max_turns`, so only a pack admin sets it.

Guardrails:

- Under `security.require_guardrails`, the runtime gets this use case's guardrail. It blocks
  Social Security numbers and card numbers. It blocks AWS keys in what the model says, but not in
  what the model is given. It does not change supplier names and addresses.
- Under `security.egress_filter`, the Gateway's guardrail masks names, addresses, emails and
  phone numbers, unless `security.egress_pii_entities` sets them to `NONE`, as
  `platform.example.yaml` does.
- The example also lists `load_sop` and `search_sap_sops` in
  `security.egress_unscanned_responses`, so the Gateway does not scan their responses. They
  return the procedures this deployment wrote, and the guardrail once read a G/L (general ledger)
  account in one as a card number. Their requests are still scanned, and the SAP layer refuses an
  SAP tool in that list.
- `deployment.mode: production` requires both guardrails.
- The example and the quickstart also set `security.egress_request_action: block`. So a tool call
  that the Gateway's guardrail would rewrite is refused, instead of being sent on masked.
  `production-check` fails without this setting.

The runtime keeps short-term history in the platform's Memory. Set
`agents.memory.strategies: []`, because a strategy extracts every stored event, and this runtime
stores SAP document contents and vendor data. The synth warns otherwise.
