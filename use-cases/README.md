# Use cases

A use case is what a customer builds on top of the platform. It is one folder here and one
or more stacks. It reads what the platform publishes in AWS Systems Manager (SSM) Parameter
Store, and its entry under `use_cases` in `platform.yaml` turns it on (a use case that
calls SAP also needs its row under `layers.sap.access`). The platform never imports a use case.
A use case never imports a core stack, except `RuntimeStack`. The contract between them is
`docs/PLATFORM_INTERFACE.md`.

```
use-cases/<name>/
  manifest.yaml     what the platform reads: name, owner, platform_interface, requires, stacks, entry
  stack.py          build(app, ctx, config) — your CDK
  verify.py         REQUIRED: one real invocation; OK/exit 0 or FAIL/exit 1
  ops.py            optional: operator verbs, `deploy.sh usecase run <name> <verb>`
  walkthrough.md    how to enable, build, verify, remove
```

Start one:

```bash
./scripts/deploy.sh usecase new release-notes --summary "Release notes from Jira transitions"
./scripts/deploy.sh design    # the plan now lists uc-release-notes
./scripts/deploy.sh build
./scripts/deploy.sh verify    # runs use-cases/release-notes/verify.py last
./scripts/deploy.sh usecase list
```

Add one from another repository, or remove one from this checkout:

```bash
./scripts/deploy.sh usecase add https://example.com/team/invoice-match.git [--ref v1.2.0]
./scripts/deploy.sh usecase remove invoice-match
```

`usecase add` copies the use case to `use-cases/<name>/`, links its skills and pages, and
adds its `platform.example.yaml` entries to `platform.yaml` without changing any value
you set. It refuses a use case written for another version of `docs/PLATFORM_INTERFACE.md`.
git ignores the folders it adds. `usecase remove` deletes the folder and its links, and
refuses while `platform.yaml` still names the use case. Neither command changes a
deployment. `CONTRIBUTING_USE_CASES.md` ("Use cases from other repositories") gives the
steps.

`hello-platform/` is the reference use case. `_template/` is what `usecase new` copies.
The rules for contributing one are in `CONTRIBUTING_USE_CASES.md`.

The use cases in this checkout that list their pages (`docs:` in `manifest.yaml`):

<!-- use-cases:begin -->
- [SAP exception handling](business-operations/README.md) (`business-operations`): Business process agents for SAP exception handling, on the shared platform's Gateway, Cedar engine and interceptor.
- [Claude Code and Cowork on Amazon Bedrock](claude-3p-on-bedrock/README.md) (`claude-3p-on-bedrock`): Claude Code and Claude Desktop/Cowork on Amazon Bedrock through Anthropic's Claude apps gateway, with Microsoft Entra ID sign-in and per-team model access, on the platform's private network.
- [hello-sap](hello-sap/walkthrough.md) (`hello-sap`): Reference SAP use case — reads SAP through the SAP layer as its own Gateway client, and writes nothing.
<!-- use-cases:end -->
