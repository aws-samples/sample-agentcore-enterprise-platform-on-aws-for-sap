# Glossary

The terms of SAP exception handling, in the order you meet them. The platform's terms, the
SAP layer's terms, the SAP terms and the acronyms are in
[Concepts](../../../docs/start/concepts.md). Each entry links to the page that covers it in
depth. The last column gives the name that you see in configuration, commands or code, where
that name is different.

![Concept nesting: the platform contains the use cases and the SAP layer; the SAP exception handling use case contains its packs; the finance_ops pack contains its general instructions and its process types, each mapped to one procedure: invoice_matching to the triage procedure, six exception types to one exception procedure each, and short_pay to a shared procedure. hello-sap has no packs](../../../docs/start/concept-nesting.drawio.png)

## Use case

| Term | What it means | In config or code |
|---|---|---|
| **SAP exception handling** | The complete example use case, preloaded and optional. A business process agent works SAP exceptions with its packs and procedures, and people run it from the operator console. [Walkthrough](../walkthrough.md) | `business-operations` |
| **Business process agent** | This use case has one business process agent. It follows the procedures of the case's pack. | |
| **Skills** | The instructions for an AI coding agent that deploy, run and change this use case. [Skills](../skills/README.md) | |

## Cases and agents

| Term | What it means | In config or code |
|---|---|---|
| **Case** | One SAP exception being worked, such as one blocked invoice. Its id comes from the SAP document, so the same exception never becomes two cases. [Lifecycle](lifecycle.md) | |
| **Case status** | Where a case is: `detected`, `processing`, `awaiting_human_input`, `sap_updated`, `complete`, `manual_review_required` or `error`. [Case states](lifecycle.md#case-states) | |
| **Run** | One pass of the agent over one case. A case can have several runs, for example one before and one after an approval. | |
| **Poller** | The scheduled job that reads SAP for new exceptions and creates their cases, every 15 minutes by default. SAP can also send events instead. | |
| **Operator console** | The web app where people see cases, start runs, approve writes and edit procedures. | |
| **Trigger mode** | Whether a new case starts a run by itself. It is set for the deployment and for each pack. `manual`, the default for both, waits for a person to start each case. When both are `auto`, the pack's cases are queued as soon as they are found. With two switches, an operator can stop automatic runs for every pack at once, and turn on each pack separately when it is trusted. | `ops trigger-mode`, `PUT /deployment/trigger-mode` |
| **Case worker** | A person who may start runs while the trigger mode is `manual`. [Roles](roles.md) | `business-operations-case-workers` |
| **Approver** | A person in a pack's approver group. Only approvers approve or deny a proposed SAP write, or answer the agent's questions on a case. [Roles](roles.md) | `entitlement.approve_groups` |
| **Limit owner** | A person who may lower a pack's write limits in one deployment, never raise them. [Roles](roles.md) | `business-operations-limit-owners` |

## Packs and procedures

| Term | What it means | In config or code |
|---|---|---|
| **Standard operating procedure (SOP)** | The written steps the agent follows for one kind of exception. Other pages define the term once and then say "procedure". Editing a procedure changes what the agent does, with no redeploy. | Files in the pack's `sops/` |
| **Agentic SOPs** | The pattern this use case uses: the agent follows a written procedure and cites the clause it relied on, instead of working from a prompt alone. | |
| **Process type** | The name a pack gives to one procedure, such as `price_variance`. Most process types are exception types. A pack also names its triage procedure and its shared procedures this way. [Kinds of SOP](packs.md#kinds-of-sop) | `process_type_to_sop` |
| **Exception type** | One kind of exception, such as a price variance, with its own procedure. The triage procedure identifies it. The case records it once a run has been given that procedure. | |
| **Pack** | Everything for one SAP process, in five parts: intake (which SAP data to watch), procedures (the procedures and the rules they cite), controls (limits, approvers, write checks), agent configuration and evaluations. `finance_ops` ships with this use case. [Pack parts](packs.md#pack-parts) | `pack.json` |
| **Incomplete pack** | A new pack whose SAP details are still placeholders waiting for a system survey. The poller ignores it until a pack admin completes it with real SAP values. | `<AWAITING_SURVEY>`, `incomplete` |
| **Rule** | A named value a procedure cites, such as `{{PRICE_VARIANCE_PCT}}`. It is versioned with the pack and is the same in every deployment. Some rules are also checked in code before a write. | `constants` |
| **Contact** | A role a procedure names, such as `{{CONTACT_AP_TEAM}}`. Each deployment sets its address. | |
| **Deployment setting** | A value set per deployment and not versioned with the pack: contact addresses, a lowered limit, the trigger mode, the active pack version. | |
| **Version** | Every edit to a pack in development creates a new version of it. | |
| **Stage of a pack** | A pack is edited only in development, and reaches quality and production as a published version. Each stage is its own deployment with its own SAP system. [Pack promotion](packs/promotion.md) | `promotion.role` |
| **Published version** | The version `export` sends from development to the next stage. Only published versions travel, so the edits between two published versions are never imported on their own. Not the same as releasing an SAP block. [Version chains](packs/promotion.md#version-chains) | `ops bundle export` |
| **Bundle** | One JSON file that contains a pack version (its `pack.json`, base prompt and procedures) with a checksum. A pack moves between stages as a bundle. [Bundles](packs.md#promotion-between-stacks) | `ops bundle import` |
| **Rehearsal** | A trial run of a pack's instructions and procedures against a case you describe. It changes nothing in SAP. [Rehearsal](packs.md#pack-rehearsal) | |
| **Seed** | Creating SAP test records in a `sandbox` system, so there is an exception to work. Off by default. Packs that a deploy imports are a different thing. [Pack import at deploy](packs/deploy-import.md) | `ops seed` |

## Controls

| Term | What it means | In config or code |
|---|---|---|
| **Run checks** | This use case's checks on every case tool and SAP write, inside the interceptor. A call needs a run grant. A write to any document other than the case's own is refused. A write over the limit waits for a person. Nothing that changes data runs during a rehearsal. The run checks refuse calls in every Cedar mode, including when Cedar is off. [Run checks](security.md#run-checks) | `business_operations_check` |
| **Run grant** | A short-lived permission written for one run, and deleted when the run ends. Without it, case tools and SAP writes are refused, so only a real run can change a case or SAP. | |
| **Case document check** | The run check that refuses an SAP write unless the write names the case's own document. If a pack does not have this setting, every SAP write from that pack is refused. | `case_document_check` |
| **Write guards** | The checks in code that a pack turns on, which run before a payment-releasing SAP write. Each one reads its rules, such as a tolerance, from the pack. | `write_guards` |
| **Verifier** | A second model that must agree with a payment write before it runs. If the verifier does not agree, the write is refused, as if a check had refused it. | `verifier` |
| **`AUTO_WRITE_LIMIT_USD`** | The largest SAP change, by amount, the agent may make without a person. Set per pack. | |
| **Approval** | An approver accepting one proposed write, field by field. Above `DUAL_APPROVAL_LIMIT_USD`, where a pack sets it, the write needs two different approvers. | |
| **Cedar on the shared Gateway** | This use case's Cedar statements stop the clients of another use case from calling its tools. The quickstart sets `ENFORCE`. [Cedar](security.md#cedar-on-the-shared-gateway) | `security.cedar` |
| **SAP access claim and access row** | This use case's SAP access claim must fit inside its access row. How the two fit inside each other with Cedar, the run checks and SAP's roles: [SAP access limits](architecture.md#sap-access-limits) | `claim`, `layers.sap.access.business-operations` |
| **`bypass_gateway`** | The SAP systems the poller and the test-data seed call directly, as the system's service account, outside the Gateway. A system not listed here is not polled. | |
| **Direct mode** | The use case under `identity.mode: direct`: people sign in with Entra ID, and app roles replace the user pool's groups. [Direct mode](direct-mode.md) | |
