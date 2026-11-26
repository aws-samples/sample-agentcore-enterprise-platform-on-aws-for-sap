<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Layers

A use case may also require a **layer**. A layer is a set of shared resources that several
use cases build on. You enable it under `layers:` in `platform.yaml`. `layers/sap/` is the
SAP layer, and its operator commands run as `deploy.sh layer run sap <verb>`.

Layers exist so that no single use case owns resources the other use cases depend on.
When several use cases need the same resources, put them in a layer, not in the use case
that came first. Examples are a backend's connectors, its credentials and the controls in
front of it.

A layer lives in `layers/<name>/`. It deploys after the core stacks it reads and before
every use case that reads it. A use case that builds on a layer does three things:

- It declares `requires: [..., layer-<name>]` in its manifest.
- It reads the values the layer publishes under `/{project}/{environment}/layers/<name>/`
  in AWS Systems Manager (SSM) Parameter Store.
- It may import the layer's `lib/` at synth, when the AWS Cloud Development Kit (CDK)
  builds the templates.

The shape, the order and the guards are described below. This repository's SAP use cases
build on `layers/sap/`. Its walkthrough shows a use case that makes an SAP access claim for
its SAP calls and permits its own clients. `layers/sap/tests/fixtures/vendor-lookup/` is the
smallest such use case.

Layers are part of the [platform interface](../docs/PLATFORM_INTERFACE.md) from its
version 2.

## The contract

A layer has the same shape as a use case:

- A `manifest.yaml` with `name`, `owner`, `summary`, `requires`, `stacks`, `entry`,
  `public_outputs`, `alarm_topic` and `use_case_maps`. `requires` names core stacks only, and `stacks`
  starts with `layer-<name>`. There is no `gateway_clients`, because other components call
  a layer, and a layer never calls the Gateway. `use_case_maps` lists the keys of the
  layer's `platform.yaml` block that map a use case's name to its settings, such as an
  `access` row per use case. Loading `platform.yaml` refuses a name there whose use case is
  not installed, in the same error that names every other key the missing use case left.
- A `stack.py` with `build(app, ctx, config)`.
- A `verify.py`. It is REQUIRED and follows the same contract as a use case's.
- An optional `ops.py`. Its commands run as `deploy.sh layer run <name> <verb>`, and its
  block is in `LAYER_CONFIG`.
- An optional `lib/`.

`layers/sap/` is the layer that ships. It contains the AWS for SAP Model Context Protocol
(MCP) Server runtimes, their `sap-<system>` Gateway targets and the check that refuses an
SAP call no use case has claimed. Its [walkthrough](sap/walkthrough.md) shows a use case
that makes its SAP access claim through the layer's `lib/`. [Use cases](../use-cases/README.md)
lists the use cases in this checkout. If SAP exception handling is installed, it is the full
worked example on this layer.

![The whole architecture: the use case's poller reads SAP and makes one case per SAP document (SAP can also push events through the SAP layer's bus, which is optional), the agent runs on the platform, and every tool call passes the Gateway's checks on its way back to SAP](../docs/start/architecture/architecture.light.drawio.png)

- **Enabling one.** Add `layers: {<name>: {...}}` to `platform.yaml`. The block goes to the
  layer's `build()` unchanged, and the layer validates it. If the block is absent, there is
  no layer and no template changes.
- **Requiring one.** A use case lists `layer-<name>` in its manifest's `requires`, like a
  core stack. If a use case requires a layer that `platform.yaml` does not enable, config
  load fails. It fails on every synth and every `deploy.sh` verb.
- **Order.** Each layer stack and use-case stack depends on the stacks that publish the SSM
  parameters it reads. So a stack that reads a layer's `layers/<name>/` parameters deploys
  after that layer. If no stack in a build reads a `requires` entry, that entry becomes a
  dependency of every stack in the build. Destroy runs in the reverse order.
  `deploy.sh verify` runs each enabled layer's `verify.py` after the core checks and before
  any use case's.
- **Reading it.** At deploy time and run time, a use case reads only the SSM parameters the
  layer publishes under `layers/<name>/`. It never reads a CloudFormation export or a
  construct. At synth, a use case may import the layer's `lib/`, which contains pure
  functions and CDK helpers that take a scope. `app.py` puts `lib/` on `sys.path` while the
  layer is enabled. Never import the layer's `stack.py`.
- **Guards.** Three guards cover layer stacks in the same way as use-case stacks: the
  shared-role guard, the AWS Key Management Service (KMS) `ViaService` guard and the
  `public_outputs` secret guard. A layer's interceptor check is listed in
  `gateway.interceptor_extensions`, like a use case's.

| Parameter | Meaning |
|---|---|
| `layers/<name>/...` | Each enabled layer publishes what its use cases read here |

| Stack | Role |
|---|---|
| `layer-<name>` | Each enabled layer (`layer-sap`: the AWS for SAP MCP Server runtimes and their Gateway targets) |
