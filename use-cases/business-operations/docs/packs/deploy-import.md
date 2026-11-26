<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Pack import at deploy

After a stack's first deploy, no pack change needs a deploy. Packs change through the console,
REST, MCP (Model Context Protocol) or `bundle import`. In development, a deploy is still one way
to bring a pack in. This page describes what a deploy does with `packs/` in each stack.

## Development stacks

In development the checkout is the source. `packs/` is deployed as an asset. The deploy's custom
resource imports these packs as the stack's next version:

- each checkout pack that has no version yet;
- each checkout pack whose files changed since the last deploy imported it.

The import is recorded with the verb `seed` (the verb keeps its stored name), by `deploy`, with
the change reference `checkout`. A deploy that changes nothing in `packs/<id>` does not touch that
pack, so a deploy never reverts a console edit.

## Conflicts

A pack can change in both places since the last deploy imported it: in the checkout, and by a
console, REST or MCP edit. Then the deploy fails and rolls back. The error names the checkout
files, and each edit's version and author. The pack's `last refused` says the same.

Resolve the conflict in one of two ways, then deploy again:

- **Keep the edits:** move `packs/<id>` aside and
  `ops bundle export <id> --to-dir use-cases/business-operations/packs` to take them into the
  checkout.
- **Keep the checkout:** `ops bundle import packs/<id> --apply` replaces the edits with it.

The deploy compares the checkout with a baseline: the version that the last deploy imported.
On the first deploy with this rule, a pack has no record of its last deploy yet. Its baseline is
then its newest version that no edit created. A checkout that matches the baseline or any later
version is recorded and skipped. A checkout that differs is imported, unless an edit created a
version after the baseline.

## Quality and production stacks

Quality and production take a pack only by `bundle import`, whatever the checkout contains
([Pack promotion](promotion.md)). In every stack, the deploy adopts the packs that are already
active as version 0.

## Validation and the composer

`packs/` is validated at synth (`business_operations/validate_packs.py`), the step that turns the
AWS Cloud Development Kit (CDK) code into templates. These fail the synth:

- an unknown tool name;
- a procedure file that is not `.txt` or `.md`;
- a process type that two packs both map.

The pack composer refuses the same things at every write, against what is published.

The composer is the only writer of pack documents, `registry.json` and procedures. The policies
of the registry bucket and the SOP bucket deny writes to every other principal. The pack and SOP
routes invoke the composer. It runs one write at a time. The routes and the deploy retry a write
that finds it busy, and a route answers 409 when it stays busy.

Only the deploy's own import invoker can start a pack import at deploy. It proves who it is to
the composer with a secret that no operator policy grants. So invoking the composer directly runs
its verbs, but never a pack import at deploy.

The stack creates every group a checkout pack's `entitlement` names
(`business-operations-ap-clerk` and `-ap-manager` for `finance_ops`).
