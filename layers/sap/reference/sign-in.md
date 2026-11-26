# SAP sign-in details

Credential rotation, how direct callers sign in, and the `unattended` block. The flows themselves
are in the [walkthrough](../walkthrough.md#sign-in-flows). A direct caller is a use case's own
function that calls SAP outside the Gateway, such as the poller.

## Flows not offered

**Passthrough** sends the sign-in token to SAP unchanged. The AWS for SAP MCP (Model Context
Protocol) Server does not have this flow. **User federation** asks each user for interactive
consent. The server has this flow, but this layer does not offer it, and config load refuses it.

## Credential rotation

A new Basic password needs no redeploy. The server reads the secret on every request, and a
direct caller reads it again when SAP refuses the old password.

Changing these after the first deploy is untested:

- a new `clientId`. The template reads it with a `{{resolve:secretsmanager:...}}` reference
  into the token-vault provider. Under `obo`, it also reads it into the runtime's audience.
- under `obo`, a rotated platform app secret (`identity.client_secret_name`). The Gateway's
  provider stores it in the same way.
- a rotated client secret, which the provider reads by reference.

AWS CloudFormation resolves such a reference only when the resource is updated. A redeploy with
an unchanged template updates nothing. So a plain `layer-sap` redeploy may apply none of these
changes. Until this is tested, plan a template change that touches the resource, so that
CloudFormation updates it.

## Direct callers sign in by the flow

A direct caller (`lib/sap_client/sap_http.py`) reads the same secret as the system's server. It
signs in with the same `flow`, with no change in the caller:

- `basic` sends the user and password as Basic authentication. It is the quick setup for a demo.
- `m2m-sap` and `m2m-oidc` post the `{"clientId", "clientSecret"}` to get a client-credentials
  token with `scopes`. They post to `token_url`, or to the `token_endpoint` that `discovery_url`
  names. Then they send the token as a Bearer token. These flows replace Basic directly for a
  system that should not give out a password. The function's process keeps the token until a
  minute before its `expires_in`, and for at most 15 minutes. When SAP answers 401, the process
  fetches a new token once.
- `obo` (principal propagation) cannot sign in a direct caller, because an unattended call has no
  person to act for. An `obo` system can be a direct system only with an
  [`unattended` block](#unattended-sign-in).

One direct read needs Basic. The test-data seeder looks up the open posting period
(`sap_odata.open_posting_date`) through ADT (ABAP Development Tools). A client-credentials token
has no scope for ADT, and SAP publishes no released OData API for the lookup. On a token flow, the
lookup is refused before any call, and a seed must be given its posting date.

Every URL that receives a credential must use https, and none follows a redirect. On `m2m-oidc`,
the token endpoint belongs to the identity provider (IdP), not to SAP. So a direct caller in the
platform VPC (virtual private cloud) needs a route to the token endpoint as well as to SAP. That
route is a NAT gateway, or the IdP's private endpoint.

## Unattended sign-in

Basic is the demo default for both the server and its direct callers. To sign in the direct
callers differently from the server, give the system an `unattended` block. The poller, the
ingest re-read and the test-data seeder then use the block, whatever the system's `flow` is. The
server keeps its own `flow` and secret.

The block exists because the direct callers have no person behind them. An `obo` system cannot
sign them in, and a team may want them on OAuth while the server uses Basic, OAuth or Entra
`obo`:

```yaml
layers:
  sap:
    systems:
      s4:
        system_role: sandbox
        base_url: https://<sap-host>/sap/opu/odata/sap/
        client: "100"
        secret_name: <project>-<env>/sap/s4   # the server's: Basic
        unattended:                           # the direct callers'
          flow: m2m-sap
          secret_name: <project>-<env>/sap/s4-oauth
          token_url: https://<sap-host>/sap/bc/sec/oauth2/token?sap-client=100
          scopes: [ZAPI_SUPPLIERINVOICE_PROCESS_SRV_0001]
```

The block has a system's sign-in keys, with the same rules:

- `flow`: `basic` (the default), `m2m-sap` or `m2m-oidc`. Never `obo`.
- `secret_name`: required, in the secret shape of its `flow`.
- `scopes`, `token_url` or `discovery_url`, as that flow needs them.

A direct caller's role can read the block's secret, and the published `connection` names it. The
egress security group allows the port of its token URL. Store the secret with
`./scripts/deploy.sh layer run sap secrets <system> unattended`.

Without the block, direct callers sign in as the server does, and an `obo` system has no direct
callers. The block changes only direct callers. An agent run reaches the system through the
Gateway, with the server's flow.
