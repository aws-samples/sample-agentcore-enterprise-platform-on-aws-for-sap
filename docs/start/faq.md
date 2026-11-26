# FAQ

Short answers, each with the page that has the detail. Each use case answers the questions
about itself on its own page:

<!-- use-cases:begin faq -->
- [SAP exception handling](../../use-cases/business-operations/docs/faq.md) (`business-operations`): Business process agents for SAP exception handling, on the shared platform's Gateway, Cedar engine and interceptor.
<!-- use-cases:end -->

## General

**Is this an AWS product? Who supports it?**
It is sample code, not an AWS product, and AWS Support does not cover it. AWS supports the AWS
services that it uses. You or your partner run and maintain the code in your own account.
[Overview](what-it-is.md#scope-and-limitations)

**What does it cost to run?**
You pay for the AWS services it uses. Most of the bill is usually the Amazon Bedrock model
calls that each agent run makes. Most of the rest is serverless and bills per request. Measure
both on a development deployment before you size production.
[Track costs per component](../PLATFORM_GUIDE.md#track-costs-per-component)

**Do I need an AI coding assistant?**
No. Every skill is written instructions that you can also follow by hand. An assistant does
the typing for you, and asks before it changes anything. The [quickstart](quickstart.md) is
written for a person.

**Is it ready for production?**
Not as it ships. First work through the
[production readiness checklist](../PRODUCTION_READINESS_PLAN.md) with your security, legal and
operations teams. Then work through the checklists of the SAP layer and of each use case.

## SAP

**Does it work with SAP ECC, or only SAP S/4HANA?**
Both. Set `type: ECC` on the system. A use case may read services that only SAP S/4HANA
publishes. On SAP ECC:

- The services a pack needs are built with the classic approach (transaction SEGW).
- The SAP Gateway may run on a separate hub system, which needs roles in both systems.
- OData (Open Data Protocol) V4 needs SAP NetWeaver 7.50 SP04 or later, which is ERP 6.0
  EHP8 or SAP S/4HANA. Earlier ECC systems support only OData V2.
[SAP layer settings](../../layers/sap/walkthrough.md#enable),
[SAP Gateway deployment and OData version](../../layers/sap/sap-setup.md#sap-gateway-deployment-and-odata-version)

**Does it need SAP Business Technology Platform (BTP) or SAP Cloud Connector?**
No. The agent, the Gateway and every function run on AWS. SAP is only the OData endpoint.
For RISE with SAP and private SAP systems, it connects over a private AWS route.
[SAP S/4HANA Cloud, SAP BTP and private SAP](../../layers/sap/walkthrough.md#network-paths-to-sap)

**Whose identity does SAP see?**
By default, SAP sees one service user per SAP system, so SAP's logs name that user. With the
`obo` (on behalf of) flow and
Microsoft Entra ID, SAP runs as the signed-in person. SAP calls this principal propagation.
[Which flow](../../layers/sap/walkthrough.md#sign-in-flows)

**Our SAP certificate comes from an internal certificate authority (CA). Will it work?**
No. The SAP endpoint needs a certificate from a publicly trusted CA, and the certificate must
name the host in `base_url`. On RISE with SAP, ask SAP for one.
[SAP setup](../../layers/sap/sap-setup.md#4-serve-https-smicm-strust)

**What does our SAP team have to set up?**
A service user and its roles, the OData services your use cases use, HTTPS, and a network path.
[SAP setup prerequisites](../../layers/sap/sap-setup.md#prerequisite-decisions)

## Data and security

**Where does our data go?**
Everything runs in your AWS account. Model calls go to Amazon Bedrock. The configured model can
be one of two kinds:

- A geographic cross-Region inference profile (such as `us.`), which keeps processing within
  that geography.
- A single-Region model.

Choose the one that your data residency rules allow.
[Data classification template](../DATA_CLASSIFICATION.md)

**Can we use our own identity provider?**
Yes: Microsoft Entra ID, Okta or Ping, federated through Amazon Cognito, or Microsoft Entra ID as
the token issuer with no Cognito.
[Enterprise identity provider (IdP) federation](../ENTERPRISE_IDP.md)

## SAP systems and removal

**Can it work with two SAP systems at once?**
Yes. Each system has its own server, target and secret. Each use case names the systems it
works with in its SAP access claim.
[Add a second SAP system](../../layers/sap/add-a-system.md)

**How do we remove it?**
Run `./scripts/deploy.sh destroy`. Then delete what the command does not own: each SAP
system's secret and, under `deployment.mode: production`, the data that it keeps on purpose.
Each use case says what else it leaves behind.
[Clean up](../PLATFORM_GUIDE.md#clean-up)

**How do we remove one use case and keep the platform?**
Destroy the use case's stacks while it is still in `platform.yaml`. Then delete its entries
from `platform.yaml` and run `./scripts/deploy.sh build`.
[Use case removal](../USE_CASE_REMOVAL.md)
