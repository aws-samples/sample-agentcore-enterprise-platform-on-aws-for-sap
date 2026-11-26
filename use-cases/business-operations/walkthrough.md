# SAP exception handling walkthrough

Terms are defined in the [glossary](docs/glossary.md) and in [Concepts](../../docs/start/concepts.md).

In this use case, a business process agent works on SAP exceptions, such as an invoice blocked
for payment, a price variance or a missing goods receipt:

1. The poller finds each exception in SAP and makes it a case.
2. The agent follows the standard operating procedure (SOP) that its pack maps to the case. This
   page calls an SOP a "procedure" from here on. Following a written procedure is the Agentic
   SOPs pattern.
3. The agent reads what it needs from SAP through the platform's Gateway (the AgentCore Gateway,
   not the SAP Gateway), and proposes the SAP write.
4. A person approves any write above the pack's limit.

The use case runs in your AWS account on the shared platform. It is sample code: adapt it, and
work with your security and legal teams before you use it with real data.

Each SAP process is one **pack**. A pack has five parts, and each part has its owners:

- the intake from SAP: the SAP functional consultant and the AI engineer;
- the procedures the agent follows: the process owner and process analysts;
- the controls on its SAP writes: the control owner;
- the agent's configuration: the AI engineer;
- the evaluations that test it.

The operator sets the contact addresses, any lowered limits and the trigger mode for each
deployment. [Pack parts](docs/packs.md#pack-parts) and [Roles](docs/roles.md) have
the detail.

> **SAP API Policy.** Use of SAP APIs is subject to the
> [SAP API Policy](https://help.sap.com/doc/sap-api-policy/latest/en-US/API_Policy_latest.pdf)
> and your agreements with SAP. Review both before connecting this sample to an SAP system.

This page explains each setting, from an empty config to a first case. The
[quickstart](docs/quickstart.md) is the same path as numbered steps, with one complete
`platform.yaml`. Start there on a first deploy. What the use case does, its architecture and the
rest of its documentation are in its [README](README.md).

## Prerequisites

- The platform deploys (`./scripts/deploy.sh doctor` passes), and you have access to an SAP system
  over HTTPS with a service user. On S/4HANA the shipped `finance_ops` pack reads services that SAP
  already publishes. If the system lacks a service, build it with the ABAP RESTful Application
  Programming Model (RAP) on S/4HANA, or with the classic approach on SAP ERP Central Component
  (ECC). The SAP side is covered by the [SAP layer](../../layers/sap/walkthrough.md).
- Amazon Bedrock model access in the Region for the Claude models the platform allows, and for **Titan
  Text Embeddings V2** (procedure search).
- Amazon API Gateway has a CloudWatch role in the account and Region:
  `aws apigateway get-account --query cloudwatchRoleArn`. If that prints `null`, set
  `api.set_account_cloudwatch_role: true` in exactly one stack
  ([Configuration](docs/configuration.md#api-contacts-test-data-alarms)).

## Enable

Merge these blocks into `platform.yaml`. `platform.example.yaml` has them with comments, and
adds the egress settings this use case asks for:

```yaml
security:
  cedar:
    enabled: true
    mode: ENFORCE        # LOG_ONLY refuses nothing: read docs/security.md first

gateway:
  interceptor_extensions:
    - path: layers/sap/sap_check
      target_prefixes: [sap-]
      request_headers: true
    - path: use-cases/business-operations/business_operations_check
      target_prefixes: [business-operations-, sap-]
      request_headers: true

layers:
  sap:                   # its systems and this use case's access row: SAP, below

use_cases:
  business-operations: {}
```

The use case requires the SAP layer. Without `layers.sap`, the config does not load. Without the
use case's `access` row, the synth (when the AWS Cloud Development Kit, CDK, builds the
templates) fails and prints the line to add. The second interceptor entry is the **run checks**:
this use case's check on every case tool and SAP write. The synth refuses the use case without
it ([Security](docs/security.md#run-checks)). This page uses `identity.mode: brokered`, the
default. Entra direct mode is supported too, and its steps are in
[Direct mode (Entra)](#direct-mode-entra).

Also set `agents.memory.strategies: []`. The runtime keeps short-term history in the platform's
Memory, and that history includes SAP document contents and vendor data. A memory strategy would
extract that data from every stored event. The synth only warns when a strategy is set:

```yaml
agents:
  memory:
    strategies: []
```

## SAP

Each entry under `layers.sap.systems` is one AWS for SAP MCP Server and its Gateway target,
`sap-<system>`, in the stack `<project>-<env>-layer-sap`. This use case's row under
`layers.sap.access`, its **access row**, sets the most it may use:

```yaml
layers:
  sap:
    systems:
      s4:                                   # at most 16 characters
        system_role: sandbox                  # required: sandbox, development, quality, production
        base_url: https://<sap-host>/sap/opu/odata/sap/
        client: "100"
        secret_name: <project>-<env>/business-operations/sap/s4
    access:
      business-operations: {read: "*", write: {s4: [function_import]}, bypass_gateway: [s4]}
```

- **The system key `s4`** is the one the shipped packs use. They name their SAP tools on that
  system (`sap-s4___odata_read`). With another key, the synth only warns
  (`business-operations:pack-sap-system`), and the pack's SAP calls then fail. Keep the key
  `s4`, or edit the pack's `gateway_tools` to name your key.
- **`read` and `write`** are what the agent and the reader may use through the Gateway. `"*"`
  means every system, and every write that the system has turned on. The shipped packs write only
  through function imports, so `write` names `function_import` on `s4`. A pack that creates or
  updates needs that operation added.
- **`bypass_gateway`** names the systems that this use case's own functions call outside the
  Gateway, as the system's service account (Basic, or a client-credentials token on `m2m-sap` and
  `m2m-oidc`). These functions are the SAP poller, the ingest consumer (which reads a pack's query
  again when SAP sends a notice that something changed) and the test-data seeder. A scheduled or
  queued function has no person and no Gateway session behind it, so it calls SAP directly.
  Listing each system keeps that direct path visible. A system in no `bypass_gateway` list is
  not polled.
- **`system_role`** says what the system is for. The seeder has IAM access only to a `sandbox`
  system's secret, and refuses any other system at run time. `evals-run` stops before its first
  case unless every pack seeds a sandbox.
- **Writes** (`create`, `update`, `function_import`) are on by default on every system, except a
  system whose `flow` is `obo` (principal propagation: SAP sees the signed-in person). Set
  `writes: []` on a system to start it read-only.

The use case also declares an **SAP access claim** in its own stack: the SAP systems, operations
and writes it will use, like the authorizations an SAP role requests. The claim must fit inside
the access row, so the use case's code cannot give itself more SAP access than the operator
allowed. What the SAP access claim and the Cedar permits add: [Architecture](docs/architecture.md#sap-access).

Create each system's secret before the first deploy (`doctor` checks that it exists), and run
the same verb to rotate one:

```bash
./scripts/deploy.sh layer run sap secrets s4
```

The poller reads a rotated secret again when SAP refuses the old one, or within 15 minutes. The
AWS for SAP MCP Server reads a Basic secret on every request
([AWS for SAP MCP Server: Security](https://docs.aws.amazon.com/mcp-sap/latest/awsforsapmcp/security.html)).

Then set the contacts that the shipped procedures cite (`{{CONTACT_AP_TEAM}}` is `ap_team`):

```yaml
use_cases:
  business-operations:
    contacts:
      ap_team: ap-team@example.com
      finance_approvals: finance-approvals@example.com
      procurement: procurement@example.com
      warehouse: warehouse@example.com
    test_data:
      enabled: true      # demo SAP systems only: the seeder posts real SAP documents
```

Leave `test_data` out on a system whose documents matter. If you turn it on now, you do not need
to redeploy before your first case.

Every other setting has a default: [Configuration](docs/configuration.md).

## Deploy and verify

```bash
./scripts/deploy.sh deploy      # the platform, then every use-case stack, in dependency order
./scripts/deploy.sh verify      # the platform's checks, then this use case's verify.py
# OK: reader lists N tool(s); the run checks refused ...get_case_state with no run and
# with a run id but not its secret, and passed it with both; ...; Cedar ENFORCE refused the reader's write; ...; the runtime
# is READY and refused the reader's token
./scripts/deploy.sh usecase run business-operations status
```

The run checks line in that output shows why a run needs a **run grant** and its secret. The
check refuses a case tool with no run, and with a run id but not its secret. Before each run, the
invoker writes a run grant, and only the invoker and that run's agent have its secret. So no other
client can change a case or SAP, even a client that knows the run id.

The deploy summary prints `ConsoleUrl`. To redeploy one stack after a change to it alone, name it:
`./scripts/deploy.sh deploy --stack <project>-<env>-uc-business-operations-runtime`. `--stack`
deploys only that stack, not the stacks it depends on. So a change that also touches the gateway
stack (a Cedar statement, the run checks, a client) needs `--stack <project>-<env>-gateway`
first, or the plain `deploy`.

`verify.py` also runs on its own. It then reads the deployment from the environment, not from
`platform.yaml`: `PROJECT_NAME=<project> ENVIRONMENT=<env> python use-cases/business-operations/verify.py`.
Without those variables it looks for `agentcore-workshop-dev` and fails with `ParameterNotFound`.

## User access

A user needs `business-operations-users` to reach anything in the console. A federated user is
in the user pool after their first sign-in. With `identity.idp: cognito` the pool starts empty,
so `--create` creates the user (Amazon Cognito mails a temporary password):

```bash
./scripts/deploy.sh usecase run business-operations add-user <email> [--create] [--admin] [--author] [--limit-owner] [--case-worker] [--remote-author] [--pack-admin] [--groups business-operations-<g>,...]
# an approver for finance_ops, in one command:
./scripts/deploy.sh usecase run business-operations add-user approver@example.com --create --groups business-operations-ap-manager
```

What each option lets the user do:

- `--admin`: change the trigger mode. It also adds the user to every group that a pack's
  `entitlement` names, because the admin group alone gives access to no pack, and the cases list
  would answer 403.
- `--author`: author packs and procedures.
- `--limit-owner`: lower a pack's write limits in this stack.
- `--case-worker`: start the agent on a case. A run that a case worker starts may post SAP writes
  under the pack's auto-write limit with no approval.
- `--remote-author`: use this use case's tools on the platform's people Gateway, from a remote
  Model Context Protocol (MCP) client such as AWS Quick. Each tool also needs what its console
  route needs, so authoring there also needs `--author`.
- `--pack-admin`: change a pack's structure in development.
- `--groups`: add a pack's own groups, such as its `entitlement.approve_groups`. Only groups that
  start with `business-operations-` are allowed, because the user pool is shared.

With a brokered identity provider (IdP), map IdP groups instead, so that nobody adds users by
hand:

```yaml
identity:
  group_claim: groups                  # the token claim that has the IdP group ids
  group_map:
    business-operations:
      "<IdP group id>": [business-operations-users]
      "<IdP admin group id>": [business-operations-users, business-operations-admins]
```

A token gets its groups at sign-in and at refresh, so a user must sign in again after a change.
Who may do what: [Security](docs/security.md#console-permissions).

## First case

Open the console URL and sign in. On **Cases**, press **Briefing**: if the agent's summary
streams into the assistant panel, the agent is reachable.

No run starts on a new exception until a person starts it. The trigger mode starts as `manual`:
the poller creates cases in `detected` and queues nothing, and you start each case from the
console. On a demo system with `test_data.enabled: true` (set above), make an exception and a case
now. The seeder posts **real SAP documents**
([Configuration](docs/configuration.md#api-contacts-test-data-alarms)).

If the seed fails and names `posting_date`, there are two possible causes. The posting period is
closed in SAP, or the system's direct callers sign in with OAuth, which cannot read the open
period. Ask your SAP team which period is open, and add `posting_date=<YYYY-MM-DD>`.

```bash
./scripts/deploy.sh usecase run business-operations seed finance_ops --preset 2 --yes
./scripts/deploy.sh usecase run business-operations poll finance_ops
```

The poll makes a case for every open exception the pack finds in SAP, not only the seeded one.
In a sandbox that other people have used, that can be thousands of cases, and none of them runs
until someone starts it. The seed prints the new `case_id`: open `<ConsoleUrl>/cases/<case_id>`.

Open the case and press **Process**. The case page shows the agent's steps as it works. A write
above `AUTO_WRITE_LIMIT_USD` stops the run and puts the case in `awaiting_human_input`. An
approver sees the proposed write as a diff.

When you trust the agent, run `trigger-mode set auto` and `trigger-mode set auto --pack finance_ops`.
The pack's cases are then queued as they are found. A case is queued only when both settings say
`auto`. With two settings, an operator stops every pack with one setting, and turns each pack to
`auto` on its own. On a fresh deploy, `auto` queues every open exception that SAP shows, and
each one is a paid run ([Lifecycle](docs/lifecycle.md#3-queueing)).

To change what the agent does, open the pack in the console's **Packs**. Edit a procedure or a
rule on its **Procedures** tab, and rehearse it there ([Packs](docs/packs.md)). When something
fails, start at [Operations: troubleshooting](docs/operating.md#troubleshooting). To remove the
use case, follow [Operations: remove](docs/operating.md#remove).

## Direct mode (Entra)

Under the platform's `identity.mode: direct`, Microsoft Entra ID issues every token. None of the
things this use case creates in a Cognito user pool exist. The app registrations to create, and
the six steps, are in
[docs/direct-mode.md](docs/direct-mode.md).
