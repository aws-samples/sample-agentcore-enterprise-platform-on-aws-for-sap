<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Controls and evidence

Terms are defined in the [glossary](glossary.md) and in [Concepts](../../../docs/start/concepts.md).

This page is for a control owner or an auditor. It lists every way this use case can change SAP
or change its own behavior, who allows each change, and where the record of it is kept. How each
control works is in [Security](security.md). What each control stops, and how that was proven, is
in the [threat model](reference/threat-model.md).

## Changes to SAP

| Path | What allows it | Who decides | Record |
|---|---|---|---|
| **The agent posts a change within the limit** | The case's own document only (`case_document_check`); at or under the pack's `AUTO_WRITE_LIMIT_USD`; an unexpired run grant; the use case's SAP access claim in the SAP layer; and Cedar's permits when Cedar is in `ENFORCE`. All are checked in the Gateway, outside the model | The pack's limit, which the process owner sets; nobody approves each change | The case's action log: each step, the SAP data the run read, the version of the standard operating procedure (SOP) it followed, and the change it posted |
| **The agent posts a change above the limit** | Everything above, plus an approval of that exact change, field for field | A member of the pack's `approve_groups`; above `DUAL_APPROVAL_LIMIT_USD`, two different members | The action log entry `human_approved`, with the approver's identity, the time and the change they approved; the approval ticket |
| **The test-data seeder posts sample documents** | `test_data.enabled`, a `sandbox` system only, the system's service account. It is invoked with AWS Identity and Access Management (IAM) credentials, or by an operator through the console's test data route for a pack on a sandbox | Whoever may invoke it in AWS, and the operators (`business-operations-admins`) | AWS CloudTrail for the invocation, the seeder's record of each seed (caller, inputs, expected outcome, documents), and its function's logs |
| **A person changes SAP directly** | Out of scope: SAP's own authorizations | SAP | SAP's own logs |

A change needs every check in its row's "What allows it" cell. The run grant is a short-lived
permission written for one run, so only a real run can change a case or SAP. The SAP access claim
is what the use case says it will use, like the authorizations an SAP role requests. The
operator's access row in the SAP layer is the upper limit that the claim must fit inside.
[SAP access limits](architecture.md#sap-access-limits) says which of these checks still apply when
Cedar is off or in `LOG_ONLY`.

**Whose identity SAP sees.** On the `basic`, `m2m-sap` and `m2m-oidc` flows, SAP's logs name the
system's service account for every change the agent makes, not the person who approved it. The case
record is what connects an SAP change to the run, the approver and the schedule behind it. Every
Gateway call includes the run id (`x-audit-correlation-id`), and the SAP layer forwards it to the
AWS for SAP MCP (Model Context Protocol) Server. Under `obo` (on behalf of), SAP works as the
signed-in person, which SAP calls principal propagation
([Which flow](../../../layers/sap/walkthrough.md#sign-in-flows)).

**Self-approval.** This use case does not stop the person who started a run from approving it.
If your separation-of-duties rule requires that, set the pack's approver group to people who do
not start runs, or set a two-approver limit ([production checklist](production-checklist.md)).

## Changes to what the agent does

| Change | Who may make it | Record |
|---|---|---|
| A procedure's wording, the agent's instructions | Pack authors (`business-operations-authors`, narrowed per pack), in a development deployment only; other stacks take it by importing a reviewed pack version | Each save is a pack version naming its author; the SOP bucket is versioned and no principal can delete a version (old versions expire after seven years, below); each case names the exact procedure version it followed |
| A tolerance's value | Pack authors with the pack's `constant_groups`, in a development deployment, as a new pack version | The pack version and its audit record |
| A contact | Pack authors, per deployment | The configuration table, with point-in-time recovery |
| A lower write limit | Limit owners (`business-operations-limit-owners`, narrowed per pack by `limit_groups`), per deployment; a limit can be lowered, never raised above the pack's value | The configuration table, with point-in-time recovery |
| A pack's structure: its SAP data, tools, limit defaults, new exception types | Pack administrators, in a development deployment only; other stacks take it by importing a reviewed pack version | Each pack version and the audit record of who activated it, when, and with which change reference (`bundle versions <pack>`, below) |
| Trigger mode: whether new cases start on their own | Administrators (`business-operations-admins`) | An alert to the operations topic naming the principal, from AWS CloudTrail |
| Anyone else writing a procedure, the pack registry or their bucket policies | Nobody: the bucket policies deny it | An alert to the operations topic ([Audit-change alerts](security.md#audit-change-alerts)) |

These alerts come from AWS CloudTrail, so they work only while a trail records management events
in the Region. An alert reaches a person only after the address in `observability.alarm_email`
confirms its subscription.

## Record locations and retention

| Record | Where | Kept |
|---|---|---|
| Cases and their action logs | The cases table (Amazon DynamoDB), point-in-time recovery on | Until you delete them; no expiry is set. Under `deployment.mode: production` the table survives stack removal |
| Approval tickets (the sample ticketing) | The tickets table | Until you delete them. Replace sample ticketing with your IT service management (ITSM) tool before production |
| Procedure versions | The SOP bucket (Amazon S3), versioned | The current version always. An old version moves to S3 Glacier after 30 days and expires after 7 years. No principal can delete a version. If your records policy needs longer, change the `glacier-noncurrent` lifecycle rule in `business_operations/data.py` |
| Pack versions and their audit records | The pack registry and its bucket | Every version |
| Agent run traces and logs | Amazon CloudWatch | `observability.log_retention_days` |
| AWS API activity | AWS CloudTrail, when the platform's security stack or an organization trail is on | Your trail's retention |

Under production, the tables and buckets use the platform's AWS Key Management Service (KMS)
key. The exception is the knowledge bases' S3 Vectors buckets, which stay on S3-managed keys,
because a use case cannot add the S3 Vectors service to the platform key's policy.

Decide each retention period with your records policy before production. The
[data classification template](../../../docs/DATA_CLASSIFICATION.md) lists what each record
contains.

## Case record contents

One item in the cases table per SAP exception; `types/cases.schema.json` is authoritative.

| Part | What it records |
|---|---|
| Identity | `case_id`, derived from the pack and the SAP document's key, so one exception is one case; `key`, the SAP fields that name the document; `pack_id` and `process_type` |
| State | `status`, `status_reason` when the status alone does not say why, and `disposition`, what was decided (such as `released`) |
| What SAP said | `attributes`: the values the pack's mapping read from SAP when the case was made |
| `action_log` | Append-only, written by the server: one entry per case update the agent made and one per human decision, with the approver's user id (`sub`) and display name, the source route, and for an approval the digest of the exact change approved and the run that used it |
| `agent_traces` | One per run: what started it and for whom, the procedure version, its S3 key and object version, the tool calls with what each read from SAP, any proposed change, the clauses cited and whether each was found in what the agent was shown, tokens and estimated cost. Oldest traces beyond the cap are dropped, and `traces_dropped` says how many |
| `ratings` | Thumbs up or down per person, with an optional comment |
| `cost_summary` | The running cost of every run on the case |

## Producing evidence

| Question | Where to look |
|---|---|
| Why did the agent do this to this invoice? | The case in the console: its steps, the SAP data it read, the procedure version and the clause it cited |
| Who approved this change? | The case's action log (`human_approved` entries) |
| Which procedure wording was in force on a date? | The procedure's object versions in the SOP bucket, and `sop_version_id` on each case |
| Who changed a pack, and when? | `./scripts/deploy.sh usecase run business-operations bundle versions <pack>` |
| Is everything still enforced? | `./scripts/deploy.sh verify`, which fails if a control is off, and `./scripts/deploy.sh usecase run business-operations production-check` |
