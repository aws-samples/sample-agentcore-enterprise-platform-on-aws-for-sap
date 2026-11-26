# SAP setup

Terms are defined in [Concepts](../../docs/start/concepts.md).

> Before you connect to an SAP system, review the
> [SAP API Policy](https://help.sap.com/doc/sap-api-policy/latest/en-US/API_Policy_latest.pdf) and
> your agreements with SAP. Programmatic or agent access to SAP can have licensing
> implications (for example, SAP Digital Access); review them with your SAP account team.

This page is the SAP side of one system in `layers.sap.systems`: the user SAP runs as, its
authorizations, the OData (Open Data Protocol) services, and HTTPS. Every flow needs it. The
`m2m-sap`, `m2m-oidc` and `obo` flows also need SAP to trust a token, which
[sap-trust.md](sap-trust.md) covers. The AWS side is in the [layer walkthrough](walkthrough.md).

"The Gateway" on other pages means the AgentCore Gateway. On this page, SAP's component is always
called the SAP Gateway.

The steps assume SAP S/4HANA, on premises or private cloud. On SAP ERP Central Component (ECC)
the steps are similar, with three differences:

- the SAP Gateway may run on a separate hub system
- services are built with the SAP Gateway Service Builder (transaction SEGW)
- OData V4 may not exist

See [SAP Gateway deployment and OData version](#sap-gateway-deployment-and-odata-version).

## Prerequisite decisions

Answer these questions with your SAP team before anyone deploys. If any answer is missing, the
first case cannot run.

| Question | What the quickest path needs | Detail |
|---|---|---|
| Which SAP client? | A **sandbox** SAP client (such as `100`). The test-data seeder posts real documents | `system_role` in the [layer walkthrough](walkthrough.md#enable) |
| Who does SAP see? | One **System** user (no dialog logon) with the roles in step 2. Every change the business process agent makes is logged under this user | [Steps 1 and 2](#1-create-the-service-user-su01), [whose roles SAP checks](#authorization-checks-in-sap) |
| Which services? | For the shipped `finance_ops` pack: `API_SUPPLIERINVOICE_PROCESS_SRV`, `API_PURCHASEORDER_PROCESS_SRV`, `API_MATERIAL_DOCUMENT_SRV`, and `FAP_VENDOR_LINE_ITEMS_SRV` for the seeder, all active | [Step 3](#3-activate-the-odata-services) |
| Can AWS reach SAP? | An HTTPS endpoint that the agent runtime can reach. Without `security.networking`, the runtime has a public network path, so SAP must be reachable from the internet. With it, SAP is reached over your private route | [Network paths](walkthrough.md#network-paths-to-sap) |
| Which certificate? | One from a publicly trusted certificate authority (CA), naming the host in `base_url` | [Step 4](#4-serve-https-smicm-strust) |
| Is the sample data there? | For the shipped pack's test data: SAP's S/4HANA sample data, company code `1710` (supplier `USSU-VSF04`, material `MZ-RM-R300-01`). Without it, post the first exception by hand | The first case in each use case's own quickstart ([Use cases](../../use-cases/README.md)) |
| Is a posting period open? | The current period open for the pack's company code, so the seeder can post | `OB52` for Financial Accounting (FI), `MMPV` for Materials Management (MM) |
| May the agent write? | Writes are on by default, except on an `obo` system. Set `writes: []` on the system to start read-only | [Writes, by flow](walkthrough.md#writes-by-flow) |
| How much load? | A use case's poller reads every open exception that its packs match again on each run, for example every 15 minutes. A shorter schedule multiplies the load | Each use case's own poll schedule ([Use cases](../../use-cases/README.md)) |
| Licensing? | Review SAP Digital Access and your agreements with your SAP account team | The note at the top of this page |

## SAP Gateway deployment and OData version

Two facts about the SAP system decide which roles, transactions and services in the steps
below apply. Find them out first, because a role that is correct for one combination returns 403
on another.

**Where the SAP Gateway runs.** SAP Gateway Foundation (`SAP_GWFND`) is part of the standard
stack from SAP NetWeaver 7.4. Older ECC systems need the SAP Gateway add-on components instead.
SAP's OAuth note for releases below 7.40 covers NetWeaver 7.02 SP14 and 7.31 SP05 (SAP Note
1797103), so the add-on runs on releases at least that old. Confirm your release with your Basis
team. In the table, EHP is an SAP enhancement package and SP is a support package.

| SAP ERP 6.0 | Runs on `SAP_BASIS` | SAP Gateway |
|---|---|---|
| EHP0 to EHP3 | 7.00 | Add-on components |
| EHP4 | 7.01 | Add-on components |
| EHP5 | 7.02 | Add-on components |
| EHP6 | 7.31 (7.40 on HANA) | Add-on components on 7.31; standard on 7.40 |
| EHP7 | 7.40 | `SAP_GWFND`, standard |
| EHP8 | 7.50 | `SAP_GWFND` 7.50, a required component; OData V4 from SP04 |

EHP8 shipped with `SAP_BASIS` 7.50 SP01, so V4 needs a later support package there. Where the
table says add-on, the SAP Gateway is not part of the stack, and a hub system is often the
simpler route.

| Deployment | Services are registered and published in | Roles needed in |
|---|---|---|
| Embedded | The ERP or S/4HANA system itself | That one system |
| Hub, service built in the backend | The hub; the service lives in the backend | The hub and the backend |
| Hub, service built on the hub | The hub, which keeps its own copy of the metadata | The hub and the backend |

SAP recommends building on the hub for releases before NetWeaver 7.50 SP04, and when the
backend must not be changed. On a hub, the service is added with a system alias that routes to
the backend. The authorization is checked against `IWSG` on the hub and `IWSV` on the backend, so
the agent's SAP user needs a role in both.

The usual trust between them is a trusted RFC (Remote Function Call) connection. The backend is
the trusting system. It has an RFC destination for the hub, which is the trusted system. SAP's
task list `SAP_SAP2GATEWAY_TRUSTED_CONFIG` sets this up. The backend then checks `S_RFCACL` for
the calling hub, with activity 16:

- the hub's system ID and SAP client
- either the same user ID (`RFC_EQUSER` yes) or a named calling user

In our reading, `base_url` points at the system that serves the OData endpoint, which is the hub.
SAP's documentation does not state this directly.

**Which OData version the service speaks.** V2 and V4 are authorized by different objects, so
a V2 role grants nothing on a V4 service:

| | OData V2 | OData V4 |
|---|---|---|
| Authorization object | `S_SERVICE` | `S_START` |
| Repository objects (hub / backend) | `IWSG`, `IWOM` / `IWSV` | `IWSG`, `IWOM` / `G4BA`, `G4BS` |
| Granularity | One service, stored as a hash | One service group, stored by name |
| Authority checks per call | 2, even when co-deployed | 1 |
| Can `*` be limited to OData? | No | Yes: `S_START` with `G4BA` |
| Activation | `/IWFND/MAINT_SERVICE` | `/IWFND/V4_ADMIN` |
| First release | Any with SAP Gateway | SAP NetWeaver 7.50 SP04 |

On SAP NetWeaver 7.50 SP04, apply SAP Note 2323678 before you use any V4 feature. V4 deep
create needs a later patch level (SAP Note 2845226: SAP_GWFND 750 SP16, 751 SP09 or 752 SP05, or
753 and later).

Everything on this page written for `S_SERVICE` applies to V4, with `S_START` in its place.
[Step 2](#2-grant-the-authorizations-pfcg) shows the V4 fields. The shipped `finance_ops`
packs read V2 services.

Sources: [SAP documentation](#sap-documentation), rows *Deployment*, *V2 and V4 checks*,
*Hub and backend checks* and *Version floor*.

## SAP users and access

Building and running an SAP use case involves up to five SAP capabilities, which different
people have. They are named, not numbered, because they are not levels: the deployed system needs
nothing that a developer needs.

| Capability | Who has it | What it authorizes | Where the tooling lives |
|---|---|---|---|
| **none** | Functional analyst | Nothing in SAP: the analyst is interviewed and a pack is drafted, with the SAP fields left as placeholders | A scoping interview, outside this repository |
| **survey** | Integration developer | Reading what the system exposes, published or inactive: the SAP Gateway catalog, object search in ABAP Development Tools (ADT), data preview. Display only: `S_DEVELOP` with `ACTVT 03`, never `01` or `02` | An SAP survey tool, outside this repository |
| **abap-write** | ABAP developer, opt in | Creating or changing ABAP source, such as a missing CDS (Core Data Services) view and its access control. Restrict it by `DEVCLASS`, never `*`, and assume it includes activation. On S/4HANA, services are built with RAP (ABAP RESTful Application Programming Model). On ECC, they are built with the classic approach. SAP's developer role templates are `/IWBEP/RT_MGW_DEV` and `/IWFND/RT_DEVELOPER`, and `/IWBEP/RT_MGW_DSP` for reaching a remote system from SEGW at design time. The delivered `/IWBEP/RT_MGW_DSP` role contains only `S_RFC` (function groups including `/IWBEP/FG_METADATA_FACADE`, activity 16) and no `S_DEVELOP`, so restrict development authorization yourself | An SAP tooling project, outside this repository |
| **service-activate** | SAP Gateway administrator and Basis, often two people | Publishing a built but inactive OData service. Needs `S_ICF_ADM`, which has no per-service form, so keep it separate from abap-write. Also needs the SAP Gateway administration objects: `S_GW_COF` (setup), `S_GW_OP` (operations) and `S_GW_MON` (monitoring), switched by the SACF (Switchable Authorization Check Framework) scenario `GW_COF_OP_MON`, plus `/IWFND/ADM` (V2) or `/IWBEP/ADM` (V4). SAP's templates are `/IWFND/RT_ADMIN` and `/IWBEP/RT_BEP_ADM` | An SAP tooling project, outside this repository |
| **runtime-consume** | Each system's service account (the system's one `secret_name`), or under `obo` each person | Reading the published services a use case's packs read, and writing through them where a pack allows | This repository |

This repository needs only **runtime-consume**. On S/4HANA, SAP already publishes the services
that the shipped packs read, so runtime-consume is enough on the first day. The other
capabilities matter on ECC, or on a system that lacks a service a use case needs.

If you host the developer tooling centrally behind one SAP account, every caller has that
account's capability, and SAP's logs name the account instead of the person. Keep a hosted
deployment at survey, and leave abap-write to each developer's own SAP user.

## Authorization checks in SAP

| Who calls SAP | SAP checks the roles of | Anything narrower in front? |
|---|---|---|
| `flow: basic` | The service user in the system's secret | No: its roles are the whole SAP-side boundary |
| `flow: m2m-sap` | The SAP OAuth client's own user (created in `SOAUTH2`, user in `SU01`) | Yes: the token reaches only the services whose scopes the client has |
| `flow: m2m-oidc` | The service user SAP's trust maps the machine token to | No: a token SAP accepts is a full logon as that user |
| `flow: obo` (principal propagation) | The signed-in person | No |
| A use case's direct caller (poller, ingest, test-data seeder) | The user that system's `flow` resolves to, as in its row above | As in its row above |

The `obo` row matters most. It gives the agent that person's own SAP authorization. A user who
can post a document in SAP GUI can post one through the agent. You cannot restrict that in
`PFCG` without restricting the person's normal work. On that flow, the restriction happens on
AWS:

- pack entitlement
- the use case's Cedar permits and checks
- the server's write switches (none are on by default under `obo`)

## 1. Create the service user (SU01)

The `basic` flow and every direct caller sign in as one SAP System user. On `m2m-sap` and
`m2m-oidc`, the token also resolves to such a user. You create the user in the user maintenance
transaction `SU01`.

| Step | Transaction | Details |
|---|---|---|
| Create the user | `SU01` | User type **System** (no dialog logon), for example `SVC_SAP_AGENT` |
| Assign roles | `SU01`, **Roles** tab | Roles granting the authorizations in step 2 |
| Set the password | `SU01`, **Logon Data** tab | Then store it: `./scripts/deploy.sh layer run sap secrets <system>` |

Each system in `layers.sap.systems` has its own secret, so give each its own user.

On S/4HANA on premises or private cloud, there are no Communication Management apps
(*Communication Systems*, *Communication Arrangements*, *Maintain Communication Users*,
*Display Communication Scenarios*, *Custom Communication Scenarios*). They exist only on S/4HANA
Cloud Public Edition and in the SAP Business Technology Platform (BTP) ABAP environment. On these
systems, SAP's own answer is the route this page follows (SAP Note 3104502):

1. Activate the OData service.
2. Assign roles and users.
3. Expose the service through the firewall or SAP Web Dispatcher.

SAP Note 2595454 covers adding and activating services.

## 2. Grant the authorizations (PFCG)

Build the roles in the role maintenance transaction `PFCG`. The user's roles need:

| Object | Field | Value | Purpose |
|---|---|---|---|
| `S_SERVICE` | `SRV_NAME` | Each service from step 3 | OData service access |
| `S_SERVICE` | `SRV_TYPE` | `HT` | HTTP service type |
| `S_START` | Type of application `TADIR Service`, program ID `R3TR`, object type `G4BA`, object name the service group ID | Each V4 service group (V4 services only, in place of `S_SERVICE`) |
| The business objects your services check | | | For business-operations' `finance_ops`, for example `M_BEST_EKO` (`EKORG`: your purchasing organizations), `F_BKPF_BUK` and `F_LFA1_BUK` (`BUKRS`: your company codes) |

`S_SERVICE` alone returns no business data. The business objects are what let a read return
rows. The user needs no development authorization (`S_DEVELOP`).

Four things can make a role that looks correct grant nothing:

- **`SRV_NAME` contains a hash, not the service name.** Add each service through the role
  menu in `PFCG`. A value typed into the field generates with no error and authorizes nothing.
- **Generate the profile and run the user comparison (`PFUD`).** An assignment without a
  generated profile authorizes nothing, and the 403 stays after every edit to the role.
- **`S_TCODE` is not checked on an OData call.** A role built by adding transactions to
  its menu authorizes none of this.
- **A user with `SAP_ALL` passes every denial test.** Check the user's roles and profiles
  before you trust a test that a write is refused.

To start a role from SAP's own proposal, open `SU22` and enter:

- type of application `TADIR Service`
- program ID `R3TR`
- object type `IWSV` (backend), `IWSG` (hub) or `G4BA` (V4)
- the service name

`SU22` then lists the authorization objects that the service checks. SAP also ships role
templates: `/IWFND/RT_GW_USER` (all users, all services), `/IWFND/RT_ADMIN`, and
`/IWBEP/RT_MGW_ADM` and `/IWBEP/RT_BEP_ADM`. Entering `*` for a V2 service makes `PFCG`
calculate a hash for it.

To see which checks a service makes, run `STAUTHTRACE` filtered to the user, and make one call.
Keep the trace for whoever reviews the role. To find which existing role already grants a
service, use `SUIM`, **Roles → Search for Startable Applications in Roles**. Enter application
type *SAP Gateway: Service Groups Metadata* and the service name (SAP Note 3165869).

A developer who builds a service also delivers its start authorization. In `SU24`:

1. Choose type of application *SAP Gateway Business Suite Enablement - Service*.
2. Enter the service's `R3TR IWSV` object name with its version. For a service published from
   CDS (Core Data Services), this is `<VIEW>_CDS`, version `0001`.
3. Add `S_SERVICE` with proposal status *No*.
4. Add the authorization objects that the view's access control (DCL) checks, with proposal
   *Yes*.
5. Save and transport it, so that the role builder in `PFCG` starts from a correct proposal.

A RAP (ABAP RESTful Application Programming Model) service binding generates an authorization
value for each service version. Its **Default Authorization Values** editor maintains the same
defaults. `SU53` shows only the last failed check, and it is unreliable for a non-dialog user.
Keep read and write authorizations in separate roles. Then you can start read-only, and show that
a write is refused (403) before you grant it.

The server and the layer's direct client fetch the CSRF (cross-site request forgery) token that
SAP requires for writes, and send it again. There is nothing to configure for it in SAP.

## 3. Activate the OData services

| Step | Transaction | Details |
|---|---|---|
| Activate each service | `/IWFND/MAINT_SERVICE` | Every `service` your packs' `pack.json` names. For business-operations' `finance_ops`: `API_SUPPLIERINVOICE_PROCESS_SRV`, `API_PURCHASEORDER_PROCESS_SRV`, `API_MATERIAL_DOCUMENT_SRV`, and `FAP_VENDOR_LINE_ITEMS_SRV` for its test-data seeder |
| Check the ICF nodes | `SICF` | `/sap/opu/odata/sap/` and each service's node, and `/sap/opu/odata/iwfnd/catalogservice/`, which the server reads before its calls (as `catalogservice;v=2`, a URL version parameter, not a separate node) |
| Test a service | `curl` | `curl -u <user> 'https://<sap-host>/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV/$metadata?sap-client=<client>'` returns EDMX |

The server reads SAP's catalog service, so the user needs `S_SERVICE` for it as well. Confirm
the objects with `STAUTHTRACE`. To find which services a system already publishes, look in the
SAP Gateway service catalog (`/IWFND/MAINT_SERVICE` for OData V2, `/IWFND/V4_ADMIN` for V4).

For each service the agent calls:

- [ ] It is active in `/IWFND/MAINT_SERVICE`.
- [ ] Its ICF (Internet Communication Framework) node is active in `SICF`, and so is the
  catalog service.
- [ ] The user has `S_SERVICE` for it (`S_START` for a V4 service group).
- [ ] `$metadata` returns EDMX.
- [ ] The business authorizations it checks are granted.

To test a service from inside SAP, use `/IWFND/GW_CLIENT`. Compare its result with an external
call: this tells an SAP-side fault apart from a network or sign-in fault. For the detail behind a
403 or 500, read `/IWFND/ERROR_LOG` on the SAP Gateway hub, or the *SAP Gateway Error Log* feed
reader in ABAP Development Tools (ADT). SAP Note 1797736 is SAP's troubleshooting guide for the
SAP Gateway.

### Moving services between systems

Activate each service in every system the agent calls, with the SAP Gateway tools. A service
that you published from a RAP service binding in development cannot be moved with a transport.
In ADT, **Publish local service endpoint** works only in the development system, and the binding
with its generated artifacts cannot be transported. In quality and production, activate the
service with `/IWFND/MAINT_SERVICE` (V2) or the SAP Gateway V4 administration (V4). Do this after
you unpublish the local endpoint in development. This is usually the cause when a service works
in development and returns 404 in quality.

### Building a service that is missing

Only the **abap-write** and **service-activate** capabilities do this
([users and access](#sap-users-and-access)). What exists depends on the release.

| Release | How a service is built | Then |
|---|---|---|
| ECC, and S/4HANA services written the classic way | `SEGW` (Service Builder): define the data model, generate the runtime artifacts. SEGW creates the model and data provider classes (`_MPC`, `_DPC`) and registers the service in the backend. The technical service name is fixed after the first publish, so choose it deliberately | Activate it on the hub or embedded SAP Gateway with a system alias in `/IWFND/MAINT_SERVICE` |
| Any system with ABAP CDS views | Annotate the view `@OData.publish: true`. It needs a key element and a name of at most 26 characters, and generates an `IWSV` service `<VIEW>_CDS` with an `IWMO` model | Activate it like a SEGW service |
| S/4HANA, new development (V2 or V4) | RAP in ADT: behavior definition, service definition, then a service binding of type OData V2 or V4. RAP exists from S/4HANA 1909, with unmanaged implementations and V2 bindings only. V4 bindings arrive with S/4HANA 2020 FSP1 | Publish the local endpoint in development. Activate with the SAP Gateway tools in the other systems ([above](#moving-services-between-systems)) |

SEGW registers the service for you when you generate. If the registration is missing, create
it in transaction `/IWBEP/REG_SERVICE`. A registration contains:

- the technical service name and version (such as `0001`), with the data provider class
- an assigned model that points at the model provider class

Deleting a registration there deletes no classes. SAP Note 2930655 also gives the steps to
create one again. In SEGW, the **Register** button can be greyed out, with no system under
*Service Maintenance*. This means the SAP Gateway settings are missing in `SPRO`, under *SAP
Gateway Service Enablement → Connection Settings to SAP Gateway → SAP Gateway Settings* (SAP Note
2550286).

SEGW is the V2 tool. SAP recommends it from NetWeaver 7.40 SP04 (Note 1749383), and says it
should be used only for V2 (Note 3491161, 2025). The ABAP Platform 2025 documentation is the
foundation of S/4HANA 2025 (SAP Note 3612209). It still describes SEGW as the Service Builder for
creating OData services, and recommends ADT only for V4.

For V4, SAP recommends ADT over SEGW (SAP Note 2485370). Before S/4HANA 2020, or on a Business
Suite system, SAP recommends writing the V4 model and data provider classes by hand, instead of
using SEGW's V4 project type. These classes inherit from `/IWBEP/CL_V4_ABS_MODEL_PROV` and
`/IWBEP/CL_V4_ABS_DATA_PROVIDER`. A service registered under a customer prefix (`Z`, `Y` or a
namespace) is listed by its technical service name in `/IWFND/MAINT_SERVICE` and in the OAuth
scope list ([sap-trust.md](sap-trust.md)). So give the packs the technical name.

## 4. Serve HTTPS (SMICM, STRUST)

`base_url` must be `https://`, and config load refuses anything else. So SAP's Internet
Communication Manager (ICM) must serve HTTPS (`SMICM`, **Goto → Services**) on the port that
`base_url` names. The server certificate goes in the **SSL Server Standard** PSE (personal
security environment) in the trust manager, `STRUST`.

Use a certificate from a publicly trusted CA where you can. On RISE with SAP, SAP Enterprise
Cloud Services (ECS) manages the Web Dispatcher's certificate: ask ECS for one from a public CA
([RISE with SAP](reference/network.md#rise-with-sap)). The server verifies SAP's certificate (the
layer sets `MCP_SERVER_SESSION_VERIFY`), and the certificate must chain to a publicly trusted CA
([threat-model.md](threat-model.md), SAP-12).

On `m2m-oidc` and `obo`, SAP also calls the identity provider (IdP) over TLS. That certificate
chain goes in the **SSL Client** PSE ([sap-trust.md](sap-trust.md#strust-import-the-idps-full-chain)).

## 5. Send events (optional)

This step is only for a use case that wants a case sooner than its poll interval. The SAP side
is the layer's generic outbox in [`abap/`](abap/), installed once per system:

- table `ZAWS_EVT_OUTBOX`
- class `ZCL_AWS_EVT_OUTBOX`
- report `ZAWS_EVT_DRAIN`

Each use case adds its own BAdI (Business Add-In) implementation that calls `record`. The class's
header lists the Basis steps:

- the AWS SDK for SAP ABAP core and EventBridge transports
- the `/AWS1/IMG` profile `ZAWS_EVT`, mapped to the system's sender role
- the TVARVC variables `ZAWS_EVT_BUS_ARN` and `ZAWS_EVT_SYSTEM`
- the user and role of the job that runs `ZAWS_EVT_DRAIN`

The AWS side, `senders` and `ingest`, is in [SAP's events](reference/events.md).

## Sign-in methods on the OData service

Each V2 OData service is an ICF node under `/sap/opu/odata`. The sign-in methods SAP accepts on
a node come from SICF and the system profile, not from the OData service.

| Method | SAP side | Notes |
|---|---|---|
| Basic | SICF **Logon Data** procedure *Standard SAP User* (or *Internet User*) | SAP limits the character set of a Basic password. The `flow: basic` path |
| X.509 client certificate | SICF Logon Data *Required with SSL Certificate*; `icm/HTTPS/verify_client = 1` (ICM, `SMICM`); the issuing CA in the SSL server PSE (`STRUST`) | SAP's recommended method for server-side and cloud consumers; SAP Note 510007 covers the SSL setup. This layer has no flow for it |
| OAuth 2.0 | `SOAUTH2` client; OAuth enabled per service in `/IWFND/MAINT_SERVICE`, which switches the node's handler to `/IWFND/CL_SODATA_HTTP_HNDL_OAT`; token endpoint `/sap/bc/sec/oauth2/token` | SAP's OAuth page for the SAP Gateway says to configure a trusted identity provider before enabling OAuth 2.0, and does not distinguish grants. The client-credentials grant this layer uses needs no trusted provider. On the development system, the client has only that grant active, no SAML 2.0 trusted provider exists (`SAML2_ENTITY` is empty), and the token endpoint issues a token that reads a service's `$metadata`. The trusted provider matters for the SAML bearer grant. Below NetWeaver 7.40, apply SAP Note 1797103, replace the service's ICF handler with `/IWFND/CL_SODATA_HTTP_HNDL_OAT` by hand, and run `/IWFND/R_OAUTH_SCOPES` to create the scopes. [sap-trust.md](sap-trust.md) |
| OpenID Connect | `SOIDC`, logon method *OIDC Bearer* | SAP_BASIS 7.56 SP1, with feature set 2 downported to 7.52 SP13 and later (SAP Notes 3105025, 3313726; the release list is in [sap-trust.md](sap-trust.md#check-the-minimum-basis-release)) |
| SAML 2.0 | SAML bearer assertion, or an IdP in front | SAP recommends it for cloud applications |

**The user type does not restrict HTTP sign-in.** Every user type except *Reference* can log on
over HTTP, and SAP says you cannot restrict HTTP logon by user type (SAP Note 3493522). A
**System** user is the right type for the service account. It has no dialog logon, and its
password cannot be initial or expired. But nothing in SAP stops a Dialog or Communication user
from signing in to the same node. Control who may reach a node with its roles and, where you need
it, with its SICF logon procedure.

This was checked on the development system with one test user of each type. Dialog, System and
Communication users all signed in over HTTP Basic to an OData service. A new Dialog or
Communication user has an initial password, and gets `401` until the password is changed. A
System user signs in at once, because its password is never initial. So a `401` for a user you
just created usually means an initial password, not a missing role.

**A node inherits its parent's logon procedure.** Setting a procedure on `/sap/opu/odata` changes
nothing on a service node under it, unless the child node has **Ignore inherited setting**
selected in SICF (**Service Data → Service Options**). Set the procedure on the node, then check
**Service → Display Inheritance**. This is why calls can still succeed with Basic after you
disable Basic on a node (SAP Notes 3734444, 2573379, 3564503).

## Troubleshooting SAP errors

| SAP returns | Usually | Look in |
|---|---|---|
| `401 Unauthorized` | Wrong credentials, a locked user, a logon procedure the node does not offer (or one it still inherits from its parent), or on a token flow the trust or mapping | `SU01` (user status, password); SICF **Logon Data** and **Display Inheritance**; on a token flow, [sap-trust.md](sap-trust.md#troubleshooting-token-rejection) |
| `403 Forbidden` | SAP resolved a user, and a role (or on `m2m-sap`, a scope) refused the call. `No authorization to access Service '<name>'` means `S_SERVICE` (V2) or `S_START` (V4) | `STAUTHTRACE` for that user, then `PFCG` and `PFUD`; `SUIM` for the role that grants the service; `/IWFND/ERROR_LOG` |
| `404 Not Found` | The service or its ICF node is not active | `/IWFND/MAINT_SERVICE`, `SICF` |
| `500 Internal Server Error` | A backend error, or an OData path that does not exist | `ST22` for dumps, `/IWFND/ERROR_LOG` for OData errors |
| A timeout or a refused connection | No network path to SAP | `security.networking`, your peering, Transit Gateway, VPN, DNS and security group rules ([walkthrough](walkthrough.md#network-paths-to-sap)) |

A 401 means that SAP did not resolve a user at all. A 403 means that it did, and an
authorization refused the call.

## SAP documentation

Where each statement on this page and in [sap-trust.md](sap-trust.md) comes from. Page links are
on SAP Help (ABAP Platform documentation). The SAP Gateway Foundation pages have a copy for each
release of SAP NetWeaver 7.4, 7.5 and 7.52. SAP Notes need an S-user.

| Statement | Source |
|---|---|
| Deployment: embedded and the two hub options; `SAP_GWFND` standard from NetWeaver 7.4; hub-side development before 7.50 SP04 | [Deployment Options](https://help.sap.com/docs/SAP_NETWEAVER_740/68bf513362174d54b58cddec28794093/3eb1ea508f88bb7ee10000000a445394.html) |
| V2 and V4 checks: `S_SERVICE` against `S_START`, check counts, `G4BA` and `G4BS`, `*` limited to OData, `S_GW_COF`, `S_GW_OP`, `S_GW_MON`, `GW_COF_OP_MON`, `/IWFND/ADM` and `/IWBEP/ADM` | [Security Enhancements for OData V4](https://help.sap.com/docs/ABAP_PLATFORM_NEW/68bf513362174d54b58cddec28794093/c6dd838722ce4b8d9cc4a0741d93d864.html) |
| Hub and backend checks: `IWSG` and `IWOM` on the hub, `IWSV` on the backend, the `SU22` steps, `/IWFND/RT_GW_USER`, the `*` hash | [Roles in the SAP Gateway Landscape](https://help.sap.com/docs/ABAP_PLATFORM_NEW/68bf513362174d54b58cddec28794093/4a5d22518bc72214e10000000a44176d.html) |
| Version floor: OData V4 from SAP NetWeaver 7.50 SP04, V4 deep create patch levels | SAP Notes 2323678 and 2845226; [Deployment Options](https://help.sap.com/docs/SAP_NETWEAVER_740/68bf513362174d54b58cddec28794093/3eb1ea508f88bb7ee10000000a445394.html) |
| `SUIM` search for the role that grants a service; `/IWFND/GW_CLIENT`; `/IWFND/ERROR_LOG` | SAP Notes 3165869 and 1797736 |
| V4 service groups are published on the hub in `/IWFND/V4_ADMIN` (**Publish Service Groups**) | SAP Notes 2729492 and 2323678 |
| Hub trust: the backend trusts the hub through a trusted RFC | [Connecting SAP Gateway to Back-End System (Trusted RFC)](https://help.sap.com/docs/FIORI_IMPLEMENTATION/6ce0dc90c4f140e68ec39242431a212a/ee805652f4c2722de10000000a44176d.html) |
| Service activation with a system alias in `/IWFND/MAINT_SERVICE` | [Activate and Maintain Services](https://help.sap.com/docs/ABAP_PLATFORM_NEW/68bf513362174d54b58cddec28794093/bb2bfe50645c741ae10000000a423f68.html) |
| SEGW: runtime artifacts, registration, the fixed technical service name | [Generating Runtime Artifacts](https://help.sap.com/docs/ABAP_PLATFORM_NEW/cc0c305d2fab47bd808adcad3ca7ee9d/fcb183efa1b64d0c9d79bd165cd16329.html) |
| `@OData.publish`: key element, 26 characters, the generated `IWSV` and `IWMO` objects | [Generate Service Artifacts From a CDS View](https://help.sap.com/docs/ABAP_PLATFORM_NEW/cc0c305d2fab47bd808adcad3ca7ee9d/9aecb367279e4df3a8608718f6dcea1e.html) |
| ADT preferred over SEGW for V4; RAP and V4 bindings by S/4HANA release; hand-written V4 classes before 2020 | SAP Note 2485370; [Working on Service Builder Projects Complying with OData V4](https://help.sap.com/docs/ABAP_PLATFORM_NEW/68bf513362174d54b58cddec28794093/da4162269f7c4253baad2196fb4e1066.html) |
| RAP service binding not transportable on premises; publish the local endpoint only in development | The *Service Transport* section of the [ABAP RESTful Application Programming Model guide](https://help.sap.com/doc/1c33f35cbf124c0a84487261f530dbaf/sap_cross_product_abap/en-US/6f0250d605d545b7a76aba30dd85fa2e.pdf) |
| Authentication methods SAP supports and recommends | [SAP Gateway Authentication and Single Sign-On](https://help.sap.com/docs/ABAP_PLATFORM_NEW/68bf513362174d54b58cddec28794093/c481215150e92414e10000000a44176d.html) |
| Developer roles `/IWBEP/RT_MGW_DEV`, `/IWFND/RT_DEVELOPER`, `/IWFND/RT_BOR_DEV` and `/IWBEP/RT_MGW_DSP`; support roles are display only | [User, Developer, and Administrator Roles](https://help.sap.com/docs/SAP_NETWEAVER_750/68bf513362174d54b58cddec28794093/24f82651c294256ee10000000a445394.html) |
| `SU24` start authorization for a service; the RAP binding's authorization value | [Assigning Authorization Defaults to OData Services](https://help.sap.com/docs/ABAP_PLATFORM_NEW/cc0c305d2fab47bd808adcad3ca7ee9d/d582314dbe574083b0381d9a50f132d8.html); [Using Service Binding Editor for OData V2 Service](https://help.sap.com/docs/ABAP_PLATFORM_NEW/c238d694b825421f940829321ffa326a/81dc788fbda74883bd775a4036fa4b67.html) |
| ERP 6.0 enhancement package to `SAP_BASIS` mapping; `SAP_GWFND` 7.50 in EHP8 | [Master Guide for Enhancement Package 8 for SAP ERP 6.0](https://help.sap.com/doc/1dc4f8c981ea45d8801fe2aa9682d751/6.18.latest/en-US/loio3764eddbf3f94d3c8249ca6b7119e72a.pdf); the software requirements list in an [SAP ERP Connectivity with Ariba Network guide](https://help.sap.com/doc/2731286fd6cd487aa1c442905685bd57/122/en-US/loio4f5e9854f1332b66e10000000a423f68_4f5e9854f1332b66e10000000a423f68.pdf) |
| No Communication Management apps on S/4HANA on premises or private cloud | SAP Notes 3104502 and 2595454 |
| Trusted RFC authorization in the backend | [Authorization Object S_RFCACL](https://help.sap.com/docs/SAP_NETWEAVER_AS_ABAP_752/c495ada972d045b2be2869f5573af8e7/488d1c6eae444e6ee10000000a421937.html) |
| SEGW for V2 only; recommended from NetWeaver 7.40 SP04 | SAP Notes 1749383 and 3491161; [SAP Gateway Service Builder](https://help.sap.com/docs/ABAP_PLATFORM_NEW/68bf513362174d54b58cddec28794093/9db1ea508f88bb7ee10000000a445394.html); SAP Note 3612209 |
| `/IWBEP/RT_MGW_DSP` contains only `S_RFC` | Read from the role's authorization data (`AGR_1251`) on the development system |
| `/IWBEP/REG_SERVICE` registration and model; SEGW **Register** prerequisites | SAP Notes 2930655 and 2550286 |
| Basic authentication and its character-set limit | [Basic Authentication System Logon](https://help.sap.com/docs/ABAP_PLATFORM_NEW/e815bb97839a4d83be6c4fca48ee5777/2eb3c591356a4598ae5873730b465fde.html) |
| X.509 client certificates: SICF setting, `icm/HTTPS/verify_client`, `STRUST` | [X.509 Client Certificate Authentication Method](https://help.sap.com/docs/ABAP_PLATFORM_NEW/68bf513362174d54b58cddec28794093/bb5d22518bc72214e10000000a44176d.html); SAP Note 510007 |
| OAuth 2.0 on SAP Gateway: per-service enablement, the handler, scopes below 7.40 | [Enabling OAuth 2.0 Authentication for OData Services](https://help.sap.com/docs/ABAP_PLATFORM_NEW/68bf513362174d54b58cddec28794093/1ec60c33be784846aad62716b4a1df39.html); SAP Note 1797103 |
| Token endpoint `/sap/bc/sec/oauth2/token` | [Token Endpoint for OAuth 2.0](https://help.sap.com/docs/ABAP_PLATFORM_NEW/e815bb97839a4d83be6c4fca48ee5777/5c8371a2370647f5a03ea6f81f6a27cb.html) |
| OpenID Connect releases and feature sets | SAP Notes 3105025, 3313726, 3452750, 3585020, 3632887 and 3730361; [OpenID Connect Authentication – Configuration Overview](https://help.sap.com/docs/ABAP_PLATFORM_NEW/e815bb97839a4d83be6c4fca48ee5777/d72c1a254a584d579c476a998b8ec0b2.html) |
| `System` user properties | [Logon Data Tab](https://help.sap.com/docs/ABAP_PLATFORM_NEW/ad77b44570314f6d8c3a8a807273084c/4b04325ec2ad2b5ce10000000a42189c.html) |
| No restriction of HTTP logon by user type | SAP Note 3493522 |
| SICF logon-procedure inheritance and **Ignore inherited setting** | SAP Notes 3734444, 2573379 and 3564503 |

Not confirmed: the `S_DEVELOP` values for `/IWBEP/RT_MGW_DEV` and `/IWFND/RT_DEVELOPER`, which
are not present in the development system's client; only `/IWBEP/RT_MGW_DSP` was read there.
