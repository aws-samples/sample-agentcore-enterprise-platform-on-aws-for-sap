# Overview

The Agentic Business Platform Accelerator for SAP on AWS is sample code. It gives an AI agent SAP
work that your team already does by hand. It deploys into your own AWS account.

## Included use cases

Each included use case is an example of that work. All of them are optional:

<!-- use-cases:begin -->
- [SAP exception handling](../../use-cases/business-operations/README.md) (`business-operations`): Business process agents for SAP exception handling, on the shared platform's Gateway, Cedar engine and interceptor.
- [Claude Code and Cowork on Amazon Bedrock](../../use-cases/claude-3p-on-bedrock/README.md) (`claude-3p-on-bedrock`): Claude Code and Claude Desktop/Cowork on Amazon Bedrock through Anthropic's Claude apps gateway, with Microsoft Entra ID sign-in and per-team model access, on the platform's private network.
- [hello-sap](../../use-cases/hello-sap/walkthrough.md) (`hello-sap`): Reference SAP use case — reads SAP through the SAP layer as its own Gateway client, and writes nothing.
<!-- use-cases:end -->

## The three parts

![The three parts in your AWS account: the platform (the Enterprise Agentic AI Platform Accelerator, on Amazon Bedrock AgentCore) at the bottom, the SAP layer above it with its SAP access claim check, and use cases on top. Two included, optional examples sit in dashed boxes: SAP exception handling, the complete one, with its business process agent, pack and procedures, and cases, and hello-sap, the smallest, which reads SAP and writes nothing. A third box is for your own use cases. The operator console connects to SAP exception handling](three-parts.drawio.png)

| Part | What it is | Who usually owns it |
|---|---|---|
| **The platform** | An enterprise agent platform on Amazon Bedrock AgentCore: Runtime, Gateway, Identity, Memory, observability and the security controls you turn on. It is deployed with the AWS Cloud Development Kit (AWS CDK) from one `platform.yaml` file. It is the [Enterprise Agentic AI Platform Accelerator](https://github.com/aws-samples/sample-agentcore-enterprise-platform) | Your cloud or platform team |
| **The SAP layer** | One AWS for SAP Model Context Protocol (MCP) Server per SAP system, connected to the Gateway. The `sap-events` bus that SAP sends its events to. A check that refuses any SAP call that no use case has claimed | Your cloud team with your SAP Basis team |
| **The use cases** | One business process each. The [included use cases](#included-use-cases) are examples, and all of them are optional | Your process owner, with a developer for new processes |

[The whole architecture](architecture/architecture.light.drawio.png) shows what each part
deploys. This figure and the architecture use the included use cases as their example.

## Scope and limitations

- **Not an AWS product, and not supported by AWS Support.** AWS supports the AWS services that
  it uses. You or your partner run and maintain the code in your account.
- **Not ready for production as it ships.** It is a baseline that you adapt and review with
  your security and legal teams. The
  [production readiness checklist](../PRODUCTION_READINESS_PLAN.md) lists what to complete.
- **Not a replacement for your SAP system's own controls.** SAP still checks the
  authorizations of the user the agent signs in as.

## Cost

You pay for the AWS services it uses, at their normal prices. Two kinds of cost matter:

- **Cost per run:** each agent run calls an Amazon Bedrock model. This is usually the
  largest cost. Each use case says when its runs start and what limits their cost.
- **Running cost while idle:** most parts are serverless and bill per request. The parts that
  bill while idle are optional:
  - the NAT gateway and the Amazon Virtual Private Cloud (VPC) endpoints, when networking is on
  - Amazon CloudWatch logs

Measure both on your own workload before you size it.
[Track costs per component](../PLATFORM_GUIDE.md#track-costs-per-component) shows how. Each
use case lists the parts of its own bill.

## Next steps

- [Concepts](concepts.md): terminology.
- [Quickstart](quickstart.md): deploy the platform, the SAP layer and a use case.
- [Next steps](next-steps.md): documentation by role.
- [FAQ](faq.md): support, cost, SAP and data handling.
