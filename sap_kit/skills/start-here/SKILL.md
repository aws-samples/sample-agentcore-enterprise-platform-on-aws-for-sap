---
name: start-here
description: Use when someone opens this repository and asks "what is this", "where do I start", "what can you do here", "help me get going" or "I'm new to this", or names their role (process owner, process analyst, control owner, SAP Basis administrator, ABAP developer, SAP functional consultant, cloud engineer, AI engineer, operator, security engineer) without a task. Explains the accelerator in plain words, finds out who they are and what is already set up (read-only), and hands them to the one skill or page that fits. Changes nothing.
---

# Start here

The person may never have used the AWS Cloud Development Kit (CDK), deployed to AWS or
automated anything in SAP. Paths are relative to the repository root. This skill only reads.
"You" in this skill is the coding agent that runs it.

## Tone

- Ask one question at a time. Say what each acronym means the first time you use it.
  Explain these words the first time too:
  - a **stack** is a group of AWS resources deployed together;
  - **CDK** is the tool that turns this repository's Python into those stacks;
  - a **use case** is one business process on the platform, in its own `use-cases/<name>/`
    folder;
  - the words of the use case they work with, from its glossary (step 1).
- Call an agent that a use case deploys the "business process agent". Do not call it only
  "the agent", because the person may think you mean yourself.
- Say what a command does before you run it, and what it proved after.
- Never run a command that changes AWS or SAP from this skill.

## 1. Introduce the accelerator

Use two or three sentences, from `docs/start/what-it-is.md`: a platform on Amazon Bedrock
AgentCore, an SAP layer that connects it to SAP, and use cases on top. Business process agents
follow procedures a person wrote, ask a person before any write over a limit, and record every
step.

Then list the use cases in this checkout. Read each `use-cases/*/manifest.yaml`, except
`use-cases/_template/`. For each one that has a `docs:` block, give its `docs.title` and its
`summary`. Its `docs` paths, relative to its folder, are its pages: `entry` to start, `glossary`
for its terms, `roles` for its roles and the pages each role reads, and `quickstart` to deploy
it. Offer `docs/start/concepts.md` if they want the parts explained.

## 2. Identify the role

Ask which role is closest. The platform and SAP roles are in `docs/start/roles.md`. Each use
case adds its own roles on its `docs.roles` page.

Skills live beside what they belong to: a use case's in `use-cases/<name>/skills/`, with a
`README.md` that lists them, a layer's in `layers/<name>/skills/`, and the shared ones in
`sap_kit/skills/`. Each skill's name starts with the role that usually runs it: `cloud-`
(cloud engineer), `ops-` (operator), `dev-` (AI engineer), `owner-` (process owner or process
analyst) and `sec-` (security engineer or control owner).

| They are | Hand them to |
|---|---|
| A cloud engineer setting it up in an AWS account | The use case's `cloud-` deploy skill, or its `docs.quickstart` page to do it by hand. Without a use case in mind, `docs/start/quickstart.md` |
| An AI engineer building a different use case | `dev-add-sap-use-case`, or `CONTRIBUTING_USE_CASES.md` without SAP |
| Someone with a use case from another repository | `./scripts/deploy.sh usecase add <git-url\|path>`: it copies the use case into `use-cases/` and adds its entries to `platform.yaml`. "Use cases from other repositories" in `CONTRIBUTING_USE_CASES.md` |
| An SAP Basis administrator | `layers/sap/sap-setup.md`, starting at "Prerequisite decisions" |
| An ABAP developer (ABAP is SAP's programming language) | `layers/sap/reference/events.md`, then "Send events" in `layers/sap/sap-setup.md` |
| A security engineer | `docs/SECURITY_CONTROLS.md`, then the use case's `sec-` skill on a stack |
| A role that a use case adds, such as an operator, a process owner or a control owner | "Pages by role" on the use case's `docs.roles` page, and the skill for that role in its skills `README.md` |

Someone setting it up alone usually has two of these roles. Before they start, name the SAP
questions that someone else will need to answer (`layers/sap/sap-setup.md`, "Prerequisite decisions").

## 3. Check the existing deployment

Read-only, and only what the role needs:

1. `platform.yaml` at the root. If it is absent, nothing is configured yet. If it is
   present, read its `project`, `environment` and `region`, and which use cases are set under
   `use_cases:`.
2. `aws sts get-caller-identity --query Account` shows whether they have AWS credentials,
   and for which account. If it fails, fix that first; it is not a reason to stop.
3. With both, and where the use case's `ops.py` has a `status` verb:
   `./scripts/deploy.sh usecase run <name> status`. It prints one line per stack,
   `<stack>: <state>`. A state of `FAIL:` that says the stack does not exist means the stack is
   not deployed yet.

## 4. Hand off

Name the one next skill or page and what it will do, then start it if they agree. To remove
a deployment they no longer want, or for a deploy that failed, use the use case's `cloud-`
skill for it where it has one. Otherwise use "Clean up" in `docs/PLATFORM_GUIDE.md`, or
`docs/TROUBLESHOOTING.md`.

## Self-check

Report `OK: <role>, <configured|not configured>, <deployed in <account>/<Region>|not deployed>, next: <skill or page>`.
