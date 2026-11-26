<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Pack status

`ops pack status` prints one line per pack. `ops pack status finance_ops` prints one pack's
facts, one per line, and `--json` prints what the composer answered. The command only reads.

```text
finance_ops: v4 from import 2026-11-10T09:30 by arn:aws:sts::111111111111:assumed-role/Deployer/ana (CHG0042); routes price_variance, qty_variance; checkout matches; search current; trigger mode auto (deployment manual); no refusals
```

## Status facts

| Fact | What it says, and where it comes from |
|---|---|
| `v4 from import …` | The active version, and the audit record that made it active. The record is one of: `deploy_import` (the pack import at deploy); `adopted` (active before bundles existed, or edits made in place that the upgrade adopted); `import`; `edit` or `create` (an author's write in development); `export` (an earlier release's export, which stored development's active edits as a version); `rollback`; `retired`. The line also gives the record's time, who the composer's invoker named (`by`) and its change reference. `(not stored)` is version 0 before any verb stored it |
| `routes …` | The process types that route to the pack now. `routable: false, …` is a pack that cannot route at all, with the reason. The reason lists each `source` field that still has a survey placeholder (`incomplete at source.key[0].field`), and the SAP tools the pack names that this deployment does not serve (`tools unserved: odata_read`). A pack that may route but owns no process type shows `routes nothing`. A retired pack shows `retired` instead |
| `checkout matches` or `checkout differs: …` | The checkout's `packs/<id>` compared with the deployed files, file by file. Absent when the checkout has no such pack. In development the next deploy imports a checkout that differs, unless the pack was also edited there since the last deploy |
| `search current`, `indexing` or `stale` | Whether procedure search serves the pack's procedures as they are now. `stale` means one was written after the latest ingestion job started, or that job failed. A write that the running job missed is indexed by the next job, which the knowledge stack starts automatically. `off` means procedure search is off |
| `trigger mode …` | The pack's trigger mode and the deployment's, read from AWS Systems Manager (SSM). `manual (unset)` is a mode nobody set. Absent when the parameters cannot be read |
| `last refused …` or `no refusals` | The last write the composer refused for this pack, with the rule that refused it |
| `PRICE_VARIANCE_PCT = 2 (rule, pack v4, guarded by within_tolerance_or_approved)` | One line per symbol that the pack's procedures can cite, when you name one pack: its value here, its kind, its source and the write guards (checks that read SAP again before a payment write) that are on and read it ([SOP-cited values](sop-values.md), [Guarded rules](sop-values.md#guarded-rules)). `--json` has them as `values`, the guards as `guarded_by` |

## Refusal records

The composer records a refusal as one object per pack under `_refusals/` in the registry bucket.
It records one when it refuses:

- an applied `bundle import`, `rollback` or `retire`;
- a publish that reached it;
- a deploy's import of a pack changed in both places (verb `seed`, the pack import at deploy).

A dry run writes nothing, so it records nothing. The pack and SOP routes refuse some writes
before they reach the composer, such as an author without the grant, or any edit outside
development. Such a refusal goes back to the caller but is not recorded. Those routes may not
write to the registry bucket. Only the composer writes there.

## Console view

The console shows the short form from `GET /packs/status`: the active version on the packs index,
and the version and the last refused write on each pack's Overview.

## Versions

`GET /packs/{pack_id}/versions` lists every version that this stack stores for the pack, newest
first, a page at a time. Anyone who may see the pack may call it. For each version it shows:

- who made it, and when;
- the stack that created it, and the verb;
- the pack parts it changed compared with the version before it. A changed `constants` is split
  by symbol, so a lowered limit shows as Controls and a rule value as Procedures;
- how it became active here, from the same audit records as the status line.

A version made from a remote MCP (Model Context Protocol) client has `door` (`via: people-gateway`,
the client, the person). `door` is `null` for a version made in the console or imported
([People Gateway](../security.md#people-gateway)). `GET /packs/{pack_id}/versions/{version}`
returns one version's whole content as sealed. Both routes only read what the composer stored.
`ops bundle export` and `rollback` are still the ways to move a version.
