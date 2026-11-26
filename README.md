# Agentic Business Platform Accelerator for SAP on AWS

An accelerator for building AI agents that work in and around SAP S/4HANA and SAP ECC. An AI
agent is software that uses a large language model to work through a task. It decides the next
step, calls a tool such as an SAP read, checks the result, and continues until the task is
done.

Most of the work in taking an agent past a demo is not the agent itself. It is sign-in, access
control, approvals, audit, monitoring and a safe connection to SAP. The Accelerator gives you
that groundwork already built. A new SAP use case starts from working patterns, and you build
the process itself instead of the infrastructure around it.

It gives you three things:

- A governed platform for running agents on Amazon Bedrock AgentCore, the AWS service for
  hosting and operating them.
- A connection to SAP with its controls already in place.
- A complete working agent to copy: it resolves accounts payable three-way match exceptions in
  SAP on its own, and escalates to a person for approval when a change is over your limit.

This is sample code that you deploy into your own AWS account and adapt.

[Deploy it](#quick-start) · [See the included use cases](#use-cases) · [Read the overview](docs/start/what-it-is.md)

## Features

Every agentic use case must meet these enterprise requirements. The Accelerator gives you a start on each:

- **Identity and access.** People sign in through your corporate identity provider, and each
  agent may call only the tools you allow it.
- **Governed SAP access.** One Gateway checks every tool call. The SAP layer refuses any SAP
  call that no use case has claimed, and each SAP system has its own write switch.
- **Human approval.** Limits on what an agent may change, and a person approves anything over
  them.
- **Audit and evidence.** A record of everything an agent read, decided and changed.
- **Operations.** Logs, traces, alarms, a dashboard and an emergency stop.
- **Repeatable delivery.** One settings file, and one command each to deploy, verify and remove.
- **One set of tools for every client.** MCP clients such as Kiro, Claude Code and AWS Quick
  use the same tools as the agents, under the same checks.
- **A head start on the first process.** [SAP exception handling](#sap-exception-handling-included-sample)
  is a complete SAP agent to copy from, with its console, procedures and approvals.

The controls are code, not prompts. They run in the Gateway, outside the model, so nothing the
model writes can turn them off.

## Prerequisites

- **An AWS account** and a Region where Amazon Bedrock AgentCore is available, with access turned
  on for the models it uses: Anthropic Claude Sonnet and Haiku for the agents, and Amazon Titan
  Text Embeddings V2 for procedure search.
- **A sandbox SAP system.** The quick start uses an SAP S/4HANA client with SAP's sample data,
  four OData services, a service user, and an HTTPS endpoint that AWS can reach. Use a sandbox,
  because the test data that the quick start posts creates real SAP documents.
- **Someone in your SAP team** to activate the services and create the user. Their checklist is
  [SAP setup](layers/sap/sap-setup.md).
- **On your machine:** Python 3.13, Node.js, the AWS CLI v2, bash 4 or newer, and git.

The full list, with versions, is in the [quickstart](docs/start/quickstart.md#prerequisites).

## Quick start

Deploy the platform, the SAP layer and one use case into one AWS account, connected to a
sandbox SAP system.

```bash
git clone <this repository's URL> && cd <its folder>
python3.13 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
./scripts/deploy.sh design --profile greenfield  # writes platform.yaml from a preset; then add the SAP layer and one use case
./scripts/deploy.sh layer run sap secrets s4     # stores the SAP user and password; reads them from the terminal
./scripts/deploy.sh doctor                       # checks tools, account, Region, secrets and models; changes nothing
./scripts/deploy.sh deploy                       # shows the stacks and asks before it creates anything
./scripts/deploy.sh verify                       # proves the controls refuse what they should
```

The [platform quickstart](docs/start/quickstart.md#2-write-platformyaml) shows what to add to
`platform.yaml` for the SAP layer. Each use case's own quickstart says what to add for that use
case, and explains its first run:

<!-- use-cases:begin quickstart -->
- [SAP exception handling](use-cases/business-operations/docs/quickstart.md) (`business-operations`): Business process agents for SAP exception handling, on the shared platform's Gateway, Cedar engine and interceptor.
- [Claude Code and Cowork on Amazon Bedrock](use-cases/claude-3p-on-bedrock/README.md#quick-start) (`claude-3p-on-bedrock`): Claude Code and Claude Desktop/Cowork on Amazon Bedrock through Anthropic's Claude apps gateway, with Microsoft Entra ID sign-in and per-team model access, on the platform's private network.
- [hello-sap](use-cases/hello-sap/walkthrough.md#enable-build-verify) (`hello-sap`): Reference SAP use case — reads SAP through the SAP layer as its own Gateway client, and writes nothing.
<!-- use-cases:end -->

The platform quickstart is the same path for any use case. Other ways in:

- **The platform alone, without SAP:** `./scripts/deploy.sh design --profile greenfield`
  ([Platform guide](docs/PLATFORM_GUIDE.md)).
- **Your own agent:** `./scripts/deploy.sh usecase new <name>` ([New use cases](#new-use-cases)).
- **An MCP client such as Kiro or Claude Code:** connect it to the deployed Gateway with the MCP
  servers for desktop clients that a use case ships ([use cases](#use-cases)).
- **A remote MCP client such as AWS Quick or Copilot Studio:** turn on the people Gateway
  (`gateway.people`), where each person signs in as themselves
  ([People Gateway](docs/modules/gateway.md#people-gateway)).
- **An AI coding assistant:** open the repository in Claude Code or Kiro and ask it to "deploy
  this and connect it to SAP". It follows the same steps and asks you before anything is deployed.

> [!WARNING]
> This is sample code, not for production use as is. It is a baseline that you adapt. Before
> you deploy it, work with your security and legal teams to meet your organization's security,
> regulatory and compliance requirements. Review in particular the sign-in and authorization,
> the trigger mode and the other run controls, and the SAP write paths. The [production readiness checklist](docs/PRODUCTION_READINESS_PLAN.md) lists
> the work and evidence to complete first, with the
> [SAP layer's](layers/sap/production-checklist.md) own checklist and the checklist of each use
> case you turn on.
>
> AWS supports the AWS services that this sample uses. AWS Support does not cover the sample
> code. You or your partner run and maintain it in your own account under the
> [AWS shared responsibility model](https://aws.amazon.com/compliance/shared-responsibility-model/).
>
> **SAP API Policy.** Use of SAP APIs is subject to the
> [SAP API Policy](https://help.sap.com/doc/sap-api-policy/latest/en-US/API_Policy_latest.pdf) and
> your agreements with SAP. Review both before you connect this sample to an SAP system.

## Use cases

A use case is where one business process lives: its agent, its procedures and its controls.
Start from one of the two included SAP samples, or create your own. The use cases in this
checkout, each with its own pages:

<!-- use-cases:begin -->
- [SAP exception handling](use-cases/business-operations/README.md) (`business-operations`): Business process agents for SAP exception handling, on the shared platform's Gateway, Cedar engine and interceptor.
- [Claude Code and Cowork on Amazon Bedrock](use-cases/claude-3p-on-bedrock/README.md) (`claude-3p-on-bedrock`): Claude Code and Claude Desktop/Cowork on Amazon Bedrock through Anthropic's Claude apps gateway, with Microsoft Entra ID sign-in and per-team model access, on the platform's private network.
- [hello-sap](use-cases/hello-sap/walkthrough.md) (`hello-sap`): Reference SAP use case — reads SAP through the SAP layer as its own Gateway client, and writes nothing.
<!-- use-cases:end -->

### SAP exception handling (included sample)

Its business process agent works SAP exceptions that people clear by hand today, starting with
supplier invoices blocked for payment.

For example, a supplier invoice for USD 25,000 is blocked for payment. The agent loads the
procedure for invoice matching, reads the invoice, the purchase order and the goods receipt,
and checks for a duplicate. Everything matches and the amount is under the limit, so it
releases the invoice. The same invoice at USD 150,000 waits in the console for an approver,
with the exact change the agent proposes.

It is optional: remove it from `platform.yaml` and the platform and the SAP layer still deploy
on their own. [Deploy it with the quick start](#quick-start).

### hello-sap (included sample)

The smallest SAP use case. It reads SAP, writes nothing and has no agent. It shows the minimum
an SAP use case needs: its SAP access claim and its Cedar permits.

### New use cases

- **A new SAP use case.** One command creates it with its SAP access claim and its Cedar
  permits already written. `verify` then proves that it reads SAP and that an SAP write is
  refused:

  ```bash
  ./scripts/deploy.sh usecase new <name> --template sap_kit/templates/sap-use-case
  ```

  The `dev-add-sap-use-case` skill gives the remaining steps. Copy the patterns you need, such
  as approvals, run grants or the console, from SAP exception handling.
- **A new process in SAP exception handling.** Add a pack with its own procedures, using the
  `dev-new-sap-process` skill.
- **A use case without SAP.** `./scripts/deploy.sh usecase new <name>` creates one on the
  platform alone ([Use cases](use-cases/README.md)).

Skills are written instructions for an AI coding assistant such as Claude Code or Kiro. The
assistant asks you before it changes anything. You can also follow each skill by hand.

## Architecture

![How a run flows: a trigger in the use case, or a person, starts a run (1); the agent runs on the platform and makes tool calls (2); the Gateway checks every call and passes only allowed ones to the SAP layer (3), whose SAP access claim check refuses SAP calls that no SAP access claim names; the AWS for SAP MCP Server calls SAP over OData (4). MCP clients such as Kiro, Claude Code and AWS Quick connect to the same Gateway](docs/start/architecture/architecture-overview.drawio.png)

1. A trigger starts a run: a schedule, an event from SAP, or a person in the console.
2. The agent runs on the platform and follows the use case's procedures. Each time it needs
   data or wants to change something, it makes a tool call.
3. Every tool call goes through the Gateway, which checks who is calling and what they may
   call. The SAP layer refuses any SAP call that no use case has claimed.
4. Allowed calls reach SAP through the AWS for SAP MCP Server.

| Part | Read more |
|---|---|
| **Platform**, from the [Enterprise Agentic AI Platform Accelerator](https://github.com/aws-samples/sample-agentcore-enterprise-platform) | [Platform guide](docs/PLATFORM_GUIDE.md) |
| **SAP layer** | [SAP layer](layers/sap/walkthrough.md) |
| **Use cases** | [All use cases](use-cases/README.md) |

The [detailed architecture](docs/start/architecture/architecture.light.drawio.png) shows every
component of one real process, the included sample.

## Key terms

![The three parts in your AWS account: the platform (the Enterprise Agentic AI Platform Accelerator, on Amazon Bedrock AgentCore) at the bottom, the SAP layer above it with its SAP access claim check, and use cases on top. Two included, optional examples sit in dashed boxes: SAP exception handling, the complete one, with its business process agent, pack and procedures, and cases, and hello-sap, the smallest, which reads SAP and writes nothing. A third box is for your own use cases. The operator console connects to SAP exception handling](docs/start/three-parts.drawio.png)

The rows follow the diagram, top to bottom.

| Term | Meaning |
|---|---|
| **Use case** | One business process automated with agents. Each is one entry under `use_cases:` in `platform.yaml`, so one line turns it on or off |
| **Business process agent** | The agent inside a use case that does the work, following the use case's procedures. A coding agent, such as Claude Code, is a different thing |
| **Pack and SOPs** | A pack is everything for one process: what to watch in SAP, the agent's procedures, its limits and approvers, its settings and its tests. A standard operating procedure (SOP) is the written steps for one kind of case. This pattern is called **Agentic SOPs**. These pages say "procedure" for an SOP |
| **Cases** | One item of work each, such as one blocked invoice, with its full record |
| **Operator console** | The web app where people see cases, start runs, approve changes and edit procedures |
| **SAP layer** | What every SAP use case shares to reach SAP safely, including the check that refuses any SAP call that no use case has claimed |
| **AWS for SAP MCP Server** | An AWS server that turns an agent's tool calls into calls to SAP's OData web APIs. The SAP layer runs one per SAP system ([documentation](https://docs.aws.amazon.com/mcp-sap/latest/awsforsapmcp/introduction.html)) |
| **Platform** | The shared agent infrastructure on Amazon Bedrock AgentCore. It works without SAP and without any use case |

Every other term is in [Concepts](docs/start/concepts.md).

## Roles

Several teams build and run the Accelerator. A role is a job, not a person. In a small team,
one person can have several roles.

| Scope | Roles |
|---|---|
| **Platform** | Cloud engineer, security engineer, identity administrator |
| **SAP** | SAP functional consultant, SAP Basis administrator, ABAP developer |
| **Use case** | AI engineer, operator |
| **Pack** | Process owner and process analyst (procedures), control owner (controls), case worker, approver, auditor |

[Roles](docs/start/roles.md) gives each role's responsibilities and the group that grants it,
and links each use case's own roles page.

## Guides

| Goal | Start here |
|---|---|
| Understand it before you deploy | [Overview](docs/start/what-it-is.md): the included use cases, the three parts, cost and scope. Then the [FAQ](docs/start/faq.md) |
| Deploy it and run a first case | [Quick start](#quick-start): each use case's quickstart, or the [platform quickstart](docs/start/quickstart.md) for any use case |
| Use an AI coding assistant | The [`start-here`](sap_kit/skills/start-here/SKILL.md) skill: it finds out who you are and hands you to the skill that fits |
| Find the pages for your role | [Documentation by role](docs/start/next-steps.md) |
| Build your own use case | [New use cases](#new-use-cases), then the [platform interface](docs/PLATFORM_INTERFACE.md) a use case builds on |

## Documentation

- [Documentation index](docs/start/README.md): every page, grouped by part.
- Accelerator v0.1.0: [changelog](CHANGELOG.md).
- Upstream platform v0.1.0: [release notes](docs/releases/platform-v0.1.0.md),
  [support matrix](docs/SUPPORT_MATRIX.md) and [known limitations](docs/KNOWN_LIMITATIONS.md).
- [Security controls](docs/SECURITY_CONTROLS.md): every control and how to turn it on.
- [Posture tiers](docs/POSTURE_TIERS.md): the security and observability settings that the
  workshop, secure and production postures set, side by side.

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md). To report a security issue, follow
[SECURITY.md](SECURITY.md). Do not open a public issue.

## License

This project is licensed under the Apache-2.0 License. See [LICENSE](LICENSE) and
[NOTICE](NOTICE).

## Trademarks

SAP, SAP S/4HANA, SAP NetWeaver, SAP Business Technology Platform (SAP BTP), ABAP and other
SAP products and services mentioned in this repository are the trademarks or registered
trademarks of SAP SE or its affiliates in Germany and in other countries. This project is not
affiliated with, sponsored by or endorsed by SAP SE.
