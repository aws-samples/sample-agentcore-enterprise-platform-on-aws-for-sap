---
name: dev-add-sap-use-case
description: Use when someone wants their own use case that reads SAP through the platform's SAP layer ("a new SAP use case", "read SAP from my own use case", "scaffold a use case that calls SAP"). Scaffolds a read-only one from sap_kit/templates/sap-use-case/, gives it a layers.sap.access row, deploys and verifies it, and says what adding a write takes. Not for a new process in business-operations; that is dev-new-sap-process.
---

# Add a SAP use case

> Before you connect to an SAP system, review the
> [SAP API Policy](https://help.sap.com/doc/sap-api-policy/latest/en-US/API_Policy_latest.pdf) and
> your agreements with SAP.

The result is `use-cases/<name>/`: one stack that claims read-only SAP access for the use
case's own `reader` Gateway client, and with Cedar on, read permits for it.
[`use-cases/hello-sap/`](../../../use-cases/hello-sap/walkthrough.md) is this template,
generated and committed. Paths are relative to the repository root.

Before you start, the platform and the SAP layer are deployed, with at least one system in
`layers.sap.systems` ([`layers/sap/walkthrough.md`](../../../layers/sap/walkthrough.md)), and
identity is Cognito (brokered). Every system states its `system_role` (`sandbox`,
`development`, `quality` or `production`); only a `sandbox` may be seeded with test data,
so a use case that seeds test data checks it first. Most of the time is the deploy.

## 1. Scaffold

```bash
./scripts/deploy.sh usecase new <name> --template sap_kit/templates/sap-use-case \
  --summary "<one line: what it reads, for whom>"
```

`<name>` is lowercase letters, digits and hyphens, 3 to 41 characters. The command writes
`use-cases/<name>/` and adds `<name>: {}` under `use_cases:` in `platform.yaml`. It also adds
`!use-cases/<name>/` to `.gitignore`, so git tracks the new folder. Keep the manifest's
`requires`, its `reader` client and its `platform_interface`: the stack claims for that
client, and the synth refuses a `platform_interface` that is not the platform's.

The use case may already exist in another repository. Then add it instead of scaffolding
it, and continue at step 2:

```bash
./scripts/deploy.sh usecase add <git-url|path> [--ref <branch, tag or commit>]
```

`usecase add` copies the use case to `use-cases/<name>/` (git does not track it), links its
skills and pages, and adds its `platform.example.yaml` entries, its access row included, to
`platform.yaml`. It never changes a value already set there, and it saves the old file as
`platform.yaml.<time>.bak` first, because the rewrite removes the comments. Then it prints
what is left to fill in, such as a placeholder address or a secret to create.
`./scripts/deploy.sh usecase remove <name>` deletes the folder and its links again. It
changes no deployment.

Its manifest's `docs:` block lists its walkthrough in the platform's entry pages. Run
`python scripts/gen_use_case_index.py` to add it there, and again whenever you change that
block. `python scripts/gen_use_case_index.py --check` (part of the test suite) fails until you do.

It also writes `platform.example.yaml` (its `use_cases` entry and its access row, with who
sets each) and `tests/test_<name>.py`, which the platform suite collects: it synthesizes the
stack from that example over one sandbox system and checks that `verify.py` fails without a
deployment. `python -m pytest -q use-cases/<name>/tests` runs it alone.

## 2. Give it an access row

`layers.sap.access` is the operator's ceiling per use case
([`config.py`](../../../layers/sap/lib/sap_layer/config.py), `Access`). `read` is `"*"` or a
list of systems; `write` is `"*"` or `{system: [operations]}`; `bypass_gateway` names the
systems the use case's own code calls outside the Gateway, each on a `basic`, `m2m-sap` or
`m2m-oidc` flow or with an `unattended` block. A use case with no row gets no
SAP and fails its synth with the line to add. Read-only, as in its `platform.example.yaml`:

```yaml
layers:
  sap:
    access:
      <name>: {read: "*"}   # or {read: [s4]}
```

## 3. Deploy

```bash
./scripts/deploy.sh design   # the plan lists uc-<name>
./scripts/deploy.sh build
```

`build` updates the gateway stack (the new `reader` client), the layer (the new ceiling) and
deploys `<project>-<env>-uc-<name>`.

## 4. Verify

```bash
./scripts/deploy.sh verify
```

It runs `use-cases/<name>/verify.py` last. As the `reader`, on the first system the claim
reads, it proves a SAP read answers and a SAP write is refused, and under Cedar `ENFORCE`
that no SAP write tool is offered:

```text
OK: <name>'s reader read SAP system s4 through sap-s4, was refused odata_update, and is offered no SAP write
```

| `FAIL:` | Cause and fix |
|---|---|
| `... not found; are the SAP layer and <name> deployed?` | `uc-<name>` is not deployed, or the layer is not; run `build` |
| `the claim reads no SAP system` | The claim names no system to read: check `layers.sap.access.<name>` and the claim in `stack.py`, then redeploy `uc-<name>` |
| `tools/list for the reader lacks sap-<system>___find_sap_services` | No read permit for its scope: redeploy `uc-<name>`. Under `identity.mode: direct` pass `principals=` to `permits` |
| `... did not answer` with `success: false` | SAP refused the layer's service account: the layer's walkthrough, troubleshooting |
| `Refused by the Gateway: no use case claims SAP access for this client` | The access row was added after the use case deployed, or the claim was rewritten outside CloudFormation: redeploy `uc-<name>` |

## 5. Build on it

Add what the use case does in `stack.py`, beside the claim. Its code calls the Gateway as the
`reader`: the client id and secret name are under `gateway/use-case-clients/<name>/reader/`.
`sap.tool(system, tool)` gives the tool name the Gateway lists. Build any Lambda function with
`sap_kit/functions.py` (`uc_function`, and `gateway_lambda_target` for a tool of your own).
For a desktop MCP host, `sap_kit/mcp_gateway_proxy.py --use-case <name> --tool find_sap_services`.

## 6. Adding a write

A write is never just a claim. Make all of these changes in one deploy; the layer's check,
Cedar and the claim each refuse a write the others do not cover.

1. **The access row:** `write: {s4: [update]}`. The operation must be on in
   `layers.sap.systems.<system>.writes`.
2. **A writer client:** add `{name: writer, scopes: [read, write]}` to `gateway_clients`, and
   its client id to the claim's `callers`.
3. **The claim:** `sap.claim(stack, callers=..., read=sap.reads, write=sap.writes,
   governed_by="<your check>")`. A claim covers every caller it names, so from here only Cedar
   and the run checks keep the `reader` from writing.
4. **The Cedar write permit:** `sap.permits(..., write=sap.writes)`, keyed on
   `uc-<name>/write`.
5. **The run checks:** before a write reaches a real SAP system, the use case gets its own
   interceptor check. Name it in `governed_by` and list it in `gateway.interceptor_extensions`
   after the layer's check, with a prefix covering `sap-`. It refuses a write that presents
   no live run grant, and returns None for any caller it did not claim. The grant is a
   short-lived row the use case's run starter writes; the check reads it with
   `sap_kit/run_grant.py` (its contract is in `sap_kit/README.md`). If SAP exception handling
   is installed, its `business_operations_check/` is the worked example; `use-cases/README.md`
   lists the installed use cases. Without
   `governed_by`, the layer lets a claimed write through only while Cedar enforces.
6. **verify.py:** its write probe expects the claim to refuse; change it to the refusal your
   run checks give, and add a write that a grant lets through.
