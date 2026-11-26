<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: Apache-2.0
-->

# sap_kit

Code that this repository's use cases share. The upstream platform has no `sap_kit/`, so a
merge from upstream never changes it. The rules for what goes here:

- Code that the platform also needs goes upstream instead.
- Code that only one use case needs stays in that use case.

`roots.txt` lists the top-level directories that this repository adds to the upstream platform.
`scripts/check_repository_structure.py` reads it.

`__init__.py` imports nothing. So a single module here can be linked into an AWS Lambda
asset on its own.

## Names and Cedar policies

- `sap_kit/names.py`: the names a use case or layer gives AWS resources. They are checked
  against service limits at synth, when the AWS Cloud Development Kit (CDK) builds the
  templates.
- `sap_kit/cedar_policies.py`: Cedar permits on the shared policy engine, each limited to its
  owner's clients. It also contains `add_policies`, which creates the permits one at a time.

## Building functions and Gateway targets

`sap_kit/functions.py` builds a use case's Lambda functions and the Gateway targets they
serve:

- `uc_function(scope, id, ctx, code, ...)`: a function on Python 3.13 and arm64, with X-Ray
  tracing, the asset excludes, and its own log group at `ctx.log_retention_days`. `code` is
  a source directory or a built `Code`. Add your grants to the function it returns.
- `code_with_wheels(src)`: the modules in `src` plus the packages in its `requirements.txt`,
  as arm64 binary wheels.
- `log_group(scope, id, ctx)`: a log group at the platform's retention, kept on delete in
  production. Use it for a resource that is not one of these functions.
- `gateway_lambda_target(scope, id, name=, fn=, tools=, gateway_id=, gateway_role=)`: the
  target `name` on the shared Gateway, which serves `tools` from `fn`. It allows the Gateway
  role to invoke `fn` only, which is what the shared-role check allows. The synth fails if a
  tool name is over the length limit.

## The MCP Gateway proxy

`sap_kit/mcp_gateway_proxy.py --use-case <name> --tool <read> ...` makes a use case's read
tools available in a desktop Model Context Protocol (MCP) host, such as Kiro or Claude Code,
over stdio. The proxy signs in as that use case's `reader` client
(`gateway/use-case-clients/<name>/reader`). It offers only the named tools on `<name>-*` and
`sap-*` targets. A use case's `mcp-config` verb, if it has one, prints the host configuration.

## Run grants

`sap_kit/run_grant.py` is the generic part of a use case's Gateway run checks. It uses the
Python standard library only. A run grant exists so that only a real run, started by the
use case's run starter, can call the tools the run checks protect.

The interceptor bundles each check's directory alone, and copies the file that a link points
to. So a check links `run_grant.py` into its own package with a relative symlink, and imports
it with `from . import run_grant`.

**The grant item.** There is one item per run in the use case's own table. Only the use
case's run starter writes it, before the run starts. Its attributes:

- `run_id`: the key.
- `client_id`: the caller key that `caller_key(claims)` returns. This is `client_id` for an
  Amazon Cognito token, `user:<oid>` for a person's Entra ID token, and `azp` for an app's
  Entra ID token.
- `secret_sha256`: the hex SHA-256 hash of a secret that only the starter and the run know.
  Only the hash is stored.
- `expires_at`: the expiry, in epoch seconds. It is also the table's time to live (TTL).
- `rehearsal`: true only for a rehearsal.

Any other attribute belongs to the use case.

**Headers.** The run id goes in `x-audit-correlation-id` (`RUN_ID_HEADER`). The secret goes in
`x-<use case>-run-grant` (`grant_header(name)`). The extension entry needs
`request_headers: true`, and no Gateway target may forward the grant header.

**API.**

- `header`, `bearer_claims`, `caller_key`.
- `read`: a consistent `GetItem`.
- `Grant.of(item)`.
- `holds_secret`: compares the secret in constant time.
- `live(grant, caller, secret, now)`: true only when the grant names the caller, has not
  expired, and the secret matches.

**What stays in the use case.** The use case decides:

- which tools require a run grant;
- which tools change data (a rehearsal refuses those);
- every check specific to the use case;
- the text of each refusal.

When a read fails, a check raises an error. It never falls back to a default. The
interceptor then refuses calls to that check's own targets.

## Building an SAP use case

> Before you connect to an SAP system, review the
> [SAP API Policy](https://help.sap.com/doc/sap-api-policy/latest/en-US/API_Policy_latest.pdf) and
> your agreements with SAP.

`sap_kit/templates/sap-use-case/` is a use-case template for reading SAP through the SAP
layer. It contains:

- a `reader` Gateway client;
- a read-only SAP access claim and Cedar read permits, made through `sap_layer.access`;
- a `verify.py` that reads SAP as that client and checks that a write is refused.

```bash
./scripts/deploy.sh usecase new <name> --template sap_kit/templates/sap-use-case
```

`use-cases/hello-sap/` is that template, generated and committed. The skill
`sap_kit/skills/dev-add-sap-use-case/SKILL.md` (linked from `.claude/skills/`) walks a coding
agent through the rest: the `layers.sap.access` row, deploy, verify, and what adding a write
takes.

## Skills

The skills here belong to no single use case. Each one is linked from `.claude/skills/`,
`.agents/skills/` and `.kiro/skills/`:

- `sap_kit/skills/start-here/SKILL.md`: the repository's entry skill. It explains the parts,
  finds out the person's role, and hands them to a page or skill of the platform, the SAP layer
  or an installed use case. It reads each use case's folder, so it names only what is there.
- `sap_kit/skills/dev-add-sap-use-case/SKILL.md`: start a new SAP use case (above).
