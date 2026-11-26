# {{name}}

{{summary}}

## Resources

One stack, `<project>-<env>-uc-{{name}}`. It reads the platform's gateway URL from the
AWS Systems Manager (SSM) interface. It publishes the URL again under this use case's own
namespace, `/<project>/<env>/use-cases/{{name}}/gateway-seen`. This is only the starting
code. Replace it with what your use case needs, and keep the same shape: read from the
platform through SSM, and publish under your own namespace.

## Enable, build, verify

`deploy.sh usecase new` added `{{name}}: {}` under `use_cases:` in `platform.yaml`.
`platform.example.yaml` holds every entry this use case takes, commented.

```bash
./scripts/deploy.sh design      # the plan now lists uc-{{name}}
./scripts/deploy.sh build       # deploys the platform and this stack
./scripts/deploy.sh verify      # runs use-cases/{{name}}/verify.py last
```

## On the shared Gateway

If this use case declares `gateway_clients`, say here what they may call. All use cases
share the Gateway's `allowedClients`. Cedar is off by default. While it is off, or in
`security.cedar.mode: LOG_ONLY`, any allowed client can call every tool on the Gateway
unless an interceptor check refuses the call. This includes the tools of this use case.
Only Cedar `ENFORCE`, which `production` mode requires, keeps one use case's clients away
from another use case's tools.

## Configuration

Anything under `use_cases: {{name}}:` in `platform.yaml` reaches `build()` as the
`config` dict. The scaffold reads one optional key, `label`.

## Tests

`tests/` holds this use case's tests, which the platform suite collects. The first one
synthesizes the stack from `platform.example.yaml` and checks that `verify.py` fails without
a deployment. Add your own beside it.

## Remove

Destroy the stack first, while `{{name}}` is still in `platform.yaml`:
`./scripts/deploy.sh destroy --stack <project>-<env>-uc-{{name}}`. Then delete the `{{name}}:` line
from `use_cases:` and run `./scripts/deploy.sh build`. In the other order, the destroy cannot
see the stack. [Use case removal](../../docs/USE_CASE_REMOVAL.md) has every step. To remove it
from the repository as well, run `./scripts/deploy.sh usecase remove {{name}}`, which
[Removing a use case from the repository](../../CONTRIBUTING_USE_CASES.md#removing-a-use-case-from-the-repository)
describes.
