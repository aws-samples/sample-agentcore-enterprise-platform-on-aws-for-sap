<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Direct mode (Entra)

Terms are defined in the [glossary](glossary.md) and in [Concepts](../../../docs/start/concepts.md).

Under the platform's `identity.mode: direct`, Microsoft Entra ID issues every token, and there is
no Amazon Cognito user pool. So nothing that this use case creates on a pool exists. Instead:

| Brokered | Direct |
|---|---|
| Console signs in at the Cognito hosted page, and sends its ID token | Console signs in at Entra, and sends its access token for the platform app |
| Groups on the pool (`business-operations-users`, `-admins`, `-authors`, `-limit-owners`, `-case-workers`, `-pack-admins`, `-remote-authors` with `people_tools`, pack groups) | App roles with the same values on the platform app, read from `roles` |
| Cognito authorizer on the API | A token authorizer: Entra signature, issuer, `aud` = platform app, `azp` = console app |
| `reader`, `writer`, delegating clients on the pool | Entra app registrations you create and name in `platform.yaml` |
| Cedar keys on `uc-business-operations/*` scopes | Cedar keys on the calling app's `azp` (an Entra token has no `scope` tag) |
| `ops.py add-user` | Assign app roles in Entra (step 6) |

A run that nobody started in the console acts as the `writer` app. Such runs come from the
poller, push ingest, the queue and the trigger mode `auto`. They can use only `basic`, `m2m-sap`
and `m2m-oidc` SAP systems.

An `obo` (on behalf of) system signs in to SAP as the signed-in person, which SAP calls principal
propagation. When the SAP layer has an `obo` system that this use case may read, a run started
in the console sends the person's own token to the Gateway. So the console app has this use
case's read and write permits there, and the run checks tie that run's grant to the person
(`user:<oid>`). This does not mean that every write sends the person's identity to SAP. Only a
run started in the console, on an `obo` system, does.

The poller, the ingest consumer and the test-data seeder also have no person behind them, so
`obo` cannot sign them in. An `obo` system can be called by them only when it has an `unattended`
block, which signs them in with Basic or OAuth
([Unattended sign-in](../../../layers/sap/reference/sign-in.md#unattended-sign-in)).

**Risk, called out:** the Gateway admits any token addressed to the platform app. Until step 1's
`appRoleAssignmentRequired` is set, every app and user in the Entra tenant can get one. With
Cedar off (the platform default) or in `LOG_ONLY`, nothing tells them apart. Set
`security.cedar.enabled: true` and `security.cedar.mode: ENFORCE`. `deployment.mode: production`
requires both ([which limits apply in each Cedar mode](architecture.md#sap-access-limits)).

CloudFormation cannot create Entra objects, so create these first. Use the Azure CLI, signed in
as an administrator of the Entra tenant. `PLATFORM` is the platform's `identity.client_id`:

```bash
PLATFORM=<platform app id>
PLATFORM_OBJ=$(az ad app show --id $PLATFORM --query id -o tsv)
PLATFORM_SP=$(az ad sp show --id $PLATFORM --query id -o tsv)
```

1. **Platform app: app roles, a delegated scope, assignment required.** The roles are the group
   names. User roles: `business-operations-users`, `-admins`, `-authors`, `-limit-owners`,
   `-case-workers`, `-pack-admins` (structure changes in a development stack), `-remote-authors`
   (when `people_tools` is set), and every `business-operations-*` group that a pack's
   `entitlement` names. One application role,
   `business-operations-gateway`, is for the machine apps below. A PATCH replaces both
   collections completely. So read them first, and keep what is there, such as the platform's own
   scope and another use case's roles:

   ```bash
   az rest --method GET --uri https://graph.microsoft.com/v1.0/applications/$PLATFORM_OBJ \
     --query "{roles: appRoles, scopes: api.oauth2PermissionScopes}"
   # Add to appRoles, each with a new id (uuidgen) and isEnabled: true:
   #   {"value": "business-operations-users", "allowedMemberTypes": ["User"], ...}
   #   ...one per role above...
   #   {"value": "business-operations-gateway", "allowedMemberTypes": ["Application"], ...}
   # Add to api.oauth2PermissionScopes:
   #   {"value": "business-operations", "type": "User", "isEnabled": true, "id": "<uuid>",
   #    "adminConsentDisplayName": "Business Operations", "adminConsentDescription": "..."}
   az rest --method PATCH --uri https://graph.microsoft.com/v1.0/applications/$PLATFORM_OBJ \
     --body @platform-app.json      # {"appRoles": [...], "api": {"requestedAccessTokenVersion": 2, "oauth2PermissionScopes": [...]}}
   az ad sp update --id $PLATFORM --set appRoleAssignmentRequired=true
   ```

   `appRoleAssignmentRequired` is a choice for the whole platform: every user of the platform then
   needs a role on its app. It is what makes "has the users role" true of everyone who can get a
   token.

2. **Console app (single-page, no secret).** Its redirect URI is the console URL, which exists
   only after the first deploy (`ConsoleUrl` in the deploy summary). Add it then.

   ```bash
   CONSOLE=$(az ad app create --display-name "<prefix> business-operations console" \
     --sign-in-audience AzureADMyOrg --query appId -o tsv)
   az ad sp create --id $CONSOLE
   az rest --method PATCH --uri "https://graph.microsoft.com/v1.0/applications(appId='$CONSOLE')" \
     --body '{"spa": {"redirectUris": ["<ConsoleUrl>"]}}'
   SCOPE_ID=<id of the business-operations scope from step 1>
   az ad app permission add --id $CONSOLE --api $PLATFORM --api-permissions $SCOPE_ID=Scope
   az ad app permission grant --id $CONSOLE --api $PLATFORM --scope business-operations
   az ad app permission grant --id $CONSOLE --api 00000003-0000-0000-c000-000000000000 \
     --scope "openid profile email offline_access"
   ```

3. **`reader` and `writer` (confidential, client credentials).** Each asks for
   `<platform app id>/.default`. So its token is addressed to the platform app, and includes its
   own id in `azp`. Repeat for `writer`:

   ```bash
   READER=$(az ad app create --display-name "<prefix> business-operations reader" \
     --sign-in-audience AzureADMyOrg --query appId -o tsv)
   READER_SP=$(az ad sp create --id $READER --query id -o tsv)
   aws secretsmanager create-secret --name business-operations/entra/reader \
     --secret-string "$(az ad app credential reset --id $READER --years 1 --query password -o tsv)"
   GATEWAY_ROLE=<id of the business-operations-gateway role from step 1>
   az rest --method POST --uri https://graph.microsoft.com/v1.0/servicePrincipals/$PLATFORM_SP/appRoleAssignedTo \
     --body "{\"principalId\": \"$READER_SP\", \"resourceId\": \"$PLATFORM_SP\", \"appRoleId\": \"$GATEWAY_ROLE\"}"
   ```

4. **Delegating app (confidential).** Only `POST /runs` and the invoker have it. It asks for its
   own `.default`, so its token reaches this use case's runtime and never the Gateway. It uses v2
   tokens, so the runtime's authorizer sees the v2 issuer:

   ```bash
   DELEGATING=$(az ad app create --display-name "<prefix> business-operations delegating" \
     --sign-in-audience AzureADMyOrg --query appId -o tsv)
   az ad sp create --id $DELEGATING
   az ad app update --id $DELEGATING --identifier-uris api://$DELEGATING \
     --set api.requestedAccessTokenVersion=2
   aws secretsmanager create-secret --name business-operations/entra/delegating \
     --secret-string "$(az ad app credential reset --id $DELEGATING --years 1 --query password -o tsv)"
   ```

5. **`platform.yaml`.**

   ```yaml
   identity:
     idp: entra_id
     mode: direct
     # tenant_id, client_id, client_secret_name: the platform's own
     use_case_clients:
       business-operations:
         reader: {client_id: <READER>, client_secret_name: business-operations/entra/reader}
         writer: {client_id: <WRITER>, client_secret_name: business-operations/entra/writer}
   use_cases:
     business-operations:
       entra:
         console_client_id: <CONSOLE>
         api_scope: api://<PLATFORM>/business-operations
         delegating_client_id: <DELEGATING>
         delegating_secret_name: business-operations/entra/delegating
   ```

6. **People.** Assign each person (or a group, with Entra ID P1) the roles they need on the
   platform app. A role is added to the token at the next sign-in:

   ```bash
   az rest --method POST --uri https://graph.microsoft.com/v1.0/servicePrincipals/$PLATFORM_SP/appRoleAssignedTo \
     --body '{"principalId": "<user or group object id>", "resourceId": "'$PLATFORM_SP'", "appRoleId": "<role id>"}'
   ```

Not yet in direct mode: `tools/mcp-authoring/`, which signs in at the Cognito pool. The person's
token is not refreshed during a run. So a run that lasts longer than the token (60 to 90 minutes)
fails its later Gateway calls.

## Known limitations

- **The console app's permits cover the whole deployment.** When the SAP layer has any `obo`
  system that this use case may read, the console app gets this use case's read and write
  permits on every tool. This is per deployment, not per system or per pack. The run checks tie
  each SAP write to the person's grant. A tool that the run checks do not gate, however, can be
  called directly by any person assigned to the console app, with their own token. Assign the
  console app only to the people who use the console.
- **The person's token waits in Lambda's event queue.** `POST /runs` starts the invoker
  asynchronously, and the person's token travels in that event, outside the agent's payload.
  So Lambda holds the token until it delivers the event. No failure destination is configured,
  so a failed event is not stored anywhere. Do not add a failure destination to the invoker,
  and do not log its event, because either would store a live token.
