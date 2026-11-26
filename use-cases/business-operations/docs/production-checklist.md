<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Production checklist

Terms are defined in the [glossary](glossary.md) and in [Concepts](../../../docs/start/concepts.md).

The business process agents are sample code. Passing this checklist does not make them a
finished product, and **the list is not complete**. It names the checks that this repository
knows about, and your processes, data and auditors will add others. Under the
[AWS shared responsibility model](https://aws.amazon.com/compliance/shared-responsibility-model/),
you own your security, compliance and data-protection decisions. These include which SAP writes
an agent may make, and who may approve them.

> Before you connect to an SAP system, review the
> [SAP API Policy](https://help.sap.com/doc/sap-api-policy/latest/en-US/API_Policy_latest.pdf) and
> your agreements with SAP. Programmatic or agent access to SAP can have licensing implications
> (for example, SAP Digital Access); review them with your SAP account team.

This page covers the business process agents (`use-cases/business-operations/`). The platform,
the SAP layer and the account-level items (identity, SAP users, network, GuardDuty, Security Hub,
budgets) are in the [platform and SAP layer checklist](../../../layers/sap/production-checklist.md).
Work through that one first.

Every command is `./scripts/deploy.sh usecase run business-operations <verb>`, run from the
repository root. This page says "standard operating procedure (SOP)" once, and "procedure" after
that. **Owners:**

- *you*: the team that deploys and runs the agents;
- the *process owner*: answers for the business process that a pack automates;
- the *control owner*: answers for the limits, approvers and checks on the pack's SAP writes;
- *SAP Basis*;
- the *IdP admin*: administers your identity provider (IdP);
- the *security team*.

The other roles are in [Roles](roles.md).

## Automated checks

- [ ] **`production-check` reports no FAIL, and you have read every WARN.** Why: it reads
      `platform.yaml` and EventBridge and names the fix for each control. Check: `production-check`.
      Owner: you.
- [ ] **`deploy.sh verify` passes.** Why: it proves three things on the deployed stack. The reader
      client cannot write. The run checks refuse a call with no run grant. Each claimed SAP system
      answers a read.
      Check: `./scripts/deploy.sh verify`. Owner: you.

## Identity and access

- [ ] **`admin_email` is not set.** Why: the user it creates is a Cognito password user outside
      your IdP and its multi-factor authentication (MFA). The user is in every group the stack
      creates, approver groups included. `deployment.mode: production` refuses it at synth.
      Check: `use_cases.business-operations.admin_email`. Under an enterprise IdP, give access by
      mapping IdP groups (`identity.group_map`) or with `add-user`
      ([configuration](configuration.md#api-contacts-test-data-alarms)). Owner: you.
- [ ] **You know who is in each group.** Why: each group gives these rights
      ([who may do what](security.md#console-permissions)):
      - `business-operations-admins` may change the trigger mode.
      - `-authors` may change packs, procedures, contacts and rule values.
      - `-limit-owners` may lower a pack's write limits.
      - `-case-workers` may start the agent on a case. That run can post SAP writes up to the
        pack's auto-write limit with no approval.
      - `-pack-admins` may change a pack's structure in development.
      - `-remote-authors` may author from a remote MCP (Model Context Protocol) client through
        the people Gateway (`people_tools`).
      - A pack's `approve_groups` approve its SAP writes.

      Check, on a schedule: the members of each group, the IdP groups that `identity.group_map`
      maps to each, and each pack's `entitlement`. In it, `limit_groups` narrows who may lower
      the pack's limits, and `process_groups` narrows who may start runs. Case workers must be
      people you would trust with a write under the limit. Owner: you, the process owner and the
      IdP admin.
- [ ] **Your IdP keeps duties separate.** Why: the kit checks group membership, never who a
      person is. Anyone the IdP admin adds to a group has its rights. One person in two groups has
      the rights of both: an author who is also an approver can edit a rule and approve the write
      it allows ([who may do what](security.md#console-permissions)). Check: makers (authors,
      pack-admins, limit owners, case workers) and approvers are in separate IdP groups, with no
      person in both. Every change to those groups' membership is reviewed in your IdP, and sends
      an alert. Owner: the IdP admin and the security team.
- [ ] **Operators use least-privilege IAM.** Why: the operator commands write the trigger mode,
      users, bundles and test data. Check: `operator-policy bundle|seed|users|trigger-mode` prints
      a policy that names this deployment's resources. `operator-policy evals` prints only for a
      development stack, and may not write a case's approvals. Never attach it in a quality or
      production account. Owner: security team.
- [ ] **CloudTrail data events cover the case records and the functions operators invoke.** Why:
      management events do not say who read or wrote the cases table outside the console. They
      also do not say who invoked `PackComposer`, `TestDataFunction`, `AgentInvoker` or
      `OperatorRerun` with IAM. The composer's own audit records the actor it was told, not the
      IAM principal. Check: a trail with advanced event selectors on the table in
      `/<project>/<environment>/use-cases/business-operations/cases-table` and on those four
      functions (`aws cloudformation list-stack-resources --stack-name
      <project>-<environment>-uc-business-operations`). Weigh it against a cost that grows with
      every case and run. The Sops, Registry and Exemplars buckets have no S3 server access
      logs either (scanner finding `CKV_AWS_18`, accepted until 2027-05-01). To record who
      reads or writes them, add S3 data events for those buckets to the same trail. Owner:
      security team.

## Authorization and autonomy

- [ ] **Cedar is in `ENFORCE`.** Why: in `LOG_ONLY`, Cedar refuses nothing, so the use cases on
      the shared Gateway are not separated ([Cedar](security.md#cedar-on-the-shared-gateway),
      [limits in each Cedar mode](architecture.md#sap-access-limits)). Check: `production-check`
      row `security.cedar`. Owner: you.
- [ ] **Each pack that writes declares a `case_document_check`, an `AUTO_WRITE_LIMIT_USD` the process
      owner chose (or a `write_limit_waiver` saying why not), `write_guards` (or `[]`) and a
      `write_amount_field`.** Why: the run checks refuse a write that does not name the case's own
      document. They send any write above the limit to a person, and any write that includes an
      amount above the limit or above the case's amount
      ([the run checks](security.md#run-checks)). A Release or Cancel is checked against the
      amount field only when it includes one, so its amount is the case row's. `write_guards`
      are checks that the agent runtime makes against SAP itself, just before a write that
      releases a payment, such as "the invoice has a purchase-order item" (`po_reference`).
      Check: each pack's `pack.json`, and the overrides stored by `PUT /packs/{pack_id}/controls`
      and `PUT /deployment/settings`. Owner: the process owner.
- [ ] **You have decided your response to an interceptor outage.** Why: the run checks, the
      guardrail and Cedar's inputs run in the interceptor Lambda. With its reserved concurrency at
      0, the Gateway refused every call. So nothing is bypassed, and every use case stops
      ([threat model](reference/threat-model.md), BO-61). A timeout or a function error was not
      tested. Check: alarms on the interceptor's errors and throttles. Run the same test with an
      interceptor that times out, on a development stack, and then remove the setting. Owner: you
      and the security team.
- [ ] **You accept the audit-header and write-guard limits.** Why: the agent container states
      `x-audit-initiator` and `x-audit-trigger` itself, and only the correlation id is checked
      against a grant (BO-63). Write guards and the verifier exist for the supplier-invoice
      service only (BO-64). The verifier is a second model, asked once before each guarded write
      whether the facts support exactly that write. Anything but a clear "agree" refuses the
      write. Check: your auditors' view of both, and, for a pack on another service, what replaces
      `write_guards`. Owner: the process owner and the security team.
- [ ] **You accept what one approval covers.** Why: a write under the limit needs no person. A
      write above it needs one approval of that exact write. The kit does not check that the
      approver is a different person from the one who started the run. So a case worker who is
      also an approver can start a run and approve its write. On an `obo` system, a run not
      started from the console reaches SAP as the system's service identity, not as a person.
      Check: your process's separation-of-duties rule. Owner: the process owner and the security
      team.
- [ ] **Each pack whose policy needs two approvers declares `DUAL_APPROVAL_LIMIT_USD`.** Why:
      above that limit, a write needs two different approvers
      ([lifecycle](lifecycle.md#5-human-decision)). In a pack that does not declare it, one
      approval allows any write. A pack declares it only beside `AUTO_WRITE_LIMIT_USD`. A case
      that a sender pushed needs two approvers above the limit, whatever amount it includes.
      Check: the constant in each pack's `pack.json` (`finance_ops` ships 250000) and any
      override, against your policy. Owner: the process owner.
- [ ] **The trigger mode starts, and stays, at `manual` until you have evidence.** Why: `auto`
      lets runs make writes up to the limit with no person. On a new deploy, it also queues every
      open exception that SAP shows ([lifecycle](lifecycle.md#3-queueing)). Check: `trigger-mode`
      prints `trigger-mode = manual`. Set it to `auto` only after the evaluations below pass and
      you have watched manual runs. Owner: you and the process owner.
- [ ] **Containment and the audit-change alerts reach a person.** Why: on the run checks, cost,
      dead-letter or guardrail alarm, containment sets the trigger mode to `manual` and tells the
      ops topic. That message is only useful if someone reads it
      ([observability](operating/observability.md)). Check: `observability.alarm_email` is
      subscribed, and `production-check` row `audit-change alarms` passes. That row checks only
      that the rule exists. The trigger-mode rule fires only while a trail logs management
      events in this Region, and the row does not check for a trail. So also check that
      `aws cloudtrail get-trail-status` shows a trail that is logging. Owner: you.
- [ ] **Every published pack version is reviewed before import, and pack imports into this stack come from
      your release pipeline only.** Why: an import records a change reference and the bundle's
      sha256. It checks neither who reviewed the published version nor a signature. So the sha256
      detects damage to the file, not a deliberate edit, and nothing stops the person who edited
      a pack from publishing it. Check: your release pipeline adds what the kit does not:
      - a diff of the published version by [part](packs.md#pack-parts), with a named reviewer
        for each changed part;
      - an approval from someone who did not make the change;
      - signed bundles, verified before import.

      Each changed part is reviewed by its owner
      ([roles](roles.md#pack-parts-and-their-owners)):
      - Controls, a [guarded rule](packs/sop-values.md#guarded-rules) included: the control owner.
      - Procedures: the process owner.
      - Intake: the SAP functional consultant and the AI engineer.
      - Agent configuration: a second AI engineer.

      Only the release pipeline's role has `lambda:InvokeFunction` on the pack composer, and
      every import includes a change reference ([packs](packs.md#promotion-between-stacks)).
      Owner: you, the security team and each part's owner.
- [ ] **Your rules do not vary by company code, or you changed the pack so they can.** Why: a
      rule has one value per pack. So a policy that differs by company code, or by another case
      field, cannot be expressed in the shipped pack. Separate packs for each company code do not
      help either: a process type routes to one pack, and the import check refuses a second.
      Check: your policy's values against each pack's rules. Where they differ by a case field,
      make the change before going live. For example, add a rule table keyed by that one case
      field, which the write checks read and enforce for the case's own value. Owner: the process
      owner and the control owner.
- [ ] **A run that does not end is stopped.** Why: a run stops at its pack's `max_turns` or
      `run_budget` (default 2,000,000 tokens or $0.50). But the defaults are set from the
      sample's own runs, and nothing limits spend across runs. Check: each pack's `max_turns`
      and `run_budget`, set from your own pack's runs, `alarms.cost_p90_usd` below the budget,
      and an AWS Budgets alert for the account ([cost](operating/cost.md)). Owner: you.

## Guardrails and data protection

- [ ] **Both guardrails are on, and the Gateway's guardrail refuses a request it would rewrite.**
      Why: `security.require_guardrails` screens the agent's model calls, and
      `security.egress_filter` screens every Gateway tool call and response. Both are off by
      default because of their cost, and production requires them. With
      `security.egress_request_action` at its default `mask`, a tool call that the guardrail
      rewrites reaches SAP changed. So a write can post a value that the run checks did not see.
      Check: `deployment.mode: production` refuses the config without the guardrails, the agent
      runtime's environment includes a `GUARDRAIL_ID`, and the `production-check` rows
      `security.egress_filter` and `security.egress_request_action` pass (`block`). Owner: you.
- [ ] **Each PII entity's action follows one rule: allow in the model what the work needs, mask
      it in what is stored or sent.** Why: the model needs supplier names and remit-to addresses
      to do the work. Logs, traces, notifications and tickets rarely need them. Check:
      - The agent's guardrail blocks Social Security numbers (SSNs) and card numbers, blocks AWS
        keys in its output, anonymizes US bank and tax ids, and does not change names and
        addresses ([runtime](configuration.md#runtime)).
      - The Gateway's guardrail anonymizes names, addresses, emails and phone numbers in tool
        traffic, unless `security.egress_pii_entities` says otherwise. This can mask a supplier's
        name or a notification's recipient.
      - SSNs and card numbers stay at `BLOCK` under production.
      - `security.egress_unscanned_responses` names only `load_sop` and `search_sap_sops`, whose
        responses are the procedures you wrote. Anything else listed there reaches the model
        unscanned.

      Owner: you and the security team.
- [ ] **The agents read only the SAP fields the process needs.** Why: every field read is sent to
      the model, and may be stored in the case and its traces. Check: each pack's `source.select`,
      and the procedure steps that name the fields to read. Owner: you and the process owner.
- [ ] **Model content logging is off.** Why: when it is on, prompts and responses with SAP data are
      written to the runtime's log group. Anyone with CloudWatch IAM access can read them there,
      whatever the pack entitlement says. Check: `runtime.log_model_content` is unset or `false`
      (off by default under production). The log groups use CloudWatch's default encryption,
      not the platform's customer managed key. Owner: you and the security team.
- [ ] **You chose how long case records are kept.** Why: cases have no expiry, and stay until you
      delete them. They contain SAP data and the approval record. Check: your retention policy
      for the cases table. Point-in-time recovery is on. Under production, the table has deletion
      protection and the platform's customer managed key in AWS Key Management Service (KMS).
      Owner: you and the security team.
- [ ] **Memory keeps short-term history only.** Why: a strategy extracts every stored event, and
      this runtime stores SAP document contents and vendor data. Check:
      `agents.memory.strategies: []` (the synth warns otherwise). Owner: you.
- [ ] **Notifications go only to domains you name.** Why: the agent picks recipients from case
      data, which is untrusted ([notifications](security.md#notifications)). Check:
      `notification.recipient_domains`, and `api.sample_ticketing: false` once your IT service
      management (ITSM) system is connected (`production-check` row `api.sample_ticketing`).
      Owner: you.
- [ ] **Every model tier is in a Region you allow.** Why: each tier is its own inference profile.
      Check: `runtime.haiku_model_id`, `runtime.opus_model_id` and `agents.model_id`. Owner: you
      and the security team.

## Evaluation

- [ ] **The eval suite passes on a sandbox stack before go-live.** Why: an agent that writes to a
      financial system is judged on the actions it took. Check: `evals-run` on a stack whose
      `promotion.role` is `development`, against SAP systems with `system_role: sandbox`
      ([evaluations](decisions.md#evaluations-run-before-promotion)). Owner: you and the process
      owner.
- [ ] **Your packs include their own eval cases, negative ones included.** Why: the shipped cases
      cover the shipped packs. Check: `packs/<pack>/evals.json` covers each process type, the
      amount limit and the refusals you rely on. Owner: you and the process owner.
- [ ] **Evals run again after every procedure, prompt, model, constant or guardrail change.** Why:
      any of these changes what the agent does. Check: `evals-run`, and a rehearsal before a new
      pack routes cases ([rehearse](packs.md#pack-rehearsal)). Owner: you.
- [ ] **You know production is not evaluated online yet.** Why: online evaluation of production
      sessions is planned, with code-based evaluators. The LLM judges need model content on
      spans, which production turns off. Check: until online evaluation is available, alarms and
      containment are what watch production. Owner: you.

## Operations

- [ ] **Contacts point at monitored addresses.** Why: the procedures send people to them. Check:
      `production-check` row `contacts`. Owner: you.
- [ ] **Concurrency and polling fit what SAP sustains.** Why: every run and every poll reads SAP.
      Check: `pipeline.max_concurrency` and `pipeline.poller_schedule`, measured under load
      ([pipeline](configuration.md#pipeline)), and SAP-side quotas that cap what the poller can
      read. Owner: you and SAP Basis.
- [ ] **An existing stack adds a new cases-table index in a deploy of its own.** Why:
      CloudFormation adds or removes one global secondary index per update, and the case list's
      amount order reads `pack-status-amount-index`. DynamoDB fills a new index from the cases
      already stored, but only from those that have its key. So a case written before
      `amount_sort` existed is not listed when the list is sorted by amount, and the kit cannot
      fill in the missing values. Check: before you upgrade a stack, compare the cases table's
      indexes in `./scripts/deploy.sh diff` with the deployed table. Deploy any new index with no
      other index change, and wait until `aws dynamodb describe-table` shows it `ACTIVE` before
      you rely on it. Owner: you.
- [ ] **Break glass is rehearsed.** Why: `manual` does not stop a run already under way. The
      emergency stop does, and stays in effect until an admin clears it, but it does not undo an
      SAP write already made. Check: a tabletop exercise each quarter, and a real one twice a
      year on a development stack, the emergency stop included
      ([break glass](operating/break-glass.md)). Owner: you and SAP Basis.
