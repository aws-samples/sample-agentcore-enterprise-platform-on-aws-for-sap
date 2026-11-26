# AWS for SAP MCP Server per system

What the layer deploys for each SAP system, the controls it sets on the AWS for SAP MCP (Model
Context Protocol) Server, and the write rules behind the walkthrough's
[writes table](../walkthrough.md#writes-by-flow).

## Write details

Under `obo` (principal propagation), no write is on by default, because no Cedar statement can
allow one yet. A Cedar write permit matches on a scope. Nobody has measured whether a person's
Entra token includes that scope in the place where Cedar reads it. Config load accepts `writes` on
an `obo` system, but this sample has not run a write that way. So treat `obo` systems as
read-only.

`writes` sets the server's write switches. It is also the upper limit for every use case's
`access` row: config load refuses an `access.<use case>.write` operation that the system does not
have turned on. `odata_delete` is never available:

- the server starts with it off
- no Cedar permit may name it
- the layer's check refuses it for everyone

## Resources per system

- A runtime of AWS's image, fixed to `image_tag`.
  - On every flow except `obo`, it has no JWT (JSON Web Token) authorizer. The Gateway calls it
    with the Gateway's own IAM role, which is allowed `InvokeAgentRuntime` on this runtime only.
    The runtime's resource policy denies every other principal.
  - Under `obo`, it accepts only an Entra token for the server's app whose `azp` is the platform
    app. That is the token that the Gateway's exchange creates.
  - Its execution role can read this system's secret and nothing else.
- When `security.networking` is on, the runtime runs in the platform's private subnets, with a
  security group that can reach only SAP's port. Otherwise it runs as `PUBLIC`, and SAP must
  then be reachable from the internet.
- Its log group, `/aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT`, with the platform's
  retention.
- No Cedar statement. Each use case permits its own app clients
  ([Use case access API](use-case-access.md#use-case-access-api)).

### The server's own controls

The layer sets these on the server from the system's keys. They apply per system, not per
caller, so every app client of one runtime gets the same set of tools.

- **Write switches.** `MCP_SERVER_WRITE_ENABLED` is the main switch over
  `MCP_SERVER_CREATE_ENABLED`, `MCP_SERVER_UPDATE_ENABLED` and
  `MCP_SERVER_FUNCTION_IMPORT_ENABLED`. All of them are set from `writes`.
  `MCP_SERVER_DELETE_ENABLED` is always `false`. A tool that is off is not listed, so no agent
  can call it.
- **Certificate verification.** `MCP_SERVER_SESSION_VERIFY` is always `true`. So the server checks
  that SAP's certificate chains to a publicly trusted certificate authority (CA), and that it
  names the host in `base_url`. Use a certificate from a publicly trusted CA on the SAP endpoint.
- **`service_prefixes` is a discovery filter.** It sets `MCP_SERVER_ALLOWED_SERVICE_PREFIXES`,
  which limits what `find_sap_services` returns. AWS documents it as limiting what can be
  discovered. This sample has not tested whether it stops a call that names a service not on the
  list. SAP is what restricts a call: the scopes on an SAP OAuth client (transaction SOAUTH2),
  and the roles of the user that SAP resolves.
- **`scopes` restricts nothing on the OIDC (OpenID Connect) flows.** On `m2m-oidc` and `obo`, it
  is the scope requested from the identity provider (IdP). It does not limit which services the
  token can reach.
- **The service catalog.** The server always reads SAP's catalog service before its calls, so the
  SAP user needs access to it ([sap-setup.md](../sap-setup.md#3-activate-the-odata-services)). A
  `catalog_bucket` adds the entries of a custom catalog to it
  ([OData V4](#odata-v4-and-mixed-systems)).
- **Tool listing.** On every flow except `obo`, the target uses the Gateway's `DEFAULT` listing.
  The Gateway lists the runtime's tools when the target is created (the target waits for the
  runtime), and the tools appear in the Gateway's combined `tools/list`.

  An `obo` target has a fixed list instead: `lib/sap_layer/sap_tools.json`, filtered to the reads
  and the system's writes. This is because listing needs a person's token, and target creation
  has none. That file is the list of server version `1.1.0-958`. Its `odata_read` and
  `odata_count` lines gave only the V2 filter syntax, and they were rewritten to give the V4
  syntax as well. When the image changes, check that the file still matches the server's
  `tools/list`.

### OData V4 and mixed systems

The server reads OData (Open Data Protocol) V2 unless told otherwise. So by default the agent
cannot read a service published only as V4, which is the default for a RAP (ABAP RESTful
Application Programming Model) service. `odata_version` sets the server's
`MCP_SERVER_ODATA_VERSION` and `MCP_SERVER_CUSTOM_CATALOG_ODATA_VERSION`, the values that AWS's
[Configuration Reference](https://docs.aws.amazon.com/mcp-sap/latest/awsforsapmcp/configuration-reference.html)
lists:

| `odata_version` | SAP's catalog and calls | The custom catalog, when `catalog_bucket` is set | Also needs |
|---|---|---|---|
| `v2` (default) | V2; nothing is set | V2 | |
| `v4` | V4 (`MCP_SERVER_ODATA_VERSION=V4`) | V4: every entry has a `ServiceName` | `service_prefixes`, which AWS's deployment guide requires under V4 |
| `mixed` | V2 | `MIXED`: every entry has a `ServiceName` and its own `ODataVersion` | `catalog_bucket` |

`base_url` stays the OData root of the system's other settings. `mixed` lets one system serve
its V2 APIs and its V4 services together. The V2 services come from SAP's catalog. Each V4
service is an entry in `catalog.json`, in the shape that the Configuration Reference gives.

Upload the file to `<catalog_bucket>/catalog.json`. The bucket's name must start with
`awsforsap-mcp-server-`, because the server reads no other bucket. The layer gives the system's
runtime `s3:GetObject` on that one object. A bucket encrypted with a customer managed AWS KMS (Key
Management Service) key also needs a key policy that lets the runtime's role decrypt. At start,
the server logs how many entries it loaded. When it cannot read the file, it logs an error and
continues without the catalog.

### Custom MCP server

To attach a server you deployed yourself, give `runtime_arn` instead of the connection
settings. Set `writes` to what that server has turned on. The server's owner must let the
Gateway's role (`gateway/role-arn`) invoke it, and the server must accept SigV4 signing. Its
owner manages its credentials, so `layer run sap secrets` refuses such a system.

## Known limitations

- **An `obo` target's tool list does not follow the image.** `sap_tools.json` is the list of
  server version `1.1.0-958`, and no test ties it to `layers.sap.image_tag`. The default tag is
  `latest`, so a newer image can change the server's tools while the target still offers the old
  list. Set `image_tag` to a fixed version on an `obo` system. When you change it, compare the
  file with the new server's `tools/list`.
- **A custom server through `runtime_arn` is tested at synth only.** It has not run live. The
  target signs with the Gateway's IAM role, so the server must accept SigV4. A server built from
  AWS's template has a Cognito JWT authorizer and does not work in this mode. Test one call
  through the Gateway before you rely on it.
