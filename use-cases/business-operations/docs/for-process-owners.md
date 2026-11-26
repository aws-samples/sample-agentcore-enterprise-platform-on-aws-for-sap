<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Process owner guide

Terms are defined in the [glossary](glossary.md) and in [Concepts](../../../docs/start/concepts.md).

This page is for the person who owns the process, such as an accounts payable lead or a finance
operations manager. It is also for the process analysts who write the process's standard operating
procedures (SOPs). This page calls an SOP a procedure from here on. The page needs no code. The
engineering detail is in [Packs](packs.md).

## SOP execution

For each kind of exception, the business process agent follows a written procedure, the same kind
of procedure you would give a new clerk. The agent does not decide the rules itself. The procedure
says what to read in SAP, what to compare, when the result is within tolerance, and who to tell
when it is not. The agent cites the step it relied on in the case record.

The procedures for one SAP process, together with the agent's settings and limits, make up a
**pack**. This use case ships one pack, `finance_ops`, for supplier invoices blocked for payment.
Every case starts with a triage procedure, general invoice matching. It identifies the kind of
exception and gives the case to the procedure for that kind: price variance, quantity variance,
missing goods receipt, missing purchase order, duplicate invoice or unit of measure mismatch.
Triage comes first because a blocked invoice alone does not show the kind of exception. The agent
must first compare the invoice with the purchase order and the goods receipt. A shared procedure,
short payment, is used when an approver chooses to pay less than billed.

The procedures, the rules they cite and the outcomes are the pack's **Procedures** part, which you
and your process analysts own. The pack's other parts are its intake from SAP, its controls, the
agent's configuration and its evaluations. Each part has its own owner
([pack parts](packs.md#pack-parts)).

## SOP structure

Here is part of the shipped price variance procedure (`packs/finance_ops/sops/price_variance.txt`):

```text
2.2  Both limits MUST be breached for the variance to be ABOVE tolerance:
       ABOVE  tolerance = price_variance_pct > {{PRICE_VARIANCE_PCT}}%
                          AND price_variance_abs > ${{PRICE_VARIANCE_ABS_USD}} per unit
       WITHIN tolerance = anything else

3.1  IF the variance is WITHIN tolerance:
     - The agent MUST auto-accept and proceed to post the invoice (STEP 4).

3.2  IF the variance is ABOVE tolerance AND the PO references a contract or
     blanket agreement:
     - The agent MUST raise a free-text ticket to {{CONTACT_PROCUREMENT}}
       asking which contract terms or pricing tier applies.
```

Three things to notice:

- **MUST, SHOULD and MAY** mean what they mean in a standard (RFC 2119). MUST is a rule. SHOULD is
  the default unless there is a reason not to. MAY is allowed. Each procedure says so at the top.
- **Numbers are named, not written in.** `{{PRICE_VARIANCE_PCT}}` is a **rule** of the pack (in
  `pack.json`, a constant). Its value belongs to the pack. Change it once, and every procedure that
  uses it follows, in every deployment that the pack's next version reaches.
- **People are named by role.** `{{CONTACT_PROCUREMENT}}` is a **contact**. Each deployment sets it
  to a real address. The procedure never contains an email address.

## Change permissions

Changes are of three kinds. The kind decides who may make the change and how fast it takes effect.

| Kind of change | Examples | Who | Takes effect |
|---|---|---|---|
| **Content** | The wording of a procedure, the agent's instructions, a pack's description, a tolerance's value | Process analysts and process owners (`business-operations-authors`; a tolerance also needs the pack's `constant_groups`) | At once, on the pack's page in the console (Procedures), in a development deployment. Quality and production take it only as a promoted pack version |
| **Current setting** | A contact's address; lowering the auto-write or two-approver limit | The operator: a contact needs `business-operations-authors`, a limit needs `business-operations-limit-owners` and the pack's `limit_groups` where it sets them | At once, in that deployment only. A limit can be lowered, never raised above the pack's value |
| **Structure** | Which SAP data the pack watches, which tools the agent may use, a new kind of exception, a limit's default value, who approves | A pack administrator (`business-operations-pack-admins`), usually the AI engineer, in a development deployment | Through the AI engineer and a review, then promoted to quality and production |

Each stage takes a different kind of edit:

| Stage | What you may change there |
|---|---|
| Development | Procedures, rules, the agent's instructions and the pack's description. You edit and rehearse them here |
| Quality | No pack edits. It takes a published version from development, and runs it against its own SAP system |
| Production | No pack edits. It takes the same published version after quality |
| Every stage | Contacts and lowered limits, which belong to one deployment and are not part of the pack |

Your organization can narrow each of these further per pack: who may see its cases, who may approve
its writes, who may edit it, and who may change its settings. Each role, and the group that grants
it, is on the [roles](roles.md) page.

Some tolerances are controls as well as rules. In `finance_ops`, the checks that run before every
SAP write read six of them: the two price tolerances, the two quantity tolerances, the line amount
tolerance and the duplicate match window. If you raise one, the agent can post more without an
approval, so agree the change with your control owner as well.

## Changing a procedure

A saved procedure is used by the next run in that deployment. So make and test changes in a
development deployment first:

1. In development, open the pack in the console, then the procedure on its **Procedures** tab, and
   press **Edit**. The check runs as you type. **Save** shows the difference before it saves.
2. Press **Rehearse** and describe a case, or start from one of the pack's example cases. The agent
   works the case with the saved procedure. It shows on the page what it would do and what it would
   post. A rehearsal changes nothing in SAP and sends nothing.
3. When the agent does what you expect, your operator moves the pack version to quality and
   production with the `ops-promote-pack` skill. The skill shows the difference before it applies
   the version.

Quality and production refuse a direct edit. A change reaches them only as a pack version. This is
like an SAP transport: what reaches production is exactly what was tested in development.

Your AI engineer may also have changed the same pack in the repository. In that case, the next
deploy of the development stack stops and names both changes, so that neither change overwrites the
other without anyone noticing.

Every case records the exact procedure version it followed, so you can always see which wording
decided a past case. While the trigger mode is `manual`, a run starts only when a person starts it.

To write a new procedure or a large revision, use the `owner-author-sop` skill in an AI coding
assistant opened on the repository, or ask your AI engineer to do it. The skill checks the structure
and the named values before anything is saved ([skills](../skills/README.md)).

## Approvals

- The agent may change SAP by itself only for the case's own document, and only up to the pack's
  **auto-write limit** (`AUTO_WRITE_LIMIT_USD`; `finance_ops` ships 100,000).
- Above that limit, the change waits as a diff, field by field, for a member of the pack's approver
  group. An approval covers exactly that change and nothing else.
- Above the **two-approver limit** (`DUAL_APPROVAL_LIMIT_USD`; `finance_ops` ships 250,000), two
  different people must approve.
- New cases wait for a person to start them until an operator sets the trigger mode to `auto`, for
  the deployment and for your pack. A new pack starts at `manual`.

When you approve, the console shows you:

- the procedure clause that stopped the run, and the limit that the change crossed;
- the exact change, field by field, and the SAP system it will be written to;
- the outcomes the procedure allows, such as approve as billed, short pay or deny.

You choose one outcome and give a reason. The agent may then post only the change of that outcome,
and the write checks read SAP again before it does
([Approving a change](console.md#approving-a-change)).

Decide these limits and groups with your control owner before production
([production checklist](production-checklist.md)).

## Automating another process

Bring the following to your AI engineer, who runs the `dev-new-sap-process` skill with you and your
SAP functional consultant:

- the SAP document the exceptions are on, and how you find them today;
- the kinds of exception, and for each kind, the procedure you follow (written down, or talked
  through so that it can be written down);
- every number the procedure uses, with its value and unit;
- who to tell in each case, by role;
- who may approve the agent's changes, and up to what amount.

The skill turns this into a pack and its procedures. It imports the pack into a development
deployment for you to rehearse.
