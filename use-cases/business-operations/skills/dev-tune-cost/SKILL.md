---
name: dev-tune-cost
description: Use when a business-operations pack costs too much per case, or when asked "can this run on haiku", "tune cost", "cut the token bill", "why is this run expensive" or "set the cost alarm". Measures a baseline with the eval corpus and the runtime's cost metrics, then changes one setting at a time (model tier, effort, context editing, prompt size and caching, max_turns and the run budget, max_tokens, the verifier's tier), keeping a change only when every case still passes and the cost per case falls. Finishes by setting the cost alarm from the measured p90.
---

# Tune a pack's cost

Paths are relative to the repository root, and `UC` is `use-cases/business-operations`. "You"
in this skill is the coding agent that runs it. The runs you measure are the business process
agent's runs.

```bash
ops() { ./scripts/deploy.sh usecase run business-operations "$@"; }
PROJECT=<project> ENV=<environment>   # from platform.yaml
NS=$(aws ssm get-parameter --name "/$PROJECT/$ENV/use-cases/business-operations/metrics-namespace" \
  --query Parameter.Value --output text)
```

Work on a `promotion.role: development` stack. Amazon Bedrock bills every evaluation case as a
run, and an `auto_release` case releases its invoice in SAP. So use a demo SAP client.

## 1. Baseline

Read both sources:

- **The runtime's metrics**, per run, under `$NS` with the dimensions `ProcessType` and
  `ModelTier` (and with none, for the totals): `AgentEstimatedCostUSD`, `AgentInputTokens`,
  `AgentOutputTokens`, `AgentCacheReadTokens`, `AgentCacheWriteTokens`, `AgentTurns`,
  `AgentLatencyMs`, `AgentSuccess`.

  ```bash
  aws cloudwatch get-metric-statistics --namespace "$NS" --metric-name AgentEstimatedCostUSD \
    --dimensions Name=ProcessType,Value=invoice_matching Name=ModelTier,Value=haiku \
    --start-time "$(date -u -v-7d +%FT%TZ)" --end-time "$(date -u +%FT%TZ)" --period 604800 \
    --statistics Average SampleCount
  ```

  For the p90, run it again with `--extended-statistics p90` instead of `--statistics ...`.
  The API takes one or the other, not both.

  On Linux, use `date -d '7 days ago'`. Repeat for the cache and turn metrics. `list-metrics`
  shows which pairs of tier and process type have data.
- **The pack's evaluation cases** (`UC/packs/<pack_id>/evals.json`), with the results kept. The
  runner:
  - seeds each case's documents in SAP itself, through the seeder, so it needs
    `test_data.enabled`;
  - runs `pipeline.max_concurrency` cases at once;
  - grades each case against its `expected` block and against SAP's own record.

  ```bash
  ops evals-run --pack finance_ops --only ap_price_variance_within_tolerance,ap_missing_goods_receipt \
    --seed posting_date=<a date in an open period> --results-file before.json --yes
  ```

  Each result includes `passed`, `assertions_passed`, `estimated_cost_usd`, `input_tokens`,
  `output_tokens`, `cache_read_tokens`, `latency_ms` and `runs`. `ops cost-report` is the
  infrastructure bill by component. It does not include Amazon Bedrock inference.

Before you choose a setting, read where the money goes. A run reads its whole prompt again on
every turn. So cache reads are most of the cost of a long run, and the cache write on the first
turn is most of the cost of a short run. The fixed prompt is the platform prompt
(`UC/packs/_platform_prompt.txt`), plus the pack's `base_prompt.txt`, plus the standard
operating procedure (SOP, or procedure) that the router chose.

## 2. One setting per iteration

Change one setting, run the same cases again into `after.json`, and compare. In a development
stack each pack setting is structure, and it is applied without a deploy:

```bash
# edit UC/packs/<pack_id>/pack.json, then
ops bundle import packs/<pack_id>            # dry run: the checks and the diff
ops bundle import packs/<pack_id> --apply    # active in seconds; a new pack version
```

In order of what they usually save:

1. **`model_tier`** (`haiku`, `sonnet`, `opus`). This setting saves the most. Over one measured
   week, haiku-tier runs cost a quarter to a tenth of sonnet-tier runs. A tier that the
   deployment does not serve is refused. It is never replaced with another tier without notice
   (`runtime.haiku_model_id`, `runtime.opus_model_id`, `agents.allowed_models`).
2. **`model_effort`** (`low` to `max`). Fewer thinking and output tokens per turn. If it is
   absent, the model's default applies. The default differs by tier, so the same value can be a
   change on one tier and have no effect on another.
3. **`context_editing`** (`keep_tool_uses`, `trigger_input_tokens`, `clear_at_least_tokens`).
   It removes old tool results from what the model is sent. So an SAP read that the business
   process agent already used is not billed again on every turn. Each removal writes the message
   cache again from that point, and `clear_at_least_tokens` limits how often that happens.
4. **Prompt size and caching.** Caching is automatic. The runtime marks the prompt for Amazon
   Bedrock prompt caching on every call, so a shorter procedure or base prompt makes both the
   first write and every read smaller.
   - Under the `auto` trigger mode, a burst of cases for one pack sends the first case and makes
     the others wait for `pipeline.warm_delay_seconds`. The default is 15, and 0 turns it off;
     it is a `platform.yaml` setting that needs a deploy.
   - The wait lets the other cases read the cache that the first case wrote, instead of each
     case writing it. Keep the wait well under the cache's five-minute lifetime.
5. **`max_turns`** (1 to 15) and **`run_budget`** (`max_tokens`, `max_cost_usd`; default
   2,000,000 tokens or $0.50). These stop a run that does not end. Lowering either one reduces
   the worst case, not the typical one. A case that needs more turns stops and waits for review.
   A run over budget ends with `RUN_BUDGET_EXCEEDED`, and its case waits for review too. Both
   settings are structure, so a pack admin sets them ([configuration](../../docs/configuration.md)).
6. **`runtime.max_tokens`** (`platform.yaml`, a runtime deploy). The largest output per call. If
   it is below what a closing answer needs, the run's answer is cut off, and a finished case
   waits for review.
7. **The verifier's `model_tier`** (`verifier` in `pack.json`). The verifier is a second model
   that must agree with every write that moves a payment, before the write runs. Its cost per
   call is in each trace's `verifier_calls`. Never lower it or turn it off unless the pack owner
   says so.

## 3. Keep or revert

Compare `before.json` and `after.json` case by case:

```bash
python3 - <<'EOF'
import json
b = {r["test_id"]: r for r in json.load(open("before.json"))}
a = {r["test_id"]: r for r in json.load(open("after.json"))}
for k in sorted(b):
    x, y = b[k], a.get(k, {})
    print(f"{k:50} pass {x.get('passed')}->{y.get('passed')} "
          f"cost {x.get('estimated_cost_usd', 0):.4f}->{y.get('estimated_cost_usd', 0):.4f}")
EOF
```

Keep the change only if every case that passed still passes, and the mean cost per case is
lower. A lower cost with one case failing is a failure. Go back with
`ops bundle rollback <pack_id> --to N --apply` (`ops bundle versions <pack_id>` lists N). When
the trace spans do not include the model content, the judge scores are absent, not zero. The run
says so, and grades the assertions alone.

## 4. Set the cost alarm

`alarms.cost_p90_usd` (default 0.10 USD) sends an alarm when the hourly p90 of
`AgentEstimatedCostUSD` goes above it. The default leaves room for haiku-tier packs. After tuning, set it to two to three times
the p90 that `cost-report` shows for the most expensive pack the stack runs. Then deploy the use case's stacks. A sonnet-tier pack needs its
own value.

## Self-check

Print one line:

- `OK: <pack_id> mean USD <before> -> <after>, p90 <before> -> <after>, <n>/<n> cases pass, alarm <threshold>`
- `FAIL: <cause>`: no baseline, a case that passed before fails now, or the cost did not fall.
