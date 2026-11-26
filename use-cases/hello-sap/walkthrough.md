# hello-sap

> Before you connect to an SAP system, review the
> [SAP API Policy](https://help.sap.com/doc/sap-api-policy/latest/en-US/API_Policy_latest.pdf) and
> your agreements with SAP.

Reference SAP use case — reads SAP through the SAP layer as its own Gateway client, and writes nothing.

The [Concepts](../../docs/start/concepts.md) page defines the terms used here.

## Resources

One stack, `<project>-<env>-uc-hello-sap`, on the SAP layer (`layers/sap/`):

- Its SAP access claim, `/<project>/<env>/layers/sap/claims/hello-sap`. The `reader` client
  reads the SAP systems that its `layers.sap.access.hello-sap` row allows, and writes nothing.
  The layer's check refuses any SAP call outside the SAP access claim.
- With Cedar on, one read permit for each system, matched on the scope `uc-hello-sap/read`.

The access row is the operator's upper limit, and the SAP access claim is what the use case
says it will use. Keeping them separate means a use case cannot give itself more SAP access
than the operator allowed.

It builds no function and no business process agent. Add those next to the SAP access claim.
They call SAP with the reader client.

## Enable, build, verify

`use_cases:` in `platform.yaml` needs `hello-sap: {}`. `deploy.sh usecase new` adds this entry
when it creates the use case. Give the use case a read-only `access` row in the SAP layer's
block of `platform.yaml`. Each system there already states its `system_role`. Only a
`sandbox` system may receive test data. `platform.example.yaml` holds both entries, with
who sets each.

```yaml
layers:
  sap:
    access:
      hello-sap: {read: "*"}
```

```bash
./scripts/deploy.sh design      # the plan now lists uc-hello-sap
./scripts/deploy.sh build       # deploys the platform, the layer and this stack
./scripts/deploy.sh verify      # runs use-cases/hello-sap/verify.py last
```

`verify.py` signs in as the reader. On the first system in its SAP access claim, it proves
three things:

- An SAP read returns an answer.
- An SAP write is refused.
- Under Cedar `ENFORCE`, the tool list shows no SAP write tool.

## Identity

The Cedar permits match on the token's `scope`. This works in brokered mode (Amazon
Cognito). Under `identity.mode: direct`, pass `principals=` to `permits`: a map from each
scope to the app ids (`azp`) that hold it (`principal_condition` in `sap_kit/cedar_policies.py`).

## Adding a write

A write is not a one-line change. See `sap_kit/skills/dev-add-sap-use-case/SKILL.md`.

## Tests

`tests/` holds this use case's tests, which the platform suite collects. The first one
synthesizes the stack from `platform.example.yaml` over one sandbox system and checks that
`verify.py` fails without a deployment. Add your own beside it.

## Remove

Destroy the stack first, while `hello-sap` is still in `platform.yaml`:
`./scripts/deploy.sh destroy --stack <project>-<env>-uc-hello-sap`. Then delete `hello-sap` from
`use_cases:` and from `layers.sap.access`, and run `./scripts/deploy.sh build`, so the Gateway
drops its client. In the other order, the destroy cannot see the stack.
[Use case removal](../../docs/USE_CASE_REMOVAL.md) has every step. To remove it from the
repository as well, run `./scripts/deploy.sh usecase remove hello-sap`, which
[Removing a use case from the repository](../../CONTRIBUTING_USE_CASES.md#removing-a-use-case-from-the-repository)
describes.
