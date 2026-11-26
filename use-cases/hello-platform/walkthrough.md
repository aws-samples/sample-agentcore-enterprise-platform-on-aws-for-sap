# hello-platform — reference use case

The smallest possible use case. It creates one AWS Systems Manager (SSM) parameter, at no
cost, to prove that your deployment's extension point works. It is also the folder you copy
to build a real use case. See `CONTRIBUTING_USE_CASES.md` for the contribution rules and
`docs/PLATFORM_INTERFACE.md` for what a use case may consume.

## Enable

In `platform.yaml` (any preset works as a base). The use case requires the gateway, and
every centralized deployment includes the gateway stack. `platform.example.yaml` has the
same entry:

```yaml
use_cases:
  hello-platform:
    greeting: ciao        # optional — your use case defines its own keys
```

An unknown name under `use_cases:` is a validation error that lists the available names.
A typo therefore stops the deploy with an error, instead of deploying nothing.

## Deploy

The stack deploys with the normal commands. It appears in `deploy.sh ls`, in the plan for
the full set of stacks, and in the contract (`python -m infra_utils.platform_config
--stacks platform.yaml`):

```bash
./scripts/deploy.sh deploy --stack <project>-<env>-uc-hello-platform
```

## Verify

```bash
python use-cases/hello-platform/verify.py
# OK: /<project>/<env>/use-cases/hello-platform/gateway-seen -> https://... lists N tool(s)
```

## Files to review before copying

- `manifest.yaml` — the only file the platform reads about you.
- `stack.py` — reads the gateway URL through the SSM interface at deploy time, and
  publishes its own output under `use-cases/<name>/`.
- `verify.py` — exits non-zero if any check fails.
