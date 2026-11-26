<!-- markdownlint-disable MD041 -->

# The platform

A deployable platform for running AI agents under enterprise governance on
**Amazon Bedrock AgentCore** — authentication, identity, tool gateway, memory,
observability, and security controls, driven by one declarative configuration
file. The default workshop path deploys in one AWS account. Multi-account and
migration paths have additional customer prerequisites. Your team designs it, builds it and a first use case on it,
verifies it, and owns it.

> Agents are easy to prototype and hard to run. The platform is the part
> that's hard: the governed base your agents deploy onto.

## What you get

| Layer | What's deployed |
|---|---|
| **Identity** | Your choice, declared in Design: Amazon Cognito as the token issuer with your corporate identity provider (IdP) (Entra ID, Okta, Ping) federated in — or Entra ID as the issuer directly, with no Cognito deployed |
| **Agents** | AgentCore Runtime hosting your framework — 7 patterns: Strands, LangGraph, Claude Agent SDK (single + multi), AG-UI (Agent-User Interaction protocol), orchestrator |
| **Tools** | AgentCore Gateway (Model Context Protocol, MCP): the platform governs tools, instead of tools being code built into agents |
| **Memory** | AgentCore Memory — conversation state that survives restarts, with optional long-term memory |
| **Governance** | Model allow-list and guardrailed-only inference **enforced by AWS Identity and Access Management (IAM)**, Cedar tool authorization, audit alerting |
| **Operations** | Vended logs, traces, CloudWatch alarms and a platform dashboard, plus a `verify` command that tests every claim your configuration makes |

## Design → Build → Verify

1. **Design.** Pick a profile, or write your own `platform.yaml`: single or
   multi-account, your IdP and who issues tokens, your agent framework, the
   controls you need. `deploy.sh usecase new <name>` scaffolds your
   application into the same file. `deploy.sh design` validates it and prints
   the exact stacks it produces — nothing is deployed yet.
2. **Build.** `deploy.sh build` deploys the platform and every use case in
   the design, with one command.
3. **Verify.** `deploy.sh verify` re-tests every promise the design made,
   platform and use cases alike, and exits non-zero on any failure.

## Two ways in

- **"I don't have a platform — how do I start fast?"** Design from the
  greenfield profile and deploy a working, governed agent platform.
  Select the required security controls during design; each one is an explicit
  configuration flag that also drives verification.
- **"I have agents on other platforms — how do I migrate?"** Keep your
  existing container and framework. The migration profile places a compatible
  arm64 container behind an adapter on AgentCore Runtime. It does not
  automatically extract tools or state, create private connectivity, copy
  customer data, or move triggers and traffic. Those changes remain explicit,
  customer-operated stages with evidence and approval steps. Follow the
  [EBA migration runbook](MIGRATION_RUNBOOK.md) (EBA: AWS Experience-Based Acceleration, a
  hands-on workshop format).

The repository's use cases run on this platform, and its SAP use cases are built on the
[SAP layer](../layers/sap/walkthrough.md):

<!-- use-cases:begin -->
- [SAP exception handling](../use-cases/business-operations/README.md) (`business-operations`): Business process agents for SAP exception handling, on the shared platform's Gateway, Cedar engine and interceptor.
- [Claude Code and Cowork on Amazon Bedrock](../use-cases/claude-3p-on-bedrock/README.md) (`claude-3p-on-bedrock`): Claude Code and Claude Desktop/Cowork on Amazon Bedrock through Anthropic's Claude apps gateway, with Microsoft Entra ID sign-in and per-team model access, on the platform's private network.
- [hello-sap](../use-cases/hello-sap/walkthrough.md) (`hello-sap`): Reference SAP use case — reads SAP through the SAP layer as its own Gateway client, and writes nothing.
<!-- use-cases:end -->

Read [How it works](how-it-works.md), then go to
[Getting started](PLATFORM_GUIDE.md#getting-started).

Before a customer session, review the upstream platform's
[v0.1.0 release notes](releases/platform-v0.1.0.md), [support matrix](SUPPORT_MATRIX.md), and
[known limitations](KNOWN_LIMITATIONS.md), then run the read-only
`./scripts/deploy.sh doctor` preflight against the completed design.

## What makes it different

Core platform-managed paths have been **verified live** in the repository's
development environment. The platform ships a `verify` command that re-tests
the promises selected by your configuration and exits non-zero on failure. If
the docs say inference without a guardrail is denied, that denial is checked
by IAM simulation on your actual runtime role, not asserted in prose.

Customer-owned migration work—data procedures, existing private connectivity,
event sources, and traffic routing—is deliberately not executed by the
platform. Its readiness commands validate the recorded plan, evidence,
rollback, and approvals; the customer remains responsible for execution and
acceptance.

## Honest scope

This is sample code, not an AWS product, and AWS Support does not cover it. Agent
*quality* evaluation and per-agent model cost attribution are not included. Run
your own security review before production use.
