<!-- markdownlint-disable MD041 -->

# Auth module

The auth module (`stacks/auth_stack.py`) declares the platform's token
issuer: the one OpenID Connect (OIDC) issuer every other component validates
JSON Web Tokens (JWTs) against. The gateway and the client-facing runtimes each
run a `CUSTOM_JWT` authorizer set to it, and the agents verify the same tokens a second time in code
([Verify caller identity](../IDENTITY.md)). It has two modes, chosen in
Design with `identity.mode`:

- **`brokered`** (default) — an Amazon Cognito user pool is the issuer.
  Optionally an enterprise identity provider (IdP) (Entra ID, Okta, Ping)
  federates in via OIDC. Users sign in at the corporate IdP, but Cognito
  creates the tokens that agents see, so nothing downstream changes
  ([Enterprise IdP federation](../ENTERPRISE_IDP.md)).
- **`direct`** — your Entra ID tenant is the issuer. No user pool is created;
  the stack only publishes the issuer facts to the same `/auth/*` interface,
  and the authorizers check the token **audience** (your app's client id)
  instead of Cognito client ids
  ([Direct mode](../ENTERPRISE_IDP.md#direct-mode-entra-id-issues-the-tokens)).

## When it deploys

| Configuration | Deployed? |
|---|---|
| Centralized / distributed (the default) | Always |
| Federated, platform account | Yes — auth is a shared service |
| Federated, workload account | No — the account consumes the platform account's issuer via the `deployment.federation` block |

Source: `expected_stacks()` in `infra_utils/platform_config.py` adds
`{prefix}-auth` when the federated role is not `workload`; `app.py` skips
`AuthStack` under the same condition. `identity.mode` does not change *whether*
the stack deploys, only what it contains — the contract, the dashboard and
`destroy` stay the same shape in both modes.

## What it creates

### `mode: brokered`

| Resource | Name pattern | Purpose |
|---|---|---|
| `AWS::Cognito::UserPool` | `{prefix}-user-pool` | Email sign-in and recovery. Self-signup is off unless `identity.self_sign_up` is true, and then only on the Cognito-only path; enterprise IdP users must enter through the corporate provider |
| Cognito user pool domain | `{prefix}-{account}` | Managed login (version 2) / OAuth endpoints (`…​.auth.{region}.amazoncognito.com`) |
| `AWS::Cognito::UserPoolResourceServer` | identifier `agentcore` | Defines the custom scope `agentcore/invoke` |
| App client | `{prefix}-app-client` | Authorization-code grant, has a secret — humans via the hosted UI |
| Web client | `{prefix}-web-client` | Authorization-code grant with PKCE, no secret — browser SPAs |
| Machine-to-machine (M2M) client | `{prefix}-m2m-client-v2` | `client_credentials` grant, scope `agentcore/invoke` — machines. The versioned name allowed the unsafe legacy credential to be replaced without an outage |
| `UserPoolIdentityProviderOidc` (optional) | `EntraID` / `Okta` / `PingIdentity` | Federated OIDC provider, created only when `idp_type` is not `cognito` |
| AWS Cloud Development Kit (CDK) custom resource + singleton Lambda | (CDK-generated) | Calls `DescribeUserPoolClient` to obtain the generated M2M secret inside this stack |
| `AWS::SecretsManager::Secret` | (CloudFormation-generated) | Stores the generated M2M secret so downstream stacks consume a dynamic reference rather than exporting the secret value |
| 9 × `AWS::SSM::Parameter` (AWS Systems Manager; 10 with an external IdP) | `/{project}/{environment}/auth/*` | Cross-stack / cross-account discovery (see Interfaces) |

### `mode: direct`

| Resource | Name pattern | Purpose |
|---|---|---|
| 6 × `AWS::SSM::Parameter` | `/{project}/{environment}/auth/*` | The issuer facts: `mode=direct`, Entra's v2 issuer, your app's client id as both `app-client-id` and `m2m-client-id`, the `.default` M2M scope, and the **name** of the Secrets Manager secret that contains the client secret |

Nothing else. The Entra app registration serves humans (authorization code)
and machines (`client_credentials`) alike; the identity module's gateway
credential provider points at Entra's discovery document and reads the same
secret.

## Configuration

Precedence at synth (when `app.py` builds the templates): CDK context > env var > `platform.yaml` > default.
`deploy.sh` reads env vars, then `platform.yaml`, then `workshop.env`.

| cdk context | Env var | `platform.yaml` | Default | Effect |
|---|---|---|---|---|
| `idp_type` | `IDP_TYPE` | `identity.idp` | `cognito` | `cognito`, `entra_id`, `okta`, or `ping` |
| `idp_mode` | `IDP_MODE` | `identity.mode` | `brokered` | `brokered` (Cognito issues) or `direct` (the IdP issues — `entra_id` only; okta/ping stay brokered) |
| `idp_tenant_id` | `IDP_TENANT_ID` | `identity.tenant_id` | `""` | Entra ID only; builds the issuer URL |
| `idp_client_id` | `IDP_CLIENT_ID` | `identity.client_id` | `""` | The IdP application's client id |
| `idp_issuer_url` | `IDP_ISSUER_URL` | `identity.issuer_url` | `""` | Okta / Ping only (cannot be derived) |
| `idp_client_secret_name` | `IDP_CLIENT_SECRET_NAME` | `identity.client_secret_name` | `""` | Secrets Manager secret **name** that contains the IdP client secret — never the value |

By default the app and web clients register no callback URL, so they offer no
hosted-UI (authorization code) sign-in. Password sign-in, Secure Remote Password
(SRP) sign-in and the M2M client still work. There is no localhost default.
Cognito accepts plain HTTP for `localhost`, so any process that can listen on
port 3000 could finish the code flow against the real pool. For local
development, add the callback by hand. The stack also accepts `callback_urls` /
`logout_urls` parameters, but `app.py` does not pass them. A `redirect_mismatch`
from Cognito means your client used a redirect URI that the client does not
register.

## Interfaces

SSM parameters under `/{project}/{environment}/auth/` (part of
[the platform interface](../PLATFORM_INTERFACE.md)):

| Parameter | brokered | direct |
|---|---|---|
| `mode` | `brokered` | `direct` — read this first; absent on platforms deployed before it existed (= brokered) |
| `issuer-url` | `https://cognito-idp.{region}.amazonaws.com/{pool-id}` | `https://login.microsoftonline.com/{tenant}/v2.0` |
| `user-pool-id` | The user pool id | — |
| `app-client-id` | Authorization-code client (humans) | Your Entra app's client id (humans and machines) |
| `web-client-id` | Authorization-code + PKCE client (browser SPAs; Cognito-only mode also permits SRP) | — |
| `m2m-client-id` | `client_credentials` client (machines) | The same Entra client id |
| `m2m-scope` | `agentcore/invoke` | `{client_id}/.default` |
| `m2m-client-secret-name` | Secrets Manager **name** containing the generated Cognito client secret | Secrets Manager **name** of the IdP client secret |

Explicit stack outputs: `IssuerUrl`, `DiscoveryUrl`, `AppClientId`,
`M2MClientId`, `IdPType`, `IdPMode` in both modes; brokered adds
`UserPoolId`, `UserPoolArn`, `WebClientId`, `DomainUrl`. `app.py` connects
these consumers:

- The gateway and runtime authorizers: issuer + allowed clients, or issuer +
  allowed audience in direct mode. `infra_utils/jwt_authorizer.py` renders both.
- The identity module: the M2M client id and secret for the gateway credential
  provider.
- The `COGNITO_ISSUER_URL` / `COGNITO_ALLOWED_CLIENTS` env vars on the
  orchestrator runtime. In direct mode they contain Entra's issuer and the
  audience; the names are kept for compatibility.

Direct mode adds
`GATEWAY_TOKEN_SCOPES` so agents request the `.default` scope Entra requires.
In a federation, `deploy.sh export` hands `issuer_url` and `m2m_client_id` to
workload accounts in either mode ([Multi-account federation](../MULTI_ACCOUNT.md)).

## Security notes

- **The IdP client secret is name-only configuration.** The stack refuses to
  synth a federated IdP without `idp_client_secret_name`, and `app.py` rejects
  any plaintext secret key outright. The value reaches CloudFormation as a
  `{{resolve:secretsmanager:...}}` dynamic reference — it never appears in
  `cdk.out`, the template, or process arguments.
- **Cognito M2M credentials stay inside the auth stack.** The generated value
  is written directly to Secrets Manager; CloudFormation and SSM publish only
  its secret name. `deploy.sh` upgrades older deployments consumer-first,
  rotates to the `-v2` client, and deletes the legacy client and stored
  credential. Its already-issued 60-minute JWTs remain accepted during a
  65-minute drain; the first later `deploy.sh` run removes the retired client
  ID from authorizers. In a federated deployment, the script refuses legacy
  deletion until two things are true. The platform authorizers accept both
  clients. The operator confirms with `FEDERATED_M2M_CONSUMERS_UPDATED=1` that
  every workload has deployed the replacement, which the safe
  `M2MClientIdV2Export` and `M2MClientSecretNameV2Export` outputs identify. Workloads set
  `FEDERATED_RETIRED_M2M_CLIENT_ID` during that deployment so their own
  authorizers also drain previously issued JWTs.
- **Mutating operations are serialized.** `deploy.sh deploy`, guided workshop
  deployments, `destroy`, and artifact export claim an atomic SSM lock for the
  project/environment. This prevents concurrent multi-phase rotations from
  recreating or deleting clients out of order. Normal exits and interrupts
  release it. After an uncatchable process termination, confirm that no
  deployment or CloudFormation operation is still active, then have the
  platform owner delete
  `/{project}/{environment}/auth/m2m-deployment-lock`; the script never takes
  over an existing lock automatically.
- **The user pool has `removal_policy=DESTROY`**: destroying the stack deletes
  the pool and every user in it. Fine for a workshop; reconsider before
  the pool contains real users.
- **Self-signup is off by default.** Create users with
  `aws cognito-idp admin-create-user`; `identity.self_sign_up: true` opens it
  for the Cognito-only path. Configuring Entra ID, Okta, or Ping disables
  signup, removes native Cognito from both user clients, and disables
  password/SRP authentication. As a result, nobody can bypass corporate
  multi-factor authentication (MFA) and conditional-access policy.
- The Cognito-only password policy is 8+ chars with upper/lower/digits and no
  symbol requirement. Treat it as a workshop default, not an enterprise
  password standard.
- The runtime and gateway authorizers validate signature, issuer, and
  **client id (brokered) or audience (direct) — not scopes**. A user token
  without `agentcore/invoke` is accepted; enforce scopes in the agent if you
  need them ([details](../IDENTITY.md)).
- **Direct mode widens who can create a platform token**: anyone who can obtain
  a token from your tenant for that app's audience. Entra's own controls
  (app role assignment required, conditional access) are the only check — the
  platform does not add one.
- Anyone with `cognito-idp:DescribeIdentityProvider` can read the federated
  IdP's client secret from `ProviderDetails` — AWS behaviour, not something
  this stack can hide. Treat that permission as sensitive.

## Verification

`scripts/check_identity.py` runs **first** in `deploy.sh verify` for any
footprint (set of deployed stacks) with an auth stack. It compares the deployed
`auth/mode` with the design. In direct mode it also creates a token exactly as
the agents do. It checks the token's `iss` against the discovery document and
its `aud` against the allowed audience, and names the fix for the two Entra
prerequisites below. Every later
check then exercises the issuer for real: `invoke.py` and `test_gateway.py`
both obtain an M2M token (`scripts/utils.py get_m2m_token`, which reads
`auth/mode` and switches endpoints), so a broken auth stack fails the whole
footprint. Preflight, `scripts/check-deploy-config.sh`
proves the IdP secret is trimmed before storage (check k) and that a
bring-your-own secret name is reused, not duplicated (check l). For federated
IdPs, [Enterprise IdP federation](../ENTERPRISE_IDP.md) has four browserless
checks that isolate failures before a human signs in.

## Known issues

- **Trailing whitespace in the IdP client secret** is the classic failure:
  stored verbatim, forwarded verbatim by Cognito, rejected by the IdP with
  `invalid_client` and no mention of whitespace. `deploy.sh` trims the value
  before storing it; if you create the secret yourself, pipe through
  `tr -d '\n\r'` ([step-by-step](../ENTERPRISE_IDP.md)).
- Entra ID apps need `--enable-id-token-issuance`; without it the login fails
  *after* the user has authenticated.
- `invalid_client_secret` from Cognito's own `/oauth2/token` usually means you
  sent another client's secret — the app client and the M2M client each have
  their own.
- Switching `idp_type` back to `cognito` removes the provider but **orphans
  its users**: they remain as `EXTERNAL_PROVIDER` accounts nobody can sign in
  as, until deleted with `admin-delete-user`.
- **Direct mode, Entra side.** Both of these pass the build without an error,
  and fail with an error at the first invoke. `check_identity.py` reports both
  by name.
  - The app needs a *service principal* in the tenant
    (`az ad sp create --id <client-id>`). Cognito federation creates one lazily
    at first sign-in; `client_credentials` never does. Otherwise you get
    `AADSTS7000229`.
  - The app must request **v2 access tokens**
    (`api.requestedAccessTokenVersion: 2`, set via a Graph PATCH;
    `az ad app update --set api.…` cannot set it). Otherwise tokens contain the
    v1 issuer `https://sts.windows.net/<tenant>/` and every authorizer rejects
    them.
- **Switching a live platform from brokered to direct** cannot go auth-first:
  the gateway, identity and runtime stacks import Cognito values as
  CloudFormation exports, and CloudFormation refuses to update auth while an
  export is imported. `deploy.sh build` detects the switch and deploys the
  consumers first (check v in `check-deploy-config.sh` tests the order). The
  reverse switch works in the default order. Either way the user pool is
  deleted and recreated — its users do not survive the round trip.
