---
name: cloud-deploy-and-wire
description: Use on a first clone, or when asked to "deploy this", "spin up the platform", "set up business operations in my account" or "wire it to SAP". Checks the account, writes platform.yaml from the quickstart's complete file, drafts the request for the SAP team, stores the SAP secret, deploys the platform and the use case, adds the first user, sets contacts, makes sure the shipped packs are live, and verifies the deployment end to end. Hands off to dev-new-sap-process while the deploy runs.
---

# Deploy and connect business-operations

This skill takes a clone to a verified stack that reads SAP. Most of the time is the deploy,
which runs in the background. Paths are relative to the repository root. "You" in this skill is
the coding agent that runs it.

```bash
ops() { ./scripts/deploy.sh usecase run business-operations "$@"; }
```

**Never** deploy before the human has confirmed the account and Region you show them. Never put
a password in a command line, a file or your own arguments: the human types it.

**Many stacks with no person at the keyboard** (a lab, a pipeline) use
`use-cases/business-operations/docs/quickstart.md`, "Scripted run". It has the same steps, a
shared SAP secret that is named instead of typed, and `NON_INTERACTIVE=1`. This skill is for a
person who deploys one stack.

**Who you are working with.** Often someone who has never used the AWS Cloud Development Kit
(CDK) or automated SAP. Before each step, say in one sentence what it does and why. After it,
say what it proved. Explain a term the first time it appears. For example, a *stack* is a set of
AWS resources deployed together, and `platform.yaml` is the one file that says what to deploy.
If they are new to all of it, `start-here` explains the parts first.

## 1. Ask

Ask for:

- the AWS Region;
- a short `project` and `environment` (`<project>_<env>` at most 28 characters);
- the admin's email;
- the SAP system: its OData (Open Data Protocol) base URL
  (`https://<host>/sap/opu/odata/sap/`), its SAP client (such as `100`), and a Basic service
  user.

The quickest path is S/4HANA with the service already published, `basic` auth, Amazon Cognito
sign-in, and a **sandbox** SAP client. Use a sandbox because seeding posts real documents.

Also ask whether that SAP client has SAP's S/4HANA sample data (company code `1710`). The
`finance_ops` seed posts on it: supplier `USSU-VSF04`, material `MZ-RM-R300-01`, plant,
purchasing organization and company code `1710`, purchasing group `002`. Without the sample data,
the seed fails at the purchase order, so step 7.3 takes the hand-posted path instead. Ask now,
because the answer decides how the first case is made.

**Send any SAP question they cannot answer to their SAP team now.** It is usually the slowest
part. Draft one message for the SAP team from `layers/sap/sap-setup.md`, "Prerequisite decisions", and show
it to the human to send. It asks for:

- the sandbox SAP client, its OData base URL, and whether it has the S/4HANA sample data;
- a System (no dialog logon) service user with the roles in `sap-setup.md` step 2. The deployer
  types its password at a prompt; it is never sent by mail;
- the shipped pack's services active in `/IWFND/MAINT_SERVICE`: `API_SUPPLIERINVOICE_PROCESS_SRV`,
  `API_PURCHASEORDER_PROCESS_SRV`, `API_MATERIAL_DOCUMENT_SRV`, and `FAP_VENDOR_LINE_ITEMS_SRV`
  for the test data;
- HTTPS with a certificate from a publicly trusted certificate authority (CA), reachable from
  AWS;
- which Materials Management (MM, `MMPV`) and Financial Accounting (FI, `OB52`) posting periods
  are open for the company code;
- optionally, ABAP Development Tools (ADT) data preview for the same user
  (`/sap/bc/adt/datapreview/` active in `SICF`, display access to tables `MARV`, `T001` and
  `T001B`). With it, the seeder finds the open period itself (step 7.3). Without it, every seed
  needs the `posting_date` that the SAP team gives you.

Link `sap-setup.md` in the message, because it is written for the SAP team.

## 2. Check the machine and the account

1. bash 4 or newer (macOS: `brew install bash`, then run `bash scripts/deploy.sh …`), Python
   3.13, Node.js with `npm`, the AWS CLI, credentials for the target account.
2. In the Amazon Bedrock console for that Region, enable model access for the Claude Sonnet
   and Haiku models the platform uses. Also enable **Titan Text Embeddings V2**, for the search
   over standard operating procedures (SOPs, or procedures). `doctor` checks one model only.
   Without Titan, the deploy succeeds and procedure indexing fails.
3. `aws apigateway get-account --query cloudwatchRoleArn`. If it prints `null` or `None`,
   this stack must set `api.set_account_cloudwatch_role: true` (step 3). Otherwise the API stage
   is not created. Set it in exactly one stack per account and Region.

## 3. Write `platform.yaml`

If `platform.yaml` already exists, show it to the human and ask before you replace it. Then
copy the complete file that the quickstart uses (the quickstart is this path, written for a
person):

```bash
cp use-cases/business-operations/quickstart.platform.yaml platform.yaml
```

Replace every value marked `CHANGE` with the answers from step 1:

- `project`, `environment` and `region`;
- `deployment.platform_account` (`aws sts get-caller-identity --query Account`);
- the SAP `base_url` and `client`;
- the contacts. Contact keys are lower snake case: `{{CONTACT_AP_TEAM}}` is `ap_team`.

Keep the system key `s4`, because the shipped pack's tools are `sap-s4___…`. Uncomment the `api:`
line at the end only if step 2.3 printed `null`. On this path, never merge `presets/` and
`platform.example.yaml` by hand: the file above is that merge, already checked.

Show the human the finished file before step 4.

Every key and its default are in `use-cases/business-operations/walkthrough.md` ("Enable",
"SAP"), `use-cases/business-operations/docs/configuration.md` and `docs/PLATFORM_YAML.md`. An
unknown key stops the synth (the step where CDK turns the code into templates), by design.

You may add `layers.sap.systems.<system>.senders` and `layers.sap.access.business-operations.ingest`
so that SAP sends events (`dev-new-sap-process` step 6). If you do, never write the sender role's
name into SAP or AWS Identity and Access Management (IAM) by hand, because CloudFormation shortens
generated role names. After the deploy, read the ARN from the parameter
`/<project>/<env>/layers/sap/systems/<system>/sender-role-arn`.

## 4. Store the SAP secret

`./scripts/deploy.sh layer run sap secrets s4`. **The human types** the user and password at the
prompt. `doctor` (step 5.1) then confirms the secret exists and is readable.

## 5. Deploy

1. `./scripts/deploy.sh design`, then `./scripts/deploy.sh doctor`. Both only read. `design`
   validates `platform.yaml` and prints the plan. `doctor` checks the tools, account, Region and
   model. Fix every `[FAIL]` before you continue. `doctor` needs `platform.yaml`, so never run it
   before step 3. Without the file, it advises `design --profile greenfield`, which writes a file
   without the use case.
2. Show the human the account (`aws sts get-caller-identity --query Account`), the Region,
   and the stacks (`./scripts/deploy.sh ls`). Deploy only after they say yes.
3. Start it in the background and log it:
   `NON_INTERACTIVE=1 ./scripts/deploy.sh deploy > deploy.log 2>&1`.
   While it runs (the first deploy takes the longest), start `dev-new-sap-process`.
4. When it ends, check the log, not only the exit code. Every stack ends `✅  <stack>`, and
   `grep -n "failed to deploy" deploy.log` finds nothing.

**A stack deployed before this version of the repository:** a change set can fail with
`'/aws/lambda/<function>' already exists`. The stack's Lambda log groups are then older than the
template that now declares them. Adopt them once per stack with `npx cdk import`, then deploy
again. The exact procedure is `docs/TROUBLESHOOTING.md`, "A Lambda log group already exists".

## 6. Connect it

1. **First user:** `ops add-user <email> --create --admin --author --case-worker --pack-admin
   --groups business-operations-ap-manager`. Cognito sends a temporary password by email.
   - Keep the `--groups`. The console shows a pack, and loads its configuration, only to someone
     in one of the pack's `entitlement.groups`. An admin in none of them sees "Failed to load
     configuration: 403".
   - `business-operations-ap-manager` is also the group that approves the shipped pack's writes.
     `--case-worker` lets the user start runs on its cases.
   - `--pack-admin` asks `Let <email> change every pack's structure and SAP values? [y/N]`. A
     coding agent cannot answer it (`needs a terminal, or --yes`). Ask the human that question,
     and if they say yes, add `--yes`.
2. **Contacts:** step 3 set them. The synth warns about any contact that a shipped procedure
   cites and that is unset. An author can change a value at once on the console's configuration
   screen (`PUT /deployment/settings`), but only for a key this block declares. A new role is a
   configuration change and a deploy.
3. **The shipped pack:** `ops bundle import packs/finance_ops` (a dry run).
   - On a new stack it answers `already active`, because the deploy imported it.
   - On an older stack it shows a diff, because a stack keeps the packs it runs across deploys.
     Show the diff, get a yes, and run the command again with `--apply`. Until then, that stack
     may run a pack without `case_document_check`, and the run checks refuse every SAP write.
4. **The trigger mode** stays `manual` (`ops trigger-mode`), for the deployment and every pack, so
   new exceptions wait for an operator. A pack's cases are queued with no person only when the
   deployment and that pack are both `auto` (`ops trigger-mode set auto`, then
   `ops trigger-mode set auto --pack <id>`). That queues every open exception SAP shows for the
   pack, and each one is a paid run. The deployment's setting stops every pack at once. The
   pack's setting lets a new pack start on `manual` while another pack runs on `auto`.

## 7. Verify

1. `./scripts/deploy.sh verify`. Each check prints `PASS:` or `OK:` lines, and the last line is
   `OK: all <n> checks passed for this footprint`. The footprint is the set of stacks this
   deployment has in the account. The use case's line starts `OK: reader lists`. It means:
   - the reader app client lists its tools;
   - the run checks refuse a call with no run;
   - Cedar refuses the reader's write;
   - the runtime is READY.

   The command exits non-zero on any failure.
2. `ops status`: every stack `*_COMPLETE`, and `console-url` among the parameters. Sign in there
   with the admin's email and temporary password.
3. **One real exception** (sandbox SAP client only): `ops seed finance_ops --list` shows the
   inputs and presets.
   - Leave `posting_date` out. The seeder reads the latest date that the pack's company code has
     open in SAP (MM's MARV and FI's T001B). It reads them over its own SAP connection, which
     needs ADT data preview.
   - If it cannot read them, the seed fails and names `posting_date`. Ask the human which period
     is open (OB52 for FI, MMPV for MM) and pass `posting_date=<YYYY-MM-DD>`. Opening a period is
     their change, never yours.
   - Then run `ops seed finance_ops --preset 1 --yes` and `ops poll finance_ops`.
   - **No sample data** (step 1): skip the seed. Ask the SAP team to post one supplier invoice
     with a payment block in the sandbox (MIRO), then run only the poll.

   The case appears in the console as `detected`. `ops-run-case` continues from there.
4. **The evaluation cases** (optional; Amazon Bedrock bills them, and each case posts SAP
   documents): run `ops evals-setup` once. It installs the judges' SDK
   (`evals/requirements-evals.txt`) into the repository's `.venv` and creates the custom judge.
   Then run `ops evals-run --pack finance_ops --yes`. A run without the SDK prints
   `JUDGES WILL BE SKIPPED` first and `JUDGES: SKIPPED` in its summary.

## Self-check

Report `OK: <project>-<env> deployed in <Region>, verify passed, console <url>, finance_ops
active` with the minutes each step took, or `FAIL: <step>: <message>`. If `verify` fails, report
FAIL, even if everything else passed.
