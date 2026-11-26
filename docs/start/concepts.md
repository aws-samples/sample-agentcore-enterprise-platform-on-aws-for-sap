# Concepts

The words that the other pages use, in the order you meet them. Each entry links to the page
that covers it in depth. The last column gives the name that you see in configuration, commands
or code, where that name is different.

Each use case defines its own terms, such as its cases and its controls, in its own glossary:

<!-- use-cases:begin glossary -->
- [SAP exception handling](../../use-cases/business-operations/docs/glossary.md) (`business-operations`): Business process agents for SAP exception handling, on the shared platform's Gateway, Cedar engine and interceptor.
<!-- use-cases:end -->

## Building blocks

![Concept nesting: the platform contains the use cases and the SAP layer; the SAP exception handling use case contains its packs; the finance_ops pack contains its general instructions and its process types, each mapped to one procedure: invoice_matching to the triage procedure, six exception types to one exception procedure each, and short_pay to a shared procedure. hello-sap has no packs](concept-nesting.drawio.png)

The figure uses the included use cases as its example.

| Term | What it means | In config or code |
|---|---|---|
| **Platform** | The shared agent infrastructure on Amazon Bedrock AgentCore: sign-in, the Gateway, agent runtimes, memory, observability and security controls. It works with no use case turned on. [How it works](../how-it-works.md) | |
| **Use case** | One business process automated on the platform, and everything it deploys. Several use cases share one platform: its Gateway, its user pool and its interceptor. Every use case is optional. [Use cases](../../use-cases/README.md) | One entry under `use_cases:` |
| **Rules for sharing the platform** | Three rules that keep the platform usable by any use case: the platform works with every use case turned off, core changes are generic, and a use case's controls touch only its own resources. [Platform sharing rules](../../AGENTS.md#rules-for-sharing-the-platform) | |
| **Layer** | Shared resources that several use cases build on. The SAP layer is the first. [Layers](../../layers/README.md) | `layers:` |
| **Business process agent** | The agent inside a use case that does the work, often by following written procedures. A use case can have one, several or none. Not the same as a coding agent (see Skill). | |
| **Gateway** | The AgentCore Gateway. Every tool an agent calls goes through it, so every call is checked in one place. Not the SAP Gateway (see [SAP terms](#sap-terms)). [Gateway](../modules/gateway.md) | |
| **Gateway target** | One set of tools behind the Gateway. Each SAP system is a target named `sap-<system>`, such as `sap-s4`. A tool's full name is the target, three underscores and the tool, such as `sap-s4___read_entity`. | `<target>___<tool>` |
| **AWS for SAP MCP Server** | The AWS server that turns a tool call into an OData call to SAP. The SAP layer runs one per SAP system. [SAP layer](../../layers/sap/walkthrough.md) | |

## Platform services

| Term | What it means | In config or code |
|---|---|---|
| **Runtime** | AgentCore Runtime, which runs each agent as a container and scales it. [Runtime](../modules/runtime.md) | `agents:` |
| **Memory** | AgentCore Memory, which stores an agent's conversation (short-term) and what it learns across conversations (long-term). [Memory](../modules/memory.md) | |
| **Sign-in (auth stack)** | Who can sign in, and the tokens they get. In **brokered** mode, the default, an Amazon Cognito user pool issues the tokens. In **direct** mode, Microsoft Entra ID issues every token and there is no user pool. [Auth](../modules/auth.md) | `identity.mode` |
| **Token vault (identity stack)** | AgentCore Identity: the credentials an agent uses to call other services, so the agent never stores a client secret itself. A different stack from sign-in, despite the similar name. [Identity](../modules/identity.md) | |
| **Interceptor** | The platform's one Gateway function. It sees every Gateway call before the call reaches a tool. A layer or a use case adds its own checks to it, such as the SAP layer's check. The platform's egress filter, also in the interceptor, masks sensitive data; it does not decide who may call what. [Interceptor checks](../../CONTRIBUTING_USE_CASES.md#checks-in-the-gateway-interceptor), [Gateway call sequence](../modules/gateway.md#call-sequence) | `gateway.interceptor_extensions` |
| **Cedar** | The policy language the Gateway's policy engine uses to allow or refuse tool calls. It is off by default, and starts in `LOG_ONLY` when turned on. In `LOG_ONLY` it refuses nothing. In `ENFORCE` it refuses every call that no policy permits. This stops the clients of one use case from calling the tools of another use case. The quickstart sets `ENFORCE`, and production mode requires it. The interceptor checks, such as the SAP layer's check, refuse calls in every Cedar mode, including when Cedar is off. [Cedar authorization](../SECURITY_CONTROLS.md#cedar-authorization-item-3) | `security.cedar` |

## Configuration and deployment

| Term | What it means | In config or code |
|---|---|---|
| **`platform.yaml`** | The one configuration file for a deployment. `deploy.sh design` writes it from a preset and checks it. An unknown key stops the deploy. [platform.yaml reference](../PLATFORM_YAML.md) | |
| **Preset** | A starting `platform.yaml` in `presets/`, such as `production` or `federated`. A preset is a set of settings that work together, so you do not have to choose each setting yourself. The commands call it a profile. [Platform guide](../PLATFORM_GUIDE.md) | `--profile <name>` |
| **Synth** | The step where the AWS Cloud Development Kit (CDK) turns the Python stacks into CloudFormation templates. Many configuration checks run here, before anything is deployed. | `cdk synth`, `deploy.sh synth` |
| **Footprint** | The set of core stacks that one deployment contains, such as `gateway` and `auth`. It depends on the configuration, the deployment strategy and the account's role in a federated setup. `verify` reports its result for the footprint. | |
| **Environment** | One deployed copy of the platform, named in the stack prefix, so one account can contain several copies side by side. Not a stage. | `environment` |
| **Deployment mode** | `workshop` keeps resources easy to delete. `production` keeps stateful resources and refuses a configuration that lacks enterprise sign-in, networking, audit, authorization, guardrails or alarms. | `deployment.mode` |
| **Stage** | The development, quality or production step a change moves through. Each stage is its own deployment with its own SAP system. A use case says how its changes move between stages. | |
| **Role** | A job someone does, such as cloud engineer or SAP Basis administrator, and the group that grants it. Each use case adds its own roles. [Roles](roles.md) | |
| **Skill** | Instructions for an AI coding agent (such as Claude Code or Kiro) that walk it through a task in this repository, like deploying or adding a use case. The `start-here` skill finds the right one. [start-here](../../sap_kit/skills/start-here/SKILL.md) | |
| **Use-case contract** | What a use case must provide so the platform can deploy and check it: a manifest, its stacks and a `verify.py` that can fail. [Use-case contract](../../CONTRIBUTING_USE_CASES.md) | `manifest.yaml` |

## SAP access

| Term | What it means | In config or code |
|---|---|---|
| **SAP access claim** | The SAP systems, operations and writes a use case says it will use, like the authorizations an SAP role requests. The SAP layer refuses any SAP call that no use case has claimed. Not a token (JWT) claim. [Use case access](../../layers/sap/walkthrough.md#use-case-access) | `claim` |
| **Access row** | The operator's upper limit for one use case in the SAP layer: which SAP systems it may read and write. The use case's SAP access claim must fit inside it. The two are separate so that a use case cannot give itself more SAP access than the operator allows. [SAP layer access](../../layers/sap/walkthrough.md#enable) | `layers.sap.access` |
| **`bypass_gateway`** | The SAP systems that a use case's own workers, such as a poller, call directly, as the system's service account, outside the Gateway. [Access paths](../../layers/sap/walkthrough.md#access-paths) | `layers.sap.access` |
| **System role** | What one SAP system is for: `sandbox`, `development`, `quality` or `production`. Only a `sandbox` may receive test data. | `system_role` |
| **Flow** | How the server signs in to SAP, and so whose authorizations SAP checks. `basic`, `m2m-sap` and `m2m-oidc` use a service account. `obo` uses the signed-in person, which SAP calls principal propagation. [Sign-in flows](../../layers/sap/walkthrough.md#sign-in-flows) | |

## SAP terms

Words that mean something different in SAP and in these pages.

| SAP term | In SAP | In these pages |
|---|---|---|
| **SAP Gateway** | The SAP component that publishes OData services. | "The Gateway" alone always means the AgentCore Gateway. |
| **Client** | An SAP client, such as `100`: one set of business data in a system. | An app client: an OAuth client that signs in to the platform, such as the use case's reader or writer. |
| **Landscape** | The whole set of systems, such as DEV, QAS and PRD. | Each system has a system role. |
| **Release** | Removing a block, such as releasing a sales order from a credit block. | The same, when the agent releases a block. A pack moves between stages as a published version, not a release. |
| **Service binding** | The ABAP object that publishes a service as OData. | The same. The case document check is a different thing. |
| **Role** | A set of SAP authorizations (PFCG). | A job someone does, and the group that grants it. |

## Acronyms

| Acronym | Meaning |
|---|---|
| 3LO | Three-legged OAuth: a person signs in to a third-party service and lets an agent call it for them |
| A2A | Agent2Agent, a protocol for agents calling other agents |
| ABAP | SAP's programming language |
| ADT | ABAP Development Tools, the Eclipse IDE for ABAP |
| AG-UI | Agent-User Interaction protocol, which streams an agent's run to a web page |
| BAdI | Business Add-In, an SAP extension point |
| BTP | SAP Business Technology Platform |
| CA | Certificate authority, which issues TLS certificates |
| CDK | AWS Cloud Development Kit, which builds the stacks from code |
| CDS | Core Data Services, SAP's data model views |
| CI/CD | Continuous integration and continuous delivery |
| CMK | Customer managed key, an AWS KMS key that you own |
| IAM | AWS Identity and Access Management |
| IdP | Identity provider, such as Microsoft Entra ID |
| JWT | JSON Web Token, the format of a sign-in token |
| KMS | AWS Key Management Service |
| M2M | Machine to machine: an app signing in as itself, with no person |
| MCP | Model Context Protocol, how an agent discovers and calls tools |
| OBO | On behalf of: calling a service as the signed-in person |
| OData | Open Data Protocol, the REST protocol SAP services use |
| OIDC | OpenID Connect, a sign-in standard built on OAuth |
| OU | Organizational unit, a group of accounts in AWS Organizations |
| RAP | ABAP RESTful Application Programming Model |
| SCP | Service control policy, an AWS Organizations policy that limits what accounts may do |
| SOP | Standard operating procedure |
| SSM | AWS Systems Manager. Its Parameter Store contains the values that the platform publishes for use cases |
| VPC | Amazon Virtual Private Cloud, a private network in AWS |
