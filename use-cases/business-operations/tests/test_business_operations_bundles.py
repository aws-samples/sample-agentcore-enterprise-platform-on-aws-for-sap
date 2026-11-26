# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Pack promotion, phase 1: bundles, the composer's verbs, and the refusals outside DEV.

The composer runs in-process against the dict-backed S3 of `test_business_operations_packs`.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path

import boto3
import pytest

from infra_utils.use_case_context import UseCaseContext

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_business_operations_packs as tp
from test_business_operations_packs import FakeAws, FakeS3

# isort: split
# The import above put the API layer on the path.
import bundle

SOP = "finance_ops/price_variance.txt"


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "packs"
    shutil.copytree(tp.UC / "packs", root, ignore=shutil.ignore_patterns("_seed"))
    tp.second_pack(root)
    return root


def _composer(monkeypatch, s3, role="development"):
    monkeypatch.setenv("PROMOTION_ROLE", role)
    return tp._composer(monkeypatch, s3)


def _old_stack(monkeypatch, tree):
    """A stack deployed before bundles: live packs, no `_bundles/` and no `_audit/`."""
    s3 = FakeS3()
    tp._seed(_composer(monkeypatch, s3), s3, tree)
    for key in [k for k in s3.objects if k[1].startswith(("_bundles/", "_audit/"))]:
        del s3.objects[key]
    return s3


def _verb(mod, verb, **kw):
    return mod.handler({"verb": verb, "actor": "tester", **kw}, None)


def _registry(s3) -> dict:
    return json.loads(s3.objects[("reg", "registry.json")])


# ── bundle.py ──


def test_the_hash_ignores_key_order_and_the_version():
    a = bundle.unsealed("p", {"pack_id": "p", "x": 1, "y": 2}, "pr", {"a.txt": "t"}, [])
    b = bundle.unsealed("p", {"y": 2, "x": 1, "pack_id": "p"}, "pr", {"a.txt": "t"}, [])
    assert bundle.content_sha(a) == bundle.content_sha(b)
    sealed = bundle.seal(a, 3, None, {})
    assert bundle.is_sealed(sealed) and bundle.content_sha(sealed) == sealed["sha256"]
    sealed["sops"]["a.txt"] = "edited after export"
    assert not bundle.is_sealed(sealed), "an edited bundle is a draft again"


@pytest.mark.parametrize(
    "edit",
    [
        {"previous": {"version": 7, "sha256": "f" * 64}},
        {"version": 4},
        {"provenance": {"exported_by": "someone-else"}},
    ],
    ids=["previous", "version", "provenance"],
)
def test_the_seal_covers_what_the_content_hash_leaves_out(edit):
    a = bundle.unsealed("p", {"pack_id": "p"}, "pr", {"a.txt": "t"}, [])
    sealed = bundle.seal(a, 3, {"version": 2, "sha256": "e" * 64}, {"exported_by": "x"})
    assert bundle.is_sealed(sealed)
    assert not bundle.is_sealed({**sealed, **edit})


def test_a_checkout_directory_round_trips(tree, tmp_path):
    original = bundle.from_dir(tree / "finance_ops")
    assert original["version"] is None and "$schema" not in original["pack"]
    bundle.to_dir(original, tmp_path / "finance_ops")
    assert bundle.content_sha(bundle.from_dir(tmp_path / "finance_ops")) == (
        bundle.content_sha(original)
    )
    needs = bundle.requires(original)
    assert "business-operations-ap-manager" in needs["groups"]
    assert needs["model_tiers"] and needs["tools"]


# ── version 0 and the DEV round trip ──


def test_an_old_stack_adopts_its_live_packs_as_version_0(monkeypatch, tree):
    s3 = _old_stack(monkeypatch, tree)
    mod = _composer(monkeypatch, s3)
    live = mod._snapshot("finance_ops")
    tp._seed(mod, s3, tree)  # the first deploy of this code
    v0 = json.loads(s3.objects[("reg", "_bundles/finance_ops/0.json")])
    assert v0["sha256"] == bundle.content_sha(live)
    assert _verb(mod, "status", pack_id=tp.SECOND)["active"]["version"] == 0
    # The live state never held the checkout's evals, so the checkout is a change: it
    # follows version 0 as version 1, and changes nothing else.
    pointer = json.loads(s3.objects[("reg", "_bundles/finance_ops/active.json")])
    v1 = json.loads(s3.objects[("reg", "_bundles/finance_ops/1.json")])
    assert pointer["version"] == 1 and v1["previous"]["version"] == 0
    assert mod._drift(v0, v1) == ["evals.json"]
    assert (
        s3.objects[("sops", SOP)]
        == (tree / "finance_ops/sops/price_variance.txt").read_bytes()
    ), "adoption writes nothing a run reads"


def test_export_import_reimport_and_rollback(monkeypatch, tree):
    s3 = _old_stack(monkeypatch, tree)
    mod = _composer(monkeypatch, s3)
    original = s3.objects[("sops", SOP)]

    exported = _verb(mod, "export", pack_id="finance_ops")["bundle"]
    assert exported["version"] == 0

    edited = json.loads(json.dumps(exported))
    edited["sops"]["price_variance.txt"] += "\nOne more rule.\n"
    puts = len(s3.puts)
    dry = _verb(mod, "import", bundle=edited)
    assert dry["result"] == "would activate", dry
    assert dry["diff"]["files"]["changed"] == ["sops/price_variance.txt"]
    assert dry["to"]["version"] == 1 and dry["from"]["version"] == 0
    assert len(s3.puts) == puts, "a dry run writes nothing"

    applied = _verb(mod, "import", bundle=edited, dry_run=False)
    assert applied["result"] == "active", applied
    assert s3.objects[("sops", SOP)].endswith(b"One more rule.\n")
    assert s3.meta[("sops", SOP)]["bundle-sha256"] == applied["to"]["sha256"]
    assert _registry(s3)["packs"]["finance_ops"]["bundle"]["version"] == 1
    assert (
        _verb(mod, "import_status", import_id=applied["import_id"])["state"] == "active"
    )

    puts = len(s3.puts)
    again = _verb(mod, "import", bundle=edited, dry_run=False)
    assert again["result"] == "already active" and len(s3.puts) == puts

    assert (
        _verb(mod, "rollback", pack_id="finance_ops", to=0)["result"]
        == "would activate"
    )
    back = _verb(mod, "rollback", pack_id="finance_ops", to=0, dry_run=False)
    assert back["result"] == "active", back
    assert s3.objects[("sops", SOP)] == original
    status = _verb(mod, "status", pack_id="finance_ops")
    assert status["active"]["version"] == 0 and "drift" not in status
    assert status["versions"] == [0, 1]
    verbs = [r["verb"] for r in _verb(mod, "versions", pack_id="finance_ops")["audit"]]
    assert verbs.count("rollback") == 1 and "import" in verbs


def test_a_failed_registry_write_puts_the_sops_back(monkeypatch, tree):
    s3 = _old_stack(monkeypatch, tree)
    mod = _composer(monkeypatch, s3)
    original = s3.objects[("sops", SOP)]
    edited = _verb(mod, "export", pack_id="finance_ops")["bundle"]
    edited["sops"]["price_variance.txt"] = "Replaced."
    real = s3.put_object

    def failing(**kw):
        if kw["Key"] == "registry.json":
            raise RuntimeError("registry write refused")
        return real(**kw)

    monkeypatch.setattr(s3, "put_object", failing)
    with pytest.raises(RuntimeError):
        _verb(mod, "import", bundle=edited, dry_run=False)
    assert s3.objects[("sops", SOP)] == original
    assert (
        json.loads(s3.objects[("reg", "_bundles/finance_ops/active.json")])["version"]
        == 0
    )


def test_retire_removes_the_pack_and_its_sops(monkeypatch, tree):
    s3 = _old_stack(monkeypatch, tree)
    mod = _composer(monkeypatch, s3)
    done = _verb(mod, "retire", pack_id=tp.SECOND, dry_run=False)
    assert done["result"] == "active", done
    assert tp.SECOND not in _registry(s3)["packs"]
    assert not [k for b, k in s3.objects if k.startswith(f"{tp.SECOND}/")]
    assert _verb(mod, "status", pack_id=tp.SECOND)["active"]["retired"]
    # The tombstone is exported like any version, so the removal can be imported in QAS.
    tombstone = _verb(mod, "export", pack_id=tp.SECOND)["bundle"]
    assert tombstone["retired"] and bundle.is_sealed(tombstone)
    qas = FakeS3()
    other = _composer(monkeypatch, qas, "quality")
    tp._seed(other, qas, tree)
    assert _verb(other, "import", bundle=tombstone, change_ref="CHG9")["status"] < 500


def test_a_previous_without_a_version_is_refused_not_a_crash(monkeypatch, tree):
    v1 = _sealed(monkeypatch, tree)
    odd = bundle.seal({**v1}, 2, {"sha256": v1["sha256"]}, {})
    qas = FakeS3()
    mod = _composer(monkeypatch, qas, "quality")
    tp._seed(mod, qas, tree)
    # Read as following nothing, which is where this environment is: no KeyError.
    assert _verb(mod, "import", bundle=odd, change_ref="CHG2")["status"] == 200


def test_a_pack_id_with_a_trailing_newline_is_not_a_pack_id(monkeypatch, tree):
    mod = _composer(monkeypatch, FakeS3())
    assert _verb(mod, "status", pack_id="finance_ops\n")["status"] == 400


def _append(path: Path, text: str) -> None:
    path.write_text(path.read_text(encoding="utf-8") + text + "\n", encoding="utf-8")


def _pointer(s3, pack_id="finance_ops") -> dict:
    return json.loads(s3.objects[("reg", f"_bundles/{pack_id}/active.json")])


def test_the_deploy_import_skips_an_unchanged_checkout_and_keeps_a_console_edit(
    monkeypatch, tree
):
    s3 = FakeS3()
    mod = _composer(monkeypatch, s3)
    tp._seed(mod, s3, tree)
    assert _pointer(s3)["version"] == 1
    seeded = json.loads(s3.objects[("reg", "_bundles/finance_ops/seeded.json")])
    assert seeded["version"] == 1
    _edit(mod, pack_id="finance_ops", pack={"display_name": "Edited in the console"})
    puts = len(s3.puts)
    tp._seed(mod, s3, tree)
    assert _pointer(s3)["version"] == 2, "an unrelated deploy never reverts an edit"
    assert _registry(s3)["packs"]["finance_ops"]["pack"]["display_name"] == (
        "Edited in the console"
    )
    assert not [
        k for _, k in s3.puts[puts:] if k.endswith(("/active.json", "/seeded.json"))
    ]


def test_the_deploy_import_takes_a_changed_checkout_as_the_next_version(
    monkeypatch, tree
):
    s3 = FakeS3()
    mod = _composer(monkeypatch, s3)
    tp._seed(mod, s3, tree)
    (tree / "finance_ops/sops/price_variance.txt").write_text("A checkout change.")
    tp._seed(mod, s3, tree)
    assert s3.objects[("sops", SOP)] == b"A checkout change."
    pointer = _pointer(s3)
    assert pointer["version"] == 2 and pointer["activated_by"] == "deploy"
    status = _verb(mod, "status", pack_id="finance_ops")
    assert status["origin"] == {
        "via": "deploy_import",
        "at": status["origin"]["at"],
        "by": "deploy",
        "change_ref": "checkout",
    }
    assert _verb(mod, "status", pack_id=tp.SECOND)["active"]["version"] == 1
    tp._seed(mod, s3, tree)
    assert _pointer(s3)["version"] == 2, "imported once"


def test_the_deploy_import_refuses_a_pack_changed_in_the_checkout_and_the_console(
    monkeypatch, tree
):
    s3 = FakeS3()
    mod = _composer(monkeypatch, s3)
    tp._seed(mod, s3, tree)
    _edit(mod, pack_id="finance_ops", pack={"display_name": "Console"})
    (tree / "finance_ops/sops/price_variance.txt").write_text("A checkout change.")
    _append((tree / tp.SECOND / "base_prompt.txt"), "Another pack's change.")
    with pytest.raises(RuntimeError) as failed:
        tp._seed(mod, s3, tree)
    message = str(failed.value)
    assert "finance_ops: the checkout changed" in message, message
    assert "(sops/price_variance.txt)" in message
    assert "(v2 by author@example.com)" in message
    for way_out in ("bundle export finance_ops --to-dir", "bundle import "):
        assert way_out in message
    assert _pointer(s3)["version"] == 2
    assert _pointer(s3, tp.SECOND)["version"] == 1, "a refusal imports no pack"
    status = _verb(mod, "status", pack_id="finance_ops")
    assert status["last_refusal"]["verb"] == "seed"
    assert "the checkout changed" in status["last_refusal"]["error"]

    # The second way out: import the checkout explicitly. The next deploy takes it.
    imported = _verb(
        mod, "import", bundle=bundle.from_dir(tree / "finance_ops"), dry_run=False
    )
    assert imported["result"] == "active", imported
    tp._seed(mod, s3, tree)
    assert _pointer(s3)["version"] == 3
    assert _pointer(s3, tp.SECOND)["version"] == 2


def test_an_edit_an_import_replaced_does_not_block_a_later_checkout(monkeypatch, tree):
    s3 = FakeS3()
    mod = _composer(monkeypatch, s3)
    tp._seed(mod, s3, tree)
    _edit(mod, pack_id="finance_ops", pack={"display_name": "Console"})
    # The operator settles the edit by importing the checkout over it...
    imported = _verb(
        mod, "import", bundle=bundle.from_dir(tree / "finance_ops"), dry_run=False
    )
    assert imported["result"] == "active", imported
    # ...and later the checkout changes: the replaced edit is no longer live.
    (tree / "finance_ops/sops/price_variance.txt").write_text("A later change.")
    tp._seed(mod, s3, tree)
    assert s3.objects[("sops", SOP)] == b"A later change."
    assert _pointer(s3)["version"] == 4


def test_the_deploy_import_takes_an_export_of_the_edits_as_the_checkout(
    monkeypatch, tree
):
    s3 = FakeS3()
    mod = _composer(monkeypatch, s3)
    tp._seed(mod, s3, tree)
    _edit(mod, pack_id="finance_ops", pack={"display_name": "Console"})
    shutil.rmtree(tree / "finance_ops")
    bundle.to_dir(
        _verb(mod, "export", pack_id="finance_ops")["bundle"], tree / "finance_ops"
    )
    tp._seed(mod, s3, tree)
    assert _pointer(s3)["version"] == 2
    seeded = json.loads(s3.objects[("reg", "_bundles/finance_ops/seeded.json")])
    assert seeded["version"] == 2
    # Now the checkout is the source again: a change to it is imported.
    _append((tree / "finance_ops/base_prompt.txt"), "Changed after the export.")
    tp._seed(mod, s3, tree)
    assert _pointer(s3)["version"] == 3


def _before_seed_records(s3):
    for key in [k for k in s3.objects if k[1].endswith("/seeded.json")]:
        del s3.objects[key]


def test_the_first_deploy_import_after_the_upgrade_takes_the_active_version_as_its_baseline(
    monkeypatch, tree
):
    s3 = FakeS3()
    mod = _composer(monkeypatch, s3)
    tp._seed(mod, s3, tree)
    _edit(mod, pack_id="finance_ops", pack={"display_name": "Console"})
    _before_seed_records(s3)
    # The checkout matches no version from the baseline (v1, the newest no edit
    # minted) on, and v2 is an edit after it: refused.
    _append((tree / "finance_ops/base_prompt.txt"), "A checkout change.")
    with pytest.raises(RuntimeError, match=r"v2 by author@example.com"):
        tp._seed(mod, s3, tree)

    # The same stack with no edit since its baseline takes the checkout.
    s3 = FakeS3()
    mod = _composer(monkeypatch, s3)
    shutil.copy(tp.UC / "packs/finance_ops/base_prompt.txt", tree / "finance_ops")
    tp._seed(mod, s3, tree)
    _before_seed_records(s3)
    tp._seed(mod, s3, tree)
    assert _pointer(s3)["version"] == 1, "a checkout matching the active version"
    assert (
        json.loads(s3.objects[("reg", "_bundles/finance_ops/seeded.json")])["version"]
        == 1
    )
    _before_seed_records(s3)
    _append((tree / "finance_ops/base_prompt.txt"), "A checkout change.")
    tp._seed(mod, s3, tree)
    assert _pointer(s3)["version"] == 2


def test_a_rollback_neither_imports_nor_refuses_a_checkout(monkeypatch, tree):
    s3 = FakeS3()
    mod = _composer(monkeypatch, s3)
    tp._seed(mod, s3, tree)
    _edit(mod, pack_id="finance_ops", pack={"display_name": "Console"})
    _append((tree / "finance_ops/base_prompt.txt"), "A checkout change.")
    s3.objects[("assets", "packs.zip")] = tp._packs_zip(tree)
    # The template before this release sends neither property.
    rollback = {
        "DeployImportProof": "deploy-proof",
        "RequestType": "Update",
        "PhysicalResourceId": "pack-seed",
        "ResourceProperties": {"AssetBucket": "assets", "AssetKey": "packs.zip"},
    }
    assert mod.handler(rollback, None) == {"PhysicalResourceId": "pack-seed"}
    assert _pointer(s3)["version"] == 2


# ── outside development ──


@pytest.mark.parametrize("role", ["quality", "production"])
def test_the_deploy_import_publishes_no_pack_outside_development(
    monkeypatch, tree, role
):
    s3 = FakeS3()
    tp._seed(_composer(monkeypatch, s3, role), s3, tree)
    assert _registry(s3)["packs"] == {}
    assert _registry(s3)["platform_prompt"], "the platform prompt is deploy-owned"


@pytest.mark.parametrize("role", ["quality", "production"])
def test_the_deploy_import_never_imports_a_changed_checkout_outside_development(
    monkeypatch, tree, role
):
    v1 = _sealed(monkeypatch, tree)
    s3 = FakeS3()
    mod = _composer(monkeypatch, s3, role)
    tp._seed(mod, s3, tree)
    _verb(mod, "import", bundle=v1, change_ref="CHG1", dry_run=False)
    _append((tree / "finance_ops/base_prompt.txt"), "A checkout change.")
    tp._seed(mod, s3, tree)
    assert _pointer(s3)["version"] == 1
    assert not [k for k in s3.objects if k[1].endswith("/seeded.json")]


def _sealed(monkeypatch, tree, pack_id="finance_ops"):
    """A DEV stack's v1 of a pack, sealed by its export."""
    dev = FakeS3()
    mod = _composer(monkeypatch, dev)
    tp._seed(mod, dev, tree)
    return _verb(mod, "export", pack_id=pack_id)["bundle"]


def test_quality_imports_a_sealed_bundle_with_a_change_reference(monkeypatch, tree):
    v1 = _sealed(monkeypatch, tree)
    qas = FakeS3()
    mod = _composer(monkeypatch, qas, "quality")
    tp._seed(mod, qas, tree)

    no_ref = _verb(mod, "import", bundle=v1, dry_run=False)
    assert no_ref["status"] == 400 and "change reference" in no_ref["error"]
    long_ref = _verb(mod, "import", bundle=v1, change_ref="x" * 129)
    assert long_ref["status"] == 400

    draft = {**v1, "sops": {**v1["sops"], "price_variance.txt": "edited in the ticket"}}
    refused = _verb(mod, "import", bundle=draft, change_ref="CHG1")
    assert refused["status"] == 400 and "Only development mints" in refused["error"]

    done = _verb(mod, "import", bundle=v1, change_ref="CHG1", dry_run=False)
    assert done["result"] == "active", done
    assert done["to"] == {"version": 1, "sha256": v1["sha256"]}
    assert ("sops", SOP) in qas.objects

    # Nothing is edited here, so an export is the active version, and publishes nothing.
    again = _verb(mod, "export", pack_id="finance_ops")["bundle"]
    assert again["sha256"] == v1["sha256"] and bundle.is_sealed(again)
    assert ("reg", "_bundles/finance_ops/released.json") not in qas.objects


def _next(b: dict, version: int) -> dict:
    """A sealed version after `b`, as a long-lived DEV stack would export it."""
    return bundle.seal(
        {**b, "base_prompt": b["base_prompt"] + f"\nv{version}\n"},
        version,
        {"version": b["version"], "sha256": b["sha256"]},
        {},
    )


def test_an_import_out_of_order_is_refused_unless_forced(monkeypatch, tree):
    v1 = _sealed(monkeypatch, tree)
    v3 = _next(_next(v1, 2), 3)
    qas = FakeS3()
    mod = _composer(monkeypatch, qas, "quality")
    tp._seed(mod, qas, tree)
    first = _verb(mod, "import", bundle=v1, change_ref="CHG1", dry_run=False)
    assert first["result"] == "active", first
    skipped = _verb(mod, "import", bundle=v3, change_ref="CHG3")
    assert skipped["status"] == 409 and "v3 follows" in skipped["error"]
    forced = _verb(mod, "import", bundle=v3, change_ref="CHG3", force=True)
    assert forced["result"] == "would activate", forced
    # Pointing `previous` at what this environment holds breaks the seal, so the skip
    # is a refused draft rather than an unforced import.
    edited = {**v3, "previous": {"version": 1, "sha256": v1["sha256"]}}
    refused = _verb(mod, "import", bundle=edited, change_ref="CHG3")
    assert refused["status"] == 400 and "Only development mints" in refused["error"]


@pytest.mark.parametrize("role", ["quality", "production"])
def test_a_first_import_into_an_empty_stack_needs_no_force(monkeypatch, tree, role):
    """A long-lived DEV's v5 follows v4, which a fresh stack never held: nothing to
    diverge from, so it is a normal, unforced import."""
    v5 = _next(_next(_next(_next(_sealed(monkeypatch, tree), 2), 3), 4), 5)
    fresh = FakeS3()
    mod = _composer(monkeypatch, fresh, role)
    tp._seed(mod, fresh, tree)
    dry = _verb(mod, "import", bundle=v5, change_ref="CHG5")
    assert dry["result"] == "would activate", dry
    done = _verb(mod, "import", bundle=v5, change_ref="CHG5", dry_run=False)
    assert done["result"] == "active", done
    record = _verb(mod, "import_status", import_id=done["import_id"])
    assert record["state"] == "active" and record["forced"] is False
    # Once a version is here, the chain holds again.
    v7 = _next(_next(v5, 6), 7)
    assert _verb(mod, "import", bundle=v7, change_ref="CHG7")["status"] == 409


def test_a_refused_import_is_audited_and_the_actor_is_named_as_asserted(
    monkeypatch, tree
):
    v1 = _sealed(monkeypatch, tree)
    qas = FakeS3()
    mod = _composer(monkeypatch, qas, "quality")
    tp._seed(mod, qas, tree)
    draft = {**v1, "provenance": {"exported_by": "someone else"}}
    assert (
        _verb(mod, "import", bundle=draft, change_ref="CHG1", dry_run=False)["status"]
        == 400
    )
    _verb(mod, "import", bundle=v1, change_ref="CHG1", dry_run=False)
    audit = _verb(mod, "versions", pack_id="finance_ops")["audit"]
    refused = [r for r in audit if r["state"] == "refused"]
    assert len(refused) == 1 and refused[0]["change_ref"] == "CHG1"
    # Nothing verifies who invoked the composer, so no record claims it did.
    assert all("actor" not in r and r["asserted_actor"] for r in audit)
    assert {r["asserted_actor"] for r in audit if r["verb"] == "import"} == {"tester"}


def test_a_bundle_stored_before_the_seal_is_sealed_and_stored_again(monkeypatch, tree):
    dev = FakeS3()
    mod = _composer(monkeypatch, dev)
    tp._seed(mod, dev, tree)
    v1 = _verb(mod, "export", pack_id="finance_ops")["bundle"]
    key = ("reg", "_bundles/finance_ops/1.json")
    old = {k: v for k, v in v1.items() if k != "seal_sha256"}
    dev.objects[key] = json.dumps(old).encode()
    assert bundle.is_sealed(mod._stored("finance_ops", 1))
    assert bundle.is_sealed(json.loads(dev.objects[key]))


@pytest.mark.parametrize("role", ["quality", "production"])
def test_every_edit_create_and_retire_are_refused_outside_development(
    monkeypatch, tree, role
):
    v1 = _sealed(monkeypatch, tree)
    s3 = FakeS3()
    mod = _composer(monkeypatch, s3, role)
    tp._seed(mod, s3, tree)
    _verb(mod, "import", bundle=v1, change_ref="CHG1", dry_run=False)
    before = dict(s3.objects)
    sop = {"op": "put", "name": "price_variance.txt", "content": "Hotfix."}
    for edit in (
        {"roles": ["pack_admin", "author"], "pack": {"constants": {"X_PCT": 9}}},
        {"roles": ["author"], "pack": {"display_name": "Hotfix"}},
        {"roles": ["author"], "sop": sop},
        {"roles": ["author"], "sop": {"op": "delete", "name": "price_variance.txt"}},
        {"tier": "create", "create": tp._SHADOW_SPEC, "pack_id": "new_pack"},
    ):
        refused = mod.handler({"pack_id": "finance_ops", **edit}, None)
        assert refused["status"] == 403, (edit, refused)
        assert f"not edited in {role}" in refused["error"]
    assert s3.objects == before, "nothing written, not even a refusal record"
    assert _verb(mod, "retire", pack_id="finance_ops")["status"] == 400
    assert "drift" not in _verb(mod, "status", pack_id="finance_ops")


# ── the routes outside development ──


def _packs_route(monkeypatch, role, published=None):
    sent = []

    class Lambda:
        def invoke(self, Payload, **_):
            sent.append(json.loads(Payload))

            class Body:
                def read(self):
                    return b'{"packs": 1}'

            return {"Payload": Body()}

    aws = FakeAws({})
    for k, v in {
        "API_ALLOWED_CLIENTS": tp.CONSOLE,
        "USERS_GROUP": tp.USERS,
        "SOP_AUTHOR_GROUPS": tp.AUTHORS,
        "REGISTRY_BUCKET": "reg",
        "PACK_COMPOSER_FUNCTION": "composer",
        "PROMOTION_ROLE": role,
    }.items():
        monkeypatch.setenv(k, v)
    aws.objects[("reg", "registry.json")] = json.dumps(
        published or tp.PUBLISHED
    ).encode()
    import registry

    monkeypatch.setattr(registry, "_get_s3", lambda: aws)
    monkeypatch.setattr(
        boto3, "client", lambda name, *a, **k: Lambda() if name == "lambda" else aws
    )
    spec = importlib.util.spec_from_file_location(
        "bo_packs_promo", tp.UC / "api" / "packs" / "index.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, sent


def _publish(mod, body, groups=(tp.USERS, tp.AUTHORS)):
    """A create to `POST /packs`; a patch to `PATCH /packs/{pack_id}`, the body's
    `pack_id` on the path."""
    if "create" in body:
        return mod.handler(tp._event("POST", "/packs", groups=groups, body=body), None)
    patch = {k: v for k, v in body.items() if k != "pack_id"}
    event = tp._event("PATCH", "/packs/{pack_id}", groups=groups, body=patch)
    event["pathParameters"] = {"pack_id": body["pack_id"]}
    return mod.handler(event, None)


ADMINS = (tp.USERS, tp.AUTHORS, "business-operations-pack-admins")


def test_a_pack_admin_patches_structure_in_development(monkeypatch):
    mod, sent = _packs_route(monkeypatch, "development")
    body = {"pack_id": "open_pack", "pack": {"constants": {"TOL_PCT": 4}}}
    assert _publish(mod, body)["statusCode"] == 403, "an author is not a pack-admin"
    assert _publish(mod, body, ADMINS)["statusCode"] == 200
    assert sent[-1]["roles"] == ["author", "constant_setter", "pack_admin"]


#: A published pack that declares constants and gates them, so the route has both a
#: declared value to retune and a grant to refuse it without.
TUNED = {
    "schema": "pack-registry/1",
    "packs": {
        "tuned_pack": {
            "pack": {
                "pack_id": "tuned_pack",
                "constants": {"TOL_PCT": 5, "AUTO_WRITE_LIMIT_USD": 100},
                "entitlement": {"constant_groups": ["business-operations-ap-manager"]},
            }
        }
    },
}
SETTERS = (tp.USERS, tp.AUTHORS, "business-operations-ap-manager")


def test_the_route_reads_constant_groups_for_a_declared_constants_value(monkeypatch):
    """A value is content, gated by the pack's `constant_groups`; a name and either write
    limit are structure."""
    mod, sent = _packs_route(monkeypatch, "development", TUNED)

    def publish(constants, groups=SETTERS):
        body = {"pack_id": "tuned_pack", "pack": {"constants": constants}}
        return _publish(mod, body, groups)["statusCode"]

    assert publish({"TOL_PCT": 4}) == 200
    assert sent[-1]["roles"] == ["author", "constant_setter"]
    assert publish({"TOL_PCT": 4}, (tp.USERS, tp.AUTHORS)) == 403
    assert publish({"NEW_PCT": 4}) == 403, "declaring a name is structure"
    assert publish({"AUTO_WRITE_LIMIT_USD": 90}) == 403
    mixed = {"TOL_PCT": 4, "AUTO_WRITE_LIMIT_USD": 90}
    assert publish(mixed) == 403
    assert publish(mixed, (*SETTERS, "business-operations-pack-admins")) == 200


@pytest.mark.parametrize("role", ["quality", "production"])
def test_the_route_refuses_every_edit_outside_development(monkeypatch, role):
    mod, sent = _packs_route(monkeypatch, role)
    for body, groups in (
        ({"pack_id": "open_pack", "pack": {"gateway_tools": []}}, ADMINS),
        ({"pack_id": "new_pack", "create": tp._SHADOW_SPEC}, ADMINS),
        ({"pack_id": "open_pack", "pack": {"display_name": "N"}}, ADMINS),
        ({"pack_id": "open_pack", "base_prompt": "x {SOP_CONTENT}"}, ADMINS),
    ):
        refused = _publish(mod, body, groups)
        assert refused["statusCode"] == 403, (body, refused)
        answer = json.loads(refused["body"])
        assert answer["error"] == f"Packs and SOPs are not edited in {role}"
        assert "--change-ref" in answer["detail"]
    assert not sent, "refused by the route, before the composer"


@pytest.mark.parametrize("role", ["quality", "production"])
def test_outside_development_the_sop_routes_refuse_every_write(monkeypatch, tree, role):
    """A mapped SOP's text included: outside development a SOP arrives by import only."""
    v1 = _sealed(monkeypatch, tree, tp.SECOND)
    aws = tp._authoring(monkeypatch, tree, role)
    _verb(aws.composer, "import", bundle=v1, change_ref="CHG1", dry_run=False)
    mod = tp._sops_route(monkeypatch, aws, None)
    mapped = f"{tp.SECOND}/po_accrual.txt"
    before = aws.objects[("sops", mapped)]

    def post(process_type, name=None):
        body = {
            "process_type": process_type,
            "content": "v",
            **({"name": name} if name else {}),
        }
        return mod.handler(tp._sop_event("POST", tp.SECOND, body=body), None)

    for refused in (
        post("new_type"),
        mod.handler(
            tp._sop_event("PUT", tp.SECOND, "po_accrual", body={"content": "v"}), None
        ),
        mod.handler(tp._sop_event("DELETE", tp.SECOND, "po_accrual"), None),
    ):
        assert refused["statusCode"] == 403, refused
        assert f"not edited in {role}" in json.loads(refused["body"])["error"]
    assert not aws.invokes, "refused by the route, before the composer"
    assert aws.objects[("sops", mapped)] == before
    # Reading and re-indexing are not edits.
    read = mod.handler(tp._sop_event("GET", tp.SECOND, "po_accrual"), None)
    assert read["statusCode"] == 200


# ── the role and the operator commands ──


@pytest.mark.parametrize(
    ("mode", "explicit", "role"),
    [
        ("production", None, "production"),
        ("development", None, "development"),
        (None, None, "development"),
        ("development", "quality", "quality"),
    ],
)
def test_the_role_defaults_from_the_deployment_mode(mode, explicit, role):
    from business_operations import config as pc

    ctx = UseCaseContext("p", "dev", None, production=mode == "production")
    assert pc.promotion_role(ctx, pc.PromotionConfig(role=explicit)) == role


def test_a_development_role_under_production_mode_is_flagged():
    from business_operations import config as pc

    ctx = UseCaseContext("p", "dev", None, production=True)
    assert pc.promotion_contradiction(ctx, pc.PromotionConfig(role="development"))
    assert pc.promotion_contradiction(ctx, pc.PromotionConfig()) is None


def test_the_bundle_command_parses_its_options():
    from business_operations import bundle_ops

    assert bundle_ops.parse(["f.json", "--change-ref", "CHG1", "--apply"]) == (
        ["f.json"],
        {"--change-ref": "CHG1", "--apply": True},
    )
    assert bundle_ops.parse(["--change-ref"]) is None
    assert bundle_ops.parse(["--typo"]) is None
    assert bundle_ops.bundle_cmd([]) == 2
    assert bundle_ops.bundle_cmd(["import"]) == 2
    assert bundle_ops.add_group(["no-prefix"]) == 2


def test_pull_and_accept_drift_are_gone(monkeypatch, capsys):
    """Nothing is edited outside development, so there is nothing to carry back or to
    overwrite: both are a usage error, before any AWS call."""
    from business_operations import bundle_ops

    monkeypatch.setitem(sys.modules, "boto3", None)  # an AWS call would raise
    assert bundle_ops.bundle_cmd(["pull", "finance_ops", "--from-file", "x.json"]) == 2
    assert bundle_ops.bundle_cmd(["pull", "finance_ops"]) == 2
    assert bundle_ops.bundle_cmd(["import", "x.json", "--accept-drift"]) == 2
    usage = capsys.readouterr().err
    assert "usage: ops.py bundle" in usage
    assert "pull" not in usage and "drift" not in usage


def test_import_reads_a_pack_dir_relative_to_the_use_case(monkeypatch):
    from business_operations import bundle_ops

    monkeypatch.chdir(tp.REPO)
    assert bundle_ops._load("packs/finance_ops")["pack_id"] == "finance_ops"


def test_a_minted_version_does_not_inherit_the_drafts_change_note(monkeypatch, tree):
    s3 = _old_stack(monkeypatch, tree)
    mod = _composer(monkeypatch, s3)
    exported = _verb(mod, "export", pack_id="finance_ops")["bundle"]
    exported["provenance"] = {"change_note": "an earlier change"}
    exported["sops"]["price_variance.txt"] += "\nEdited.\n"
    _verb(mod, "import", bundle=exported, dry_run=False)
    minted = json.loads(s3.objects[("reg", "_bundles/finance_ops/1.json")])
    assert minted["provenance"]["change_note"] is None


def test_a_created_draft_exports_and_round_trips_through_a_checkout(
    monkeypatch, tree, tmp_path
):
    """Analyst drafts in DEV, integrator completes: an incomplete pack exports unversioned, its
    directory form is completed in a checkout, and the import mints the first complete version."""
    import registry

    s3 = FakeS3()
    mod = _composer(monkeypatch, s3)
    tp._seed(mod, s3, tree)
    spec = {**tp._SHADOW_SPEC, "process_types": ["draft_type"]}
    created = mod.handler(
        {"pack_id": "zz_draft", "tier": "create", "create": spec}, None
    )
    assert "error" not in created, created
    assert created["version"] == 1, "a create is the pack's first version"
    # The analyst's SOP, as `POST /sops` has the composer write it: an incomplete pack takes
    # its SOPs one at a time, so the ones not written yet are no refusal.
    sop = {"op": "put", "name": "draft_type.txt", "content": "Park it."}
    put = mod.handler({"pack_id": "zz_draft", "roles": ["author"], "sop": sop}, None)
    assert put["version"] == 2, put
    for edited in (False, True):
        if edited:  # an author's content edit after the create
            mod.handler(
                {
                    "pack_id": "zz_draft",
                    "roles": ["author"],
                    "pack": {"description": "Edited."},
                },
                None,
            )
        out = _verb(mod, "export", pack_id="zz_draft")
        assert out["status"] == 200, out
        draft = out["bundle"]
        assert draft["version"] is None and registry.is_incomplete(
            draft["pack"]["source"]
        )
    assert draft["pack"]["description"] == "Edited."

    from business_operations import bundle_ops

    assert bundle_ops._write_dir(draft, str(tmp_path)) == f"{tmp_path}/zz_draft/"
    with pytest.raises(FileExistsError):
        bundle_ops._write_dir(draft, str(tmp_path))
    reread = bundle.from_dir(tmp_path / "zz_draft")
    assert bundle.content_sha(reread) == bundle.content_sha(draft)
    assert ("reg", "_bundles/zz_draft/released.json") not in s3.objects, (
        "an incomplete pack's export is a draft, not a published version"
    )

    # The integrator completes the source in the checkout, then imports the directory.
    pack_file = tmp_path / "zz_draft" / "pack.json"
    bound = re.sub(r"<AWAITING_SURVEY[^>\"]*>?", "Bound", pack_file.read_text())
    pack_file.write_text(bound)
    reread = bundle.from_dir(tmp_path / "zz_draft")
    assert not registry.is_incomplete(reread["pack"]["source"])
    dry = _verb(mod, "import", bundle=reread)
    assert dry["result"] == "would activate", dry
    assert dry["diff"]["keys"]["changed"] == ["source"]
    assert dry["to"]["version"] == 4, "after the create, the SOP and the edit"


# ── every edit in development is a version ──


def _edit(mod, **event):
    return mod.handler(
        {"actor": "author@example.com", "roles": ["author"], **event}, None
    )


def test_a_content_edit_mints_a_version_its_author_and_rollback_undo(monkeypatch, tree):
    s3 = FakeS3()
    mod = _composer(monkeypatch, s3)
    tp._seed(mod, s3, tree)
    original = _registry(s3)["packs"]["finance_ops"]["pack"]["display_name"]

    done = _edit(mod, pack_id="finance_ops", pack={"display_name": "AP"})
    assert done["result"] == "active" and done["version"] == 2, done
    assert _registry(s3)["packs"]["finance_ops"]["pack"]["display_name"] == "AP"
    assert _registry(s3)["packs"]["finance_ops"]["bundle"]["version"] == 2
    history = _verb(mod, "versions", pack_id="finance_ops")
    assert history["versions"][-1]["edited_by"] == "author@example.com"
    edit = history["audit"][-1]
    assert edit["verb"] == "edit" and edit["asserted_actor"] == "author@example.com"
    status = _verb(mod, "status", pack_id="finance_ops")
    assert status["origin"]["via"] == "edit"
    assert status["origin"]["by"] == "author@example.com"
    assert "drift" not in status, "an edit is a version, never drift"

    prompt = _edit(
        mod,
        pack_id="finance_ops",
        base_prompt=mod._snapshot("finance_ops")["base_prompt"] + "\nBe brief.\n",
    )
    assert prompt["version"] == 3
    puts = len(s3.puts)
    same = _edit(mod, pack_id="finance_ops", pack={"display_name": "AP"})
    assert same["result"] == "already active" and same["version"] == 3
    assert len(s3.puts) == puts, "an edit that changes nothing writes nothing"

    back = _verb(mod, "rollback", pack_id="finance_ops", to=1, dry_run=False)
    assert back["result"] == "active", back
    assert _registry(s3)["packs"]["finance_ops"]["pack"]["display_name"] == original
    assert _verb(mod, "status", pack_id="finance_ops")["versions"] == [1, 2, 3]


def test_a_constant_value_is_an_authors_version_and_a_limit_is_not(monkeypatch, tree):
    """Retuning a tolerance is the author's everyday change, so in development it mints
    a version like any other content edit — and the two write limits do not."""
    s3 = FakeS3()
    mod = _composer(monkeypatch, s3)
    tp._seed(mod, s3, tree)
    setter = ["author", "constant_setter"]

    tuned = _edit(
        mod,
        pack_id="finance_ops",
        roles=setter,
        pack={"constants": {"PRICE_VARIANCE_PCT": 3}},
    )
    assert tuned["version"] == 2, tuned
    constants = _registry(s3)["packs"]["finance_ops"]["pack"]["constants"]
    assert constants["PRICE_VARIANCE_PCT"] == 3
    assert constants["QTY_VARIANCE_PCT"] == 5, "the merge keeps what it did not name"
    history = _verb(mod, "versions", pack_id="finance_ops")
    assert history["versions"][-1]["edited_by"] == "author@example.com"

    for symbol, value in (
        ("AUTO_WRITE_LIMIT_USD", 90000),
        ("DUAL_APPROVAL_LIMIT_USD", 300000),
    ):
        refused = _edit(
            mod,
            pack_id="finance_ops",
            roles=setter,
            pack={"constants": {symbol: value}},
        )
        assert refused["status"] == 400 and "pack-admins" in refused["error"], refused
    mixed = {"constants": {"PRICE_VARIANCE_PCT": 4, "AUTO_WRITE_LIMIT_USD": 90000}}
    assert _edit(mod, pack_id="finance_ops", roles=setter, pack=mixed)["status"] == 400
    assert (
        _registry(s3)["packs"]["finance_ops"]["pack"]["constants"][
            "AUTO_WRITE_LIMIT_USD"
        ]
        == 100000
    ), "a refused patch writes none of itself"
    admin = _edit(mod, pack_id="finance_ops", roles=[*setter, "pack_admin"], pack=mixed)
    assert admin["version"] == 3, admin


def test_a_sop_edit_writes_only_that_sop(monkeypatch, tree):
    s3 = FakeS3()
    mod = _composer(monkeypatch, s3)
    tp._seed(mod, s3, tree)
    others = {k: v for k, v in s3.objects.items() if k[0] == "sops" and k[1] != SOP}
    s3.puts.clear()
    sop = {"op": "put", "name": "price_variance.txt", "content": "Version 9\nNew."}
    assert _edit(mod, pack_id="finance_ops", sop=sop)["version"] == 2
    assert {k for k in s3.puts if k[0] == "sops"} == {
        ("sops", SOP),
        ("sops", f"{SOP}.metadata.json"),
    }
    assert {k: v for k, v in s3.objects.items() if k in others} == others


def test_a_route_cannot_hand_the_composer_a_bundle(monkeypatch, tree):
    """The composer builds every draft from the request's own fields: a `bundle`, or
    structure inside `sop`, is not read, so no author carries structure past the tiers."""
    s3 = FakeS3()
    mod = _composer(monkeypatch, s3)
    tp._seed(mod, s3, tree)
    tools = _registry(s3)["packs"]["finance_ops"]["pack"]["gateway_tools"]
    smuggled = {"pack": {"pack_id": "finance_ops", "gateway_tools": ["x___y"]}}
    done = _edit(
        mod, pack_id="finance_ops", pack={"display_name": "AP"}, bundle=smuggled
    )
    assert done["version"] == 2
    sop = {
        "op": "put",
        "name": "price_variance.txt",
        "content": "Text.",
        "pack": smuggled["pack"],
    }
    assert _edit(mod, pack_id="finance_ops", sop=sop)["version"] == 3
    assert _registry(s3)["packs"]["finance_ops"]["pack"]["gateway_tools"] == tools
    structure = _edit(mod, pack_id="finance_ops", pack={"gateway_tools": []})
    assert structure["status"] == 400 and "pack-admins" in structure["error"]


def test_a_stale_edit_is_a_409_naming_the_version_here(monkeypatch, tree):
    s3 = FakeS3()
    mod = _composer(monkeypatch, s3)
    tp._seed(mod, s3, tree)
    assert (
        _edit(
            mod, pack_id="finance_ops", pack={"display_name": "A"}, expected_version=1
        )["version"]
        == 2
    )
    puts = len(s3.puts)
    stale = _edit(
        mod, pack_id="finance_ops", pack={"display_name": "B"}, expected_version=1
    )
    assert stale["status"] == 409 and stale["version"] == 2, stale
    assert "since you opened it" in stale["detail"]
    assert _registry(s3)["packs"]["finance_ops"]["pack"]["display_name"] == "A"
    assert {k for k in s3.puts[puts:]} == {("reg", "_refusals/finance_ops.json")}
    sop = {"op": "delete", "name": "price_variance.txt"}
    assert (
        _edit(mod, pack_id="finance_ops", sop=sop, expected_version=1)["status"] == 409
    )


def test_the_packs_route_sends_the_version_the_author_read(monkeypatch, tree):
    aws = tp._authoring(monkeypatch, tree)
    for k, v in {
        "API_ALLOWED_CLIENTS": tp.CONSOLE,
        "USERS_GROUP": tp.USERS,
        "SOP_AUTHOR_GROUPS": tp.AUTHORS,
        "REGISTRY_BUCKET": "reg",
    }.items():
        monkeypatch.setenv(k, v)
    spec = importlib.util.spec_from_file_location(
        "bo_packs_edit", tp.UC / "api" / "packs" / "index.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    read = mod.handler(
        {
            **tp._event("GET", "/packs/{pack_id}/base_prompt"),
            "pathParameters": {"pack_id": tp.SECOND},
        },
        None,
    )
    version = json.loads(read["body"])["version"]
    assert version == 1, read

    def publish(name, expected):
        body = {
            "pack_id": tp.SECOND,
            "pack": {"display_name": name},
            "expected_version": expected,
        }
        return _publish(mod, body)

    first = publish("One", version)
    assert first["statusCode"] == 200 and json.loads(first["body"])["version"] == 2
    assert aws.invokes[-1]["expected_version"] == 1
    assert publish("Two", version)["statusCode"] == 409
    assert publish("Two", "1")["statusCode"] == 400


# ── versions chain over published versions ──


def test_a_published_chain_promotes_without_force(monkeypatch, tree):
    dev = FakeS3()
    mod = _composer(monkeypatch, dev)
    tp._seed(mod, dev, tree)
    published1 = _verb(mod, "export", pack_id="finance_ops")["bundle"]
    assert published1["version"] == 1
    for name in ("A", "B", "C"):
        _edit(mod, pack_id="finance_ops", pack={"display_name": name})
    published2 = _verb(mod, "export", pack_id="finance_ops")["bundle"]
    assert published2["version"] == 4 and bundle.is_sealed(published2)
    assert published2["previous"]["version"] == 3, (
        "DEV's own chain runs over every edit"
    )
    assert published2["base"] == {"version": 1, "sha256": published1["sha256"]}
    released = json.loads(dev.objects[("reg", "_bundles/finance_ops/released.json")])
    assert released["version"] == 4
    assert (
        _verb(mod, "versions", pack_id="finance_ops")["audit"][-1]["verb"] == "release"
    )

    qas = FakeS3()
    other = _composer(monkeypatch, qas, "quality")
    tp._seed(other, qas, tree)
    for published in (published1, published2):
        done = _verb(
            other, "import", bundle=published, change_ref="CHG1", dry_run=False
        )
        assert done["result"] == "active", done
        assert (
            _verb(other, "import_status", import_id=done["import_id"])["forced"]
            is False
        )
    # A version past an unimported published version names the one it needs.
    mod = _composer(monkeypatch, dev)
    _edit(mod, pack_id="finance_ops", pack={"display_name": "D"})
    published3 = _verb(mod, "export", pack_id="finance_ops")["bundle"]
    _edit(mod, pack_id="finance_ops", pack={"display_name": "E"})
    published4 = _verb(mod, "export", pack_id="finance_ops")["bundle"]
    other = _composer(monkeypatch, qas, "quality")
    skipped = _verb(other, "import", bundle=published4, change_ref="CHG2")
    assert skipped["status"] == 409 and "follows {'version': 5" in skipped["error"]
    assert (
        _verb(other, "import", bundle=published3, change_ref="CHG2")["result"]
        == "would activate"
    )


def test_a_bundle_minted_before_publishing_chains_on_previous(monkeypatch, tree):
    v1 = _sealed(monkeypatch, tree)
    old = {k: v for k, v in v1.items() if k != "base"}
    old["seal_sha256"] = bundle.seal_sha(old)
    v2 = bundle.seal(
        {**old, "base_prompt": old["base_prompt"] + "\nv2\n"},
        2,
        {"version": 1, "sha256": v1["sha256"]},
        {},
    )
    v2 = {k: v for k, v in v2.items() if k != "base"}
    v2["seal_sha256"] = bundle.seal_sha(v2)
    assert bundle.is_sealed(old) and bundle.is_sealed(v2) and "base" not in v2
    qas = FakeS3()
    mod = _composer(monkeypatch, qas, "quality")
    tp._seed(mod, qas, tree)
    assert (
        _verb(mod, "import", bundle=old, change_ref="C1", dry_run=False)["result"]
        == "active"
    )
    assert (
        _verb(mod, "import", bundle=v2, change_ref="C2")["result"] == "would activate"
    )


def test_the_upgrade_import_publishes_what_dev_holds(monkeypatch, tree):
    """A DEV stack from before publishing: its active version is what other stacks chain
    on, so the next deploy marks it published."""
    s3 = FakeS3()
    mod = _composer(monkeypatch, s3)
    tp._seed(mod, s3, tree)
    # A new stack's first seed publishes nothing: its first export does.
    assert ("reg", "_bundles/finance_ops/released.json") not in s3.objects
    tp._seed(mod, s3, tree)
    released = json.loads(s3.objects[("reg", "_bundles/finance_ops/released.json")])
    assert released["version"] == 1 and released["released_by"] == "deploy"


# ── the upgrade from a release that edited packs in place ──


def _edit_in_place(s3):
    """What an earlier release's `PUT /sops` wrote: the live SOP, outside any version."""
    s3.put_object(Bucket="sops", Key=SOP, Body=b"Hotfixed in place.", Metadata={})


@pytest.mark.parametrize("role", ["quality", "production"])
def test_the_upgrade_fails_outside_development_while_a_pack_holds_an_edit_in_place(
    monkeypatch, tree, role
):
    v1 = _sealed(monkeypatch, tree)
    s3 = FakeS3()
    mod = _composer(monkeypatch, s3, role)
    tp._seed(mod, s3, tree)
    _verb(mod, "import", bundle=v1, change_ref="CHG1", dry_run=False)
    tp._seed(mod, s3, tree)  # no edit in place: the deploy goes through

    _edit_in_place(s3)
    before = dict(s3.objects)
    with pytest.raises(RuntimeError) as failed:
        tp._seed(mod, s3, tree)
    message = str(failed.value)
    assert "finance_ops (sops/price_variance.txt)" in message, message
    assert f"this {role} stack" in message and "rolls back" in message
    # Only verbs this checkout's ops.py has: `bundle pull` left with the release
    # before, so the move into development is the page's, from that release.
    assert "bundle pull" not in message
    assert "`ops.py bundle rollback <pack_id>" in message and "--change-ref" in message
    assert "docs/packs/promotion.md" in message
    assert s3.objects == before, "it fails before writing anything"

    # A rollback sends the template before this release, which asks for no guard.
    rollback = {
        "DeployImportProof": "deploy-proof",
        "RequestType": "Update",
        "PhysicalResourceId": "pack-seed",
        "ResourceProperties": {"AssetBucket": "assets", "AssetKey": "packs.zip"},
    }
    assert mod.handler(rollback, None) == {"PhysicalResourceId": "pack-seed"}
    assert s3.objects[("sops", SOP)] == b"Hotfixed in place."


def test_the_upgrade_in_development_adopts_an_edit_in_place_as_a_version(
    monkeypatch, tree
):
    s3 = FakeS3()
    mod = _composer(monkeypatch, s3)
    tp._seed(mod, s3, tree)
    _edit_in_place(s3)
    tp._seed(mod, s3, tree)

    pointer = json.loads(s3.objects[("reg", "_bundles/finance_ops/active.json")])
    assert pointer["version"] == 2
    adopted = json.loads(s3.objects[("reg", "_bundles/finance_ops/2.json")])
    assert adopted["sops"]["price_variance.txt"] == "Hotfixed in place."
    assert adopted["provenance"]["change_note"] == "adopted edits"
    # Released first, so the adopted version chains on what other stacks hold.
    assert adopted["base"]["version"] == 1 and bundle.is_sealed(adopted)
    status = _verb(mod, "status", pack_id="finance_ops")
    assert status["origin"]["via"] == "adopted"
    assert _registry(s3)["packs"]["finance_ops"]["bundle"]["version"] == 2
    assert _verb(mod, "status", pack_id=tp.SECOND)["active"]["version"] == 1
    tp._seed(mod, s3, tree)
    assert _verb(mod, "status", pack_id="finance_ops")["versions"] == [1, 2]


# ── the upgrade from a release that let a stack override a rule constant ──


def _overrides(monkeypatch, mod, rows: dict):
    """`{pack_id: {SYMBOL: value}}` as the Config table's override rows, for `status`."""
    monkeypatch.setattr(
        mod.config_overrides,
        "_query",
        lambda ns: (
            rows.get(ns.removeprefix("constant#"), {}) if ns != "contact" else {}
        ),
    )


@pytest.mark.parametrize("role", ["development", "quality", "production"])
def test_the_upgrade_fails_while_the_stack_overrides_a_rule_constant(
    monkeypatch, tree, role
):
    s3 = FakeS3()
    mod = _composer(monkeypatch, s3, role)
    monkeypatch.setattr(mod, "_rule_overrides", dict)
    tp._seed(mod, s3, tree)  # none left: the deploy goes through

    rows = {"finance_ops": ["PRICE_VARIANCE_PCT", "QTY_VARIANCE_PCT"]}
    monkeypatch.setattr(mod, "_rule_overrides", lambda: rows)
    before = dict(s3.objects)
    with pytest.raises(RuntimeError) as failed:
        tp._seed(mod, s3, tree)
    message = str(failed.value)
    assert "finance_ops (PRICE_VARIANCE_PCT, QTY_VARIANCE_PCT)" in message, message
    assert f"This {role} stack" in message and "rolls back" in message
    assert "PUT /packs/<pack_id>/controls" in message and "null" in message
    assert ("import that version here" in message) is (role != "development")
    assert s3.objects == before, "it fails before writing anything"

    # A rollback sends the template before this release, which asks for no guard.
    rollback = {
        "DeployImportProof": "deploy-proof",
        "RequestType": "Update",
        "PhysicalResourceId": "pack-seed",
        "ResourceProperties": {"AssetBucket": "assets", "AssetKey": "packs.zip"},
    }
    assert mod.handler(rollback, None) == {"PhysicalResourceId": "pack-seed"}


def test_the_guard_reads_every_page_of_the_config_table(monkeypatch, tree):
    mod = _composer(monkeypatch, FakeS3())
    pages = [
        {
            "Items": [
                {"namespace": "contact", "config_key": "ap_team"},
                {"namespace": "constant#b", "config_key": "X_PCT"},
            ],
            "LastEvaluatedKey": {"k": 1},
        },
        {
            "Items": [
                {"namespace": "constant#a", "config_key": "DUAL_APPROVAL_LIMIT_USD"},
                {"namespace": "constant#a", "config_key": "Y_DAYS"},
            ]
        },
    ]

    class Table:
        def scan(self, **_kw):
            return pages.pop(0)

    monkeypatch.setenv("CONFIG_TABLE", "cfg")

    class Dynamo:
        def Table(self, _name):
            return Table()

    monkeypatch.setattr(boto3, "resource", lambda *_a, **_k: Dynamo())
    assert mod._rule_overrides() == {"a": ["Y_DAYS"], "b": ["X_PCT"]}
    monkeypatch.delenv("CONFIG_TABLE")
    assert mod._rule_overrides() == {}


def test_status_shows_each_values_effective_value_and_source(monkeypatch, tree):
    s3 = FakeS3()
    mod = _composer(monkeypatch, s3)
    tp._seed(mod, s3, tree)
    declared = _registry(s3)["packs"]["finance_ops"]["pack"]["constants"]
    ceiling = declared["AUTO_WRITE_LIMIT_USD"]
    monkeypatch.setenv("CONTACTS_JSON", json.dumps({"ap_team": "ap@example.com"}))
    _overrides(
        monkeypatch,
        mod,
        # PRICE_VARIANCE_PCT's row is stale, so ignored.
        {
            "finance_ops": {
                "AUTO_WRITE_LIMIT_USD": ceiling - 1,
                "PRICE_VARIANCE_PCT": 99,
            }
        },
    )
    values = _verb(mod, "status", pack_id="finance_ops")["values"]
    assert values["PRICE_VARIANCE_PCT"] == {
        "value": declared["PRICE_VARIANCE_PCT"],
        "kind": "rule",
        "source": "pack v1",
        # finance_ops enables the tolerance guard, which reads this rule.
        "guarded_by": ["within_tolerance_or_approved"],
    }
    assert values["AUTO_WRITE_LIMIT_USD"] == {
        "value": ceiling - 1,
        "kind": "limit",
        "source": f"stack override (lowered from {ceiling})",
        "guarded_by": [],
    }
    assert values["CONTACT_AP_TEAM"] == {
        "value": "ap@example.com",
        "kind": "contact",
        "source": "platform.yaml",
    }
    json.dumps(values)  # the verb's reply crosses a Lambda invoke
