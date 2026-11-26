# SAP layer threat model

Terms are defined in [Concepts](../../docs/start/concepts.md).

The threat register for `layers/sap/`. It covers:

- the AWS for SAP MCP (Model Context Protocol) Server runtimes and Gateway targets that the layer
  builds for each SAP system
- the SAP access claim protocol
- the layer's interceptor check

It covers what the layer adds, and where the layer meets the platform and the use cases that
require it. The platform's own threats are in [`docs/THREAT_MODEL.md`](../../docs/THREAT_MODEL.md).
A use case's threats are in its own register, which its own pages link
([Use cases](../../use-cases/README.md)).

Paths are relative to the repository root. `L` stands for `layers/sap`.

## Evidence

| Evidence | Meaning |
|---|---|
| **Live** | Observed on a deployed stack. Rows marked "as business-operations" were measured when these resources were in that use case's stack, before a stack refactor moved them unchanged to the layer. The layer's `verify.py` checks them again on every deployment |
| **Unit** | Checked by a unit or synth test, never run against a deployed stack |
| **Reasoned** | Read in code and reasoned about. Not tested |

## Changes the layer introduces

Before the layer, every client that the shared Gateway accepted could read SAP as the service
account while Cedar was off or in `LOG_ONLY`. Only the run checks of business-operations
controlled SAP writes. Now the layer's check refuses any SAP call, read or write, from an app
that no use case's SAP access claim names, in every Cedar mode. Under `ENFORCE`, a call must pass
both Cedar (a use case's permit) and the SAP access claim check.

## Assets

| Asset | Where | Why it matters |
|---|---|---|
| SAP data | Read through the AWS for SAP MCP Server | Confidential business records |
| SAP write authority | Each system's service account, used by its MCP server and by the direct callers a use case's `access.bypass_gateway` names | A write posts or releases real documents |
| SAP access claims | `layers/sap/claims/<use case>`, one SSM (AWS Systems Manager) parameter per use case, written by its stack | An SAP access claim gives its apps SAP access through the Gateway |
| The check's settings | `layers/sap/check`: targets, whether Cedar enforces, the access row (upper limit) per use case | The check decides on these |
| SAP credentials | AWS Secrets Manager, created by the operator, named in `layers.sap.systems` | Each signs in as the service account |

## Threats

| ID | STRIDE | Threat | Control | Enforced at | Evidence | Residual |
|---|---|---|---|---|---|---|
| SAP-01 | S, E | A principal other than the Gateway calls an SAP MCP runtime and reaches SAP past the Gateway, Cedar and every check | The runtime's Gateway-only resource policy: an allow for the Gateway role and an explicit deny of every runtime data-plane action to any other principal (`aws:PrincipalArn`). Every `RuntimeStack` role's invoke is scoped to its own deployment's runtimes, which `bo_sap_` is outside, and the Gateway role trusts AgentCore only for this deployment's Gateway | `L/stack.py` (`gateway_only_policy`), `stacks/runtime_stack.py`, `stacks/gateway_stack.py` | Live as business-operations (IAM simulation: explicit deny for the other runtime roles, allow for the Gateway role); `L/verify.py` checks the policy is present | Low to medium: the Gateway role's trust matches the Gateway's name prefix, not its id. A bring-your-own server's policy is its owner's |
| SAP-02 | E | A client that no use case's SAP access claim names reads or writes SAP | The layer's check refuses every SAP call from an app (`client_id`, or `azp` on an Entra token) that no SAP access claim names, before Cedar, in every mode. No SAP access claim names the platform's own app clients | `L/sap_check/decide.py` | Unit (read and write refused, claimed allowed); `L/verify.py` checks on every deployment that the platform's M2M client is refused a read | Low |
| SAP-03 | E | A use case's SAP access claim asks for more SAP access than the operator intends | `claim` refuses at synth anything outside the access row `layers.sap.access.<use case>`. At run time, the check limits each SAP access claim by that access row again, and reads only the SAP access claims of use cases that have an access row | `L/lib/sap_layer/access.py`, `L/sap_check/decide.py` | Unit | Low |
| SAP-04 | T, E | Someone with `ssm:PutParameter` in the account rewrites an SAP access claim or the check's settings | Three parts. **Prevent:** no role any stack here defines may write, delete or label a parameter under `layers/sap/` (a synth test fails on such a grant, wildcards included), so only CloudFormation's own deploy role and account administrators can. **Detect and refuse:** the check reads each parameter with GetParameterHistory and trusts only a latest version whose `LastModifiedUser` is this account's and Region's CDK bootstrap execution role in session `AWSCloudFormation`. An SAP access claim written by hand allows nothing, and settings written by hand refuse every SAP call. An EventBridge rule on CloudTrail sends the platform ops topic the call, the parameter and the principal (never the value) for any write under the prefix by anyone else. **Recover:** `deploy.sh layer run sap rewrite` bumps each owning stack's `SapRewriteNonce` (in the parameters' descriptions) and updates it on its previous template as the execution role, so CloudFormation writes them again | `L/sap_check/__init__.py`, `L/sap_check/trusted.py`, `L/stack.py` (`write_alert_pattern`, `WriteAlert`), `L/ops.py` (`rewrite`), `L/tests/test_sap_layer.py` | Unit; Live on a development deployment: an SAP access claim written by hand was refused and reported, and `rewrite` restored it | Low. Forging the writer needs the execution role, which can deploy anything. The alert needs a trail logging management events in the account (an organization trail, or the platform's security stack); without one, refusal and recovery still work. A deploy made without the execution role (another synthesizer, a direct `deploy-stack`) is refused too: the check fails closed |
| SAP-05 | T | `odata_delete` | The check refuses it for every caller in every mode, before any read; no permit may name it; each server runs with delete off | `L/sap_check/decide.py`, `L/stack.py` (`runtime_env`) | Unit; Live as business-operations (no delete listed under `ENFORCE`); `L/verify.py` proves the refusal | Low |
| SAP-06 | E | A use case's writes have no check of their own while Cedar refuses nothing | An SAP access claim that names no check of its own (`governed_by: None`) can write only while Cedar enforces, so its Cedar write permit is the only control that allows a write. An SAP access claim that names a check needs that check listed with a prefix covering the SAP targets, checked at synth | `L/sap_check/decide.py`, `L/lib/sap_layer/access.py` | Unit | Low to medium: a use case's own check is what governs its writes outside `ENFORCE` |
| SAP-07 | E | Two use cases' SAP access claims name one app, and each one's rules assume the other checks it | The check refuses an app that more than one SAP access claim names | `L/sap_check/decide.py` | Unit | Low |
| SAP-08 | D | The check fails to load or cannot read its inputs, and SAP is lost to every use case | It fails closed for the SAP targets only; the layer's verify runs before any use case's | `tools/egress_interceptor/handler.py`, `L/sap_check/__init__.py` | Unit | Low: an outage, not an exposure |
| SAP-09 | D | A use case's own check refuses another use case's SAP calls | Its contract: return None for a caller that its SAP access claim does not name. A check that forgets refuses, it does not admit | Each use case's own check | Unit, in the tests of each use case that has a check | Low |
| SAP-10 | T, E | A use case's own code calls SAP directly, outside the Gateway, Cedar and the SAP access claim check | Allowed only for the systems `access.<use case>.bypass_gateway` names, signed in as that system's service account (Basic, or OAuth client credentials when the system or its `unattended` block sets `m2m-sap` or `m2m-oidc`; never `obo`); `grant_direct` refuses any other | `L/lib/sap_layer/access.py` | Unit | Medium, by decision: those calls are governed only by the use case (business-operations: its poller reads, its admin-only test-data seeder writes, and is granted only the systems whose `system_role` is `sandbox`, BO-59) |
| SAP-14 | T | Something meant for a test system (a seed, an eval) is pointed at a production one, because nothing says which is which | Every system declares `system_role` (`sandbox`, `development`, `quality` or `production`), required with no default, so config load names a system that does not; a use case that writes test data reads it and refuses anything but `sandbox` (business-operations: BO-59) | `L/lib/sap_layer/config.py` | Unit | Medium: the value is the operator's word, and the layer never asks SAP (`T000-CCCATEGORY` would) |
| SAP-15 | S, T | Someone other than an SAP system puts an event as that system on the `sap-events` bus, or a use case reads another's events | The bus policy contains only deny statements: `PutEvents` to every principal but the sender roles (`aws:PrincipalArn`), and each sender role any source but its own or any topic outside its use cases' (no allow: in the bus's account one would bypass the role's conditions, measured); each sender role trusts only its system's `senders` and may put only `source` `sap.<system>` with the `detail-type`s of the use cases whose `ingest` names it (`ForAllValues` plus `Null: false`). `subscribe` fixes a rule's `detail-type` to the subscribing use case's name, refuses a system outside its `ingest`, and admits only that rule to its queues (`aws:SourceArn`), and publishes the rule, queue and DLQ it made (`layers/sap/subscriptions/<use case>`) | `L/stack.py` (`bus_policy`, `sender_statement`), `L/lib/sap_layer/access.py` (`subscribe`), `L/verify.py` (`check_ingest`) | Unit; `L/verify.py` checks that the policy contains no allow and restricts each role, sends a test event that must be refused, and compares the bus's rules with each use case's `ingest` and with its subscription, trusted only from a stack deploy (its ARN, one target, that queue and DLQ, no input transform or role); Live, 2026-11-12, from the SAP host as the sender role: a mixed-source batch and a foreign topic were refused whole, after the fix that left the bus policy with deny statements only | Low. A use case writing its own rule on the bus, or a target on another's, bypasses `subscribe`; verify fails it, after the fact. What an event says is only as true as SAP's sender |
| SAP-11 | I | An SAP credential leaks | Operator-created and named in config; each runtime's role reads its own secret and provider only; a direct caller's grant is scoped to the one secret | `L/stack.py`, `L/lib/sap_layer/access.py` | Reasoned | Medium: one service account per system (a Basic password or an OAuth client), shared by its server and its direct callers |
| SAP-12 | T, I | SAP's TLS certificate goes unverified | `runtime_env` sets `MCP_SERVER_SESSION_VERIFY=true` | `L/stack.py` (`runtime_env`), `L/verify.py` (the banner) | Live, 2026-11-09, image `1.1.0-1042`: the server prints `Session Verify: True` at start and reads SAP by hostname; with `base_url` set to the SAP host's IP address, which its certificate does not name, it refuses with `CERTIFICATE_VERIFY_FAILED` (IP address mismatch) | Low. `verify.py` reads the banner only after a session has started since the deploy, so run it after each image change. SAP's certificate must chain to a publicly trusted CA |
| SAP-13 | E | SAP authorizes more than any use case intends | SAP's own roles on the service user; the server's write flags and service-prefix allowlist | `L/stack.py` (`runtime_env`) | Reasoned | Adopter-owned: a sandbox `SAP_ALL` user makes every other control advisory |
