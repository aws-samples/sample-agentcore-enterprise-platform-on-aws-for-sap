<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Cost

No figures here: measure your own with `cost-report` and the cost alarm.

## Cost drivers

What drives the bill, in the order it usually matters:

- **Amazon Bedrock inference.** Every turn sends the whole conversation to the model again, so a
  wide SAP read early in a run is billed again on each later turn. Cases run only when queued, so
  the `manual` trigger mode costs nothing until someone starts a case.
- **Agent knowledge**, when it is on: the Amazon Aurora cluster bills for storage and for its
  secret, even while paused.
- **Amazon CloudWatch**, with model content on spans: every prompt and response is ingested as a
  log event.
- **Amazon S3 Vectors** for the knowledge bases: storage while idle, plus ingestion and queries.
- **Everything else** is serverless and bills per request.

## Cost levers

| Lever | Where |
|---|---|
| Narrow the OData (Open Data Protocol) reads: a pack's `source.select` for the poller, and procedure steps that name the fields to read | `pack.json`, the procedure (standard operating procedure, SOP) |
| A cheaper model for a pack, after checking its accuracy with `evals-run` | `model_tier: haiku` in `pack.json` |
| Lower effort, or clear old tool results from what the model is sent | `model_effort`, `context_editing` in `pack.json` |
| Limit turns, so a case that loops cannot run without end | `max_turns` in `pack.json` (15 at most) |
| Limit what one run may spend in tokens or estimated USD | `run_budget` in `pack.json` (default 2,000,000 tokens or $0.50) |
| Limit how many runs go at once, and how often SAP is read again | `pipeline.max_concurrency`, `pipeline.poller_schedule`, `pipeline.sweep_schedule` and a pack's `source.poll` |
| Keep agent knowledge and API-docs search off until a pack uses them | `knowledge.agent_knowledge.enabled`, `knowledge.api_docs_search` |
| Turn model content logging off where evaluation is not needed | `runtime.log_model_content` |

## Measuring and sizing cost

The numbers depend on your SAP data, your procedures and the model each pack uses. So measure
them on a development deployment before you size production.

1. **Cost per case.** Run a set of representative cases. Use the evaluation cases
   (`evals-run --pack <pack> --yes`), which are billed by Amazon Bedrock and post documents to a
   sandbox SAP client, or a few real cases from the console. Then read the runtime's
   `AgentEstimatedCostUSD` metric per process type and model tier: its average and its p90
   ([dev-tune-cost](../../skills/dev-tune-cost/SKILL.md) has the commands).
2. **Idle cost.** Activate the cost allocation tags once
   ([Track costs per component](../../../../docs/PLATFORM_GUIDE.md#track-costs-per-component)),
   leave the deployment idle with the trigger mode `manual` for a few days, and run `cost-report`.
   What remains is what the deployment costs with no cases.
3. **Size it:** cases per month times the p90 cost per case, plus the idle cost. Check that
   `pipeline.max_concurrency` (runs at once) and your Amazon Bedrock quotas cover your busiest
   hour, and set the cost alarm (`alarms.cost_p90_usd`) from the measured p90.
