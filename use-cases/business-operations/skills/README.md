# Skills

A skill is a written procedure that an AI coding agent, such as Claude Code or Kiro, follows
step by step for one task in this repository: deploying, writing a pack or running a case, for
example. Each one is a `SKILL.md` file in a folder here. The coding agent reads each skill's name
and description at startup. It loads the whole procedure when your request matches the
description, or when you call the skill by name. A skill is not the business process agent that
the use case deploys to work SAP exceptions.

## Usage

Open the repository in a coding agent that reads skills. Ask for what you want in plain
words, such as "deploy this and wire it to SAP" or "automate blocked purchase requisitions".

| Coding agent | Where it finds the skills |
|---|---|
| Claude Code | `.claude/skills/` |
| Kiro | `.kiro/skills/` |
| Other coding agents that read the shared skills folder | `.agents/skills/` |

Each folder contains only links to the skills below, so there is one copy of each skill.

The skills ask before they change anything that matters:

- Before a deploy, they show you the account, Region and stacks, and wait for your yes.
- They never take a password as an argument. You type each secret yourself at the prompt.

## The skills

Each name starts with the role that usually runs it, from [Roles](../docs/roles.md) and the platform's [Roles](../../../docs/start/roles.md):

| Prefix | Role |
|---|---|
| `cloud-` | Cloud engineer: deploys into the AWS account |
| `ops-` | Operator: runs cases and moves packs between stacks |
| `dev-` | AI engineer: writes packs and use cases in the checkout, with the SAP functional consultant for a pack's intake |
| `owner-` | Process owner or process analyst: decides what the business process agent does, in its standard operating procedures (SOPs, or procedures) |
| `sec-` | Security engineer or control owner: decides whether it may go into production |

`start-here` has no prefix: it is for anyone opening the repository for the first time, and it
lives in `sap_kit/skills/` because it routes to every use case.

In the order you usually need them:

| Skill | Use it to | What it changes |
|---|---|---|
| [`start-here`](../../../sap_kit/skills/start-here/SKILL.md) | Find out what this is, who does what, and which skill to run first | Nothing: it only reads |
| [`cloud-deploy-and-wire`](cloud-deploy-and-wire/SKILL.md) | Deploy the platform, the SAP layer and this use case, connect one SAP system, add the first user and verify it all | Deploys stacks into your account, after you confirm |
| [`cloud-fix-deploy`](cloud-fix-deploy/SKILL.md) | Find why a deploy failed, in plain words, and fix it with one change you agree to | The one change you agree to, then deploys again |
| [`cloud-connect-mcp-client`](cloud-connect-mcp-client/SKILL.md) | Connect AWS Quick or Copilot Studio to the people Gateway (an AgentCore Gateway) to read and author packs and procedures, each person signed in as themselves, and remove the connection again | Deploys the people Gateway with its app client, and this use case's people target and group, after you confirm |
| [`ops-run-case`](ops-run-case/SKILL.md) | Take one exception end to end: seed it in a sandbox SAP system, make it a case, follow the run and the approval, and check the SAP data afterwards | Posts test documents to a sandbox SAP system |
| [`dev-new-sap-process`](dev-new-sap-process/SKILL.md) | Automate a new kind of SAP exception: scope it with you, find the SAP service and fields, write the whole pack, and import it into a development stack | Writes a pack in your checkout, imports it into a development stack |
| [`dev-author-pack`](dev-author-pack/SKILL.md) | Change a pack: its model, limits, tools, or the business process agent's instructions | Edits the pack, then validates it |
| [`owner-author-sop`](owner-author-sop/SKILL.md) | Write or revise a procedure the business process agent follows, and check it before it is saved | Edits a procedure, then validates it |
| [`dev-tune-cost`](dev-tune-cost/SKILL.md) | Lower the cost per case one setting at a time, and keep a change only if every test case still passes. Then set the cost alarm | Edits the pack; runs the billed evaluation cases |
| [`ops-promote-pack`](ops-promote-pack/SKILL.md) | Move a pack version from development to quality or production, see the difference first, and roll back | Imports a pack version into another stack, after you confirm |
| [`sec-production-check`](sec-production-check/SKILL.md) | Ask "what blocks go-live?" and get every gap with its fix | Nothing: it only reads |
| [`dev-add-sap-use-case`](../../../sap_kit/skills/dev-add-sap-use-case/SKILL.md) | Start your own use case that reads SAP through the SAP layer | Scaffolds a new use case and deploys it, after you confirm |
| [`cloud-remove`](cloud-remove/SKILL.md) | Remove the deployment, or only this use case, and say what is kept and what the SAP team removes | Destroys stacks, after you confirm each step |

## Manual use

Every skill is plain Markdown with the exact commands it runs, so a person can follow one by hand.
The [quickstart](../docs/quickstart.md) is `cloud-deploy-and-wire` and
`ops-run-case` written for a person.
