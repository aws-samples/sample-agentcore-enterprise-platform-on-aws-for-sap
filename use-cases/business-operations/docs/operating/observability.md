<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Observability

The use case has its own Amazon CloudWatch dashboard, next to the platform's. The deploy prints
its URL as `BusinessDashboardUrl`. Metrics are in the `<project>-<env>/business-operations` namespace.

## Model content in the log group

The agent writes prompts and model responses as span events, so the evaluators can score them.
This logging is on by default outside `deployment.mode: production`, and off in it. Set it either
way with `runtime.log_model_content`.

When it is on, SAP document contents, vendor names and operators' words are stored in CloudWatch
as plain text. They are event records in the runtime's own log group (stream `otel-rt-logs`).
Anyone whom IAM allows to read that log group can read them, whatever their pack entitlement. The
spans in `aws/spans` contain metadata only.

## Alarms

Each alarm publishes to the platform's alarm topic on ALARM and on OK. It reaches a person once
`observability.alarm_email` is subscribed, and `deploy.sh verify` checks that connection. The use
case has alarms on:

- a dead notification channel (`NotificationFailure`);
- the registry;
- agent runs in the dead-letter queue;
- success under 90% over an hour;
- p90 estimated cost per run over `alarms.cost_p90_usd`. Set it from your own pack's runs. A
  `sonnet`-tier pack needs a higher figure than a `haiku`-tier one;
- run-channel publish failures;
- rejected or dead-lettered ingest messages (`IngestRejected` by reason, `IngestDlq`);
- the SAP events rule failing to deliver to the ingest queue (`FailedInvocations`). The dead
  letters include `ERROR_CODE`;
- cases the poller made that an SAP sender should have nudged first (`IngestMissed`, whose log
  lines name the pack and key). This alarm is only for systems in
  `layers.sap.access.business-operations.ingest` and packs that set `source.push`, after two
  polls in a row;
- errors or throttles in the pipeline functions;
- the run checks refusing this use case's Gateway calls: more than four in 15 minutes. They are
  counted from the shared interceptor's log lines that name `business_operations_check`.

The audit-change alerts are in [Security](../security.md#audit-change-alerts).

## Automatic containment

Four alarms start containment when they go to ALARM:

- the run checks alarm;
- the cost alarm;
- the dead-letter alarm;
- the platform's guardrail content-filter alarm (`<prefix>-guardrail-content-interventions`,
  present while `observability.alarms` is on).

An Amazon EventBridge rule then invokes the containment function (`containment/index.py`). The
function sets the trigger mode to `manual`, and tells the ops topic which alarm fired and why.

The function only ever writes `manual`, and it does not overrule a person. It skips a trigger mode
that someone wrote after the alarm fired. An operator who sets `auto` again keeps it until the
next alarm, because an alarm that stays in ALARM sends no second event.

The guardrail alarm counts every guardrail in the account. So a prompt attack on another use case
also starts containment for this use case. The function's role reads and writes only the
trigger-mode parameter and the attribution record beside it, and publishes only to the ops
topic.

`manual` stops unattended work from starting: the poller, SAP push and the sweeper create cases
in `detected` and queue nothing. It does not stop:

- a run already queued or under way;
- a run that a person starts from the console;
- a reply to the agent's question;
- `rerun` or `evals-run`.

For those, use [Break glass](break-glass.md).
