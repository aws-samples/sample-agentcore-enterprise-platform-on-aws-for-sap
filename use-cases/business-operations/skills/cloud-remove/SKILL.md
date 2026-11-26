---
name: cloud-remove
description: Use when asked to "tear it down", "destroy the stacks", "remove the deployment", "clean up my account", "stop the billing" or "remove only business-operations". Shows exactly what will be removed and from which account, runs `deploy.sh destroy` only after the human confirms, deals with the buckets and log groups a destroy leaves, lists what a production destroy keeps on purpose, and hands the SAP-side clean-up to the SAP team. Never deletes retained production data without an explicit instruction.
---

# Remove a deployment

Deleting is the one thing here that cannot be undone. The procedures are
`use-cases/business-operations/docs/operating/removal.md` and, to remove only this use case,
`docs/USE_CASE_REMOVAL.md`. Follow them, and keep no copy of them here. Paths are relative to
the repository root. "You" in this skill is the coding agent that runs it.

**Never** run a destroy, delete a bucket, table, secret, user pool or key, or turn off deletion
protection, unless the human says yes to that exact action. Before you ask, show them the
account, the Region and what the resource contains. Assume a stack is production unless
`./scripts/deploy.sh design` prints `Mode: workshop` for it. A `platform.yaml` with no
`deployment.mode` key, such as the quickstart's file, means `workshop`.

## 1. Decide what to remove

1. Ask whether to remove the whole deployment, or only the business-operations use case. In
   the second case, the platform stays for other use cases.
2. Show the account (`aws sts get-caller-identity --query Account`), the Region, the `Mode:`
   line of `./scripts/deploy.sh design`, and the stacks (`./scripts/deploy.sh ls`). Check that
   `project` and `environment` are the deployment they mean. Another deployment in the same
   account has the same stack names with a different prefix.
3. Ask whether anything must be kept first, such as case records for an audit. Exporting
   them is their decision, and it comes before any destroy.

## 2. Remove

- **Whole deployment:** `operating/removal.md`, "Deployment removal", steps 1 to 5.
  - Under `deployment.mode: production`, note the AWS Key Management Service (KMS) key's ARN
    before the destroy (step 2).
  - `./scripts/deploy.sh destroy` lists what it will remove and asks again. Read that list to
    the human. A coding agent with no terminal cannot answer the prompt, so the command stops
    before any change (`Aborted before any change`). That is a safe way to get the list.
  - After the human's yes to that list, run `./scripts/deploy.sh destroy --yes`. Never use
    `NON_INTERACTIVE=1`. Both skip the prompt, and `--yes` also deletes, without asking, the
    remaining `<project>-<env>` log groups and the secrets that `deploy.sh` created itself. So
    name those in the same question.
  - A stack that stops on a bucket that is not empty is expected. The destroy offers to empty
    the bucket, except in production or for a versioned bucket.
  - A networking stack can take hours to delete while AWS removes its network interfaces. Wait
    and run it again; never force it.
- **Only the use case:** `docs/USE_CASE_REMOVAL.md`, "Procedure", with the details in
  `operating/removal.md`, "Use case removal":
  1. Save each stack's resource list (step 3), so the names of what a production destroy keeps
     are known afterwards.
  2. Destroy its five stacks in the order that section gives, while `business-operations` is
     still in `platform.yaml`. Ask before each one.
  3. Then delete its entries from `platform.yaml` (the table in that section), and show the human
     the diff of the file.
  4. Then run `NON_INTERACTIVE=1 ./scripts/deploy.sh build > build.log 2>&1`, after the same
     yes as for a deploy. Without `NON_INTERACTIVE=1`, it waits for a Region answer that you
     cannot type. Then run `./scripts/deploy.sh verify`.
  5. Optional, if the human also wants it out of the checkout:
     `./scripts/deploy.sh usecase remove business-operations` (`operating/removal.md`,
     "Repository removal"). It deletes the folder and its skill links, changes no
     deployment, and refuses while `platform.yaml` still names the use case.

  If `business-operations` already left `platform.yaml`, `destroy --stack` stops with
  `is not a stack of the current platform.yaml`. Follow "Stacks left running" in
  `docs/USE_CASE_REMOVAL.md`: put the entries back, and never delete the stacks yourself
  without a yes for each one.

## 3. Remaining resources

1. What the destroy does not own: the SAP secret, and log groups named with
   `<project>-<env>`. List them, and delete each one only after a yes. Some log groups
   come back after any destroy, in workshop mode too ("Retained data" in
   `docs/USE_CASE_REMOVAL.md`). They do not block turning the use case on again.
2. Under production: the data kept on purpose (`operating/removal.md`, "Resources a production
   destroy retains"). Report it as a list, with what each resource contains. Delete it only when
   the human says their retention plan allows it. Use the order that section gives, with the key
   last. When only the use case went, delete only the rows marked "Use case", and keep the key
   and the SAP secret: the platform and the other use cases still use them.
3. Until the CloudTrail bucket, the Amazon Elastic Container Registry (ECR) repositories and the
   AgentCore log groups are gone, a new deployment with the same `project` and `environment`
   fails. Say so if they plan to deploy again.
4. The SAP side (`operating/removal.md`, step 5): give the human a message for the SAP team. It
   names the service user, any service activated only for this, and the event sender if SAP
   sent events.

## Self-check

Whole deployment: `./scripts/deploy.sh ls` and `aws cloudformation list-stacks
--stack-status-filter DELETE_FAILED` show none of the deployment's stacks. Report
`OK: <project>-<env> removed from <account>/<Region>; kept: <list or none>; SAP clean-up sent`,
or `FAIL: <stack>: <reason>`.

Only the use case: `./scripts/deploy.sh ls` lists no `-uc-business-operations` stack,
`aws cloudformation describe-stacks --stack-name <project>-<env>-uc-business-operations` says
the stack does not exist, and `./scripts/deploy.sh verify` prints `OK` with no
`use case business-operations` line. Report
`OK: business-operations removed from <project>-<env>; kept: <list or none>; SAP clean-up sent`,
or `FAIL: <stack>: <reason>`.
