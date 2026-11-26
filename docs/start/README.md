# Documentation index

The repository has three parts. The documentation for each part is next to its code. Start
with the [overview](what-it-is.md), then the [quickstart](quickstart.md).

| Part | What it is | Where its pages live |
|---|---|---|
| **The use cases** | Optional examples, one business process each. [Use cases](#use-cases) lists them | `use-cases/<name>/`, each with its own pages |
| **The SAP layer** | One AWS for SAP Model Context Protocol (MCP) Server per SAP system, its Gateway target, and the check that refuses an SAP call that no use case has claimed | `layers/sap/`, starting at its [walkthrough](../../layers/sap/walkthrough.md) |
| **The platform** | The Amazon Bedrock AgentCore platform everything runs on. It works without SAP | `docs/`, starting at its [overview](../home.md) |

## Getting started

- [Overview](what-it-is.md): the included use cases, the three parts, cost
- [Concepts](concepts.md): terminology
- [Quickstart](quickstart.md): deploy and verify the platform, the SAP layer and a use case
- [Next steps](next-steps.md): documentation by role
- [Roles](roles.md): who does what, by scope, and which group grants each role
- [FAQ](faq.md)

## Use cases

The included use cases, each with its own pages:

<!-- use-cases:begin index -->
- [SAP exception handling](../../use-cases/business-operations/README.md) (`business-operations`): Business process agents for SAP exception handling, on the shared platform's Gateway, Cedar engine and interceptor.
  Pages: [Overview](../../use-cases/business-operations/README.md), [Quickstart](../../use-cases/business-operations/docs/quickstart.md), [Walkthrough](../../use-cases/business-operations/walkthrough.md), [The operator console](../../use-cases/business-operations/docs/console.md), [Case lifecycle](../../use-cases/business-operations/docs/lifecycle.md), [Process owner guide](../../use-cases/business-operations/docs/for-process-owners.md), [Packs and SOPs](../../use-cases/business-operations/docs/packs.md), [Security](../../use-cases/business-operations/docs/security.md), [Controls and evidence](../../use-cases/business-operations/docs/controls-and-evidence.md), [Operations](../../use-cases/business-operations/docs/operating.md), [Configuration](../../use-cases/business-operations/docs/configuration.md), [Architecture](../../use-cases/business-operations/docs/architecture.md), [Production checklist](../../use-cases/business-operations/docs/production-checklist.md), [Glossary](../../use-cases/business-operations/docs/glossary.md), [Roles](../../use-cases/business-operations/docs/roles.md), [FAQ](../../use-cases/business-operations/docs/faq.md), [Skills](../../use-cases/business-operations/skills/README.md)
- [Claude Code and Cowork on Amazon Bedrock](../../use-cases/claude-3p-on-bedrock/README.md) (`claude-3p-on-bedrock`): Claude Code and Claude Desktop/Cowork on Amazon Bedrock through Anthropic's Claude apps gateway, with Microsoft Entra ID sign-in and per-team model access, on the platform's private network.
  Pages: [Network, Entra ID and devices](../../use-cases/claude-3p-on-bedrock/docs/walkthrough.md), [Changes](../../use-cases/claude-3p-on-bedrock/docs/changes.md)
- [hello-sap](../../use-cases/hello-sap/walkthrough.md) (`hello-sap`): Reference SAP use case — reads SAP through the SAP layer as its own Gateway client, and writes nothing.
<!-- use-cases:end -->

To build your own:

- [Use cases](../../use-cases/README.md): what a use case is, and how to start one
- [Add an SAP use case](../../sap_kit/skills/dev-add-sap-use-case/SKILL.md): create one that reads SAP through the SAP layer

## The SAP layer

- [The SAP layer](../../layers/sap/walkthrough.md): enable it, choose a sign-in flow, connect a use case
- [SAP layer reference](../../layers/sap/reference/README.md): every setting, resource, check and failure mode
- [SAP setup](../../layers/sap/sap-setup.md): for your SAP team
- [SAP trust and token sign-in](../../layers/sap/sap-trust.md)
- [Add a second SAP system](../../layers/sap/add-a-system.md)
- [Gateway, direct Lambda, or self-hosted MCP](../../layers/sap/choices.md)
- [Production checklist](../../layers/sap/production-checklist.md)
- [Layers](../../layers/README.md): what a layer is

## The platform

- [Overview](../home.md) and [How it works](../how-it-works.md): design, build, verify
- [Platform guide](../PLATFORM_GUIDE.md): deploy and extend the platform itself
- Design: [`platform.yaml` reference](../PLATFORM_YAML.md), [Architecture](../ARCHITECTURE.md),
  [Multi-account](../MULTI_ACCOUNT.md), [Security controls](../SECURITY_CONTROLS.md),
  [Verify caller identity](../IDENTITY.md), [Enterprise identity provider (IdP) federation](../ENTERPRISE_IDP.md)
- Build: [Modules](../modules/README.md), [Add Gateway tools](../GATEWAY_TARGETS.md),
  [Build a use case](../PLATFORM_INTERFACE.md), [Repository structure](../REPOSITORY_STRUCTURE.md)
- Migrate: [Migration runbook](../MIGRATION_RUNBOOK.md)
- Verify: [Testing](../TESTING.md), [Tracing](../TRACING.md), [Troubleshooting](../TROUBLESHOOTING.md)
- Own: [Upstream platform release notes](../releases/platform-v0.1.0.md),
  [Agentic Business Platform Accelerator for SAP on AWS changelog](../../CHANGELOG.md),
  [Support matrix](../SUPPORT_MATRIX.md),
  [Known limitations](../KNOWN_LIMITATIONS.md), [Participant guide](../PARTICIPANT_GUIDE.md),
  [Production readiness checklist](../PRODUCTION_READINESS_PLAN.md)
- Templates for your review: [Threat model](../THREAT_MODEL.md),
  [Data classification](../DATA_CLASSIFICATION.md), [Service level objective (SLO), recovery time objective (RTO) and recovery point objective (RPO)](../SLO_RTO_RPO.md),
  [Operating model](../OPERATING_MODEL.md), [Risk register](../RISK_REGISTER.md)
