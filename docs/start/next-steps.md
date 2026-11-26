# Next steps

Find your role. Each row starts with the page to read first. The other pages give more
detail. [Roles](roles.md) says what each role is responsible for and which group grants it.
The [documentation index](README.md) lists every page by part.

## Platform roles

| You are | Read first | Then |
|---|---|---|
| **Cloud engineer** | [How it works](../how-it-works.md): design, build and verify | [Platform guide](../PLATFORM_GUIDE.md), [`platform.yaml` reference](../PLATFORM_YAML.md), [Architecture](../ARCHITECTURE.md), [Multi-account](../MULTI_ACCOUNT.md), the [quickstart](quickstart.md) |
| **Security engineer** | [Security controls](../SECURITY_CONTROLS.md): every control and how to turn it on | The threat models ([platform](../THREAT_MODEL.md), [SAP layer](../../layers/sap/threat-model.md)), the production checklists ([platform](../PRODUCTION_READINESS_PLAN.md), [SAP layer](../../layers/sap/production-checklist.md)) |
| **Identity administrator** | [Enterprise identity provider (IdP) federation](../ENTERPRISE_IDP.md) | [Roles](roles.md) for the groups to map, the `obo` (on behalf of) flow in [SAP trust](../../layers/sap/sap-trust.md) |

## SAP roles

| You are | Read first | Then |
|---|---|---|
| **SAP functional consultant** | [SAP layer](../../layers/sap/walkthrough.md): how a use case reaches SAP | Each use case's roles page, below, for what it needs from SAP |
| **SAP Basis administrator** | [SAP setup](../../layers/sap/sap-setup.md): the user, roles, services and certificate SAP needs | [SAP layer](../../layers/sap/walkthrough.md) for the sign-in flows and what reaches SAP, [SAP trust](../../layers/sap/sap-trust.md) for token-based sign-in, [adding a second system](../../layers/sap/add-a-system.md) |
| **ABAP developer** | [SAP events](../../layers/sap/reference/events.md): the outbox and what SAP sends | [Send events](../../layers/sap/sap-setup.md#5-send-events-optional) in SAP setup, [building a missing service](../../layers/sap/sap-setup.md#building-a-service-that-is-missing) |

## Use cases

| You are | Read first | Then |
|---|---|---|
| **AI engineer building a new use case** | [Use cases](../../use-cases/README.md) and [Contributing use cases](../../CONTRIBUTING_USE_CASES.md); for an SAP use case, the [`dev-add-sap-use-case`](../../sap_kit/skills/dev-add-sap-use-case/SKILL.md) skill | [Platform interface](../PLATFORM_INTERFACE.md), [`sap_kit`](../../sap_kit/README.md) for SAP use cases |

Each use case lists the pages for its own roles, and its pages for the platform and SAP roles
above:

<!-- use-cases:begin roles -->
- [SAP exception handling](../../use-cases/business-operations/docs/roles.md) (`business-operations`): Business process agents for SAP exception handling, on the shared platform's Gateway, Cedar engine and interceptor.
<!-- use-cases:end -->

## Before production

Work through the [production readiness checklist](../PRODUCTION_READINESS_PLAN.md) with your
security, legal and operations teams, then the
[SAP layer's](../../layers/sap/production-checklist.md) own checklist and the checklist of each
use case you turn on. Each use case's roles page links to its checklist.

## Workshops

The [participant guide](../PARTICIPANT_GUIDE.md) is a guided workshop on the platform, one
module at a time. It covers the platform only, not SAP.
