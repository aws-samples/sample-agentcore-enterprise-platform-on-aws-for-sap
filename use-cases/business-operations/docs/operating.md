<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Operations

Terms are defined in the [glossary](glossary.md) and in [Concepts](../../../docs/start/concepts.md).

Every command here is `./scripts/deploy.sh usecase run business-operations <verb>`.

| Page | Covers |
|---|---|
| This page | Operator commands, production readiness, troubleshooting |
| [Observability](operating/observability.md) | The dashboard, the alarms, model content in logs, automatic containment |
| [Break glass](operating/break-glass.md) | Stopping agent writes to SAP, step by step, and undoing each step |
| [Cost](operating/cost.md) | What drives the bill, the levers, measuring and sizing |
| [Evaluation runs](operating/evals.md) | What `evals-run` checks, where it may run, when it fails |
| [Removal](operating/removal.md) | Removing the whole deployment or only this use case, and what a production destroy keeps |

## Operator commands

| Verb | What it does |
|---|---|
| `status` | Every stack of the use case, and the parameters it published |
| `add-user <email> [--create] [--admin] [--author] [--limit-owner] [--case-worker] [--remote-author] [--pack-admin] [--groups …]`, `add-group <name>` | Add a pool user to the use case's groups. Create a `business-operations-` group that a bundle requires ([walkthrough](../walkthrough.md#user-access)) |
| `trigger-mode`, `trigger-mode set auto\|manual [--pack <pack_id>] [--yes]` | Read the deployment's trigger mode and each pack's mode, or change one with your AWS credentials. The change is attributed to you, as `PUT /deployment/trigger-mode` attributes it. A case is queued unattended only when the deployment's mode and its pack's mode are both `auto`. An unset pack is `manual`. `--pack` names a pack in the checkout, or one that already has a mode. `auto` asks first |
| `emergency-stop "<reason>" [--yes]`, `emergency-stop --clear "<reason>" [--yes]` | Start the emergency stop, or clear it, with your AWS credentials, through the stop function, as `POST /deployment/stop` and `/clear` do. Nothing starts while the stop stands ([Break glass](operating/break-glass.md#emergency-stop)) |
| `break-glass [--undo] [--yes]` | Stop every agent run from starting. It sets the trigger mode to `manual`, pauses the agent queue's consumer, and sets the invoker's reserved concurrency to zero. `--undo` resumes the invoker and leaves the trigger mode `manual`. Asks first ([Break glass](operating/break-glass.md)) |
| `poll [<pack>]` | Run the SAP poller now, for one pack or every pack. It creates cases exactly as the schedule does |
| `seed <pack> …` | Post one exception into SAP through the test-data seeder ([Configuration](configuration.md#api-contacts-test-data-alarms)). Asks first |
| `rerun <case_id> [--force]`, `rerun --dlq [--max N] [--force]` | Put a case back on the agent queue, through the `OperatorRerun` producer. The run is billed by Amazon Bedrock and runs as the use case's machine client. Your credentials need `lambda:InvokeFunction` on the producer, and the queue refuses every other sender. The run includes no human decision, so a case that waits for an approval stops at the approval again. `rerun` refuses a settled case, and a case that waits for a person needs `--force`. `--dlq` queues again the case of each dead letter (10 by default). It drops the dead letter's trigger and payload and prints what it dropped, so a person's reply that was dead-lettered must be given again |
| `bundle …` | Move a pack version between stacks ([Packs](packs.md#promotion-between-stacks)) |
| `kb-sync` | Start an ingestion job on every deployed knowledge base |
| `knowledge-load <file.json>` | Load vendors, vendor edges and lessons into agent knowledge ([Configuration](configuration.md#knowledge)) |
| `mcp-config` | Print the Model Context Protocol (MCP) host config for the two MCP servers ([tools/README.md](../tools/README.md)) |
| `production-check [--offline]` | Read-only: one PASS, WARN or FAIL row per production control, each with its fix ([Before production](#before-production)). Exits 1 on a FAIL |
| `types [--check]` | Regenerate the console's TypeScript twins of `types/*.schema.json`, or fail when one is stale (needs no AWS) |
| `cost-report [--months N] [--json]` | Cost Explorer by the `Component` tag and by service. Needs the `Project`, `Environment` and `Component` cost-allocation tags activated. Amazon Bedrock inference has no tags, so it is not in the report |
| `evals-setup` | Install the judges' SDK (`bedrock-agentcore`) into the `.venv` when it is missing, then create this deployment's `SAPActionAccuracy` judge |
| `evals-run [--pack P] [--only a,b] [--tag T] [--seed name=value] [--results-file f] [--no-judges] [--allow-quality] [--publish] [--yes]` | Seed each case of `packs/<pack>/evals.json` in SAP, run it, assert its outcome and score it with the judges; `--publish` records the results for the console. Development stacks only (or `quality` with `--allow-quality`), since every case posts real SAP documents and bills Amazon Bedrock ([Evaluation runs](operating/evals.md)) |

## Observability

The deploy prints the use case's CloudWatch dashboard URL as `BusinessDashboardUrl`. Alarms
publish to the platform's alarm topic and reach a person once `observability.alarm_email` is
subscribed.

When the run checks alarm, the cost alarm, the dead-letter alarm or the guardrail alarm fires,
containment sets the trigger mode to `manual` by itself. `manual` stops new unattended runs only.
To stop everything, use [break glass](#break-glass). The alarm list, model content in logs and
containment are in [Observability](operating/observability.md).

## Break glass

To stop agent writes to SAP now, follow [Break glass](operating/break-glass.md). It has five
steps, from the narrowest effect to the widest, and each has its check and its undo. The second
step stops every run from starting, console runs included:

```bash
./scripts/deploy.sh usecase run business-operations break-glass
```

Keep a copy of the break-glass page, and your on-call and SAP administrator contacts, somewhere
that does not depend on this deployment.

## Cost

Amazon Bedrock inference is usually most of the bill. With the `manual` trigger mode, inference
costs nothing until someone starts a case. Measure your own figures with `cost-report` and the
cost alarm. [Cost](operating/cost.md) lists what drives the bill, the levers in `pack.json` and
`platform.yaml`, and how to size production.

## Before production

This is sample code, not a hardened product. `production-check` reads `platform.yaml` and
EventBridge, and reports on:

- the deployment mode, Cedar and the promotion role;
- bundle signing (bundles are not signed), self sign-up, sample ticketing and test data;
- log retention and contacts;
- the AWS for SAP MCP Server's TLS and the audit-change alarms.

What no verb checks, the SAP side included, is in two checklists: the
[platform and SAP layer's](../../../layers/sap/production-checklist.md) and
[this use case's](production-checklist.md). Neither is complete.

## Troubleshooting

**Sign-in and access**

- **A user never signed in, and the temporary password expired.** Amazon Cognito keeps the user
  in `FORCE_CHANGE_PASSWORD` and refuses a reset ("User password cannot be reset in the current
  state"). Send the invitation again, which mails a new temporary password:
  `aws cognito-idp admin-create-user --user-pool-id <pool id> --username <email> --message-action RESEND`.
  The pool id is in the AWS Systems Manager (SSM) parameter `/<project>/<env>/auth/user-pool-id`.
- **The briefing does not use your name.** The console takes it only from the token's
  `given_name` claim (`frontend/src/lib/identity.ts`), and the platform's pool does not require
  that attribute.
  Set it: `aws cognito-idp admin-update-user-attributes --user-pool-id <id> --username <email> --user-attributes Name=given_name,Value=Ada`.
  For a federated user, map `given_name` on the Cognito identity provider, because the identity
  provider (IdP) owns that attribute.
- **403 `Not a Business Operations user`.** The token lacks `business-operations-users`: add the
  user, or map their IdP group, and have them sign in again.
- **403 `Token was not issued for this application`.** The id token is from another client on the
  shared pool, such as the platform's own. Only the console client's tokens are accepted.
- **A tool is missing from `tools/list`.** Under `ENFORCE`, `tools/list` leaves out a tool that
  no permit allows, instead of refusing it. A missing tool is an authorization answer.

**Deploy**

- **`AWS::BedrockAgentCore::Policy … ConflictException`.** An AgentCore control-plane call was
  throttled and then retried, but the first call had already succeeded. The stack ends in
  `UPDATE_ROLLBACK_FAILED`. Finish the rollback with
  `aws cloudformation continue-update-rollback --stack-name <stack>` (about five minutes), wait for
  `UPDATE_ROLLBACK_COMPLETE`, then deploy again.
- **`Cannot perform more than one GSI creation or deletion in a single update`** on the `Cases`
  table, when you upgrade a stack deployed before 2026-11-19. The upgrade adds two indexes to the
  table, and DynamoDB accepts one per update. The stack rolls back with no damage. Deploy once with
  the second index deferred, then again without the variable:
  `DEFER_CASES_AMOUNT_INDEX=1 ./scripts/deploy.sh build`, then `./scripts/deploy.sh build`.
  Until the second deploy, sorting cases by amount fails.
- **`… must be an https:// URL`.** The SAP layer refuses a `base_url` that is not `https://`, so
  a service account's credentials are never sent over the network unencrypted. Enable TLS on the
  SAP endpoint, or on a trusted reverse proxy in front of it. There is no exception for plain
  HTTP.
- **The API stage fails to create.** API Gateway has no CloudWatch role in this account and Region
  ([Configuration](configuration.md#api-contacts-test-data-alarms)).
- **`Access denied while validating ECR URI` on the SAP runtime**, with CloudTrail saying "no
  resource-based policy allows the ecr:BatchGetImage action". AWS's image repository allows a
  pull only from its own Region's registry account, and only by a role named
  `AWSForSAP-MCP-Server-ExecutionRole-<id>`, where `<id>` is at most 8 lowercase letters and
  digits. The stack does both. A Region missing from `IMAGE_REGISTRY_ACCOUNTS` fails the synth
  instead.

**SAP**

- **401, 403 or 404 from SAP**, in the poller's log or an SAP tool's result:
  - A 401 is the credential. Check the system's secret, and rotate it with
    `deploy.sh layer run sap secrets <system>`.
  - A 403 means the service user lacks SAP authorizations.
  - A 404 means the OData (Open Data Protocol) service is not activated on the system.

  The SAP-side steps are in the [SAP layer walkthrough](../../../layers/sap/walkthrough.md).
- **An SAP tool call answers with a connection error while `tools/list` works.** The server lists
  its tools without calling SAP. Check that SAP is up and reachable from the runtime's network.
- **A run stops streaming but the case finishes.** A run-channel publish failed. The run and its
  trace are not affected, and a reload shows the result ([runtime reference](reference/runtime.md#failure-behaviour)).

## Remove

`./scripts/deploy.sh destroy` removes every stack, and asks first. Close every console tab
before you run it. Under `deployment.mode: production`, note the platform key's ARN first. The
destroy leaves each SAP system's secret, the log groups that Lambda created, and, under
production, the data kept on purpose.

[Removal](operating/removal.md) has the steps, what a production destroy keeps and how to delete
it, and how to remove only this use case.
