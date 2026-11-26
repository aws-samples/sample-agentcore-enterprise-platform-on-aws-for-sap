---
name: dev-new-sap-process
description: Use when someone wants to automate a new SAP exception process with business-operations ("automate blocked purchase requisitions", "add a pack for …", "new exception type", "handle <SAP document> exceptions"). Scopes it, finds the SAP service and fields, writes the whole pack (`source`, `fields`, `case_document_check`, prompt and SOPs) under `use-cases/business-operations/packs/<pack_id>/`, validates it and imports it into a development stack, then optionally sets up SAP to send events. Orchestrates `dev-author-pack` and `owner-author-sop`.
---

# Automate a new SAP process

The result is one directory, `use-cases/business-operations/packs/<pack_id>/`, imported into a
development stack. There the poller turns each matching SAP record into a case. It needs no
code, no AWS Cloud Development Kit (CDK) change and no new deploy. Paths here are relative to the
repository root, and `UC` is `use-cases/business-operations`. "You" in this skill is the coding
agent that runs it; "the business process agent" is the deployed agent that works the cases.

```bash
ops() { ./scripts/deploy.sh usecase run business-operations "$@"; }
```

**The quickest path** is S/4HANA with the service already published, `basic` SAP auth, Amazon
Cognito sign-in, and a sandbox SAP client. Start `cloud-deploy-and-wire` first, so that its
deploy runs while you do steps 1 to 5 here. Some paths need a person in SAP or in the identity
provider: a service that is not active, a missing service, SAP ERP Central Component (ECC),
push from SAP, or any other auth flow. When you meet one, say so and give that person the
handoff text. Those paths work; they are only slower.

## 1. Scope it

Before you write anything, agree on these points, in the user's words:

- the exception: what makes a record wrong;
- the SAP transaction they use today;
- the process types, and how each one is resolved;
- every threshold (name, default, unit);
- who reads, approves and is notified (as roles);
- whether the business process agent only reads or also writes, and which write.

Ask one question at a time. Keep the answers in a short scoping note outside the repository,
and read it back to them before step 2. A process owner often knows the transaction and the
rule but not the SAP service. Finding the service is step 2's job, not theirs.

Never invent a threshold, contact, company code or field name. An unanswered item stays open,
with a named owner.

## 2. Find the service and its fields

- **S/4 with a published service** (the quickest path): take the service and entity set that
  the user names. When the stack is up, confirm them through the reader's Model Context
  Protocol (MCP) proxy (`ops mcp-config` prints the host config for
  `sap_kit/mcp_gateway_proxy.py`):
  - `get_metadata` gives field types, navigation properties and whether the entity is updatable;
  - `odata_read` with `$top=3` and the candidate `$filter` shows that real rows match.
- **Otherwise**, look through the same proxy before you ask anyone. `find_sap_services` lists
  the services the system publishes. It lists only those that match the system's
  `service_prefixes`; if the list is empty, widen `service_prefixes` in
  `layers.sap.systems.<system>`. Then:

  | What you find | Next |
  |---|---|
  | A published service with the entity | As above: `get_metadata`, then `odata_read` with `$top=3` |
  | A standard service the system has but has not activated | Basis activates it in `/IWFND/MAINT_SERVICE` ([SAP setup, step 3](../../../../layers/sap/sap-setup.md#3-activate-the-odata-services)) and adds it to the service user's `S_SERVICE`. Write the pack now; it polls once the service is published. Not on the quickest path |
  | No service exposes the exception | See the triage below. An ABAP developer builds and publishes a Core Data Services (CDS) view. Not on the quickest path |

  Give the person in SAP the exact service name and the reason, in one message. Take field
  labels from `$metadata`'s `sap:label`. Field types, navigation properties and whether an
  entity is updatable come from `get_metadata` or from the ABAP developer. Never guess a type
  from a field name. If a CDS view has `@AccessControl.authorizationCheck: #NOT_REQUIRED`, say
  so before anyone publishes it, because every caller would then read every row.

**Triage when no service exposes the exception.** Find which of these three kinds it is:

- SAP stores it, in a table (parked documents, IDoc errors, workflow items);
- it is a status field on the document (a `WHERE`);
- it is computed (a join and a comparison).

One CDS view covers each kind. Anything driven by SAP Business Workflow is the first kind.

## 3. Write the pack

`dev-author-pack` writes `pack.json` and `base_prompt.txt`. `owner-author-sop` writes one
standard operating procedure (SOP, or procedure) per process type. This step covers the two
blocks that only this skill writes, and the case document check.

**Completing an analyst's draft.** A pack made with `create_pack` (or **New pack** on `/packs`) is
incomplete and exists only in the development stack. Start from it, not from an empty
directory. `ops bundle export <pack_id> --to-dir UC/packs` writes `UC/packs/<pack_id>/` with
its prompt and procedures, with no version. Replace each `<AWAITING_SURVEY>` marker below, then
validate and import it (steps 4 and 5).

**`source`**, from `UC/packs/finance_ops/pack.json` as the model:

| Key | What to put |
|---|---|
| `short_id` | 2 to 8 characters of `[a-z0-9]`, starting with a letter, and unique across packs: the readable prefix of every case id |
| `label` | What the source is called on screen |
| `kind` | `filter` (a record matches a predicate), `absence` (its `iterate.path` collection is empty), `aging` (older than `aging.older_than_days` on `aging.field`; never write a date into `filter`), or `event` (nothing to poll: SAP sends records). Ask; never guess. `comparison` and `derived` have no code that runs them |
| `service`, `entity` | The names from step 2, as this system spells them |
| `odata_version` | `v4` for an OData V4 service (the default when a service is published with the ABAP RESTful Application Programming Model (RAP)). Then `service` is its whole path under `/sap/opu/odata4/` (`sap/<group>/srvd_a2x/sap/<service>/0001`), and child paths have no `.results`. Omit it for V2. The AWS for SAP MCP Server reads V2 by default. For the business process agent's own reads of a V4 service, set the system's `odata_version` (`layers/sap/reference/server.md`, "OData V4 and mixed systems") |
| `created_field` | The record's creation date (`CreationDate` on `A_SupplierInvoice`), when it has one. A stack that sets `pipeline.poll_created_since` then reads only records created since that date. Without it, a lab stack on a shared sandbox polls the whole backlog |
| `filter`, `select`, `expand` | The `$filter`, `$select` and `$expand`. Every `expand` name also goes in `select`; otherwise SAP leaves out the children with no error |
| `key` | Ordered `[{field, source: parent\|child, strip, as}]`: the fields that say which object an exception is about. The case id is a digest of them. So a key that does not identify one exception merges two cases |
| `iterate` | `{path: <nav property>}`, only when one record gives one case per child, or for `absence`. Omit it for a flat entity |
| `skip_when`, `process_type`, `title` | Records to drop (a blank key field), the default process type (and conditional ones), and a one-line title over `{parent.X}`, `{child.X}` and `{abs_amount}`. The default is the pack's triage type, which a case never records as its exception type. The poller usually cannot tell the exception type from the record alone, so the triage procedure reads SAP and decides. When `rules` (`lte` only) send some records directly to an exception procedure, make the default a triage procedure that loads the right one with `load_sop` ([Kinds of SOP](../../docs/packs.md#kinds-of-sop)) |
| `field_map` | `<attribute>: {path: parent.X\|child.X, cast?}`: lower snake case, never a case property (`status`, `title`, `priority`, …). Casts: `sap_date`, `decimal2`, `abs_decimal` |
| `push` | `nudge` (the default for a polled kind), `records` (required for `event`), or `off`. See step 6 |
| `poll` | For a polled kind: `schedule` (the default, every `poller_schedule` run), `sweep` (only the hourly `sweep_schedule` run, once SAP pushes for the pack) or `off` (push only; the synth warns). `sweep` and `off` need a `push` value other than `off`. Leave `poll` unset until step 6 works |

**`fields`**: one entry per `field_map` attribute, in display order:
`{"label": "...", "type": "string|date|amount|decimal"}`, plus `currency_field` on every
`amount`. Labels come from `$metadata`'s `sap:label` (or the CDS view's `@EndUserText.label`),
not from you. The synth fails when `fields` and `field_map` do not match, in either direction.

**`case_document_check`**, when the pack includes an SAP write: map each write parameter that
names the document to the `source.key` field it must equal, such as
`{"SupplierInvoice": "SupplierInvoice", "FiscalYear": "FiscalYear"}`. The run checks then refuse
any write to another document, in every Cedar mode. A pack with a write and no case document
check has every write refused.

A pack that includes an SAP write also declares:

- `constants.AUTO_WRITE_LIMIT_USD`, or `write_limit_waiver` to say why not;
- `write_guards`, or `[]`. Write guards are the fixed checks the agent runtime makes against
  SAP before a payment-release write (`dev-author-pack` lists them).

The synth refuses the pack otherwise. Set `write_amount_field` to the write field that contains
the amount (for the supplier invoice service, `InvoiceGrossAmount`). The run checks then compare
the amount the write posts with the case's amount and with the limit.

## 4. Validate

1. `./scripts/deploy.sh synth`, with the use case enabled. `packs/ is not publishable:` lists
   every problem. Fix them all and run it again.
2. `ops bundle import packs/<pack_id>` (a dry run) against the development stack. It shows the
   diff, and the checks against what that stack offers: tools, model tiers, groups, contacts,
   and which pack owns each process type.
   - For a missing group, run `ops add-group business-operations-<g>`.
   - For a missing contact, add it under `use_cases.business-operations.contacts` and deploy
     again, or ask an author to set it through `PUT /deployment/settings`.

## 5. Import into development

Show the dry run's diff, get a yes, then run `ops bundle import packs/<pack_id> --apply`. The
pack is active at once: the next poll reads its `source`. Give people access with
`ops add-user <email> --groups business-operations-<g>` for each `entitlement` group.

To make a case without waiting for the schedule, post or find one exception in SAP, then run
`ops poll <pack_id>`. Following the case to a final status, and tuning it, is the job of
`ops-run-case`.

## 6. SAP event trigger (optional)

The poller still runs as the regular sweep, so an event only makes a case arrive sooner. With
`push: nudge`, SAP sends `{"version": 1, "pack_id", "keys": [{<key field>: <value>, ...}]}`
(`UC/types/sap-nudge.schema.json`). The consumer then runs the pack's own query again for those
keys. So a nudge for a record that is not an exception creates nothing.

1. In `platform.yaml`, under `layers.sap.systems.<system>.senders`, list the AWS Identity and
   Access Management (IAM) role or roles the SAP host runs as. Add the system to
   `layers.sap.access.business-operations.ingest`; it must also be in that access row's
   `bypass_gateway`. Deploy the layer, then the use case.
2. Read the system's sender role ARN from the parameter
   `/<project>/<env>/layers/sap/systems/<system>/sender-role-arn`, and the bus ARN from
   `/<project>/<env>/layers/sap/bus-arn`. **Never build either from a stack name**, because
   CloudFormation shortens generated role names. Grant the SAP host's role `sts:AssumeRole`
   on exactly that ARN.
3. On the SAP side, import the SAP layer's generic outbox once per system
   ([`layers/sap/abap/`](../../../../layers/sap/abap/): table `ZAWS_EVT_OUTBOX`, class
   `ZCL_AWS_EVT_OUTBOX`, SM36 report `ZAWS_EVT_DRAIN`). Its class header lists the SDK setup
   (`/AWS1/IMG` profile `ZAWS_EVT`, TVARVC `ZAWS_EVT_BUS_ARN` and `ZAWS_EVT_SYSTEM`).
   - Then write the pack's Business Add-In (BAdI) implementation, which calls
     `zcl_aws_evt_outbox=>record( iv_topic = 'business-operations' iv_body = <the nudge> )`.
   - Its `IF` repeats the condition of the pack's `source.filter`. The nudge's keys use the
     field names of the pack's parent `source.key` (`UC/docs/abap/zcl_im_bo_ingest.clas.abap`
     is the one for `finance_ops`).
   - SE19, which activates the BAdI, is a manual step in the SAP GUI.
4. Dry run: classrun `ZCL_AWS_EVT_OUTBOX` over ABAP Development Tools (ADT). It prints each
   entry's source, detail-type and detail, and sends nothing. Check the detail against
   `UC/types/sap-nudge.schema.json`, and the source against `sap.<system>`.
5. Test it. Send a nudge for a key that is not an exception, and expect no case. Then post a
   real exception, and expect its case within a minute or two, with trigger `sap-nudge`.
6. Optional, once step 5 works: set `source.poll: sweep`. The poller then reads the pack again
   only on the hourly `sweep_schedule` run, and finds what the sender lost. It is a structure
   change, so import the pack again, as in section 5.

## Self-check

- `ops bundle status <pack_id>` shows an active version.
- `list_packs` (authoring MCP server) or `GET /registry` lists the pack with its `source`.
- `ops poll <pack_id>` reports the pack polled, with no `rejected` records you did not expect.

Report `OK: <pack_id>@<version> active in <stack>, <n> case(s) from the first poll`, or
`FAIL: <the step and the message>`, plus the list of what is still open and who owns it.
