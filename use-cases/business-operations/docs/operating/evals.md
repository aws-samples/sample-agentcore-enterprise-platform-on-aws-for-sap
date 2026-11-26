<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Evaluation runs

`evals-run` seeds each case of `packs/<pack>/evals.json` in SAP, and runs it through the deployed
invoker. Then it checks the case's outcome and scores it with the judges.
[Decisions](../decisions.md#evaluations-run-before-promotion) explains why a model or procedure
change must pass the evaluations first.

```bash
./scripts/deploy.sh usecase run business-operations evals-setup     # once: the judges' SDK and the SAPActionAccuracy judge
./scripts/deploy.sh usecase run business-operations evals-run --pack finance_ops --yes
```

Options: `[--pack P] [--only a,b] [--tag T] [--seed name=value] [--results-file f] [--no-judges]
[--allow-quality] [--publish] [--yes]`. `--tag red-team` runs the adversarial cases only.

## Results in the console

`--publish` records each pack's results in the stack once the run ends. For each case it records
whether the case passed, why not, its status, its cost and each judge's score. It also records the
run's totals and the pack version it graded. The results go through the pack composer's
`record_evaluation` verb, with your AWS credentials. They are stored under
`_evaluations/<pack_id>/` in the registry bucket, which no pack version contains.

The console's Evaluations part shows the latest results (`GET /packs/{pack_id}/evaluations`),
next to the pack's example cases. It estimates the price of the next run from the results' cost
per case. A run still starts only from this command.

The example cases themselves are pack content. In development, a pack's authors add, replace or
remove an example case from the console ("Save as example case" on a rehearsal or a test data
post), through `PUT` and `DELETE /packs/{pack_id}/evaluations/{test_id}`. Each change is the
pack's next version, with its author recorded, like an edit to a standard operating procedure
(SOP). In other stages, example cases arrive by bundle import.

## Stack requirements

`evals-run` runs only on a stack whose `promotion.role` is `development`, or `quality` with
`--allow-quality`, because every case posts real SAP documents. It stops before the first case
unless the seeder reports that every pack is on an SAP system that has `system_role: sandbox`
and that answered.

It bills Amazon Bedrock per case, and an `auto_release` case releases its invoice in SAP.

## Case checks

Each case checks its outcome, its tool calls and SAP's end state. Where the case asks, it also
checks the Gateway's verdicts and the guardrails. The judges then score it. They take the case's
`expected.reason` and the pack's published rules (its `constants`) as the correct answer.

On a stack without `security.require_guardrails`, a case that grades the guardrails reports
SKIPPED, which is neither a pass nor a fail. The judges need `observability.transaction_search` and model
content on spans; without them the run grades the checks alone, and says so.

## Failure conditions

The run fails when:

- a case fails its checks;
- more than 10% of cases recovered from a tool error (a warning, not a failure, on the case);
- a judge's mean over the run is under `--threshold` (0.7).

It reports cost per case and p90 wall time. `evals/compare_results.py` compares one results file
with another.
