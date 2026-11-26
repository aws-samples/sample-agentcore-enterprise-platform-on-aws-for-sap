# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The composer: the only writer of a pack document and of registry.json.

Here: `_publish`, the write order and rollback every import, edit and create ends in; the
refusal of every edit outside development; and the create, which is development's alone.
An edit in development is imported as a version (`test_business_operations_bundles`).

The module is loaded with `spec_from_file_location` under a unique name rather than
`import index`, because several handlers in this suite are each called `index`, and
`sys.modules["index"]` holds one at a time.
"""

import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

_UC = Path(__file__).resolve().parents[3] / "use-cases" / "business-operations"
_COMPOSER = _UC / "pack_composer" / "index.py"
sys.path.insert(0, str(_UC / "api" / "layer" / "python"))

_PACK = {
    "$schema": "../pack.schema.json",
    "pack_id": "finance_x",
    "display_name": "Finance",
    "description": "AP exceptions",
    "model_tier": "sonnet",
    "gateway_tools": ["business-operations-sops___load_sop"],
    "process_type_to_sop": {"a_check": "finance_x/a.txt"},
    "constants": {"TOL_PCT": 5, "MAX_DAYS": 10, "MIN_QTY": 1},
    "entitlement": {"groups": ["g"], "author_groups": ["fa"]},
    "source": {
        "kind": "filter",
        "service": "ZSVC",
        "entity": "Items",
        "short_id": "fx",
    },
    "fields": {},
}
_PROMPT = "Handle AP.\n\n{SOP_CONTENT}\n\n{PLATFORM_MECHANICS}\n"


class _FakeS3:
    """Just enough S3: a dict of key -> bytes, and the four calls the composer makes.

    `fail_on` names a key whose `put_object` raises — the rollback tests need a write to
    fail *after* an earlier one succeeded, which is the only state a rollback exists for.

    A refusal record goes to `refusals`, not `puts`: a refused patch writes nothing to
    the corpus, and `puts` is what the "writes nothing" tests read.
    """

    def __init__(self, objects):
        self.objects = dict(objects)
        self.puts = []
        self.refusals = []
        self.deletes = []
        self.fail_on = None

    def get_paginator(self, _name):
        objects = self.objects

        class _P:
            def paginate(self, **_kw):
                yield {"Contents": [{"Key": k} for k in sorted(objects)]}

        return _P()

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": _Body(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, **_kw):
        if Key == self.fail_on:
            raise RuntimeError(f"put refused for {Key}")
        self.objects[Key] = Body
        (self.refusals if Key.startswith("_refusals/") else self.puts).append(Key)
        return {}

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)
        self.deletes.append(Key)


class _Body:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


@pytest.fixture
def composer(monkeypatch):
    """Import the handler with its module-level boto3 client replaced."""
    monkeypatch.setenv("REGISTRY_BUCKET", "test-bucket")
    monkeypatch.setenv("SOPS_BUCKET", "sops-bucket")
    monkeypatch.setenv("PROMOTION_ROLE", "quality")
    fake = _FakeS3(
        {
            "finance_x/pack.json": json.dumps(_PACK).encode(),
            "finance_x/base_prompt.txt": _PROMPT.encode(),
            "_platform_prompt.txt": b"Platform mechanics.\n",
            "registry.json": b"{}",
        }
    )
    with mock.patch("boto3.client", return_value=fake):
        spec = importlib.util.spec_from_file_location(
            "pack_composer_under_test", _COMPOSER
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        yield module, fake
    sys.modules.pop(spec.name, None)


@pytest.fixture
def created(composer, monkeypatch):
    """The composer in development, the only role that creates a pack."""
    monkeypatch.setattr(composer[0], "ROLE", "development")
    return composer


def _publish(module, *, pack=None, base_prompt=None, raw=None):
    """`_publish` with a change that rewrites finance_x's documents in its mirror, as an
    import or an edit stages them."""

    def change(root):
        written = []
        document = root / "finance_x" / "pack.json"
        if raw is not None:
            document.write_bytes(raw)
        elif pack is not None:
            document.write_text(
                json.dumps({**json.loads(document.read_text()), **pack})
            )
        if raw is not None or pack is not None:
            written.append("finance_x/pack.json")
        if base_prompt is not None:
            (root / "finance_x" / "base_prompt.txt").write_text(base_prompt)
            written.append("finance_x/base_prompt.txt")
        return [], written

    with tempfile.TemporaryDirectory(prefix="corpus-") as staging:
        return module._publish(Path(staging), change, "a@b.c", ["finance_x"])


@pytest.mark.parametrize("role", ["quality", "production"])
def test_quality_and_production_refuse_every_edit(composer, monkeypatch, role):
    """Outside development a pack arrives only by an import with a change reference, so
    no edit a route sends is written: content, prompt, structure, a SOP or a create."""
    module, fake = composer
    monkeypatch.setattr(module, "ROLE", role)
    sop = {"name": "a.txt", "content": "Hotfix."}
    edits = [
        {"pack": {"display_name": "Hotfix"}},
        {"base_prompt": _PROMPT + "More.\n"},
        {"pack": {"model_tier": "premium"}, "roles": ["author", "pack_admin"]},
        {"sop": {**sop, "op": "put"}},
        {"sop": {"name": "a.txt", "op": "delete"}},
        {"tier": "create", "create": {"display_name": "New"}, "pack_id": "new_pack"},
    ]
    for edit in edits:
        result = module.handler(
            {"pack_id": "finance_x", "roles": ["author"], "actor": "a@b.c", **edit},
            None,
        )
        assert result["status"] == 403, (edit, result)
        assert result["error"] == f"Packs and SOPs are not edited in {role}"
        assert "--change-ref" in result["detail"]
    assert not fake.puts and not fake.deletes and not fake.refusals


def test_a_publish_writes_the_documents_then_the_registry(composer):
    module, fake = composer
    result = _publish(
        module, pack={"display_name": "Renamed"}, base_prompt=_PROMPT + "More.\n"
    )
    assert "error" not in result, result
    assert result["written"] == [
        "finance_x/pack.json",
        "finance_x/base_prompt.txt",
        "registry.json",
    ]
    # The order the puts actually happened in, not just the order they are reported in.
    # `registry.json` last is what makes a mid-write failure recoverable: it carries each
    # pack's content inline, so a registry naming content no document holds is what every
    # runtime would serve.
    assert fake.puts == result["written"]
    written = json.loads(fake.objects["finance_x/pack.json"])
    assert written["gateway_tools"] == ["business-operations-sops___load_sop"], (
        "untouched keys must survive"
    )
    published = json.loads(fake.objects["registry.json"])["packs"]["finance_x"]
    assert published["pack"]["display_name"] == "Renamed"
    assert fake.objects["finance_x/base_prompt.txt"].endswith(b"More.\n")
    assert result["skipped"] == []


def test_a_change_that_makes_the_pack_unloadable_writes_nothing(composer):
    """`registry.build` skips a pack it cannot load, so the guard is a post-build check."""
    module, fake = composer
    result = _publish(module, raw=b"{not json")
    assert result["status"] == 500 and "did not survive" in result["error"]
    assert not fake.puts, "a refused publish must not write anything"


def test_a_constants_patch_merges_key_by_key(composer):
    """A patch naming one constant must not delete the ones it does not name — a
    wholesale replace of `constants` would silently drop live business thresholds a
    patch never mentioned."""
    module, _ = composer
    merged = module._merge(json.loads(json.dumps(_PACK)), {"constants": {"TOL_PCT": 9}})
    assert merged["constants"] == {"TOL_PCT": 9, "MAX_DAYS": 10, "MIN_QTY": 1}
    assert module._merge({"constants": 1}, {"constants": {"A": 1}})["constants"] == {
        "A": 1
    }


def test_a_publish_reports_a_pack_the_build_silently_dropped(composer):
    """`registry.build` skips a pack it cannot load rather than failing, so the composer
    has to say which pack it dropped."""
    module, fake = composer
    fake.objects["broken/pack.json"] = b"{not json"
    result = _publish(module, pack={"display_name": "R"})
    assert "error" not in result, result
    assert result["skipped"] == ["broken"]


# --- write ordering and rollback ---


def test_a_failed_registry_write_rolls_the_document_back(composer):
    """The failure the ordering cannot fix on its own. A pack document left changed with
    no registry update is inert to every runtime — they read the registry — but live for
    the next rebuild, so a later publish of a *different* pack would silently apply it."""
    module, fake = composer
    original = fake.objects["finance_x/pack.json"]
    fake.fail_on = "registry.json"
    with pytest.raises(RuntimeError):
        _publish(module, pack={"display_name": "Renamed"})
    assert fake.objects["finance_x/pack.json"] == original, (
        "the document must be back to its pre-publish bytes"
    )
    assert fake.objects["registry.json"] == b"{}", "the registry must be untouched"


def test_a_rollback_deletes_a_prompt_the_pack_did_not_have(composer):
    """A `base_prompt.txt` may be new, so rollback is restore-or-delete per key."""
    module, fake = composer
    del fake.objects["finance_x/base_prompt.txt"]
    fake.fail_on = "registry.json"
    with pytest.raises(RuntimeError):
        _publish(module, base_prompt=_PROMPT)
    assert fake.deletes == ["finance_x/base_prompt.txt"]
    assert "finance_x/base_prompt.txt" not in fake.objects


def test_a_failed_document_write_publishes_no_registry(composer):
    """The other half of the ordering: nothing reaches `registry.json` after a document
    write failed, and the key that failed is never deleted — a `delete_object` on it would
    remove what a previous publish left there."""
    module, fake = composer
    original = fake.objects["finance_x/pack.json"]
    fake.fail_on = "finance_x/pack.json"
    with pytest.raises(RuntimeError):
        _publish(module, pack={"display_name": "Renamed"})
    assert fake.objects["registry.json"] == b"{}"
    assert fake.objects["finance_x/pack.json"] == original
    assert fake.deletes == []


def test_a_rollback_that_cannot_restore_reraises_the_real_failure(composer):
    """A best-effort rollback: if putting the original bytes back also fails, the caller
    still gets the write failure rather than the rollback's own, so the route's 502 names
    the right cause and the log names the object left mixed."""
    module, fake = composer
    real_put, calls = fake.put_object, []

    def _fails_after_the_first(**kw):
        calls.append(kw["Key"])
        if len(calls) == 1:
            return real_put(**kw)
        raise RuntimeError(f"bucket is unreachable for {kw['Key']}")

    fake.put_object = _fails_after_the_first
    with pytest.raises(RuntimeError, match="unreachable for registry.json"):
        _publish(module, pack={"display_name": "Renamed"})
    # Three puts attempted: the document, the registry, and the rollback of the document.
    assert calls == ["finance_x/pack.json", "registry.json", "finance_x/pack.json"]
    # And the honest ceiling: the corpus really is mixed now. That is what the logged
    # rollback failure is for — it names the object, so a human can put it back.
    assert json.loads(fake.objects["finance_x/pack.json"])["display_name"] == "Renamed"


# --- the create tier: the one request that adds a pack ------------------------

_CREATE_SPEC = {
    "display_name": "Field Service Parts",
    "description": "Parts shortages that block a scheduled service call.",
    "short_id": "fsp",
    "kind": "filter",
    "definition": "A confirmed service order whose parts are not in stock.",
    "process_types": ["parts_shortage"],
    "attributes": [
        {"name": "order_number", "label": "Service Order", "type": "string"}
    ],
    "constants": {"MIN_STOCK_PCT": 20},
}


def _create(module, **overrides):
    spec = {**_CREATE_SPEC, **overrides.pop("spec", {})}
    return module.handler(
        {
            "pack_id": overrides.pop("pack_id", "field_service"),
            "tier": "create",
            "create": spec,
            "actor": "analyst@example.com",
            **overrides,
        },
        None,
    )


def test_a_create_writes_the_document_the_prompt_and_the_registry(created):
    module, fake = created
    result = _create(module)
    assert "error" not in result, result
    assert result["written"] == [
        "field_service/pack.json",
        "field_service/base_prompt.txt",
        "registry.json",
    ]
    document = json.loads(fake.objects["field_service/pack.json"])
    assert document["pack_id"] == "field_service"
    assert document["$schema"] == "../pack.schema.json"
    # The pack reached the registry every runtime reads, and the pack it did not touch
    # is still there.
    published = json.loads(fake.objects["registry.json"])["packs"]
    assert set(published) == {"finance_x", "field_service"}


def test_a_created_pack_is_incomplete_and_holds_read_tools_only(created):
    """The two properties that make minting a pack from a browser a small act. Asserted on
    the bytes the composer actually wrote, not on `pack_compose`'s return value."""
    import registry as registry_module

    module, fake = created
    _create(module)
    document = json.loads(fake.objects["field_service/pack.json"])
    assert registry_module.is_incomplete(document["source"])
    # Published once, as a flag every reader reads.
    published = json.loads(fake.objects["registry.json"])
    assert published["packs"]["field_service"]["incomplete"] is True
    assert published["packs"]["finance_x"]["incomplete"] is False
    assert "odata_update" not in document["gateway_tools"]
    assert "odata_create" not in document["gateway_tools"]
    assert "odata_function_import" not in document["gateway_tools"]


def test_a_create_naming_an_existing_pack_is_a_conflict_and_writes_nothing(created):
    """The inverse of the patch path's guard. Overwriting would replace `finance_x`'s
    `source`, `fields` and `gateway_tools` with markers and the read set — a deletion
    wearing a create's clothes."""
    module, fake = created
    before = dict(fake.objects)
    result = _create(module, pack_id="finance_x")
    assert result["status"] == 409
    assert "already published" in result["error"]
    assert fake.puts == []
    assert fake.objects == before


def test_a_create_claiming_another_packs_short_id_writes_nothing(created):
    module, fake = created
    fake.objects["finance_x/pack.json"] = json.dumps(
        {**_PACK, "source": {**_PACK["source"], "short_id": "fsp"}}
    ).encode()
    result = _create(module)
    assert result["status"] == 400
    assert "short_id" in result["error"] and "finance_x" in result["error"]
    assert fake.puts == []


def test_a_create_spec_naming_a_privileged_key_writes_nothing(created):
    module, fake = created
    result = _create(module, spec={"gateway_tools": ["odata_update"]})
    assert result["status"] == 400
    assert "gateway_tools" in result["error"]
    assert fake.puts == []


def test_a_create_with_no_spec_writes_nothing(created):
    """The composer re-checks rather than trusting the route: an invoke that reached here
    with `tier: create` and no spec must refuse, not compose a pack out of nothing."""
    module, fake = created
    result = module.handler({"pack_id": "field_service", "tier": "create"}, None)
    assert result["status"] == 400
    assert fake.puts == []


def test_a_failed_prompt_write_rolls_the_created_document_back(created):
    """A create writes two documents, so it is the request with the widest rollback. The
    pack must not be left half-created: a `pack.json` with no prompt beside it is a pack
    `registry.build` publishes with an empty `base_prompt`."""
    module, fake = created
    fake.fail_on = "field_service/base_prompt.txt"
    with pytest.raises(RuntimeError):
        _create(module)
    # The document was written, then deleted — it had no `before` to restore to.
    assert "field_service/pack.json" in fake.deletes
    assert "field_service/pack.json" not in fake.objects
    assert json.loads(fake.objects["registry.json"]) == {}, "no registry was published"
