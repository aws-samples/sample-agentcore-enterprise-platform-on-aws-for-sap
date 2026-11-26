<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Roles

Who does what in the Agentic Business Platform Accelerator for SAP on AWS, and what gives each
role its access today. A role is a job, not a person. In a small team, one person can have
several roles. [Next steps](next-steps.md) lists the pages that each role reads first.

Roles come in scopes, from the widest to the narrowest. This page covers the two widest:

| Scope | Covers | Roles |
|---|---|---|
| **Platform** | One deployment in one AWS account, and every use case on it | Cloud engineer, security engineer, identity administrator |
| **SAP** | One SAP system and what the SAP layer needs from it | SAP functional consultant, SAP Basis administrator, ABAP developer |

Each use case adds its own roles in narrower scopes, and gives the group that grants each one:

<!-- use-cases:begin roles -->
- [SAP exception handling](../../use-cases/business-operations/docs/roles.md) (`business-operations`): Business process agents for SAP exception handling, on the shared platform's Gateway, Cedar engine and interceptor.
<!-- use-cases:end -->

## Platform roles

| Role | Responsible for | Access today |
|---|---|---|
| **Cloud engineer** | The AWS accounts, the continuous integration and delivery (CI/CD) pipeline and `platform.yaml`. Deploying, verifying and removing the platform, the SAP layer and the use cases. Networking, encryption keys and alarms | AWS credentials that may deploy in the account |
| **Security engineer** | The security controls (Cedar, Amazon Bedrock Guardrails, Amazon Virtual Private Cloud (VPC) isolation, AWS Key Management Service (AWS KMS), AWS CloudTrail, service control policies (SCPs)), the threat models, the security scans and the production readiness checklist | Read access to the account |
| **Identity administrator** | Federation with the corporate identity provider, and the mapping from its groups to the user pool's groups. This mapping decides who has each role that a use case grants through a group | The identity provider, and `identity.group_map` in `platform.yaml` |

## SAP roles

| Role | Responsible for | Access today |
|---|---|---|
| **SAP functional consultant** | What an exception is in SAP: which documents and fields mark it (for example, a payment block reason), and which SAP change clears it. Designs which SAP data a use case watches, with the process owner and the AI engineer | Display access in SAP |
| **SAP Basis administrator** | The service user and its authorizations (transactions SU01 and PFCG). Activating the OData (Open Data Protocol) services. HTTPS and certificates, token-based trust, and the event drain job. Moving all of it between SAP systems. Many SAP teams give the authorizations to a separate SAP security administrator | SAP administration |
| **ABAP developer** | The code that SAP runs for the Accelerator: the Business Add-In (BAdI) implementation that records an event in the SAP layer's outbox, and any OData service that a use case needs and SAP does not ship | An SAP development system |

The SAP team's checklist is [SAP setup](../../layers/sap/sap-setup.md). The event code is
described in [SAP events](../../layers/sap/reference/events.md).
