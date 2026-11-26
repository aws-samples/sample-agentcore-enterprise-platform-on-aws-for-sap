<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Use cases and layers

Terms are defined in [Concepts](../../docs/start/concepts.md).

![The whole architecture: the use case's poller reads SAP and makes one case per SAP document (SAP can also push events through the SAP layer's bus, which is optional), the agent runs on the platform, and every tool call passes the Gateway's checks on its way back to SAP](../../docs/start/architecture/architecture.light.drawio.png)

The [platform](../../docs/ARCHITECTURE.md) runs unchanged below two more tiers. Both use the
platform only through its [published interface](../../docs/PLATFORM_INTERFACE.md):

- SSM parameters (AWS Systems Manager Parameter Store)
- OAuth app clients
- the Gateway, with its Cedar policy engine and its interceptor

The two tiers are these:

- **Layers** (`layers/<name>/`) contain what several use cases share. Each layer stack deploys
  after the stacks that publish the SSM parameters it reads, as a use-case stack does. So a
  use-case stack that reads a layer deploys after that layer. `layers/sap/` is the layer that
  ships. It deploys:
  - one AWS for SAP MCP (Model Context Protocol) Server runtime per SAP system, each behind a
    Gateway target `sap-<system>`
  - the credentials for each system, in AWS Secrets Manager
  - an interceptor check that refuses an SAP call that no use case's SAP access claim names

  The layer adds no Cedar permit. Each use case permits its own app clients. See the layer's
  [walkthrough](walkthrough.md).
- **Use cases** (`use-cases/<name>/`) are the business process agents. Each has its own stacks,
  its own Gateway targets and its own OAuth app clients. A use case that reaches SAP also has an
  SAP access claim on the SAP layer, which names the apps and operations it uses.

The use cases in this checkout that have their own pages:

<!-- use-cases:begin -->
- [SAP exception handling](../../use-cases/business-operations/README.md) (`business-operations`): Business process agents for SAP exception handling, on the shared platform's Gateway, Cedar engine and interceptor.
- [Claude Code and Cowork on Amazon Bedrock](../../use-cases/claude-3p-on-bedrock/README.md) (`claude-3p-on-bedrock`): Claude Code and Claude Desktop/Cowork on Amazon Bedrock through Anthropic's Claude apps gateway, with Microsoft Entra ID sign-in and per-team model access, on the platform's private network.
- [hello-sap](../../use-cases/hello-sap/walkthrough.md) (`hello-sap`): Reference SAP use case — reads SAP through the SAP layer as its own Gateway client, and writes nothing.
<!-- use-cases:end -->

A use case reaches SAP by two paths:

- **The agent's tool calls** go through the Gateway to the `sap-<system>` targets. There, the SAP
  layer's check and the use case's own check each decide. Under `ENFORCE`, Cedar decides too.
- **The use case's own functions** call SAP directly over OData (Open Data Protocol) with the
  system's service account, such as a poller, the consumer of its SAP event queue, or a
  test-data seeder. They may do this only for a system that the operator listed in that use
  case's `bypass_gateway` access. These calls pass none of the Gateway's checks. The direct path
  exists because no model and no run is behind these calls.

> **SAP API Policy.** Use of SAP APIs is subject to the
> [SAP API Policy](https://help.sap.com/doc/sap-api-policy/latest/en-US/API_Policy_latest.pdf)
> and your agreements with SAP. Review both before you connect this sample to an SAP system.
