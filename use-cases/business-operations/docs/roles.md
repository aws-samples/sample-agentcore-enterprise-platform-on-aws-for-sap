<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Roles

Who does what in SAP exception handling, and what gives each role its access today. A role is
a job, not a person. In a small team, one person can have several roles. The platform roles and
the SAP roles are in [Roles](../../../docs/start/roles.md).

This use case adds roles in two scopes:

| Scope | Covers | Roles |
|---|---|---|
| **Use case** | SAP exception handling and all of its packs | AI engineer, operator |
| **Pack** | One SAP process, such as accounts payable invoice exceptions | Process owner, process analyst, control owner, case worker, approver, auditor |

## Use case roles

| Role | Responsible for | Access today |
|---|---|---|
| **AI engineer** | The business process agent, its tools and the platform prompt. A new pack's starting files, its intake and its agent configuration, built with the SAP functional consultant. Evaluation runs and cost tuning | `business-operations-pack-admins` and `business-operations-authors` in a development deployment, and AWS credentials for `ops.py` |
| **Operator** | Running the use case day to day: the trigger mode, contact addresses, a lowered limit, reruns, monitoring, break glass (the emergency stop), and importing a published pack version with a change reference | `business-operations-admins` for the trigger mode. `business-operations-authors` for contacts. `business-operations-limit-owners`, plus a pack's `entitlement.limit_groups` where the pack sets them, to lower its limits. AWS credentials for `ops.py bundle import` |

## Pack roles

| Role | Responsible for | Access today |
|---|---|---|
| **Process owner** | The process, and what its standard operating procedures (SOPs) decide. Agrees to every change to the pack's procedures before the change leaves development | `business-operations-authors`. There is no process owner group |
| **Process analyst** | Writing and revising the procedures, the general instructions and the rules they cite. Rehearsing each change. Keeping the example cases current | `business-operations-authors`, plus the pack's `author_groups` and `constant_groups` where the pack sets them. To do this from a remote Model Context Protocol (MCP) client such as AWS Quick, also `business-operations-remote-authors` |
| **Control owner** | The controls on SAP writes: the limits, who approves, the checks before every write, the verifier (a second model that must agree with a payment write), and the tolerances those checks enforce | `business-operations-limit-owners`, plus the pack's `entitlement.limit_groups` where the pack sets them, to lower a limit in one deployment. Limit ceilings, approvers and checks are part of the pack's structure, which only `business-operations-pack-admins` can change |
| **Case worker** | Working cases: starting runs while the trigger mode is `manual` (**Process** on a case, or **Start *n* runs** on **Cases**), asking the agent about a case, rating finished cases. A run that a case worker starts may post SAP writes up to the pack's auto-write limit with no approval | `business-operations-case-workers`, plus the pack's `entitlement.process_groups` where the pack sets them, to start runs. The pack's `entitlement.groups` to see cases and ask about them. Only an approver can answer the agent's question or decide a case |
| **Approver** | Approving or denying one proposed SAP write. Making every other decision on a case, including a reply to the agent's question. Above the two-approver limit, a second, different approver must also approve | The pack's `entitlement.approve_groups` |
| **Auditor** | Reviewing cases, approvals, pack versions and the audit trail | Cases through the pack's `entitlement.groups`. Versions and audit records through `ops.py` with AWS credentials. There is no read-only console role |

Every console route also needs `business-operations-users`. The names of a pack's groups start
with `business-operations-` because all use cases share the user pool ([Security](security.md#console-permissions)).

## Pack parts and their owners

A pack's contents are in five parts, and each part has one owner. The parts are described in
[Packs and SOPs](packs.md#pack-parts).

| Part | Owner |
|---|---|
| **Intake** | SAP functional consultant and AI engineer, with the process owner |
| **Procedures** | Process owner and process analysts |
| **Controls** | Control owner |
| **Agent configuration** | AI engineer |
| **Evaluations** | Process analysts, with the AI engineer |

The settings that differ per deployment, such as contact addresses and the trigger mode, are the
operator's.

## Separation of duties

What the code enforces:

- An approval counts only when it comes from a member of the pack's approver group who did not
  raise the ticket. Above `DUAL_APPROVAL_LIMIT_USD`, two different approvers must approve.
- Quality and production deployments refuse every edit to a pack or procedure. A change reaches
  them only as an imported version with a change reference. Each version records who edited it.

What it leaves to your own process:

- No second person has to review a pack version before it is published or imported. Approve
  each import through your change management. Use the difference that `ops bundle import`
  prints in its dry run. The
  [production checklist](production-checklist.md#authorization-and-autonomy)
  names what your release pipeline adds.
- The code checks group membership, never who a person is. Anyone added to a group gets its
  rights, and one person in two groups gets the rights of both. Keep makers and approvers in
  separate groups in your identity provider.
- Some rules are also enforced in code. In `finance_ops`, six of the nine rules are read by the
  checks that run before every SAP write. These are the price and quantity tolerances, the line
  amount tolerance and the duplicate match window. Changing one changes what the agent may post
  without an approval, so treat it as a change to a control and involve the control owner.

## Pages by role

Find your role. Each row starts with the page to read first. The other pages give more detail.

### Pages for platform and SAP roles

The platform and SAP roles read [Next steps](../../../docs/start/next-steps.md) first. These
are this use case's pages for them:

| You are | Read first | Then |
|---|---|---|
| **Cloud engineer** | The [`cloud-deploy-and-wire`](../skills/cloud-deploy-and-wire/SKILL.md) skill, or the [quickstart](quickstart.md) by hand | [Configuration](configuration.md), [Removal](operating/removal.md) |
| **Security engineer** | [Security](security.md) | The use case's [threat model](reference/threat-model.md) and [production checklist](production-checklist.md). The `sec-production-check` skill reads a deployed stack and changes nothing |
| **Identity administrator** | [Direct mode (Entra)](direct-mode.md) | The groups to map, in [Use case roles](#use-case-roles) and [Pack roles](#pack-roles) |
| **SAP functional consultant** | [Pack parts](packs.md#pack-parts): what a pack's intake needs from SAP | [Detection](lifecycle.md#1-detection), the [`dev-new-sap-process`](../skills/dev-new-sap-process/SKILL.md) skill, which you run with the AI engineer |
| **ABAP developer** | The use case's [Business Add-In (BAdI) implementation](abap/zcl_im_bo_ingest.clas.abap) | |

### Pages for use case roles

| You are | Read first | Then |
|---|---|---|
| **AI engineer adding an SAP process** | The [`dev-new-sap-process`](../skills/dev-new-sap-process/SKILL.md) skill, run by your coding assistant | [Packs](packs.md), [Decisions](decisions.md) for why it is built this way, [evaluation runs](operating/evals.md) |
| **Operator** (runs it day to day) | [The operator console](console.md), then [Operations](operating.md): commands, monitoring, cost, troubleshooting | [Configuration](configuration.md), [Pack promotion](packs/promotion.md), [Troubleshooting](../../../docs/TROUBLESHOOTING.md), the [`ops-run-case`](../skills/ops-run-case/SKILL.md) skill on a deployed stack |

### Pages for pack roles

| You are | Read first | Then |
|---|---|---|
| **Process owner or process analyst** (accounts payable lead, finance operations) | [Process owner guide](for-process-owners.md): how the standard operating procedures (SOPs) decide what the business process agent does, and who may change what | A shipped procedure, such as [price variance](../packs/finance_ops/sops/price_variance.txt), [Lifecycle](lifecycle.md) for what happens to a case, the [`owner-author-sop`](../skills/owner-author-sop/SKILL.md) skill |
| **Control owner** | [Controls and evidence](controls-and-evidence.md): every path that changes SAP, who decides, and where the record is | [Approvals](for-process-owners.md#approvals), [Rules, limits and contacts](packs.md#rules-limits-and-contacts), the [use case's production checklist](production-checklist.md), the `sec-production-check` skill on a stack |
| **Case worker or approver** | [The operator console](console.md) | [Human decision](lifecycle.md#5-human-decision) in the case lifecycle |
| **Auditor** | [Controls and evidence](controls-and-evidence.md) | [Closure and feedback](lifecycle.md#6-closure-and-feedback), [Pack status](packs/status.md), [Separation of duties](#separation-of-duties) |

