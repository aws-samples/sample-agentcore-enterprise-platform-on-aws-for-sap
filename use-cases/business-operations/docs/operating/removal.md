<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Removal

This page removes the whole deployment, or only this use case while the platform keeps running,
or the use case's folder from the repository.

## Deployment removal

1. **Close every console tab.** A tab that still polls the API during the destroy can create the
   API's access-log group again after its stack deleted it.
2. **Under `deployment.mode: production`, note the platform key's ARN first.** The destroy
   deletes the key's alias but keeps the key. You need the ARN to remove the key last:

   ```bash
   aws ssm get-parameter --name /<project>/<env>/security/kms-key-arn \
     --query Parameter.Value --output text
   ```

3. **Destroy every stack:**

   ```bash
   ./scripts/deploy.sh destroy
   ```

   It shows what it will remove, and asks first. A stack can stop because a bucket it owns is not
   empty, since access logs can arrive after the bucket was emptied. Then the destroy offers to
   empty that bucket and run again, and `--yes` agrees for you. It never does this under
   `deployment.mode: production`, or for a versioned bucket. Empty those buckets yourself
   (`aws s3 rm s3://<bucket> --recursive`), and run the destroy again.

   With runtimes in a VPC, the networking stack can fail to delete for up to about 8 hours, until
   AgentCore's network interfaces are removed. Run the destroy again after that
   ([Runtimes in a VPC](../../../../docs/PLATFORM_GUIDE.md#runtimes-in-a-vpc-enable_networking)).
4. **Delete what the destroy does not own:**
   - each SAP system's secret (`secret_name` under `layers.sap.systems`), which you created with
     `layer run sap secrets`;
   - under `deployment.mode: production`, the data kept on purpose, once your retention plan
     allows: see [Resources a production destroy retains](#resources-a-production-destroy-retains);
   - any log group whose name contains `<project>-<env>` and that still exists after its stack, such as
     `/aws/lambda/<project>-<env>-...` and `/aws/codebuild/<project>-<env>-...`. Lambda creates
     its log groups itself, so no stack owns them.
5. **On the SAP side**, ask your SAP team to remove what they set up for it: the service user and
   its roles, any service activated only for this, and, if SAP was sending events, the sender
   ([SAP setup](../../../../layers/sap/sap-setup.md)).

### Resources a production destroy retains

Under `deployment.mode: production`, the stacks keep their data when they are deleted, so a
destroy by mistake loses nothing. Every name below, except the key's, is built from your
`project` and `environment`:

| What stays | Part | Why it stays |
|---|---|---|
| The cases, config, run grants and tickets tables, with deletion protection on | Use case | Case records and decisions |
| The buckets for standard operating procedures (SOPs), the registry and exemplars (the procedure and registry buckets are versioned), and the console's access-log bucket | Use case | Procedures, packs and their history; who fetched the console |
| With `knowledge.api_docs_search` on: the API docs bucket, versioned | Use case | The example API specifications |
| With `knowledge.agent_knowledge.enabled`: the Aurora final snapshot of the agent knowledge cluster | Use case | Precedent and vendor risk |
| The ECR repository `<project>-<env>-business-operations` | Use case | The agent image |
| The log groups of its Lambda functions, its API's access log and its case management log | Use case | What each function and the API did |
| The CloudTrail bucket `<project>-<env>-cloudtrail-<account>` and its access-log bucket, both versioned | Platform | The audit trail |
| The Amazon Cognito user pool, with deletion protection on, and the Gateway M2M client secret | Platform | Users, groups and sign-in |
| AgentCore Memory `<project>_<env>_memory-...` | Platform | Conversation memory |
| The ECR repositories `<project>-<env>-<agent>` | Platform | Agent images |
| The AgentCore log groups `/aws/bedrock-agentcore/<project>-<env>/...` | Platform | Agent, Gateway and memory logs |
| The platform's AWS Key Management Service (KMS) key, enabled | Platform | The retained data and the snapshot are encrypted with it |

A new deployment with the same `project` and `environment` fails until the CloudTrail bucket, the
ECR repositories and the AgentCore log groups are deleted, because their names are fixed.

To remove them, once your retention plan allows
([Controls and evidence](../controls-and-evidence.md#record-locations-and-retention)), work in
this order and keep the key for last:

1. **Export what you must keep**, such as case records for an audit, before you delete anything.
2. **Empty and delete the buckets.** A versioned bucket also keeps every earlier version. The
   **Empty** action in the Amazon S3 console deletes those too, and `aws s3 rm --recursive` does
   not. Then run `aws s3 rb s3://<bucket>`.
3. **Delete the tables**, turning off deletion protection first:

   ```bash
   aws dynamodb update-table --table-name <table> --no-deletion-protection-enabled
   aws dynamodb delete-table --table-name <table>
   ```

4. **Delete the platform's identity and agent state:** the user pool (turn off its deletion
   protection in the Amazon Cognito console, then delete it), the machine-to-machine (M2M) client secret
   (`aws secretsmanager delete-secret`), the memory
   (`aws bedrock-agentcore-control delete-memory --memory-id <id>`), the ECR repositories
   (`aws ecr delete-repository --repository-name <name> --force`) and the log groups.
5. **Delete the Aurora snapshot:**
   `aws rds delete-db-cluster-snapshot --db-cluster-snapshot-identifier <snapshot>`.
6. **Schedule the key's deletion last**, with the ARN you noted before the destroy. AWS KMS waits
   7 to 30 days, and you can cancel the deletion during that time:
   `aws kms schedule-key-deletion --key-id <key-arn> --pending-window-in-days 30`.

## Use case removal

Follow [Use case removal](../../../../docs/USE_CASE_REMOVAL.md). The platform, the SAP layer
and the other use cases keep running. This section adds what is specific to this use case.

**Before the destroy.** Close every console tab, as for the whole deployment. Under production,
note what the stacks keep (the rows marked "Use case" in
[Resources a production destroy retains](#resources-a-production-destroy-retains)) and export
the case records you must keep. With `knowledge.agent_knowledge.enabled`, the Aurora cluster
has deletion protection under production. Turn it off first
(`aws rds modify-db-cluster --db-cluster-identifier <cluster-id> --no-deletion-protection`).
The destroy then leaves a final snapshot. Under `deployment.mode: workshop` the stacks keep no
data.

**Destroy its stacks** while `business-operations` is still in `platform.yaml`, in this order:

```bash
for s in runtime sap knowledge; do
  ./scripts/deploy.sh destroy --stack <project>-<env>-uc-business-operations-$s
done
./scripts/deploy.sh destroy --stack <project>-<env>-uc-business-operations
./scripts/deploy.sh destroy --stack <project>-<env>-uc-business-operations-console
```

They hold its Gateway targets, its Cedar policies, its user pool groups, its API and its
console, so all of these go with them.

**Delete its entries from `platform.yaml`:**

| Entry | Note |
|---|---|
| `use_cases.business-operations` | Its settings: contacts, test data, promotion, people tools |
| `layers.sap.access.business-operations` | The operator's grant. Keep `layers.sap.systems`: the systems belong to the SAP layer, and another use case may read them |
| `use-cases/business-operations/business_operations_check` under `gateway.interceptor_extensions` | Keep `layers/sap/sap_check` while any SAP use case is on |
| `business-operations-sops___load_sop` and `business-operations-knowledge___search_sap_sops` under `security.egress_unscanned_responses` | Delete the key when nothing is left under it |
| `identity.group_map.business-operations` and, under `identity.mode: direct`, `identity.use_case_clients.business-operations` | The identity administrator's |

`security.egress_pii_entities` and `security.egress_request_action: block` are platform
settings that this use case asked for. They apply to every use case. With this use case gone,
consider the defaults again: without the `NONE` entries, the guardrail masks names, addresses,
email addresses and phone numbers in every tool call again.

**Deploy again** with `./scripts/deploy.sh build`. Measured on a synthesis before and after,
and on a live removal, the build changes two platform stacks and nothing else:

- The gateway stack deletes the `reader` and `writer` clients, their secrets, the resource
  server `uc-business-operations` and the four parameters under
  `gateway/use-case-clients/business-operations/`. The Gateway stops accepting their tokens, and
  the interceptor stops running this use case's check.
- The SAP layer's stack removes this use case from its check parameter
  (`/<project>/<env>/layers/sap/check`). It deletes the parameters for the calls that went around
  the Gateway, `layers/sap/systems/<system>/connection` and, with networking,
  `layers/sap/egress-security-group-id`, unless another use case has `bypass_gateway` on that
  system.

The SAP layer's runtime, role and security group keep `business-operations` in their
descriptions and in their `Component` cost tag (`uc-business-operations-sap`), and the names it
derived from it. This use case built them first and the layer took them over unchanged, so they
stay. Cost reports show the SAP layer's spend under that component.

**After the build.** `./scripts/deploy.sh verify` has no `use case business-operations` line.
Then:

- Under production, delete the retained data once your retention plan allows, in the order of
  [Resources a production destroy retains](#resources-a-production-destroy-retains), steps 1 to 3
  and 5, and the ECR repository and log groups. Keep the platform's key: the platform still
  uses it. A new deployment of this use case with the same `project` and `environment` fails
  until the ECR repository is gone.
- In either mode, delete the log groups that came back after the destroy
  ([Retained data](../../../../docs/USE_CASE_REMOVAL.md#retained-data)). They do not block
  turning this use case on again.
- Packs and SOPs live in the retained buckets and tables, so retiring them first is not needed.
  The SAP documents that `ops seed` posted stay in SAP.
- Users stay in the user pool, without this use case's groups. If you turn the use case on
  again, its groups come back empty: run `add-user` for each user again
  ([User access](../../walkthrough.md#user-access)). Under workshop, it also starts with no
  cases or tickets, and with the packs of the checkout.
- Under `identity.mode: direct`, ask the identity administrator to delete the use case's Entra
  app registrations (`reader`, `writer`, and those under `use_cases.business-operations.entra`)
  and their secrets.
- Ask the SAP team to remove what only this use case needed: the write authorizations of the
  SAP layer's service user (no other shipped use case writes), and the event sender if SAP sent
  events (`senders`, `ingest`). Keep the service user and the secret while another use case
  reads the system ([SAP setup](../../../../layers/sap/sap-setup.md)).

## Repository removal

After the section above, or before you ever deploy it, you can delete the use case from your
checkout. The platform, the SAP layer and the other use cases keep working, and their
documentation keeps no link to it:

```bash
./scripts/deploy.sh usecase remove business-operations
```

It deletes the folder and the links to its skills, and removes it from the platform's entry
pages. It changes no deployment, and it refuses while `platform.yaml` still names the use case,
such as in its `layers.sap.access` row
([Removing a use case from the repository](../../../../CONTRIBUTING_USE_CASES.md#removing-a-use-case-from-the-repository)).
