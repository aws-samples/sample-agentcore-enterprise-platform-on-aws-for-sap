---
name: cloud-connect-mcp-client
description: Use when someone wants to "connect AWS Quick", "use this from Quick", "add an MCP connector in Quick", "connect Copilot Studio", "author SOPs from Quick" or "let people reach the packs from their chat client" on a deployed business-operations stack. Connects a remote Model Context Protocol (MCP) client to the platform's people Gateway (gateway.people), with each person signing in as themselves (Cognito, authorization code), and attaches this use case's pack and SOP tools (people_tools). Shows who could reach what and asks before the deploy, prints the connector fields, tests denial first and then the connection, and removes it all on request.
---

# Remote MCP client connection

AWS Quick, or Copilot Studio, is a remote Model Context Protocol (MCP) client. It signs each
person in to the deployment's Amazon Cognito user pool and calls the platform's **people
Gateway** with that person's own token. The people Gateway is a second AgentCore Gateway,
separate from the one the business process agent uses:

- It admits only its remote clients.
- Its interceptor verifies each person's token.
- It gives this use case's tools the verified person, never the token.

Paths are relative to the repository root. "You" in this skill is the coding agent that runs it.

**What a person gets.** This use case's `people_tools` tiers on the
`business-operations-people` target:

- `read`: packs, standard operating procedures (SOPs, or procedures), the procedure lint, the
  base prompt, rules and versions.
- `author`: a procedure's text, the base prompt, display name and description, rule values, and
  a new pack.

Every write is a pack version recorded against the person. The console's Versions tab shows the
channel each version came through. A person gets no access to limits, the trigger mode, the
emergency stop, structure, runs, decisions or SAP calls. Say this before you start, so nobody
expects more.

**Where it works.** In a brokered deployment (`identity.mode: brokered`), with Cognito sign-in
or a brokered corporate identity provider (IdP). When the organisation requires its connectors to
sign in through its own identity provider, see [Own issuer](#own-issuer), which works under
`identity.mode: direct` too.

- `author` works only in a development stack (`promotion.role: development`, not
  `deployment.mode: production`). Validation refuses `author` in any other stack, and allows
  `read` alone there.
- Under `deployment.mode: production`, each client entry needs `allow_in_production: true`.
  Stop and ask the human before you set it.

**Never** deploy before the human has said yes. First tell them who will be able to reach what.
A person can call the tools above, as themselves, on the packs their groups allow, if both of
these are true:

- they have this client's secret;
- they can sign in to the pool as a member of `business-operations-users` and
  `business-operations-remote-authors`.

The people Gateway's endpoint is on the internet by design, because Quick and Copilot Studio
call it from their own clouds. It admits nothing without a valid token from one of its clients.
Never print the client secret.

## 1. Deployment check

```bash
grep -n -A8 "^gateway:\|^identity:\|people_tools\|promotion:" platform.yaml
./scripts/deploy.sh usecase run business-operations status
```

Note `identity.mode` (brokered), whether `gateway.people` is already on, the stack's promotion
role, and which `people_tools` tiers are set.

## 2. Configuration

In `platform.yaml`:

```yaml
gateway:
  people:
    enabled: true
    remote_clients:
      - name: quick
        regions: [us-east-1]   # the Regions people use Quick from: the first part of their Quick address
      # - name: copilot-studio
      #   callbacks: [https://global.consent.azure-apim.net/redirect]   # the bare URL until the connector exists
use_cases:
  business-operations:
    people_tools: [read, author]   # read alone outside development
```

Copilot Studio shows the connector's own redirect
(`https://global.consent.azure-apim.net/redirect/<id>`) only after the connector is created. So
deploy with the bare URL, create the connector, then replace the bare URL with the one Copilot
Studio shows, and deploy again.

## 3. Deploy

After the human's yes: `./scripts/deploy.sh build`. The deploy creates:

1. The `<project>-<env>-people-gateway` stack, with:
   - the people Gateway;
   - the app client `<project>-<env>-remote-<name>`, with its login style;
   - the resource server named with the people Gateway's URL, which Quick's `resource` needs;
   - the people interceptor.
2. The use case's `business-operations-people` target and the
   `business-operations-remote-authors` group.

A later deploy keeps them all.

**Denial first.** Before anyone connects, run `./scripts/deploy.sh verify`. It checks that:

- an anonymous call to the people Gateway gets a 401 with `WWW-Authenticate`;
- the reader app client's token is refused;
- only the people Gateway's role may invoke the target;
- the target refuses a call with no caller, and a call from outside the remote authors group.

Do not continue while it fails.

## 4. Pool users

Run `./scripts/deploy.sh usecase run business-operations add-user <email> --create --author --remote-author`.
Cognito sends the temporary password by email. To author a pack, a person also needs the pack's
own groups (`--groups business-operations-<group>` for its `entitlement.groups` and
`author_groups`). Under a brokered corporate IdP, the groups come from `identity.group_map`
instead.

## 5. Connector

`./scripts/deploy.sh usecase run business-operations remote-client-config quick` prints every
field of the connector form, with the people Gateway's URL as the endpoint. It copies the client
secret to the clipboard and never prints it. With `copilot-studio`, it prints Copilot's Manual
OAuth fields.

In Quick: **Connectors**, **Create for your team**, **Model Context Protocol**. Fill in the
printed fields. Leave the Redirect URL as Quick fills it; it must be one that the app client
allows. Share the connector with the people who should use it. Each person signs in as
themselves, so only the connector's creator handles the secret.

- After the tools change (a new tier in `people_tools`), the connector owner presses **Sync** on
  the connector's details page. A custom connector registers its tools only when it is created
  or synced. Quick Desktop may keep its old list until it restarts.
- Test as a member of the groups above, in this order:
  1. list the tools;
  2. `list_packs`;
  3. `get_sop` on one process type;
  4. `check_sop` with its text;
  5. in development, `put_sop`, with the `version` that the read returned as `based_on`.

  The console's Versions tab shows the new version from the people Gateway, with the person's
  name. The people interceptor's log (`/aws/lambda/<project>-<env>-people-interceptor`) shows
  each request's verdict. The target's log shows each call.
- Revocation: when you remove someone from `business-operations-remote-authors`, the change
  applies at their next access token, within 15 minutes.
  `aws cognito-idp admin-user-global-sign-out --user-pool-id <pool> --username <user>` ends their
  refresh tokens at once.

## Own issuer

Some organisations require a remote MCP client's connector to sign in through their own identity
provider, which then issues the token for the people Gateway itself. The people Gateway trusts
that issuer instead of the Cognito remote clients; it cannot trust both. Ask the human for the
values (never guess them, and never write them into a tracked file: `platform.yaml` is
gitignored):

```yaml
gateway:
  people:
    enabled: true
    issuer:
      discovery_url: <the provider's .well-known/openid-configuration URL>
      clients: [<the connector's client id at the provider>]
      audience: gateway        # the people Gateway's URL, or the literal the provider puts in `aud`
      groups:
        from: claim            # or pool: see below
        claim: <the claim that carries groups>
```

- **At the provider**, someone with its administration sets up an OAuth client for the
  connector (authorization code, the connector's redirect URIs), an audience equal to the people
  Gateway's URL (or a value of its own, then set as `audience`), and the claims. They hold the
  client secret; it goes only into the connector, never into this repository, a terminal
  argument or a chat.
- **Groups.** `from: claim` reads the provider's groups claim, through `identity.group_map` when
  it has entries. `from: pool` takes the groups of the pool user with the same verified email:
  only set `email_is_verified_by_issuer: true` when the human confirms the provider never lets a
  person set an unproved address, and add each person with `add-user` as in step 4. Stop and ask
  if they are not sure.
- **Deploy and denial first**, as in step 3: `verify` also checks that no remote client is left.
- **Connector.** `./scripts/deploy.sh usecase run business-operations remote-client-config issuer`
  prints the people Gateway's URL, the provider's Authorization and Token URLs from its discovery
  document, the client ids and the audience. It reads no secret and asks for none.
- **Refusals** in the people interceptor's log name the check: "not issued for this Gateway" (the
  audience), "wrong client", "wrong issuer", "the algorithm or key id is not accepted", or a pool
  lookup that found no single enabled user with that verified email.

## 6. Removal

On request: set `people_tools: []` and remove `gateway.people` (or set `enabled: false` and
remove its clients). Then deploy, after the human's yes. The deploy removes the target, the
group, the people Gateway, its app clients, the resource server and the interceptor. The people
Gateway's URL then no longer answers. Ask the human to delete the connector in Quick.

## Self-check

Report `OK: <client> connected to <project>-<env>; <n> tools listed; a procedure read and checked;
verify's denial checks pass`, or `FAIL: <step>: <message, verbatim>`.

| Message | Cause |
|---|---|
| "No access token was found for an OAuth authorization code connection" | The resource server named with the people Gateway's URL is missing: the people Gateway stack did not deploy |
| "Login pages unavailable" | The app client has no managed-login style |
| "Refused by the people Gateway: the token could not be verified" | The token was not issued for the people Gateway (it has no `aud` naming its URL, because the app client sent no `resource`), or it is an ID token, or it belongs to another app client |
| A tool answers `403` "Not a remote author" | The person is not in `business-operations-remote-authors` |
| A tool answers `401` "No caller" | The call did not come through the people Gateway's interceptor |
| No tool listed | `people_tools` is empty, the use case has not deployed since it was set, or the owner has not pressed Sync |
