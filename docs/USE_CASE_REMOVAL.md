# Use case removal

This page turns one use case off in a deployment. The platform and the other use cases keep
running. It is a different task from these two:

- To delete the use case's folder from your checkout, see
  [Removing a use case from the repository](../CONTRIBUTING_USE_CASES.md#removing-a-use-case-from-the-repository).
  Do that after this page, never before.
- To remove the whole deployment, see [Clean up](PLATFORM_GUIDE.md#clean-up).

Each use case has its own removal page. It adds what is specific to the use case: the order
of its stacks, the data it keeps, and the clean-up outside AWS:

<!-- use-cases:begin remove -->
- [SAP exception handling](../use-cases/business-operations/docs/operating/removal.md#use-case-removal) (`business-operations`): Business process agents for SAP exception handling, on the shared platform's Gateway, Cedar engine and interceptor.
- [Claude Code and Cowork on Amazon Bedrock](../use-cases/claude-3p-on-bedrock/README.md#remove) (`claude-3p-on-bedrock`): Claude Code and Claude Desktop/Cowork on Amazon Bedrock through Anthropic's Claude apps gateway, with Microsoft Entra ID sign-in and per-team model access, on the platform's private network.
- [hello-sap](../use-cases/hello-sap/walkthrough.md#remove) (`hello-sap`): Reference SAP use case — reads SAP through the SAP layer as its own Gateway client, and writes nothing.
<!-- use-cases:end -->

## Order of the steps

`deploy.sh` sees only the stacks that the current `platform.yaml` produces. So destroy the use
case's stacks **while the use case is still on**, and take it out of `platform.yaml` after that.

If you take it out of `platform.yaml` first, nothing deletes its stacks:

- `./scripts/deploy.sh build` deploys the new footprint and leaves the use case's stacks
  running. They still bill, and their Gateway targets and Cedar policies stay on the shared
  Gateway.
- `./scripts/deploy.sh destroy --stack <stack>` stops before any change, with
  `<stack> is not a stack of the current platform.yaml, so destroy cannot see it.`

[Stacks left running](#stacks-left-running) says how to recover.

Delete the use case's folder last, for the same reason. If the folder is gone while
`platform.yaml` still names the use case, every synth stops with `use case(s) not installed:
<name>` and lists each key that names it. While its stacks are still deployed, do not only
delete those keys: put the folder back (`git checkout -- use-cases/<name>`) and start at step 1.

## Procedure

Run every command from the root of the checkout that deployed the stacks, with that
deployment's `platform.yaml` and AWS credentials for its account.

1. **Check the target.** Confirm the account (`aws sts get-caller-identity --query Account`),
   and that `project` and `environment` in `platform.yaml` name the deployment you mean. Another
   deployment in the same account has the same stack names with another prefix. Read
   `deployment.mode`: no key means `workshop`, which keeps no data.
2. **List the use case's stacks.** They are the stacks under `stacks:` in
   `use-cases/<name>/manifest.yaml`, each named `<project>-<env>-<suffix>`, such as
   `<project>-<env>-uc-<name>`. `./scripts/deploy.sh ls` shows them.
3. **Save what the destroy keeps.** Under `deployment.mode: production`, export the data you
   must keep, such as records for an audit. Then save each stack's resource list. After the
   destroy, it is the only list of the names of what stays:

   ```bash
   aws cloudformation list-stack-resources --stack-name <stack> > <stack>-resources.json
   ```

   To see which resources a stack keeps, synthesize it and list its `Retain` and `Snapshot`
   resources:

   ```bash
   ./scripts/deploy.sh synth --stack <stack> > /dev/null
   jq -r '.Resources | to_entries[]
     | select(.value.DeletionPolicy == "Retain" or .value.DeletionPolicy == "Snapshot")
     | "\(.value.Type)\t\(.key)"' cdk.out/<stack>.template.json
   ```

   A guardrail version in that list is deleted with its Amazon Bedrock guardrail. Also run
   `./scripts/deploy.sh diff` now, while the use case is still on. A stack that it already
   changes has drifted since the last build, so you can tell that drift apart from the removal
   in step 6.

4. **Destroy the stacks, one at a time,** in the reverse of the order under `stacks:`, unless the
   use case's removal page gives another order. Do anything that page says to do first, such
   as turning off deletion protection on a database.

   ```bash
   ./scripts/deploy.sh destroy --stack <project>-<env>-uc-<name>
   ```

   A targeted destroy removes that stack only. If another stack still uses one of its exports,
   CloudFormation refuses, and the error names that stack. Destroy it first.
5. **Take the use case out of `platform.yaml`.** Delete every entry that names it:

   | Entry | Who owns it | What stays if you leave it |
   |---|---|---|
   | `use_cases.<name>` | The operator | The use case's stacks, built again by the next build |
   | `layers.<layer>.access.<name>` | The operator, who granted the use case access through a layer | The layer still grants the access, to clients that no longer exist |
   | Its entry under `gateway.interceptor_extensions` (a `path` under `use-cases/<name>/`) | The operator | The interceptor still runs the use case's check on every call to its targets |
   | Its tools under `security.egress_unscanned_responses` (`<name>-...___<tool>`) | The operator | The guardrail still skips those tool names |
   | `identity.group_map.<name>` | The identity administrator | Nothing: the platform ignores a use case that is off. Delete it so the file stays true |
   | `identity.use_case_clients.<name>`, under `identity.mode: direct` | The identity administrator | The synth fails with `identity.mode direct: identity.use_case_clients must name an app registration ...` |

   Keep the platform settings the use case asked for, such as `security.cedar`, unless you
   know that no other use case needs them. A platform setting applies to every use case. While
   more than one use case is on, keep Cedar in `ENFORCE`: otherwise `deploy.sh design` and the
   synth warn that one use case's clients can call another's tools
   ([Gateway configuration](modules/gateway.md#configuration)).
6. **Check the new footprint.** `./scripts/deploy.sh ls` lists none of the use case's stacks,
   and `./scripts/deploy.sh diff` shows the changes in
   [Platform stack changes](#platform-stack-changes).
7. **Deploy the platform again**, so that it drops the use case's clients and access:

   ```bash
   ./scripts/deploy.sh build
   ```

   It asks for the Region first. With no terminal to answer, as in CI or a coding agent, run
   `NON_INTERACTIVE=1 ./scripts/deploy.sh build`.
8. **Verify.** `./scripts/deploy.sh verify` ends with
   `OK: all <n> checks passed for this footprint`, and no line names the use case.
9. **Delete what the destroy left:** the log groups in
   [Retained data](#retained-data), and the retained data when your retention plan allows it.
10. **Clean up outside AWS:** app registrations and groups in your identity provider (IdP), and
    anything the use case's removal page lists, such as users on a connected system.

## Platform stack changes

A synthesis with the use case on and one with it off, after step 5, differ in these ways:

| Stack | Change |
|---|---|
| The use case's own stacks | Gone from the footprint. Its Gateway targets, its Cedar policies and the user pool groups it created are resources of these stacks, so step 4 deletes them |
| `gateway` | Deletes one user pool app client for each entry under `gateway_clients` in its manifest, the client secrets in AWS Secrets Manager, the resource server `uc-<name>`, and the SSM parameters `gateway/use-case-clients/<name>/...`. The Gateway stops accepting tokens from those clients. The interceptor function's configuration drops the use case's check, and the tools it had under `egress_unscanned_responses` |
| A layer's stack, if you deleted the use case's access row | The layer's check parameter no longer grants the use case anything. Parameters that only this use case needed are deleted |
| Every other platform stack and every other use case's stack | No change |

Users stay in the user pool, without the use case's groups. Delete a user who needed only
this use case yourself. If you turn the use case on again, its groups come back empty, so add
its users to them again.

## Retained data

Under `deployment.mode: workshop`, the stacks delete their data with them. In either mode,
some log groups come back after the destroy: a function writes its last log lines after
CloudFormation deleted its group, so Lambda creates the group again, outside any stack and with
no retention. List them:

```bash
aws logs describe-log-groups --log-group-name-pattern <project>-<env>-uc- \
  --query 'logGroups[].logGroupName' --output text | tr '\t' '\n'
```

A group's name starts with its stack's name, after `/aws/lambda/` for a group that Lambda named
after its function, where the name is cut short to about 25 characters. Pick the groups of the
stacks you destroyed: other use cases' groups are in this list too. Delete each one with
`aws logs delete-log-group --log-group-name <name>`. None of them blocks turning the use case on
again, because their names are generated and a new deployment creates groups with new names. The
CodeBuild log group `/aws/codebuild/<project>-<env>-build-<name>` has the platform's retention. It
is deleted with its stack, except under `deployment.mode: production`, and does not block a new
deployment either.

Under `deployment.mode: production`, a stack keeps what holds data when it is deleted, so a
destroy by mistake loses nothing. The use case's removal page lists what it keeps. It is often:

| Resource | How to delete it |
|---|---|
| An Amazon S3 bucket | A versioned bucket keeps every earlier version. The **Empty** action in the Amazon S3 console deletes them too, and `aws s3 rm --recursive` does not. Then run `aws s3 rb s3://<bucket>` |
| An Amazon DynamoDB table with deletion protection | `aws dynamodb update-table --table-name <table> --no-deletion-protection-enabled`, then `aws dynamodb delete-table --table-name <table>` |
| An Amazon CloudWatch Logs log group | `aws logs delete-log-group --log-group-name <name>` |
| An Amazon ECR repository | `aws ecr delete-repository --repository-name <name> --force` |
| An Amazon Aurora final snapshot | `aws rds delete-db-cluster-snapshot --db-cluster-snapshot-identifier <snapshot>` |

Find the names in the resource list you saved in step 3. A retained resource with a fixed name,
such as an ECR repository, makes a new deployment of the same use case fail until you delete it.

The platform's AWS Key Management Service (KMS) key encrypts much of this data. It belongs to
the platform and stays while the platform runs.

## Stacks left running

If the use case left `platform.yaml` before its stacks were destroyed, choose one:

- Put its entries back in `platform.yaml`, then follow the procedure from step 4.
- Or delete each stack with CloudFormation, in the order of step 4. A stack's delete removes
  its Gateway targets and Cedar policies too:

  ```bash
  aws cloudformation delete-stack --stack-name <stack>
  ```

A full `./scripts/deploy.sh destroy` also finds such stacks and asks before it deletes them,
but it removes the whole deployment.
