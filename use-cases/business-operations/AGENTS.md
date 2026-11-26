# Agent Instructions (business-operations)

The SAP exception handling use case. It is one use case on the shared platform, so the root
`AGENTS.md` applies here too, most of all its "Rules for sharing the platform". Paths below are
relative to this directory.

## Layout

| Path | What it is |
|------|------------|
| `manifest.yaml` | The only file the platform reads: stacks, `requires`, Gateway clients, public outputs |
| `stack.py`, `business_operations/` | The use case's CDK, one module per area. `config.py` is its pydantic config model; `names.py` builds every physical name |
| `business_operations_check/` | The run checks: the check that the shared Gateway interceptor runs for this use case's targets. On the SAP layer's targets, it decides only for this use case's own apps |
| `api_authorizer/`, `audit_alert/`, `containment/` | The REST API's token authorizer under direct (Entra) mode; the alert to the platform ops topic on a trigger-mode change, a `registry.json` write, an SOP written by anyone but its writer, or a change to the SOP or registry bucket's policy; and the function that sets the trigger mode to `manual` when an anomaly alarm fires |
| `business_operations/sap.py` | Its SAP access on the SAP layer (`../../layers/sap/`): its SAP access claim and its Cedar permits. The runtimes and targets belong to the layer |
| `agent/` | The business process agent's runtime image, with its own tests in `agent/tests/` |
| `tests/`, `fixtures/contract/` | The use case's tests, collected with the platform suite, and the `platform.yaml` fixtures `scripts/check-contract.sh` synthesizes. The fixtures check that the platform works with this use case turned off (rule 1 in the root `AGENTS.md`) |
| `api/<route>/index.py` | REST handlers, one directory per route group, each wrapped in `api_http.route`. `api/layer/` is the shared Lambda layer, and the only way a Lambda gets shared code. A module that the agent image needs too is in `agent/utils/`, and the layer links to it with a symlink. `openapi.yaml` is the REST contract |
| `case_management/`, `sop_tools/`, `knowledge_search/`, `notification/`, `tickets_tool/`, `agent_knowledge/` | Gateway target Lambdas |
| `pipeline/` | Ingest and workers: the SAP poller, ERP event intake, batch runner, exemplar builder |
| `invoker/`, `pack_composer/`, `run_events_auth/` | The queue consumer that invokes the agent; the only writer of pack documents, the registry and standard operating procedures (SOPs); and the run-channel subscribe authorizer |
| `test_data/` | The seeder. It posts one pack's exception into SAP for a demo, and records each seed. `ops.py seed <pack>` invokes it over IAM, and `api/test_data/` (the console's test data page) invokes it asynchronously |
| `packs/<pack_id>/` | Use case packs: `pack.json`, `base_prompt.txt`, `sops/`. `packs/pack.schema.json` is authoritative for `pack.json`. A leading underscore means "not a pack" (`_platform_prompt.txt`, `_seed/`) |
| `types/*.schema.json` | Runtime data contracts. The console's TypeScript copies and `agent/utils/contracts.py` (statuses, id patterns, the live-run window) are generated from them |
| `frontend/` | The operator console (React, vitest) |
| `tools/`, `packages/` | Model Context Protocol (MCP) servers for desktop MCP clients, and the run client package |
| `evals/` | Regression evals against a deployed agent |
| `skills/<name>/SKILL.md` | Skills for coding agents, which deploy this use case and author its packs and SOPs. A clone finds them through symlinks at the repository root: `.claude/skills/<name>`, `.agents/skills/<name>` and `.kiro/skills/<name>`. Add all three links with a new skill (`tests/test_business_operations_skills.py` checks) |
| `verify.py`, `ops.py`, `walkthrough.md` | The check of the deployed stack that `deploy.sh verify` runs, the operator commands, and the getting-started walkthrough |
| `docs/` | One page per question (architecture, lifecycle, configuration, packs, security, operating, direct mode), linked from the walkthrough. `docs/reference/` contains the internals: `runtime.md` (runs, streaming, the run channel) and `threat-model.md`. Read the page for an area before changing it |

## Packs and deploys

- No pack change needs a deploy after a stack's first deploy. A pack moves as a bundle
  (`api/layer/python/bundle.py`) through `pack_composer`'s verbs, which `ops.py bundle …` invokes
  over IAM.
- In a `promotion.role: development` stack, the checkout is the source. The pack import at
  deploy takes a checkout pack that has no version, or that changed since the last deploy took
  it. It fails the deploy for a pack that an edit in the stack also changed
  (`_checkout_step`).
- Every stack adopts its active packs as version 0. SOPs are written first, so a published
  mapping always has its SOP behind it.
- `packs/` is validated at synth (`business_operations/validate_packs.py`), so a broken
  shipped pack fails `cdk synth`. An import checks a bundle again against the deployed stack.
- `pack_tiers.check_patch(keys, roles, env)` is the one rule. Content (`display_name`,
  `description`, `base_prompt`) needs the pack's author grant. Every other key is structure,
  which needs `business-operations-pack-admins`.
- Both kinds of edit happen only in development. In a quality or production stack,
  `pack_tiers.edit_refusal` refuses every pack and SOP write over HTTP, so a pack arrives there
  only by an import with a change reference. `POST /packs` creates a pack in development only.
- Pack entitlement groups start with the use case's prefix, because the user pool is shared.
- Docs, skills and the console name a pack's contents by its five parts: intake, procedures,
  controls, agent configuration and evaluations (`docs/packs.md#pack-parts`). They name people by
  the roles in `docs/roles.md` and `../../docs/start/roles.md`. Use those names, and update the
  pages when a key, a group or an owner changes.

## Tests and checks

| What | Command (from the repo root) |
|------|---------|
| Use case unit tests | `python -m pytest -q use-cases/business-operations/tests` (part of the full suite) |
| Agent image tests | `python -m pytest -q use-cases/business-operations/agent/tests`, with `agent/requirements.txt` installed |
| Console | `npm ci`, `npx vitest --run` and `npm run build` (also the typecheck) in `frontend/` |
| Generated TypeScript copies | `scripts/deploy.sh usecase run business-operations types --check` |
| Deployed stack | `scripts/deploy.sh verify` runs this use case's `verify.py` |

## Do not modify, or modify only deliberately

- `packs/*/sops/`: SOPs are written in RFC 2119 language (MUST, SHOULD, MAY). Revise one
  deliberately and keep that form.
- `frontend/src/types/generated-*.ts`: edit the schema under `types/`, then run
  `scripts/deploy.sh usecase run business-operations types`.
