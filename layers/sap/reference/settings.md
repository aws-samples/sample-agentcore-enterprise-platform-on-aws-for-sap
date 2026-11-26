# SAP layer settings

Every key under `layers.sap.systems.<system>`, and the server image.

| Key under `systems.<system>` | Default | What it sets |
|---|---|---|
| `system_role` | required | `sandbox`, `development`, `quality` or `production`: what the system is for. Only a `sandbox` may be seeded with test data or have evaluations run against it; each use case's own seeder and eval runner enforce this. A use case's stack may warn at synth when a deployment that is not `deployment.mode: production` reaches a `production` system (the SAP exception handling use case does). The operator sets the value, and nothing checks it with SAP |
| `base_url` | required | The SAP OData root. Must be `https://` |
| `type` | `S4HANA` | `S4HANA` or `ECC` (SAP ERP Central Component) |
| `client` | none | The SAP client number, such as `100`: one set of business data in the system. Not an app client |
| `flow` | `basic` | How the server signs in to SAP ([Sign-in flows](../walkthrough.md#sign-in-flows)) |
| `secret_name` | required | The system's one AWS Secrets Manager secret, in the deployment's Region |
| `writes` | per flow ([Writes, by flow](../walkthrough.md#writes-by-flow)) | Any of `create`, `update`, `function_import`; never delete |
| `scopes` | none | Every flow except `basic`, and required there: the scopes the server asks for in SAP's token |
| `token_url` | none | `m2m-sap` only, and required there: SAP's token endpoint |
| `discovery_url` | none | `m2m-oidc` only, and required there: the identity provider's (IdP's) `.well-known/openid-configuration` |
| `gateway_scopes` | none | `obo` only, and required there: the scopes the Gateway asks Entra for, addressed to the server's own app |
| `service_prefixes` | none | Limits what the server's `find_sap_services` returns ([controls](server.md#the-servers-own-controls)). Required under `odata_version: v4`. `["*"]` lists every service |
| `odata_version` | `v2` | `v2`, `v4` or `mixed`: the OData (Open Data Protocol) version in which the server reads SAP's catalog and calls services ([OData V4](server.md#odata-v4-and-mixed-systems)) |
| `catalog_bucket` | none | The Amazon S3 bucket, and optional `/sub-path`, of a custom `catalog.json`. Required under `mixed` |
| `runtime_arn` | none | Attaches a server you deployed yourself instead ([Custom MCP server](server.md#custom-mcp-server)) |
| `unattended` | none | How direct callers sign in instead of by the server's flow ([Unattended sign-in](sign-in.md#unattended-sign-in)) |
| `senders` | none | The IAM role ARNs that may put this system's events on the layer's `sap-events` bus ([The sap-events bus](events.md)). Each is a machine's role, never a user, an account or an IAM Identity Center role |

A use case that requires `layer-sap` fails at config load unless `layers.sap` is in
`platform.yaml`. A use case with no `access` row fails at synth, and the error shows the line to
add. (Synth is the step where the AWS Cloud Development Kit, CDK, builds the templates.) The synth
also refuses:

- systems when the layer's check is not listed
- a check whose prefixes cover more than the SAP targets, because it would refuse other use
  cases' calls

Each system's Gateway target is `sap-<system>`, so the tools the model sees are
`sap-<system>___<tool>`. A use case's own check that decides its SAP writes also lists `sap-`
among its prefixes.

## Server image

`layers.sap.image_tag` chooses the server image for every system. It defaults to `latest`, so a
new deployment takes AWS's current server.

A runtime keeps the image it was created or last updated with. A runtime gets a newly published
image only when something else changes the runtime. So two stacks deployed a week apart can run
different servers. Each runtime prints its version in its startup banner (`Version:`). To keep one
version, set `image_tag` to that version or to a `sha256:` digest. `deployment.mode: production`
refuses `latest`.
