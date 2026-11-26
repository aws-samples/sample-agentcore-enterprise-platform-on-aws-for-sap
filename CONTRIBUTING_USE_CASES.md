# Contributing a use case

A use case is a self-contained product integration, such as a desktop Model Context
Protocol (MCP) client fleet, an Amazon Connect connection or an internal-API bridge. Customers
turn it on with a few lines of `platform.yaml` after the platform itself is deployed.
You own your folder; the platform guarantees the interface.

## Kinds of contribution

- **A new agent framework or pattern** → `agent-code/<pattern>/`, following an
  existing pattern (Dockerfile + agent module, selected via `agent_pattern`).
- **A platform capability** (changes auth, gateway, memory, networking for
  everyone) → a core stack. Open an issue first — core changes need
  coordination the use-case path deliberately avoids.
- **A product integration on top of the platform** → this document.

## The shape

`./scripts/deploy.sh usecase new <your-name>` scaffolds the folder from
`use-cases/_template/` and enables it in `platform.yaml`;
`use-cases/hello-platform/` is the same shape, filled in. Keep every part:

```
use-cases/<your-name>/
  manifest.yaml          # the ONLY file the platform reads about you (validated)
  stack.py               # CDK; build(app, ctx, config) is the entry point
  verify.py              # REQUIRED: one real invocation; OK/exit 0 or FAIL/exit 1
  walkthrough.md         # enable → deploy → verify, written for a participant
  platform.example.yaml  # your platform.yaml entries, with who sets each
  tests/                 # collected with the platform suite
```

`platform.example.yaml` holds your `use_cases.<your-name>` block and any other entry you
need, such as a layer's access row for you. Merged into a preset, it must load
(`tests/test_use_case_examples.py`). The scaffold's `tests/test_<your-name>.py`
synthesizes your stack from it and checks that `verify.py` fails without a deployment.

`manifest.yaml` declares:

- your identity (`name` must match the folder);
- your `owner` (you review changes here);
- `platform_interface`: the version of `docs/PLATFORM_INTERFACE.md` you wrote the use case
  against. The scaffold fills in the current one. Loading `platform.yaml`, and so the synth,
  refuses a use case whose version is missing or is not the platform's, and the error names
  both versions. When the interface version goes up, check your use case against the
  changes, then raise the number;
- what you `require` from the platform footprint, which is the set of core
  stacks a deployment contains, named by suffix (like `gateway`, `auth`);
- the `stacks` you add, which must have the `uc-` prefix so a contribution can
  never collide with a core stack;
- optionally, your `docs`: the pages the platform's entry pages list for you. Give a `title`,
  an `entry` page, and, where you have them, the `enable`, `quickstart`, `glossary`, `roles`,
  `faq` and `remove` pages and the `pages` for the documentation sidebar. Each path is a `.md` file in
  your folder, and may end in `#<heading>`. `python scripts/gen_use_case_index.py` writes them
  into the marked blocks of those pages. A platform page never links into your folder by hand.

## Use cases from other repositories

A use case can live in its own repository and be added to a checkout of the platform:

```bash
./scripts/deploy.sh usecase add https://example.com/team/invoice-match.git --ref v1.2.0
./scripts/deploy.sh usecase remove invoice-match
```

`usecase add <git-url|path> [--ref <ref>]` does these steps:

1. It gets the use case. A git URL, or the path of a local repository, is cloned, so only
   committed files arrive; `--ref` checks out a branch, tag or commit. A path to a plain
   folder is copied as it is. `manifest.yaml` must be at the top of the repository or folder.
2. It reads `manifest.yaml`. It refuses a use case whose `platform_interface` is not this
   platform's, and one whose name is already a folder under `use-cases/`.
3. It copies the use case to `use-cases/<name>/`, without `.git` or caches.
4. It runs the same steps as `check_repository_structure.py --fix` and
   `gen_use_case_index.py`: it links the use case's skills and lists its pages in the docs.
5. It merges the use case's `platform.example.yaml` into `platform.yaml`. It adds only the
   keys that `platform.yaml` does not have, and never changes a value that is already set.
   It writes the file again, which removes its comments, so first it saves a copy, with
   the comments, as `platform.yaml.<time>.bak`.
6. It prints what you still have to fill in: placeholder values, such as `example.com`
   addresses, and the Secrets Manager secrets to create. It also prints every key where your
   value differs from the example's.

`add` runs no code from the use case. The next synth runs its `stack.py`, so add only use
cases whose code you trust. `.gitignore` ignores every folder under `use-cases/` except
the shipped ones, so git does not track an added use case. To commit one to your
repository, add a `!use-cases/<name>/` line to `.gitignore`. `usecase new` adds that line
for the use case it creates.

`usecase remove <name>` takes the use case out of the checkout again:
[Removing a use case from the repository](#removing-a-use-case-from-the-repository).

## The rules the tooling enforces

- Nothing deploys unless `platform.yaml` names your use case under
  `use_cases:`. Opt-in is not a convention here; it is the mechanism.
- Your `requires` are checked against the actual footprint for each federation
  role. A use case that requires the gateway cannot be enabled in an account with
  no local gateway. The error says which side of a federation it belongs on.
- Your stacks appear in the deployment contract (`expected_stacks()`), the
  full-footprint plan, the parity gate in CI and the destroy path, with no
  work from you. All of them read the contract.
  Each stack depends on the stacks that publish the AWS Systems Manager (SSM)
  parameters it reads, through a `value_for_string_parameter` call or a
  `{{resolve:ssm:...}}` string. So `deploy --all` creates those stacks first and
  deploys independent stacks side by side. `destroy --all` removes your stacks
  before them. If none of your stacks reads a `requires` entry, every one of your
  stacks depends on it. Name a stack there when you depend on it in some other
  way, such as a custom resource that reads it at deploy time. The stacks that
  create Cedar policies deploy one at a time.
- Stack outputs are private. The deploy summary and `deploy.sh export` print
  only the output keys that `manifest.yaml` lists under `public_outputs`. The
  synth, the step where the AWS Cloud Development Kit (CDK) builds the
  CloudFormation templates, fails if one of those keys resolves a Secrets Manager secret or an SSM
  SecureString.
- Consume the platform via `docs/PLATFORM_INTERFACE.md` (SSM parameters +
  Cognito tokens), never by importing core stacks. The one exception is
  `stacks.runtime_stack.RuntimeStack` (see `docs/PLATFORM_INTERFACE.md`).
  `stack.py` gets `ctx`, a frozen `UseCaseContext`. It contains project,
  environment, prefix, ssm_prefix, region, cdk_env, and `platform`, the
  validated `platform.yaml`. The full list is in `docs/PLATFORM_INTERFACE.md`.
  `ctx` and SSM are everything you need.

## Clients on the shared Gateway

A use case that calls the Gateway with its own machine identity declares the
clients in `manifest.yaml`:

```yaml
requires: [auth, gateway]
gateway_clients:
  - name: machine
    scopes: [read, write]
```

For each client, the gateway stack does three things:

- It creates the client on the platform's user pool, with its scopes on a
  resource server named after the use case (`uc-<name>/read`).
- It adds the client to the Gateway's `allowedClients`.
- It publishes the client's id and secret name under
  `gateway/use-case-clients/<name>/<client>/`.

Amazon Cognito generates client ids. So the manifest declares the client, and
the platform creates it. When you disable the use case, the same update removes
the client and its entry in the list. A use-case client never gets
`agentcore/invoke`.

Under `identity.mode: direct` there is no user pool, and CloudFormation cannot
create the identity provider's (IdP's) app registrations. The operator creates
one registration per declared client, using client_credentials for the platform
app's `.default`. The operator names it under
`identity.use_case_clients.<name>.<client>` in `platform.yaml`. The platform
refuses a mismatch, and publishes the same two parameters. A token from such a
client contains no `scope` claim, so a use case's Cedar statements match its
`azp` claim instead.

**`allowedClients` is one list for the whole Gateway.** Cedar is off by default
(`security.cedar.enabled: false`). When Cedar is off, or in
`security.cedar.mode: LOG_ONLY`, any allowed client can call every tool on the
shared Gateway that no interceptor check (below) refuses. This includes your
clients, the platform's and another use case's, and all of them can call your
tools. Only Cedar in `ENFORCE` mode keeps use cases apart: write permits that
name your targets and your scopes. `deployment.mode: production` requires
`ENFORCE`. With more than one use case enabled and Cedar not in `ENFORCE`,
`deploy.sh design` and the synth warn. Your walkthrough must say this.

## Groups from a brokered IdP

A user federated through the platform's IdP arrives with no Cognito groups. So
a route that allows only certain `cognito:groups` never matches that user. The
deployment maps the IdP's group values to your groups in `platform.yaml`:

```yaml
identity:
  group_claim: groups            # or roles, for Entra app roles
  group_map:
    <name>:
      "<IdP group id or role value>": [<name>-approver]
```

The platform's pre token generation trigger then adds `<name>-approver` to
`cognito:groups` in that user's ID and access tokens. The trigger:

- adds only the groups the map names;
- keeps any groups the user already has;
- ignores users who did not sign in through the IdP.

You may name only groups under your own prefix. The mapping stops applying when
your use case is disabled. There is one limit: the group claim is copied into
`custom:idp_groups`, which can contain 2,048 characters. An Entra ID user in more
groups than fit there gets no mapped groups. The same happens to a user in more
groups than Entra ID puts in a token.

Your own user client, if federated users sign in through it, must list
`custom:idp_groups` in its `write_attributes`. It must also list every other
mapped attribute (`email`, `name`), or Cognito fails the sign-in. Such a client
must not also let the user write the attribute, because the user could then give
themselves a mapped group. The synth refuses any pool client that can write
`custom:idp_groups` and also allows either of these:

- the `aws.cognito.signin.user.admin` scope;
- a sign-in flow other than `ALLOW_REFRESH_TOKEN_AUTH`.

When you remove someone from an IdP group, their mapped Cognito group changes
only at their next federated sign-in. Cognito rewrites `custom:idp_groups` at
sign-in, and a token refresh keeps the old value. With Cognito's default 30-day
refresh token, the change can take a month. So such a client must also set
`refresh_token_validity` to 12 hours at most
(`infra_utils.idp_group_guard.MAX_REFRESH_MINUTES`). The synth refuses a client
that does not. The platform's own user clients set it. The cost is a sign-in at
the IdP every 12 hours. While the IdP session lasts, this sign-in usually needs
no action from the user.

## Alarms and the platform key

A use case whose alarms should reach the platform's ops inbox sets
`alarm_topic: true` in `manifest.yaml`. The platform then creates its topic even
when `observability.alarms` is off, and publishes `ops-topic/arn`. Point your
alarm and OK actions at that topic, instead of creating a second inbox.

Under `deployment.mode: production`, the platform's AWS Key Management Service (KMS)
customer managed key (CMK), published at `security/kms-key-arn`, is there to encrypt
your tables, buckets and queues. Leave your log groups on CloudWatch's default
encryption: the key policy has no statement for CloudWatch Logs, so a log group
encrypted with this key fails to deploy. Allow your roles to use it only
through a service. Every KMS statement on a key you do not define needs a
`kms:ViaService` condition. The condition names DynamoDB, S3, CloudWatch Logs,
SNS or SQS (`dynamodb.<region>.amazonaws.com`, ...). The synth refuses a
statement without one. The CDK `grant_*` helpers, on a table or bucket
encrypted with an imported key, create KMS statements with no
condition. So add the KMS statement yourself.

## Checks in the Gateway interceptor

The platform runs one Gateway interceptor. A use case may need to refuse some
calls to its own targets, for example with an authorization check or an amount
limit. It adds a check module to the interceptor through `platform.yaml`:

```yaml
gateway:
  interceptor_extensions:
    - path: use-cases/<name>/<name_as_identifier>_check  # a Python package
      target_prefixes: [<name>-]                          # the targets it governs
      request_headers: false                              # true only if it reads them
```

The directory name must be unique across every enabled extension, so name it after
your use case (`my_case_check` for `my-case`). Loading `platform.yaml` refuses two
extensions with the same directory name.

The package exposes `check(body, headers)`. It gets a copy of the JSON-RPC body
of each `tools/call` whose target name starts with one of its prefixes. It
returns `None` to allow the call, or a reason string to refuse it. The
interceptor enforces this contract:

- The check can only allow or refuse. Any other return value refuses the call.
  Nothing it returns or changes is forwarded.
- It never sees another use case's calls, `tools/list`, or a response.
- `headers` is `None` unless the check declared `request_headers: true`. Headers
  contain the caller's bearer token. The interceptor never logs or forwards them.
- If the check raises an error, or fails to import, calls to its own targets are
  refused. Every other target is unaffected.
- For a tool whose target starts with none of the prefixes your use case gives
  it, the check returns `None` at once: no refusal, no change to the body, and no
  read (no AWS call, network call or environment lookup). The interceptor already
  calls it only for its prefixes. An operator's broader prefix, or another use case
  whose name begins like yours, would otherwise hand it calls that are not yours.
  List the check under `gateway.interceptor_extensions` in your
  `platform.example.yaml`: `tests/test_interceptor_extensions.py` loads every
  installed use case's checks from there, as the interceptor loads them, and
  tests this.

Checks run in the listed order, before the egress guardrail. The first refusal
answers the call. The package is imported as `extensions.<directory name>`, so
use relative imports inside it. To read your own tables or parameters, grant
`gateway/interceptor-role-arn` access to them from your stack. The synth refuses
a grant on that role that names anything your use case does not define. The
same rule applies to the Gateway role.

Layers, resources several use cases share, are in [`layers/README.md`](layers/README.md).

## Operator commands

A command that an operator runs against a deployed use case goes in an optional
`use-cases/<your-name>/ops.py`. Examples are publishing content, rotating a
setting and printing client configuration. The command runs through the one
dispatcher, so every operator command starts the same way and gets the same
deployment settings:

```bash
./scripts/deploy.sh usecase run <your-name> <verb> [args...]
```

`ops.py` receives:

- `<verb> [args...]` as its arguments;
- the platform settings in the environment, under the names `deploy.sh` uses
  (`PROJECT_NAME`, `ENVIRONMENT`, `AWS_REGION`, ...);
- your `use_cases.<your-name>` block from `platform.yaml`, as JSON in
  `USE_CASE_CONFIG`.

It runs with the repository's `.venv` Python, from the repository root. Parse
the verb yourself, and exit non-zero on failure.

## The verify contract

`verify.py` is REQUIRED: a test fails when a discovered use case has none.
`deploy.sh verify` runs it automatically whenever your use case is enabled in
`platform.yaml`. It runs after every core check, so a broken platform fails on
the platform's own check, not on yours.

- It MUST perform one real invocation of what the use case deploys: call the
  Lambda, hit the endpoint, ask the gateway for its tools. A check that a
  resource exists proves only that CloudFormation ran. The verify must prove
  that the thing works. `hello-platform/verify.py` is the reference: it takes the URL
  out of the parameter it published and actually calls that gateway.
- Exit `0` and print one `OK: ...` line on success; exit `1` and print
  `FAIL: <cause>` otherwise. When the stack is absent, when there are no
  credentials, when the network is down: `FAIL:`, never a traceback (a test
  runs every `verify.py` with a scrubbed environment and expects exactly that,
  within 20 seconds).
- It receives the same environment as `deploy.sh verify`: AWS credentials,
  `PROJECT_NAME`, `ENVIRONMENT`, `AWS_REGION`, plus the `platform.yaml` values
  exported by `scripts/verify.py`. Read those; do not hardcode a project name.
- It runs with `scripts/` as the working directory. Import the helpers there
  rather than copying them (`sys.path.insert(0, <repo>/scripts)`, then
  `from utils import get_m2m_token`, `from test_gateway import mcp_request`).
- It MUST also work standalone: `python use-cases/<name>/verify.py` — that is
  what your walkthrough tells participants to run.

## The rules a reviewer enforces

- `verify.py` must be able to FAIL. A review refuses a verify that prints
  success whatever happens.
- Secrets follow the house pattern: Secrets Manager names in config, never
  values (see `identity:` handling in `platform.yaml` for the precedent).
- No credentials, account ids, or internal endpoints anywhere in the folder.
- The walkthrough is written for a workshop participant: enable → deploy →
  verify, with the failure modes you actually hit while building it.

## Removing a use case from the repository

A use case lives entirely in its folder, so removing it from a checkout is three commands:

```bash
rm -rf use-cases/<name>
python scripts/check_repository_structure.py --fix   # removes the links to its skills
python scripts/gen_use_case_index.py                  # removes it from the platform's entry pages
```

`./scripts/deploy.sh usecase remove <name>` runs these three steps. It refuses while
`platform.yaml` still names the use case anywhere, such as under `use_cases:`, in a layer's
access row or in an interceptor extension's path, and it changes no deployment.

Run the same two scripts after you copy a use case's folder into `use-cases/` by hand: they
add its skill links and its entries. `usecase add` runs them for you.

Before you remove a deployed use case, destroy its stacks and remove its entries from
`platform.yaml`, in that order:
[Use case removal](docs/USE_CASE_REMOVAL.md). Its own removal page adds what is specific to it.
If you delete the folder while `platform.yaml` still names the use case, the next synth stops
with `use case(s) not installed: <name>` and lists every key that names it.

## Known limitations

- **A check deploys with the platform's Gateway stack, not with your use case's stack.**
  The Gateway stack bundles every package in `gateway.interceptor_extensions` into the one
  interceptor function. So a change to your check reaches the Gateway only when the
  Gateway stack deploys. A deploy of your use case's stack alone does not deliver it. Two
  people who deploy different versions of a check to one Gateway overwrite each other's
  code, so deploy a check change from one branch at a time.

## Submitting

One branch, one pull request (PR) against `main`. Continuous integration (CI) must pass. It
already covers your folder: ruff, the synth parity gate and ASH (Automated Security Helper).
Put your tests in your folder's `tests/`. The PR description says what you verified live and
what you could not.
