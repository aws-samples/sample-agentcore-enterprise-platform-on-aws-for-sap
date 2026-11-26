# SAP token trust

Terms are defined in [Concepts](../../docs/start/concepts.md).

> Review the [SAP API Policy](https://help.sap.com/doc/sap-api-policy/latest/en-US/API_Policy_latest.pdf)
> and your agreements with SAP before you connect a system ([sap-setup.md](sap-setup.md)).

On `flow: basic`, SAP receives a user and password, and [sap-setup.md](sap-setup.md) covers the
whole SAP side. On the other three flows, SAP receives a token. This page describes what SAP,
your identity provider (IdP) and the layer each need, so that SAP accepts the token and runs as
the right user. Read sap-setup.md first: the user SAP resolves still needs its roles, and the
services still need to be activated.

On this page, a **claim** is a field in a token (a JWT, JSON Web Token), such as `aud` or `oid`.
It is not the SAP access claim that a use case writes for the SAP layer.

| `flow` | Who creates the token SAP receives | SAP-side mechanism | Sections |
|---|---|---|---|
| `m2m-sap` | SAP itself (SOAUTH2 client credentials) | An OAuth 2.0 client in `SOAUTH2` | [M2M via SOAUTH2](#m2m-via-sap-soauth2-m2m-sap) |
| `m2m-oidc` | Your IdP (client credentials) | An OIDC (OpenID Connect) trust in `SOIDC`, mapping the app to a service user | [Shared steps](#shared-steps-for-an-oidc-trust), [M2M via an IdP](#m2m-via-an-identity-provider-m2m-oidc) |
| `obo` (principal propagation) | Microsoft Entra ID, on behalf of the signed-in person | An OIDC trust in `SOIDC`, mapping the person to their SAP user | [Shared steps](#shared-steps-for-an-oidc-trust), [Entra on-behalf-of](#entra-on-behalf-of-obo) |

The layer builds the token-vault providers from `layers.sap.systems.<system>`, so you do not
create them. Field labels in `SOAUTH2` and `SOIDC` vary by release, so confirm them in the
transaction on your system. The minimum `SOIDC` release below comes from SAP's notes. The other
minimum releases come from secondary sources, not from SAP's documentation. Confirm each one on
your system before you plan with it.

## Shared steps for an OIDC trust

On both `m2m-oidc` and `obo`, SAP validates a token that your IdP created.

### Check the minimum Basis release

In SAP GUI, **System → Status** shows `SAP_BASIS`, and `SPAM` or `SAINT` shows the support
package. `SOIDC` first shipped in SAP_BASIS 7.56 SP1 (SAP Note 3105025), with:

- the *OIDC Bearer* and *OIDC Logon* logon methods
- automatic download of the provider's metadata

Feature set 2 (SAP Note 3313726) added more token claims for user mapping, including a custom
claim. It was downported to these support packages: 7.52 SP13, 7.53 SP11, 7.54 SP09, 7.55 SP07,
7.56 SP05, 7.57 SP03 and 7.58 SP01. It also needs a new enough kernel: at least 789 patch 116,
785 patch 244, 777 patch 554, 754 patch 211 or 753 patch 1215. ABAP and the kernel are updated
separately, and `SOIDC` works only if both are new enough. SAP ships these by support package,
not by `SNOTE`.

Later feature sets add:

- set 3: supportability (`SOIDC_ANALYZER`), scheduled key updates and local proxy support (SAP
  Note 3452750)
- set 4: activating and deactivating a configuration (3585020)
- set 5: choosing a configuration for interactive logon, and token exchange for SAP Cloud
  (3632887)
- set 6: a custom provider-metadata URL, and OIDC Bearer for API protection in selected SAP
  scenarios (3730361)

Each note lists its own support packages. A custom claim mapping, which Entra needs (below),
needs feature set 2 or later. Below the minimum release, the mechanism does not exist; it is not
a configuration error. Use `m2m-sap` instead, which needs no `SOIDC`.

`m2m-oidc` uses the *OIDC Bearer* method. SAP describes it as an SAP-proprietary use of OIDC ID
tokens: the caller sends a token in an `Authorization: Bearer` header. SAP's note says that the
token is an ID token. It does not say that a client-credentials access token is accepted. So this
flow works because the layer's IdP creates a JWT that SAP validates, not because SAP names that
case.

The audience matters:

- mode `O` trusts only the configuration's own client ID
- mode `S` also trusts a listed set
- mode `A` accepts any token that contains the configuration's own client ID

These limits apply:

- one configuration per SAP client, logon category and issuer
- only RS256, RS384 and RS512 signatures
- no OIDC flow except the authorization-code flow for interactive logon

### STRUST: import the IdP's full chain

Use the **SSL Client (Standard)** PSE (personal security environment) in `STRUST`. SAP fetches
the IdP's discovery document and signing keys over TLS, so it must trust the certificate chain
that serves them. Import the leaf, the intermediates and the public root certificate authority
(CA). Then restart the Internet Communication Manager (ICM). A restart closes open HTTP
connections, so plan when you do it.

Importing only the root is a common failure that reports no error: nothing reports a missing
intermediate as a certificate problem. SAP must also be able to reach the IdP outbound. If it
cannot, the key download fails with `ThSAPOCMINIT, CM_PRODUCT_SPECIFIC_ERROR cmRc=20`. Read that
as a network, proxy or TLS block from SAP to the IdP, never as a token problem.

### SOIDC: the provider trust

In `SOIDC`, register the IdP as an OIDC provider.

- **Issuer** must match the token's `iss` exactly, character for character. Copy it from a real
  token. On Entra, the **resource** app's token version decides it. A v1 token contains
  `https://sts.windows.net/<tenant-id>/`, whichever endpoint issued it. A v2 token contains
  `https://login.microsoftonline.com/<tenant-id>/v2.0`.
- **Audience** is the audience of the token that **arrives at SAP**: the app the token was
  issued for. It is never the app client that a person signed in to. Decode the token and read
  its `aud` before you fill in the field.
- **Bearer mode** on (`OIDCMODE_BEARER = X` in table `SECOIDC_CONFIG`). Without it, the trust
  exists, but no OData endpoint accepts a bearer token.
- `AUDIENCE_EVAL_MODE`: `O` checks `aud` against the trust's own client ID, `A` accepts
  any audience.

There is one trust row per issuer, and each trust belongs to one SAP client: a trust created in
another SAP client looks empty. `SOIDC_ANALYZER` reports how many signing keys SAP has cached.
**0** means the TLS or network problem above, not a mapping problem. SOIDC can also trace a
token: paste a captured access token, and SOIDC validates it. This tells a trust problem apart
from a mapping problem before anyone looks at AWS.

### Map the token to an SAP user

SAP resolves the token to a user (transaction SU01) with two independent settings:

- the **user mapping claim**: which token claim SAP reads
- the **user mapping mechanism**: how SAP matches it to a user

Nothing checks that the two settings fit together. On SAP_BASIS 7.58, the `SECOIDC_CONFIG`
values were:

| Field | Values |
|---|---|
| `USERMAPPING_MECHANISM` | `E` E-Mail, `U` User Name, `G` Global User ID, `A` Alias, `P` Provider |
| `USERMAPPING_CLAIM` | `S` `sub`, `E` `email` (verified), `U` `email` (unverified), `O` `oid`, `N` `user_name`, `C` custom |

| Token | Join on |
|---|---|
| A person's Entra token (`obo`) | **Not `sub`**: Entra's `sub` is opaque and different for each app, and matches no SU01 user. Map a custom claim such as `preferred_username`, or `email`, with the E-Mail mechanism. The token must contain that claim (an optional access-token claim, set in Entra), and guest users have no `upn` |
| A machine token (`m2m-oidc`) | The token's **`oid`** onto the service user's **Alias** (`SU01`, **Logon Data** tab, up to 40 characters). On Entra, `oid` is the app's service principal object ID under **Enterprise applications**, not the app registration's Object ID. The Alias can contain the 36-character GUID, which the 12-character user name cannot |

`E` with `S` compares `sub` to SU01 e-mail addresses. This is right for some IdPs and
impossible for Entra, and a trust copied from another IdP keeps the setting. `U` reads an
unverified `email`. So where your users are guests in the Entra tenant, whoever can set their
mail attribute can change which SAP user they map to. A mapped e-mail address must belong to
exactly one SU01 user. With two users, SAP refuses the logon with a plain `Basic` challenge,
while SOIDC's trace still shows that the token is valid.

Each mapped user must exist and have the roles from
[sap-setup.md](sap-setup.md#2-grant-the-authorizations-pfcg). A missing role gives a 403, not a
401. A token that maps to no user gets a 401, because the server has no fallback credential on
these flows.

### Test SAP in isolation

1. **Record the 401 baseline before you change anything.** Every failed SAP logon returns the
   same 401 and logon page, so a failing call proves nothing. Send three requests to one
   service: one with the real token, one with no `Authorization` header, and one with
   `Authorization: Bearer garbage`. All three must return 401. If the real token already returns
   200, a trust already exists. After each change, run the real token again, and keep the
   request with no header as a control.
2. **The trust is reachable**: no handshake error in `SMICM` (`dev_icm`), the provider active in
   `SOIDC`, and keys cached.
3. **One service, one real token**, obtained in the same way as the flow will obtain it (below).
   A 403 means that the resolved user lacks a role for that service, or on `m2m-sap` that the
   scope is wrong.
4. **The audit proof.** Read the call back in `STAD`, or in `SM20` if the logon classes of the
   security audit log were on beforehand. It must show the user the flow should run as: the
   mapped person on `obo`, the service user on the M2M (machine-to-machine) flows. For a token
   you have, `GET /sap/bc/ui2/start_up` with that token returns the user the session mapped to
   (`{"id": "<SU01 user>", ...}`), without a write. No log on the AWS side records the user that
   SAP ran as.

Test against the host by name. A copied system keeps its SID and instance name, so nothing in a
response says which of two copies answered.

To read `STAD`:

- Set the start time to the call time, and the length to a few minutes. Check the application
  server's display offset against a timestamp you made yourself.
- **Leave the user field blank.** Filtering on the expected user hides a call that ran as
  someone else.
- Find the `SAPMHTTP` rows whose program contains the OData path. Their **User** column shows
  who the call ran as.

## M2M via SAP SOAUTH2 (`m2m-sap`)

SAP is its own authorization server and creates the token. There is no external issuer to
trust, so skip the shared steps. With client credentials, the token runs as the OAuth client's
own SU01 user, so every call reaches SAP as that user.

1. **Make the service user the client user.** Use the System-type user from
   [sap-setup.md](sap-setup.md#1-create-the-service-user-su01) as the OAuth client. It has the
   roles, which limit what the agent can do, and its password becomes the client secret.
2. **Create the OAuth 2.0 client** in `SOAUTH2` for that user, with the **client
   credentials** grant. Store its client ID and secret with
   `./scripts/deploy.sh layer run sap secrets <system>`.
3. **Assign scopes.** Enable OAuth for each service in `/IWFND/MAINT_SERVICE`. This changes the
   service's ICF (Internet Communication Framework) handler to
   `/IWFND/CL_SODATA_HTTP_HNDL_OAT`. Below NetWeaver 7.40, you need SAP Note 1797103 and the
   report `/IWFND/R_OAUTH_SCOPES` to create scopes. Only then does the service's scope ID exist,
   in the form `<SERVICE>_0001` (`API_SALES_ORDER_SRV_0001`).

   A system that registered its services under a customer prefix lists them by technical
   service name (`ZAPI_SALES_ORDER_SRV`). So filter on **Technical Service Name**, because a
   search by the external name finds nothing.

   Assign each scope to the client, **plus `/IWFND/SG_MED_CATALOG_0002`**. The server reads the
   service catalog before every call. Without that scope, every call fails with
   `insufficient_scope` naming the catalog, which looks like a fault in the service you asked
   for. The catalog scope lets the client list services, not call them. Save the client after
   you add scopes. An unsaved assignment answers `400 invalid_scope`, and `oa2_sd_cl_sc` (below)
   shows only what was saved.
4. **Configure the system**:

   ```yaml
   s4-m2m:
     system_role: sandbox
     flow: m2m-sap
     base_url: https://<sap-host>/sap/opu/odata/sap/
     client: "100"
     secret_name: <project>-<env>/sap/s4-m2m            # {"clientId", "clientSecret"}
     token_url: https://<sap-host>/sap/bc/sec/oauth2/token
     scopes: [API_PURCHASEORDER_PROCESS_SRV_0001, /IWFND/SG_MED_CATALOG_0002]
   ```

   The layer builds the provider with SAP's token endpoint as explicit metadata, because SAP
   publishes no discovery document. It sends the client in the `Authorization` header.
5. **Test** with the steps in [Test SAP in isolation](#test-sap-in-isolation). Take the token
   from SAP's token endpoint with the client's credentials. A 401 here comes from the client,
   its secret or the token endpoint, because there is no trust to configure wrongly.

   SOAUTH2 compares the client ID and secret in the `Authorization` header exactly as they are.
   It does not form-decode them. So a secret with `+` or `/` that is sent form-encoded (as RFC
   6749 describes) answers `401 invalid_client`, even though it is correct. The layer's direct
   client sends them as they are. Send them the same way when you test by hand.

The scope ID follows the service-group registration, which can differ from the service
(`ZAPI_SALES_ORDER_SRV_0001` can serve `API_SALES_ORDER_SRV`). Read which scopes a client has,
instead of working them out. Never work out an OData path from a scope name: a guessed path
returns a 500 with an empty body. With SQL access to the system (for example, the data preview
in ABAP Development Tools, ADT), you can answer most of these questions without administrator
rights:

| Question | Read |
|---|---|
| Which clients exist, and each one's user | `select client_id, client_user, client_auth_basic from oa2_sd_cl` |
| Is the client-credentials grant on | The same table, `gt_client_cred_is_active` |
| Which scopes a client has | `select scope_id from oa2_sd_cl_sc where client_id = '<client-id>'` |
| Does a request naming no scope get them all | `select client_id, is_omitted_scope_allowed from oa2_sd_cl`; when off, SAP answers `400 invalid_scope` |
| Is the token endpoint active | An unauthenticated `POST /sap/bc/sec/oauth2/token`: 404 means its ICF node is inactive, a 401 naming SICF means it is active |

## M2M via an identity provider (`m2m-oidc`)

Your IdP creates a machine token, and SAP trusts it through `SOIDC`. Every call reaches SAP as
the service user that the token maps to. A token that SAP accepts this way is a full logon as
that user: the IdP's scope does not limit which services the token can reach.

**In the IdP:**

1. **Register the machine app.** In Entra, use **App registrations → New registration**, then
   create a client secret under **Certificates & secrets**. In Okta, create an OIDC app and a
   custom authorization server. Store its client ID and secret with
   `./scripts/deploy.sh layer run sap secrets <system>`. Read its `oid` from a decoded token. On
   Entra, `oid` is the service principal's object ID (**Enterprise applications**), not the
   Object ID on the app registration.
2. **Expose what SAP's trust validates.** The token must contain an `aud` that SAP's trust
   accepts, and the `oid` that SAP maps. On Entra, `<app-id>/.default` is accepted as the
   audience, and `api://<app-id>/.default` is refused, with the same app and secret. So decode
   the token you get before you configure SAP.

**In SAP:**

1. Follow the [shared steps](#shared-steps-for-an-oidc-trust), with the IdP as issuer and the
   audience that the machine token contains.
2. Use the machine-token row of the mapping: set the service user's **Alias** to the token's
   `oid`, and map `oid` with the Alias mechanism.

**The system:**

```yaml
s4-oidc:
  system_role: sandbox
  flow: m2m-oidc
  base_url: https://<sap-host>/sap/opu/odata/sap/
  client: "100"
  secret_name: <project>-<env>/sap/s4-oidc            # {"clientId", "clientSecret"}
  discovery_url: https://login.microsoftonline.com/<tenant-id>/v2.0/.well-known/openid-configuration
  scopes: ["<app-id>/.default"]
```

Test with the steps in [Test SAP in isolation](#test-sap-in-isolation), taking the token from the
IdP with the client's credentials. A 401 comes from the trust's issuer or audience, or from the
mapping of `oid` to Alias. This sample has not run this flow with Okta.

## Entra on-behalf-of (`obo`)

`obo` is what SAP calls principal propagation. SAP runs as the signed-in person, with no second
sign-in. It needs the platform in
[direct Entra mode](../../docs/ENTERPRISE_IDP.md#direct-mode-entra-id-issues-the-tokens). The
person's token is exchanged twice, each time with Entra's on-behalf-of grant:

```
console sign-in: token for the platform app
  -> Gateway (hop one, as the platform app; asks for gateway_scopes)
  -> token for the server app  ->  the system's AWS for SAP MCP Server
  -> server (hop two, as the server app; asks for scopes)
  -> token for the SAP app  ->  SAP: SOIDC trust, token claim mapped to the person's SU01 user
```

There are three Entra apps, one for each audience. So a console token is neither a credential
for the server nor an SAP logon by itself:

| App | Is | Its token is for |
|---|---|---|
| The platform app | `identity.client_id`, already registered for direct mode | The Gateway |
| The server app | The system's secret, `{"clientId", "clientSecret"}` | This system's server runtime, which accepts it only when `azp` is the platform app |
| The SAP app | Whatever SAP's `SOIDC` trust names as audience | SAP |

The layer builds both exchanges:

- a token-vault provider for the platform app on the target, with the `TOKEN_EXCHANGE` grant and
  `gateway_scopes`
- the runtime's authorizer: the server app's audience, the platform app's `azp`, the v2 issuer
- a provider for the server app, which the server uses with `scopes`

The platform's Gateway role gets the exchange permissions in direct mode. No callback URL is
involved, because the exchange is server to server.

### In Entra

1. **The SAP app.** In **Expose an API**, set an Application ID URI and add a delegated scope,
   for example `SAP.Access`. Under **Token configuration**, add the token claim that SAP will map
   (`preferred_username` or `email`) to the **access** token. Access tokens contain no `email` by
   default. A common mistake is to add it to the ID token only. The app's token version decides
   the issuer that SAP must trust.
2. **The server app.** Register it and create a client secret. Set
   `requestedAccessTokenVersion` to 2, as for the platform app
   ([the command](../../docs/ENTERPRISE_IDP.md#entra-side-prerequisites)).
   The runtime's issuer is v2, and only a v2 token contains the `azp` that the runtime checks.
   Expose a scope (or use `api://<server-app-id>/.default`). Give the app the delegated
   permission to the SAP app's scope, and grant admin consent. Keep it a plain app registration,
   because an app with a custom signing key cannot be an on-behalf-of middle tier. Store its
   client ID and secret with `./scripts/deploy.sh layer run sap secrets <system>`.
3. **The platform app.** Give it the delegated permission to the server app's scope, and grant
   admin consent, so that hop one runs with no prompt. Do not combine `.default` with other
   delegated scopes in one request (`AADSTS70011`).

### In SAP

The [shared steps](#shared-steps-for-an-oidc-trust), with:

- **STRUST**: the chain serving
  `login.microsoftonline.com/<tenant-id>/v2.0/.well-known/openid-configuration`.
- **SOIDC**: the issuer that matches the SAP app's token version. The audience is the **SAP
  app**, not the platform app or the server app. Bearer mode is on.
- **Mapping**: the Entra row. One combination worked on a test system: E-Mail on the custom
  claim `preferred_username`, with the audience checked against the trust's own client. Mapping
  on `sub` is the most common failure specific to `obo`, and it looks like a trust problem.

### The system

```yaml
s4-person:
  system_role: sandbox
  flow: obo
  base_url: https://<sap-host>/sap/opu/odata/sap/
  client: "100"
  secret_name: <project>-<env>/sap/s4-person              # the server app's clientId and clientSecret
  gateway_scopes: ["api://<server-app-id>/.default"]      # hop one: for the server app
  scopes: ["api://<sap-app-id>/SAP.Access"]               # hop two: for the SAP app
```

No write is on by default here ([walkthrough](walkthrough.md#writes-by-flow)). To test SAP
alone, get a token for the SAP app in the same way as hop two: send an on-behalf-of request to
Entra's token endpoint as the server app, with a token for the server app as the assertion. In
the audit proof, a service user in `STAD` means that the person's identity was lost on the way.

Entra's on-behalf-of grant works only for a person's token. So a use case's unattended work (a
poller, a batch) cannot use `obo`. Give the system an
[`unattended` block](reference/sign-in.md#unattended-sign-in) on a service-account flow.

| Symptom | Likely cause |
|---|---|
| The runtime refuses the call | The token that hop one created is not for the server app (`gateway_scopes`), is v1, or its `azp` is not the platform app |
| Hop one or hop two fails at Entra | A delegated permission or its admin consent is missing (platform app to server app, server app to SAP app), or `AADSTS70011` |
| `interaction_required` | Conditional Access or MFA (multi-factor authentication) step-up on the downstream app. The client must answer the claims challenge |

### Known limitations

- **`obo` has not run live.** No test deployment runs direct mode, so the two exchanges and the
  `azp` check are tested at synth only. Test one read as a person before you rely on it.
- **The runtime's check trusts anything that holds the platform app's secret.** The runtime
  accepts a token for the server app whose `azp` is the platform app. Anyone who has the
  platform app's client secret can get such a token without a console sign-in. So can the
  platform app itself, when it gets a token for the server app outside the Gateway. Protect that
  secret as a credential for every `obo` system. The layer does not configure Entra
  pre-authorization (`preAuthorizedApplications`) on the server app. Set it yourself if your
  tenant uses it.

## Troubleshooting token rejection

**401: the trust, the audience or the mapping, almost never the signature.** SAP returns the
same 401 in all of these cases:

- the trust does not match the token (issuer or audience)
- the mapping finds no user
- the token is garbage, or there is no header at all

Only a 200 tells the cases apart, and only when you compare it with the baseline. First, decode
the token you got. The `aud` is the field that people most often read wrongly. On `obo`, it is the
SAP app, which is a different app from the one the person signed in to. Read `iss` from it too
(the token version, above). Then use SOIDC's token trace to tell a trust problem from a mapping
problem. `cmRc=20` means the network, never the token. On `m2m-sap`, a 401 comes from the client
or its secret.

**403: SAP knows who this is.** Either the resolved user lacks a role for the service
([sap-setup.md](sap-setup.md#2-grant-the-authorizations-pfcg)), or on `m2m-sap` the scope is not
that service's scope ID. A 403 says nothing about the trust, so do not go back to `SOIDC`.

**No matching user.** The token claim that SAP matches on does not identify anyone in SU01. The
usual causes are Entra's `sub`, a machine token with no claim that names a person, or an `E`/`S`
pair copied from another IdP ([the mapping](#map-the-token-to-an-sap-user)).

**Every step succeeds, and SAP still saw the service user.** Before you trust a result for one
person, read the flow from the deployed runtime, not from the repository:

- `MCP_SERVER_SAP_OAUTH_FLOW` in its environment (`aws bedrock-agentcore-control
  get-agent-runtime`) must be `ON_BEHALF_OF_TOKEN_EXCHANGE`.
- `MCP_SERVER_SAP_BASE_URL` must be the host you meant.

Then ask SAP whose name it recorded: `STAD`, or `start_up` for a token you have. Check that the
user is not the service user, not only that it is not empty. The server reads only OData services
that are in the catalog, so it cannot call `start_up` for you.
