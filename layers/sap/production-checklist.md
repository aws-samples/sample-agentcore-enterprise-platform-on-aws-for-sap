<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: Apache-2.0
-->

# SAP layer production checklist

Terms are defined in [Concepts](../../docs/start/concepts.md).

The Agentic Business Platform Accelerator for SAP on AWS is sample code. It is a starting point
for your own deployment, not a finished product, and passing this checklist does not change that.
**The list is not exhaustive.** It names the checks this repository knows about. Your workload,
your data and your regulators will add others.

Under the [AWS shared responsibility model](https://aws.amazon.com/compliance/shared-responsibility-model/),
you own your security, compliance and data-protection decisions:

- what data the business process agents may read
- who may approve what
- how long records are kept
- which controls your organization requires

> Before you connect to an SAP system, review the
> [SAP API Policy](https://help.sap.com/doc/sap-api-policy/latest/en-US/API_Policy_latest.pdf) and
> your agreements with SAP. Programmatic or agent access to SAP can have licensing implications
> (for example, SAP Digital Access); review them with your SAP account team.

This page covers the platform and the SAP layer. Each use case has its own items, such as trigger
mode, approvals, evaluations, and PII in what is stored and sent. They are in that use case's own
checklist ([Use cases](../../use-cases/README.md)). The
platform's own checklist, with the evidence a launch review asks for, is
[docs/PRODUCTION_READINESS_PLAN.md](../../docs/PRODUCTION_READINESS_PLAN.md).

**Owners.**

- *You*: the team that deploys and runs the Accelerator.
- *SAP Basis*: your SAP administrators.
- *IdP admin*: whoever administers your identity provider (IdP): Microsoft Entra ID, Okta or Ping.
- *Security team*: whoever owns your AWS account baseline.

## Automated checks

Commands run from the repository root.

- [ ] **`deployment.mode: production` is set in `platform.yaml`.** Why: it retains stateful
      resources and refuses the config until every platform control below is set. Check:
      `.venv/bin/python -m infra_utils.platform_config platform.yaml` lists each missing
      control; start from [presets/production.yaml](../../presets/production.yaml). Owner: you.
- [ ] **Each use case's `production-check` reports no FAIL.** This applies to each use case that
      has this command. Why: one read-only row per control, each naming its fix. Check:
      `./scripts/deploy.sh usecase run <name> production-check`. Owner: you.
- [ ] **`deploy.sh verify` passes after every deploy.** Why: it checks on the deployed stacks
      what the config promises (the SAP layer's part is in the [reference](reference/verify.md)). Check:
      `./scripts/deploy.sh verify`. Owner: you.

What `deployment.mode: production` requires (`infra_utils/platform_config.py`):

- an enterprise IdP (`identity.idp`)
- `deployment.platform_account`
- `security.networking`, `security.cloudtrail_alerting` and `security.resource_policies`
- `security.egress_filter`, with SSNs and card numbers at `BLOCK`
- `security.require_guardrails` (AWS keys blocked in model output)
- `security.cedar` enabled in `ENFORCE`
- `security.traceability` and `security.org_id`
- `agents.model_id` and a non-empty `agents.allowed_models`
- an explicit `gateway.web_search`
- `observability.transaction_search`, `observability.alarms` and `observability.alarm_email`

The two guardrail settings are off by default, because each one adds Amazon Bedrock Guardrails
charges to every model or tool call. Production turns them on. A setting that the mode does not
require is still your decision: read each one in [docs/PLATFORM_YAML.md](../../docs/PLATFORM_YAML.md).

## Identity

- [ ] **Sign-in goes through your enterprise IdP.** Why: with a brokered IdP the platform's
      sign-in clients offer only that IdP, so its policies apply to every sign-in. Check: `identity.idp` is
      `entra_id`, `okta` or `ping` ([docs/ENTERPRISE_IDP.md](../../docs/ENTERPRISE_IDP.md)).
      Owner: you and the IdP admin.
- [ ] **MFA (multi-factor authentication) and conditional access are enforced in the IdP.** Why:
      the platform leaves them to the IdP and checks neither. Check: your IdP's policy for the app registration. Owner:
      IdP admin.
- [ ] **Self sign-up is off.** Why: when it is on, anyone can register in the shared user pool. Check:
      `identity.self_sign_up: false`, `production-check` row `identity.self_sign_up`. Owner: you.
- [ ] **Group membership is reviewed on a schedule.** Why: groups decide who may change the trigger mode,
      author packs and approve SAP writes, and anyone with `cognito-idp:Admin*` can change them.
      Check: the groups mapped in `identity.group_map`, and who has `cognito-idp:Admin*` in
      IAM. Owner: IdP admin and security team.

## Platform settings

- [ ] **The model and its inference profile match your data-residency requirements.** Why: a
      geographic cross-Region inference profile (`us.`, `eu.`, `apac.` model ids) can serve a
      request from any Region in that geography. A `global.` profile can serve it from any
      supported Region.
      Check: `agents.model_id` and `agents.allowed_models`, and the Region you deploy to.
      Owner: you and the security team.
- [ ] **Memory is encrypted with your customer managed key (CMK), and its retention is chosen.**
      Why: Memory stores conversation events with SAP data in them. Check: under production, the
      platform's CMK is set. `aws bedrock-agentcore-control get-memory --memory-id <id>` shows an
      `encryptionKeyArn` (the id is in SSM, AWS Systems Manager, at
      `/<project>/<env>/memory/memory-id`). `agents.memory.event_expiry_days` is what your policy
      names. Owner: you.
- [ ] **Log and span retention match your retention policy.** Why: logs and spans are the audit
      record of what the agents read and did. Check: `observability.log_retention_days`
      (`production-check` fails it under 90 days and warns under 365) and
      `observability.span_retention_days` for the account-wide span groups. Owner: you and the
      security team.
- [ ] **Alarms reach an on-call person.** Why: the operations topic has no subscriber until you add one.
      Check: `observability.alarm_email` is a monitored inbox or an on-call integration, its
      Amazon SNS subscription is confirmed, and `deploy.sh verify` passes its alarm check. Owner: you.

## SAP system and users

- [ ] **Every SAP system's `system_role` is right.** Why: only a `sandbox` is seeded with test data
      or has evaluations run against it, and only you set the value. Nothing checks it with SAP.
      Check: `layers.sap.systems.<system>.system_role`. Never mark a system whose documents
      matter as `sandbox`. Your SAP Basis team can confirm the SAP client's role in transaction
      `SCC4`. Owner: you and SAP
      Basis.
- [ ] **Test data is off on any system whose documents matter.** Why: the seeder posts real SAP
      documents outside the Gateway, Cedar and the run checks. Check: `production-check` row
      `test_data.enabled`. A production synth refuses it. Owner: you.
- [ ] **Each SAP user has least privilege, in the right SAP client only.** Why: SAP's own roles
      are the last boundary. A user with `SAP_ALL` makes every other control advisory
      ([threat model](threat-model.md), SAP-13). Check
      ([sap-setup.md](sap-setup.md#2-grant-the-authorizations-pfcg)):
      - one System-type user per system and SAP client (`client` in the system's config)
      - roles for only the OData (Open Data Protocol) services and business objects that the
        packs use
      - read and write in separate roles
      - a write shown to be refused before the write role is granted

      Owner: SAP Basis.
- [ ] **Every OData service is activated in this system, not only in development.**
      Why: a RAP (ABAP RESTful Application Programming Model) service published from its service
      binding in development cannot be transported. So a service that works in development
      returns 404 in quality or production until it is activated there. Check: each service the
      packs name is active in `/IWFND/MAINT_SERVICE` (V2) or in the SAP Gateway V4
      administration. Its ICF (Internet Communication Framework) node is active, and `$metadata`
      returns EDMX
      ([sap-setup.md](sap-setup.md#moving-services-between-systems)). Owner: SAP Basis.
- [ ] **The role matches the OData version, and the SAP Gateway hub has one too.** Why: V2
      checks `S_SERVICE` and V4 checks `S_START`, so a V2 role grants nothing on a V4 service.
      On a hub deployment, the user needs a role in the hub and in the backend. Check: run
      `STAUTHTRACE` for the user while one call runs against each service, in each system
      ([sap-setup.md](sap-setup.md#sap-gateway-deployment-and-odata-version)). Owner: SAP Basis.
- [ ] **The sign-in methods you meant to disable are disabled on the service's own node.**
      Why: an ICF node inherits its parent's logon procedure, and SAP does not restrict HTTP
      sign-in by user type. So a System-type service account does not stop other users from
      using the node. Check: SICF **Display Inheritance** on each service node, and that a call
      with a method you turned off is refused ([sap-setup.md](sap-setup.md#sign-in-methods-on-the-odata-service)).
      Owner: SAP Basis.
- [ ] **You chose the flow knowing whose authority SAP sees.** Why: on `basic`, `m2m-sap` and
      `m2m-oidc`, every caller acts as one service account. So anyone the use case lets in reads
      what that account reads, and SAP's logs name the account, not the person. On `obo`
      (principal propagation), SAP runs as the signed-in person, with that person's own roles
      ([whose roles SAP checks](sap-setup.md#authorization-checks-in-sap)). Check:
      `layers.sap.systems.<system>.flow`. Treat `obo` systems as read-only, because no write has
      been run that way. Owner: you, the security team and SAP Basis.
- [ ] **A token flow's trust is set up and tested on its own.** Why: on `m2m-oidc` and `obo`, a
      token that SAP accepts is a full logon as the user it maps to. Check: the steps and the
      SAP-only test in [sap-trust.md](sap-trust.md). Owner: SAP Basis and IdP admin.
- [ ] **ABAP objects you add go through your SAP change control.** Why: a BAdI (Business Add-In)
      implementation or an outbox runs inside SAP, outside every control in this repository.
      Examples are this layer's `abap/` sample and a use case's BAdI implementation. Check: the
      objects are transported through DEV and QAS with your code review. The developer's ABAP
      write authorization is restricted by package (`DEVCLASS`), and no runtime user has it. Owner: SAP Basis.

## SAP agent permissions

- [ ] **Writes are on only where your organization decided they should be.** Why: on the
      service-account flows, writes are on by default. Check: `layers.sap.systems.<system>.writes`
      (`[]` keeps a system read-only) ([writes, by flow](walkthrough.md#writes-by-flow)). Owner:
      you.
- [ ] **Each use case's access row and SAP access claim name only what it needs.** Why: they are
      what the layer's check lets that use case do. `write: "*"` means every write that each
      system has turned on. Check: `layers.sap.access.<use case>` names the read and write
      services. Its `bypass_gateway` names only the systems that the use case's own functions
      must call outside the Gateway. Owner: you.
- [ ] **An AWS CloudTrail trail logs management events in this Region.** Why: an SAP access claim
      written by hand is refused either way. But only a trail lets the layer's alert tell anyone
      ([when an SAP access claim is written by hand](reference/use-case-access.md#hand-written-claims-and-settings)).
      Check: `security.cloudtrail_alerting`, or an organization trail. Owner: security team.

## Network and secrets

- [ ] **SAP is reached over a private route.** Why: a private route keeps SAP off the internet,
      in addition to the certificate check the SAP layer turns on
      ([threat model](threat-model.md), SAP-12). Check: `security.networking: true`, with peering, a Transit Gateway, VPN or
      AWS Direct Connect to SAP. On RISE with SAP, also the SAP Enterprise Cloud Services (ECS)
      requests in
      [RISE with SAP](reference/network.md#rise-with-sap)
      ([private SAP](walkthrough.md#network-paths-to-sap)). Owner: you and
      your network team.
- [ ] **SAP serves HTTPS with a certificate from a trusted certificate authority (CA).** Why:
      `base_url` must be `https://`, and a client that verifies certificates needs a trusted
      chain. Check: transactions `SMICM` and `STRUST` ([sap-setup.md](sap-setup.md#4-serve-https-smicm-strust)). Owner: SAP Basis.
- [ ] **The AWS for SAP MCP (Model Context Protocol) Server image is set to a fixed version.**
      Why: `layers.sap.image_tag` defaults to `latest`. So a runtime gets a newly published image
      the next time something updates it, and two stacks can run different servers. The runtime
      has the SAP credentials, and its certificate check depends on the server reading
      `MCP_SERVER_SESSION_VERIFY` ([threat model](threat-model.md), SAP-12).
      `deployment.mode: production` refuses `latest` at synth. In production, `deploy.sh verify`
      fails until it has read each runtime's certificate banner. Check: `layers.sap.image_tag` is
      a version tag (read it from a runtime's startup banner) or a `sha256:` digest. Verify passes
      after a session has started since the deploy. Owner: you.
- [ ] **Each SAP secret rotates on a schedule.** Why: one credential per system is shared by its
      server and its direct callers. Check: rotate with
      `./scripts/deploy.sh layer run sap secrets <system>`. A changed `clientId` or client secret
      on a token flow has not been tested after the first deploy. Plan a template change that
      makes CloudFormation update the resource ([walkthrough](walkthrough.md#sign-in-flows)). Owner: you and SAP Basis.
- [ ] **Each SAP secret uses the key your policy allows.** Why: when a secret does not exist,
      `./scripts/deploy.sh layer run sap secrets <system>` creates it on the AWS managed key
      `aws/secretsmanager`. It does so in production mode too, not on the platform CMK. The
      layer grants the readers of a secret no `kms:Decrypt` on the CMK. So a secret on the CMK
      also needs that grant for each SAP runtime and each direct caller. Check: `aws
      secretsmanager describe-secret --secret-id <name>`. No `KmsKeyId` in the output means the
      AWS managed key. Accept that key, or move the secret to your key, add the grants, and test
      one call. Owner: you.

## Account-level controls

The Accelerator does not turn these on, because they belong to the account or the organization and other
workloads share them.

- [ ] **Amazon GuardDuty is on, Lambda Protection included.** Why: the Accelerator has no runtime
      threat detection of its own. Owner: security team.
- [ ] **AWS Security Hub CSPM is on with the AWS Foundational Security Best Practices standard.**
      Why: the Accelerator's CloudTrail alerts feed an account baseline; they do not replace one. Owner:
      security team.
- [ ] **CloudTrail data events are a decision you made.** Why: without them, DynamoDB reads of
      case records, and invocations of functions that write to SAP, are not recorded. Check: your
      trail's data event selectors. Owner: security team.
- [ ] **AWS Config records, with CloudFormation drift detection.** Why: nothing in the Accelerator
      detects a resource changed outside CloudFormation. Check: the managed rule
      `cloudformation-stack-drift-detection-check`, or
      `aws cloudformation detect-stack-drift --stack-name <stack>` on a schedule. Owner: security
      team.
- [ ] **An AWS Budget and AWS Cost Anomaly Detection watch this deployment.** Why: the Accelerator's cost
      alarm is per run, and nothing limits a month's total spend. Check: activate the `Project`,
      `Environment` and `Component` cost-allocation tags, then a budget with a budget action and
      an anomaly monitor on the `Project` tag. Owner: you and your finance team.

## Operations

- [ ] **Break glass is rehearsed.** Why: the steps that stop SAP writes, from a use case's own
      stop to locking the SAP user, help only if someone has rehearsed them. Check: each use
      case's own break-glass page ([Use cases](../../use-cases/README.md)) and the layer's
      [emergency stop](reference/use-case-access.md#hand-written-claims-and-settings), with a copy
      kept outside this deployment. Owner: you and SAP Basis.
- [ ] **Backups and removal behave as you expect.** Why: under production, stateful resources are
      kept when a stack is deleted, and tables keep point-in-time recovery. Outside production,
      removing the stacks deletes them. Check: a restore and a removal rehearsed on a non-production stack. Owner:
      you.
