---
name: ops-promote-pack
description: Use when asked to "promote a pack", "move finance_ops to QAS", "release the pack to production", "import a bundle", "roll back a pack" or "which version is live" across business-operations stacks. Exports a pack version from the development stack, dry-runs its import into the target and shows the checks and the diff, takes the change reference from the human, applies, waits for the import, and rolls back on request. Phase 1 of promotion: bundles are not signed and no evals run on import.
---

# Promote a pack between stacks

A pack moves between stages as a **bundle**, much as an SAP transport moves a change from DEV
to QAS to PRD. A bundle is one JSON file that contains:

- the pack's `pack.json`, `base_prompt.txt`, standard operating procedures (SOPs, or
  procedures) and evaluation cases;
- a `sha256` over them;
- what the target must supply (`requires`);
- where it came from (`provenance`).

The development stack creates versions. A `quality` or `production` stack only imports them.
Every verb invokes the target's pack composer over AWS Identity and Access Management (IAM). So
your AWS credentials need `lambda:InvokeFunction` on it, and CloudTrail records your name.
"You" in this skill is the coding agent that runs it.

```bash
ops() { ./scripts/deploy.sh usecase run business-operations "$@"; }
DEV=<dev platform.yaml> QAS=<qas platform.yaml> PRD=<prd platform.yaml>
```

**What phase 1 does not do.** Say this plainly to the human:

- Bundles have no signature. The `sha256` detects corruption, not a deliberate edit. So anyone
  who may invoke a pack composer can import a bundle edited by hand.
- No evaluations run on import. QAS signs nothing, and production does not check that QAS
  passed this hash.
- There is no HTTP import route yet; there is only `ops.py`.

Until those exist, **the change reference is the attestation**. A human reads the dry run and
names the approved change. You confirm that the hash production imports is the hash QAS ran.

## Prerequisites

- Both stacks run the same code level. A bundle names the registry schema it targets, and an
  older stack refuses it. Deploy the code first, and the packs after.
- The target has every SAP system key, tool, model tier, group and contact in the bundle's
  `requires`. Create a missing group with `ops add-group business-operations-<name>`; the import
  never creates one. Add a missing contact to that stack's `platform.yaml`.

## 1. Export from development

```bash
PLATFORM_CONFIG=$DEV ops bundle status finance_ops            # the active version
PLATFORM_CONFIG=$DEV ops bundle export finance_ops -o finance_ops.bundle.json --note "why"
```

Every edit in development is already a version. So `export` returns the active version and
marks it as a published version (and ignores `--note`). The versions edited after it record that
published version as their `base`. `export` refuses a pack that is still incomplete (its SAP
values are not filled in) or invalid. Note the printed `v<N> <sha256>`.

## 2. Dry run into the target

```bash
PLATFORM_CONFIG=$QAS ops bundle import finance_ops.bundle.json --change-ref CHG0042
```

Without `--apply` it writes nothing. It returns the checks, and the diff against the target's
active pack. Nothing is edited in a quality or production stack, so that diff is everything the
import changes. It refuses:

- a `base` that is not the target's active version, which means a published version was
  skipped. The `base` is the published version before this one (`previous` for a bundle
  exported before published versions existed). This check makes sure an import never replaces a
  version that the bundle did not start from;
- a group, tool, model tier or contact that the target does not have;
- a procedure that cites a constant the pack does not declare, or a process type that another
  pack owns;
- outside development, a missing change reference (at most 128 characters).

If you import the same bundle twice, the second import changes nothing and says the bundle is
already active.

## 3. Confirm, apply, wait

Show the human the diff and the warnings, and ask for the change reference. Apply
only after they confirm it:

```bash
PLATFORM_CONFIG=$QAS ops bundle import finance_ops.bundle.json --change-ref CHG0042 --apply
PLATFORM_CONFIG=$QAS ops bundle wait <import_id>
PLATFORM_CONFIG=$QAS ops bundle status finance_ops   # the new version active
```

- **Never pass `--force` unless the human types it.** It skips the version chain check, and it
  is audited.
- If the answer was lost (a timeout), `ops bundle versions finance_ops` reads the audit trail.
  Never run `--apply` again without reading it first.
- Then repeat the same steps into production with `PLATFORM_CONFIG=$PRD`, from the **same file**.
  Compare the `sha256` that QAS reports as active with the one you import.

## 4. Roll back

```bash
PLATFORM_CONFIG=$QAS ops bundle versions finance_ops                         # stored versions
PLATFORM_CONFIG=$QAS ops bundle rollback finance_ops --to 3 --change-ref CHG0043          # dry run
PLATFORM_CONFIG=$QAS ops bundle rollback finance_ops --to 3 --change-ref CHG0043 --apply
```

A rollback imports again a version stored in that stack, and validates it again, because the
stack may have changed since then. `ops bundle retire <pack_id>` in development creates a version
that removes the pack. That version is promoted like any bundle.

## Self-check

Print one line:

- `OK: <pack_id>@<version> active in <stack> (change <ref>), sha256 <first 12>`, read back from
  `bundle status` on the target, or
- `FAIL: <cause>`: a refused check, or a target whose active `sha256` is not the bundle's.

For a dry run only, `OK: dry run of <pack_id>@<version> into <stack>: <n> change(s)` is the
line.
