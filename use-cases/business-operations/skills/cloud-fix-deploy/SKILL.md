---
name: cloud-fix-deploy
description: Use when `./scripts/deploy.sh deploy` (or `build`, `design`, `doctor`, `verify`) fails or hangs, or when asked "the deploy failed", "why did this stack fail", "CREATE_FAILED", "ROLLBACK", "the script froze" or "fix my deployment". Reads the log and CloudFormation's own reason, matches it to docs/TROUBLESHOOTING.md, explains the cause in plain words, proposes one fix, and deploys again only after the human agrees. Never destroys or deletes to make a deploy pass.
---

# Fix a failed deploy

The person may never have used AWS CloudFormation or the AWS Cloud Development Kit (CDK). Say
what failed in their terms (which part of the system, and why) before you name any resource.
Paths are relative to the repository root. "You" in this skill is the coding agent that runs it.

**Never** delete a stack, a bucket, a table, a secret or a log group to get past an error. The
only exception: you have said what it contains, and the human then asks for that exact deletion.
Never retry the same deploy unchanged more than once. A second identical failure is information.

## 1. Find the failure

1. The log: `deploy.log` if `cloud-deploy-and-wire` wrote one. Otherwise ask them to paste
   the end of the output. `grep -n -E "failed to deploy|❌|Error|FAIL" deploy.log | head`.
2. Which step failed:
   - `doctor` and `design` fail before anything is created (a tool, a credential or
     `platform.yaml`);
   - `deploy` fails inside a stack;
   - `verify` fails after a deploy succeeded: the stack works but does not yet do its job.
3. For a stack failure, ask CloudFormation for the reason at resource level, because the CDK
   output often hides it:

   ```bash
   aws cloudformation describe-stack-events --stack-name <stack> \
     --query "StackEvents[?contains(ResourceStatus,'FAILED')].{r:LogicalResourceId,reason:ResourceStatusReason}" \
     --output json | head -30
   ```

   The first failure in time is the cause. The later ones are usually "Resource creation
   cancelled", and they are results of the first.
4. Confirm the account and Region are the intended ones (`aws sts get-caller-identity`, and
   `region` in `platform.yaml`). A wrong account explains most surprises.

## 2. Match it

Look the message up in `docs/TROUBLESHOOTING.md`. Its "Quick index" maps each symptom to a
section. For a failure on the SAP side in `verify`, `layers/sap/walkthrough.md` has its own
troubleshooting. Read the matching section in full before you propose anything, because it
often says what not to do.

If nothing matches, say so. Show the human the exact reason CloudFormation gave, and search
the docs for it before you guess.

## 3. Propose one fix

Tell the human what failed, why, the one change you propose, and what it changes (a line of
`platform.yaml`, a console setting such as Amazon Bedrock model access, a one-off command). Wait
for a yes. Then make that change only.

Usual causes, in plain words:

- **The machine:** bash older than 4 on macOS, the wrong Python, or no Node. `doctor` names it.
- **Credentials expired** during the deploy: refresh them and run the same command again. CDK
  continues from where each stack is.
- **The network connection was lost** during the deploy (`read EADDRNOTAVAIL`, `ECONNRESET`,
  `Could not connect to the endpoint URL`). The stack it names may have finished. Before you
  call it failed, check each stack's status with
  `aws cloudformation describe-stacks --stack-name <stack> --query "Stacks[0].StackStatus"`.
  - The log may also say `Could not release the deployment lock` (the deploy tries again for
    about 30 seconds first). Then the next deploy refuses with
    `Another deployment holds the <project>/<env> lock`.
  - Confirm that no `deploy.sh` or `cdk` process is running on any machine for this deployment.
  - Show the human the AWS Systems Manager (SSM) parameter
    `/<project>/<env>/auth/m2m-deployment-lock`. Its value is `<account>:<pid>:...` of the
    deploy that set it.
  - Delete it only if the human says yes:
    `aws ssm delete-parameter --name /<project>/<env>/auth/m2m-deployment-lock`.
- **Amazon Bedrock model access** not enabled for the Region, including Titan Text Embeddings V2.
- **`platform.yaml` invalid:** the message names the key. An unknown key is refused by design.
- **Something with the same name already exists**, such as a log group or a bucket left by an
  earlier deployment with the same `project` and `environment`. The troubleshooting section says
  whether to import it or choose another `environment`. Never delete it to make room unless the
  human says so.

## 4. Deploy again

Show the account, Region and stacks again (`./scripts/deploy.sh ls`) and get a yes. Run the
same command that failed, and log it (`NON_INTERACTIVE=1 ./scripts/deploy.sh deploy > deploy.log 2>&1`).
Check the log, not only the exit code. Then run `./scripts/deploy.sh verify`.

## Self-check

Report `OK: <stack> failed on <cause>; fixed by <change>; deploy and verify pass`, or
`FAIL: <stack>: <CloudFormation's reason, verbatim>; tried <change>; next: <what a person must decide>`.
