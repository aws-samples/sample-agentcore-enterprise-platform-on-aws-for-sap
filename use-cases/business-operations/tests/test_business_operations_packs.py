# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""business-operations packs: synth-time validation, the composer's seed, the SOP and
pack routes, and who the buckets let write.

The composer and the handlers run in-process against a dict-backed S3.
"""

from __future__ import annotations

import importlib.util
import io
import json
import shutil
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import aws_cdk as cdk
import bo_sap_helper
import boto3
import pytest
from aws_cdk.assertions import Template
from botocore.exceptions import ClientError

from infra_utils.platform_config import load_platform_config
from infra_utils.use_case_context import UseCaseContext

REPO = Path(__file__).resolve().parents[3]
UC = REPO / "use-cases" / "business-operations"
LAYER = UC / "api" / "layer" / "python"
FIXTURES = REPO / "use-cases" / "business-operations" / "fixtures" / "contract"
for p in (str(UC), str(LAYER)):
    if p not in sys.path:
        sys.path.insert(0, p)

from business_operations import config as uc_config
from business_operations import packs as uc_packs
from business_operations import validate_packs

CONSOLE = "console-client"
USERS = "business-operations-users"
AUTHORS = "business-operations-authors"


def _case_tools() -> list[dict]:
    spec = importlib.util.spec_from_file_location("bo_stack_packs", UC / "stack.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.TOOLS


def _known(settings: dict | None = None) -> set[str]:
    return uc_packs.known_tools(bo_sap_helper.config(settings), _case_tools())


def test_every_tool_a_read_only_run_is_denied_is_one_the_run_checks_see_and_the_agent_withholds():
    # A name no served tool carries, or one the run checks pass ungated, refuses nothing.
    from business_operations_check import run_checks

    spec = importlib.util.spec_from_file_location(
        "bo_mcp_topology_packs", UC / "agent/utils/mcp_topology.py"
    )
    topology = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(topology)
    gated = {run_checks.split(t)[1] for t in _known() if run_checks.gated(t)}
    assert run_checks.READ_ONLY_DENIED_TOOLS <= gated
    # The agent also withholds odata_delete, which the SAP layer refuses for everyone.
    assert (
        topology._READ_ONLY_DENIED_TOOLS - {"odata_delete"}
        == run_checks.READ_ONLY_DENIED_TOOLS
    )


# ── validate_packs ──


def test_the_shipped_packs_validate_with_and_without_a_sap_system():
    assert validate_packs.validate(_known()) == []
    s4 = {
        "sap": {
            "systems": {
                "s4": {
                    "system_role": "sandbox",
                    "base_url": "https://s.example/",
                    "secret_name": "x",
                }
            }
        }
    }
    assert validate_packs.validate(_known(s4)) == []


def test_a_pack_naming_an_unconfigured_sap_system_warns_but_synthesizes():
    prd = {
        "sap": {
            "systems": {
                "prd": {
                    "system_role": "sandbox",
                    "base_url": "https://s.example/",
                    "secret_name": "x",
                }
            }
        }
    }
    assert validate_packs.validate(_known(prd)) == []
    assert uc_packs.unconfigured_systems(bo_sap_helper.config(prd)) == ["s4"]
    assert uc_packs.unconfigured_systems(uc_config.load({})) == []


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "packs"
    shutil.copytree(UC / "packs", root, ignore=shutil.ignore_patterns("_seed"))
    second_pack(root)
    return root


#: The checkout ships one pack; the cross-pack checks need a second.
SECOND = "second_pack"


def second_pack(root: Path) -> None:
    """A minimal second pack beside the shipped ones, with one process type."""
    directory = root / SECOND
    (directory / "sops").mkdir(parents=True)
    (directory / "pack.json").write_text(
        json.dumps(
            {
                "$schema": "../pack.schema.json",
                "pack_id": SECOND,
                "display_name": "Second",
                "description": "A second pack, for the checks that need two.",
                "model_tier": "sonnet",
                "max_turns": 15,
                "gateway_tools": [
                    "business-operations-case-management___get_case_state",
                    "sap-s4___odata_read",
                ],
                "process_type_to_sop": {"po_accrual": f"{SECOND}/po_accrual.txt"},
            }
        )
    )
    (directory / "base_prompt.txt").write_text(
        "Work the case.\n{SOP_CONTENT}\n{PLATFORM_MECHANICS}\n"
    )
    (directory / "sops" / "po_accrual.txt").write_text(
        "Version 1.0\n1. The agent MUST read the case before acting.\n"
    )


def _edit(root: Path, pack: str, **changes):
    path = root / pack / "pack.json"
    doc = json.loads(path.read_text())
    doc.update(changes)
    path.write_text(json.dumps(doc))


def test_a_pdf_sop_is_refused_at_synth(tree):
    (tree / "finance_ops" / "sops" / "scan.pdf").write_bytes(b"%PDF-1.4")
    _edit(tree, "finance_ops", process_type_to_sop={"scan": "finance_ops/scan.pdf"})
    errors = validate_packs.validate(_known(), tree)
    assert any("scan.pdf" in e and "text" in e for e in errors), errors


def test_a_bare_tool_name_and_an_unprefixed_group_are_refused(tree):
    _edit(
        tree,
        "finance_ops",
        gateway_tools=["odata_read"],
        entitlement={"groups": ["ap-clerk"]},
    )
    errors = validate_packs.validate(_known(), tree)
    assert any("odata_read" in e for e in errors)
    assert any("'ap-clerk'" in e and "shared" in e for e in errors)


def test_a_precedent_tool_on_a_pack_with_no_supplier_is_refused(tree):
    tool = "business-operations-agent-knowledge___get_precedent"
    finance = json.loads((tree / "finance_ops" / "pack.json").read_text())
    assert tool in finance["gateway_tools"]
    _edit(tree, SECOND, gateway_tools=[tool])
    errors = validate_packs.validate(_known(), tree)
    assert errors and all(SECOND in e for e in errors), errors
    assert "supplier_number" in errors[0]


def test_a_sap_writing_pack_must_declare_a_case_document_check(tree):
    finance = json.loads((tree / "finance_ops" / "pack.json").read_text())
    doc = json.loads((tree / "finance_ops" / "pack.json").read_text())
    del doc["case_document_check"]
    (tree / "finance_ops" / "pack.json").write_text(json.dumps(doc))
    errors = validate_packs.validate(_known(), tree)
    assert errors and "no case_document_check" in errors[0], errors
    _edit(tree, "finance_ops", case_document_check={"SupplierInvoice": "InvoiceNumber"})
    errors = validate_packs.validate(_known(), tree)
    assert errors and "['InvoiceNumber']" in errors[0], errors
    _edit(tree, "finance_ops", case_document_check=finance["case_document_check"])
    assert validate_packs.validate(_known(), tree) == []


def _drop(root: Path, pack: str, *keys: str, constant: str | None = None):
    path = root / pack / "pack.json"
    doc = json.loads(path.read_text())
    for k in keys:
        doc.pop(k, None)
    if constant:
        doc["constants"].pop(constant, None)
    path.write_text(json.dumps(doc))


def test_a_sap_writing_pack_needs_a_write_limit_or_a_stated_reason(tree):
    _drop(tree, "finance_ops", constant="AUTO_WRITE_LIMIT_USD")
    errors = validate_packs.validate(_known(), tree)
    assert any(
        "AUTO_WRITE_LIMIT_USD" in e and "write_limit_waiver" in e for e in errors
    )
    _edit(tree, "finance_ops", write_limit_waiver="   ")
    assert validate_packs.validate(_known(), tree) != []
    _edit(tree, "finance_ops", write_limit_waiver="a sandbox pack with no money in it")
    assert not any(
        "write_limit_waiver" in e for e in validate_packs.validate(_known(), tree)
    )


def test_a_sap_writing_pack_must_declare_write_guards_or_an_empty_list(tree):
    _drop(tree, "finance_ops", "write_guards")
    errors = validate_packs.validate(_known(), tree)
    assert any("write_guards" in e and "write_guards: []" in e for e in errors), errors
    _edit(tree, "finance_ops", write_guards=[])
    assert validate_packs.validate(_known(), tree) == []


def test_the_shipped_supplier_invoice_pack_declares_its_amount_field():
    pack = json.loads((validate_packs.PACKS / "finance_ops" / "pack.json").read_text())
    assert pack["write_amount_field"] == "InvoiceGrossAmount"
    assert "AUTO_WRITE_LIMIT_USD" in pack["constants"] and pack["write_guards"]


def test_a_process_type_claimed_twice_is_refused(tree):
    _edit(
        tree,
        SECOND,
        process_type_to_sop={"price_variance": f"{SECOND}/po_accrual.txt"},
    )
    errors = validate_packs.validate(_known(), tree)
    assert any(
        "'price_variance' is already owned by" in e and "finance_ops" in e
        for e in errors
    ), errors


def test_a_sop_citing_an_undeclared_constant_is_refused_at_synth(tree):
    sop = next((tree / "finance_ops" / "sops").glob("*.txt"))
    sop.write_text(
        sop.read_text() + "\nWithin {{NO_SUCH_LIMIT}}, tell {{CONTACT_NOBODY}}."
    )
    errors = validate_packs.validate(_known(), tree)
    assert len(errors) == 1, errors
    assert "{{NO_SUCH_LIMIT}}" in errors[0] and "'finance_ops'" in errors[0]
    # Contacts are the deployment's directory, not the pack's.
    assert "CONTACT_NOBODY" not in errors[0]


def test_the_shipped_sops_cite_only_declared_constants():
    from pack_tiers import undeclared_symbols

    for sop in (UC / "packs").glob("[!_]*/sops/*"):
        pack = json.loads((sop.parents[1] / "pack.json").read_text())
        key = f"{sop.parents[1].name}/{sop.name}"
        assert undeclared_symbols(key, sop.read_text(), pack.get("constants")) is None


def test_the_symbol_check_reads_the_pattern_the_agent_substitutes():
    import re

    from pack_tiers import SYMBOL_RE

    agent = (UC / "agent" / "utils" / "config_overrides.py").read_text()
    assert f'_PLACEHOLDER_RE = re.compile(r"{SYMBOL_RE.pattern}")' in agent
    assert re.compile(SYMBOL_RE.pattern).findall("{{A_1}} {{a}} {{CONTACT_X}}") == [
        "A_1",
        "CONTACT_X",
    ]


def test_contacts_the_sops_cite_and_the_deployment_leaves_unset_are_named(tree):
    sop = next((tree / "finance_ops" / "sops").glob("*.txt"))
    sop.write_text("Tell {{CONTACT_GOLF_X}} and {{CONTACT_AP_TEAM}}.")
    for other in (tree / "finance_ops" / "sops").glob("*.txt"):
        if other != sop:
            other.unlink()
    shutil.rmtree(tree / SECOND / "sops")
    unset = uc_config.load({"contacts": {"ap_team": " "}})
    assert uc_packs.unconfigured_contacts(unset, tree) == ["ap_team", "golf_x"]
    both = {"ap_team": "ap@example.com", "golf_x": "x@example.com"}
    set_ = uc_config.load({"contacts": both})
    assert uc_packs.unconfigured_contacts(set_, tree) == []
    # The resolver upper-cases a key either way; the checks compare lower case.
    upper = uc_config.load({"contacts": {"AP_TEAM": "ap@example.com", "Golf_X": "x"}})
    assert upper.contacts == {"ap_team": "ap@example.com", "golf_x": "x"}
    assert uc_packs.unconfigured_contacts(upper, tree) == []
    with pytest.raises(ValueError, match="two cases"):
        uc_config.load({"contacts": {"ap_team": "a@example.com", "AP_TEAM": "b"}})


def test_a_pack_naming_a_tier_the_deployment_does_not_serve_fails_synth(tree):
    _edit(tree, SECOND, model_tier="opus")
    served = {"haiku", "sonnet"}
    errors = validate_packs.validate(_known(), tree, served_tiers=served)
    assert len(errors) == 1 and "model_tier is 'opus'" in errors[0], errors
    assert "opus_model_id" in errors[0]
    assert validate_packs.validate(_known(), tree, served_tiers={*served, "opus"}) == []
    # Without a deployment to check against (tests, tooling), the tier is not checked.
    assert validate_packs.validate(_known(), tree) == []


# ── a dict-backed S3 ──


class FakeS3:
    class exceptions:
        # boto3's is a ClientError carrying the code, which both readers rely on.
        NoSuchKey = type("NoSuchKey", (ClientError,), {})

    def __init__(self, objects: dict[tuple[str, str], bytes] | None = None):
        self.objects = dict(objects or {})
        self.puts: list[tuple[str, str]] = []
        self.meta: dict[tuple[str, str], dict] = {}

    def _etag(self, key):
        return f'"{hash(self.objects[key])}"'

    def _missing(self, op):
        return self.exceptions.NoSuchKey({"Error": {"Code": "NoSuchKey"}}, op)

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise self._missing("GetObject")
        body = self.objects[(Bucket, Key)]
        return {
            "Body": io.BytesIO(body),
            "ContentLength": len(body),
            "LastModified": datetime.now(UTC),
            "VersionId": f"v{len(body)}",
            "ETag": self._etag((Bucket, Key)),
            "Metadata": self.meta.get((Bucket, Key), {}),
        }

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise self._missing("HeadObject")
        return {}

    def put_object(
        self, Bucket, Key, Body, IfNoneMatch=None, IfMatch=None, Metadata=None, **_
    ):
        exists = (Bucket, Key) in self.objects
        if (IfNoneMatch == "*" and exists) or (
            IfMatch and (not exists or IfMatch != self._etag((Bucket, Key)))
        ):
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.objects[(Bucket, Key)] = Body if isinstance(Body, bytes) else Body.encode()
        self.meta[(Bucket, Key)] = dict(Metadata or {})
        self.puts.append((Bucket, Key))
        return {"VersionId": "v-new", "ETag": self._etag((Bucket, Key))}

    def copy_object(self, Bucket, Key, CopySourceIfMatch, Metadata, **_):
        if CopySourceIfMatch != self._etag((Bucket, Key)):
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "CopyObject")
        self.meta[(Bucket, Key)] = dict(Metadata)

    def delete_object(self, Bucket, Key):
        self.objects.pop((Bucket, Key), None)

    def download_file(self, Bucket, Key, Filename):
        Path(Filename).write_bytes(self.objects[(Bucket, Key)])

    def get_paginator(self, _name):
        fake = self

        class P:
            def paginate(self, Bucket, Prefix=""):
                yield {
                    "Contents": [
                        {"Key": k, "Size": len(v), "LastModified": datetime.now(UTC)}
                        for (b, k), v in sorted(fake.objects.items())
                        if b == Bucket and k.startswith(Prefix)
                    ]
                }

        return P()


def _packs_zip(root: Path) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for path in root.rglob("*"):
            if path.is_file():
                z.write(path, path.relative_to(root))
    return out.getvalue()


def _composer(monkeypatch, s3: FakeS3):
    monkeypatch.setenv("REGISTRY_BUCKET", "reg")
    monkeypatch.setenv("SOPS_BUCKET", "sops")
    monkeypatch.setattr(boto3, "client", lambda *a, **k: s3)
    import registry

    monkeypatch.setattr(registry, "_get_s3", lambda: s3)
    registry.reset()
    spec = importlib.util.spec_from_file_location(
        "bo_composer", UC / "pack_composer" / "index.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._DEPLOY_IMPORT_PROOF = (
        "deploy-proof"  # what the import invoker reads from the secret
    )
    return mod


def _seed(mod, s3: FakeS3, root: Path):
    s3.objects[("assets", "packs.zip")] = _packs_zip(root)
    return mod.handler(
        {
            "DeployImportProof": "deploy-proof",
            "RequestType": "Create",
            # What the deployed template sends (`business_operations/packs.py`).
            "ResourceProperties": {
                "AssetBucket": "assets",
                "AssetKey": "packs.zip",
                "InPlaceEdits": "refuse",
                "CheckoutChanges": "import",
                "RuleOverrides": "refuse",
            },
        },
        None,
    )


def _doc(s3: FakeS3, key: str) -> dict:
    return json.loads(s3.objects[("reg", key)])


# ── the composer ──


def test_the_deploy_import_fails_the_deploy_on_a_pdf_sop(monkeypatch, tree):
    (tree / "finance_ops" / "sops" / "scan.pdf").write_bytes(b"%PDF")
    s3 = FakeS3()
    mod = _composer(monkeypatch, s3)
    with pytest.raises(RuntimeError, match="scan.pdf"):
        _seed(mod, s3, tree)
    # Each pack's import is all or nothing; a retried deploy seeds what is still missing.
    assert ("reg", "finance_ops/pack.json") not in s3.objects
    assert ("reg", "_bundles/finance_ops/active.json") not in s3.objects


def test_a_structure_patch_needs_a_pack_admin(monkeypatch, tree):
    s3 = FakeS3()
    mod = _composer(monkeypatch, s3)
    _seed(mod, s3, tree)
    result = mod.handler(
        {"pack_id": "finance_ops", "roles": ["author"], "pack": {"gateway_tools": []}},
        None,
    )
    assert result["status"] == 400
    result = mod.handler(
        {
            "pack_id": "finance_ops",
            "roles": ["pack_admin"],
            "pack": {"constants": {"QTY_VARIANCE_PCT": 7}},
        },
        None,
    )
    assert result["result"] == "active" and result["version"] == 2, result
    assert _doc(s3, "finance_ops/pack.json")["constants"]["QTY_VARIANCE_PCT"] == 7


def test_a_created_pack_holds_only_the_tools_the_stack_chose(monkeypatch, tree):
    from business_operations import names

    monkeypatch.setenv("CREATED_PACK_TOOLS", json.dumps(names.created_pack_tools("s4")))
    s3 = FakeS3()
    mod = _composer(monkeypatch, s3)
    _seed(mod, s3, tree)
    spec = {
        "display_name": "Vendor bank changes",
        "description": "Changed bank details awaiting review.",
        "short_id": "vbank",
        "kind": "filter",
        "definition": "A bank detail changed in the last day.",
        "process_types": ["bank_change"],
    }
    result = mod.handler(
        {"pack_id": "vendor_bank", "tier": "create", "create": spec}, None
    )
    assert "error" not in result, result
    tools = _doc(s3, "vendor_bank/pack.json")["gateway_tools"]
    assert tools and all(t.startswith(("business-operations-", "sap-")) for t in tools)
    assert not [
        t
        for t in tools
        if t.endswith(("send_notification", "odata_update", "odata_create"))
    ]


# ── the agent reads text SOPs only ──


def test_the_agent_fails_closed_on_a_non_text_sop_and_records_the_version(monkeypatch):
    # The agent's `utils` package, by path: `scripts/utils.py` holds the bare name.
    spec = importlib.util.spec_from_file_location(
        "bo_agent_utils",
        UC / "agent" / "utils" / "__init__.py",
        submodule_search_locations=[str(UC / "agent" / "utils")],
    )
    package = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "bo_agent_utils", package)
    spec.loader.exec_module(package)
    skill_router = importlib.import_module("bo_agent_utils.skill_router")

    assert skill_router._sop_key_candidates("p/a.pdf") == []
    assert skill_router._sop_key_candidates("p/a.txt") == ["p/a.txt", "p/a.md"]
    s3 = FakeS3({("b", "p/a.md"): b"Version 1.0\nthe procedure"})
    monkeypatch.setattr(skill_router, "_get_s3", lambda: s3)
    assert skill_router._fetch_sop_from_s3("b", "p/a.pdf") is None
    assert skill_router._fetch_sop_from_s3("b", "p/a.txt") == (
        "Version 1.0\nthe procedure",
        "v25",
    )


# ── one pack per process type ──

_SHADOW_SPEC = {
    "display_name": "Shadow",
    "description": "A pack minted over HTTP.",
    "short_id": "zzs",
    "kind": "filter",
    "definition": "Anything at all.",
    "process_types": ["price_variance"],
    "attributes": [{"name": "doc", "label": "Document", "type": "string"}],
}


def test_a_create_claiming_a_live_packs_process_type_is_refused(monkeypatch, tree):
    """The takeover: `zz_shadow` minted with finance_ops's `price_variance` used to publish,
    and the agent's index then routed every price_variance case to it."""
    s3 = FakeS3()
    mod = _composer(monkeypatch, s3)
    _seed(mod, s3, tree)
    published = s3.objects[("reg", "registry.json")]

    result = mod.handler(
        {
            "pack_id": "zz_shadow",
            "tier": "create",
            "create": _SHADOW_SPEC,
            "actor": "a",
        },
        None,
    )

    assert result["status"] == 409, result
    assert "zz_shadow" in result["error"] and "'price_variance'" in result["error"]
    assert "already owned by finance_ops" in result["error"]
    assert s3.objects[("reg", "registry.json")] == published
    assert not any(k.startswith("zz_shadow/") for _, k in s3.objects)

    fresh = {**_SHADOW_SPEC, "process_types": ["shadow_check"]}
    result = mod.handler(
        {"pack_id": "zz_shadow", "tier": "create", "create": fresh, "actor": "a"}, None
    )
    assert "error" not in result, result


def test_an_import_claiming_a_published_type_is_refused(monkeypatch, tree):
    import bundle

    s3 = FakeS3()
    mod = _composer(monkeypatch, s3)
    _seed(mod, s3, tree)
    published = s3.objects[("reg", "registry.json")]
    _edit(
        tree,
        SECOND,
        process_type_to_sop={"price_variance": f"{SECOND}/po_accrual.txt"},
    )
    draft = bundle.from_dir(tree / SECOND)
    for dry_run in (True, False):
        result = mod.handler(
            {"verb": "import", "bundle": draft, "dry_run": dry_run}, None
        )
        assert result["status"] == 409, result
        assert "already owned by finance_ops" in result["error"]
    assert s3.objects[("reg", "registry.json")] == published


def test_an_import_off_the_schema_is_refused(monkeypatch, tree):
    """The composer validates every bundle it imports or seeds against the schema,
    not only the keys `pack_tiers` knows by name."""
    import bundle

    s3 = FakeS3()
    mod = _composer(monkeypatch, s3)
    _seed(mod, s3, tree)
    _edit(tree, SECOND, model_tier="standard", display_name="x" * 201)
    result = mod.handler(
        {"verb": "import", "bundle": bundle.from_dir(tree / SECOND)}, None
    )
    assert result["status"] == 400, result
    said = " ".join([result["error"], result.get("detail") or ""])
    assert "model_tier" in said and "longer than 200" in said


def test_a_constant_out_of_bounds_fails_synth(tree):
    _edit(tree, SECOND, constants={"PRICE_VARIANCE_PCT": 150})
    errors = validate_packs.validate(_known(), tree)
    assert any("PRICE_VARIANCE_PCT" in e and "between 0 and 100" in e for e in errors)


def _agent_utils(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "bo_agent_utils",
        UC / "agent" / "utils" / "__init__.py",
        submodule_search_locations=[str(UC / "agent" / "utils")],
    )
    package = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "bo_agent_utils", package)
    spec.loader.exec_module(package)
    return importlib.import_module("bo_agent_utils.skill_router")


def test_a_contested_process_type_routes_nowhere(monkeypatch):
    """A registry with a duplicate in it, as one published before the write-time check
    would be: a tie refuses to route, and an incomplete pack never beats a complete one."""
    skill_router = _agent_utils(monkeypatch)
    registry = skill_router.registry
    monkeypatch.setenv("SAP_MCP_ENABLED", "true")

    def pack(pack_id, types, service):
        return {
            "pack": {
                "pack_id": pack_id,
                "process_type_to_sop": {t: f"{pack_id}/{t}.txt" for t in types},
            },
            "base_prompt": "{SOP_CONTENT}{PLATFORM_MECHANICS}",
            "source": {"service": service},
            "incomplete": registry.is_incomplete({"service": service}),
        }

    registry.reset()
    registry._cache = {
        "schema": registry.SCHEMA,
        "platform_prompt": "",
        "packs": {
            "finance_ops": pack("finance_ops", ["price_variance", "short_pay"], "S"),
            "rogue": pack("rogue", ["price_variance"], "R"),
            "zz_shadow": pack("zz_shadow", ["short_pay", "fresh"], "<AWAITING_SURVEY>"),
        },
    }
    skill_router._skills_index = None
    try:
        routes, refused = registry.process_type_routes()
        assert routes == {"short_pay": "finance_ops"}
        assert refused == {"price_variance": ["finance_ops", "rogue"]}
        assert registry.process_type_pack_map(include_incomplete=True) == {
            "short_pay": "finance_ops",
            "fresh": "zz_shadow",
        }
        assert {p: e["sops"] for p, e in registry.sop_index().items()} == {
            "finance_ops": {"short_pay": "finance_ops/short_pay.txt"},
            "rogue": {},
            "zz_shadow": {},
        }
        index = skill_router._load_skills_index()
        assert {t: e["pack_id"] for t, e in index.items()} == {
            "short_pay": "finance_ops",
            "fresh": "zz_shadow",
        }
        with pytest.raises(
            skill_router.ContestedProcessType, match="finance_ops, rogue"
        ):
            skill_router.resolve_skill("price_variance")
        assert issubclass(skill_router.ContestedProcessType, skill_router.SopLoadError)
    finally:
        skill_router._skills_index = None
        registry.reset()


def test_the_shipped_packs_route_every_type_they_declare(monkeypatch):
    import registry

    monkeypatch.delenv("REGISTRY_BUCKET", raising=False)
    monkeypatch.delenv("PACKS_DIR", raising=False)
    registry.reset()
    try:
        routes, refused = registry.process_type_routes()
        assert refused == {}
        for pack_id, entry in registry.packs().items():
            for process_type in entry["pack"]["process_type_to_sop"]:
                assert routes[process_type] == pack_id
    finally:
        registry.reset()


# ── the routes ──


def _event(method, resource, groups=(USERS, AUTHORS), body=None, query=None):
    claims = {
        "aud": CONSOLE,
        "sub": "s",
        "email": "a@example.com",
        "cognito:groups": "[" + " ".join(groups) + "]",
    }
    return {
        "httpMethod": method,
        "resource": resource,
        "path": resource,
        "headers": {},
        "queryStringParameters": query,
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {"authorizer": {"claims": claims}},
    }


class FakeAws(FakeS3):
    """S3, plus the SSM and Amazon Bedrock calls a SOP write makes, and the composer invoke
    the routes make: `composer`, when set, answers it in-process."""

    composer = None

    def __init__(self, objects):
        super().__init__(objects)
        self.jobs: list[dict] = []
        self.invokes: list[dict] = []

    def invoke(self, FunctionName, Payload, **_):
        self.invokes.append(json.loads(Payload))
        answer = self.composer.handler(json.loads(Payload), None)
        return {"Payload": io.BytesIO(json.dumps(answer).encode())}

    def get_parameter(self, Name):
        return {"Parameter": {"Value": Name.rsplit("/", 1)[1]}}

    def list_ingestion_jobs(self, **_):
        return {"ingestionJobSummaries": []}

    def start_ingestion_job(self, **kw):
        self.jobs.append(kw)
        return {"ingestionJob": {"ingestionJobId": "j1", "status": "STARTING"}}


def _authoring(monkeypatch, tree: Path, role: str = "development") -> FakeAws:
    """A seeded stack whose routes reach the composer in-process, as deployed."""
    aws = FakeAws({})
    monkeypatch.setenv("PROMOTION_ROLE", role)
    monkeypatch.setenv("PACK_COMPOSER_FUNCTION", "composer")
    # The seed re-indexes through `sop_store`, which reads its env once: no SOP search.
    monkeypatch.delitem(sys.modules, "sop_store", raising=False)
    aws.composer = _composer(monkeypatch, aws)
    _seed(aws.composer, aws, tree)
    return aws


def _sops_route(monkeypatch, aws: FakeAws, published: dict | None):
    """The `/sops` handler over `aws`; `published` replaces its registry, None keeps the
    one `_authoring` seeded."""
    for k, v in {
        "API_ALLOWED_CLIENTS": CONSOLE,
        "USERS_GROUP": USERS,
        "SOP_AUTHOR_GROUPS": AUTHORS,
        "SOPS_BUCKET": "sops",
        "REGISTRY_BUCKET": "reg",
        "SOPS_KB_ID_PARAM": "/p/sops-kb-id",
        "SOPS_DATA_SOURCE_ID_PARAM": "/p/sops-data-source-id",
    }.items():
        monkeypatch.setenv(k, v)
    if published is not None:
        aws.objects[("reg", "registry.json")] = json.dumps(published).encode()
    monkeypatch.setattr(boto3, "client", lambda *a, **k: aws)
    import registry

    monkeypatch.setattr(registry, "_get_s3", lambda: aws)
    registry.reset()
    for name in ("sop_store",):
        sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(
        "bo_sops", UC / "api" / "sops" / "index.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PUBLISHED = {
    "schema": "pack-registry/1",
    "packs": {
        "open_pack": {"pack": {"pack_id": "open_pack"}},
        "closed_pack": {
            "pack": {
                "pack_id": "closed_pack",
                "entitlement": {"groups": ["business-operations-x"]},
            }
        },
    },
}


def _versions(aws: FakeAws, pack_id: str) -> list[dict]:
    return aws.composer.handler(
        {"verb": "versions", "pack_id": pack_id, "actor": "t"}, None
    )["versions"]


PACK_ADMINS = "business-operations-pack-admins"
#: An author who may also change a pack's mapping: what a new SOP and a delete need.
MAPPERS = (USERS, AUTHORS, PACK_ADMINS)


def _sop_event(
    method, pack_id, process_type=None, body=None, groups=MAPPERS, check=False
):
    """An event for `/packs/{pack_id}/sops[/{process_type}[/check]]`."""
    resource = "/packs/{pack_id}/sops"
    params = {"pack_id": pack_id}
    if process_type is not None:
        resource += "/{process_type}" + ("/check" if check else "")
        params["process_type"] = process_type
    event = _event(method, resource, groups=groups, body=body)
    event["pathParameters"] = params
    return event


def test_a_new_sop_comes_with_its_process_type_in_one_version(monkeypatch, tree):
    aws = _authoring(monkeypatch, tree)
    mod = _sops_route(monkeypatch, aws, None)
    key = f"{SECOND}/a.txt"
    body = {"process_type": "accrual_review", "name": "a.txt", "content": "Version 1.0"}
    resp = mod.handler(_sop_event("POST", SECOND, body=body), None)
    assert resp["statusCode"] == 201, resp
    answer = json.loads(resp["body"])
    assert answer["version"] == 2 and answer["uploaded_by"] == "a@example.com"
    assert answer["ingestion"] == {"job_id": "j1", "status": "STARTING"}
    assert aws.jobs[0]["knowledgeBaseId"] == "sops-kb-id"
    # The route sent the file, the text and the mapping, never a bundle.
    assert set(aws.invokes[-1]) == {"pack_id", "sop", "roles", "actor"}
    assert aws.invokes[-1]["sop"]["map_as"] == "accrual_review"
    assert aws.objects[("sops", key)] == b"Version 1.0"
    sidecar = ("sops", f"{key}.metadata.json")
    assert json.loads(aws.objects[sidecar])["metadataAttributes"] == {"pack_id": SECOND}
    published = json.loads(aws.objects[("reg", "registry.json")])
    mapping = published["packs"][SECOND]["pack"]["process_type_to_sop"]
    assert mapping["accrual_review"] == key
    minted = _versions(aws, SECOND)[-1]
    assert minted["version"] == 2 and minted["edited_by"] == "a@example.com"
    audit = aws.composer.handler(
        {"verb": "versions", "pack_id": SECOND, "actor": "t"}, None
    )["audit"]
    assert audit[-1]["verb"] == "edit"
    assert audit[-1]["asserted_actor"] == "a@example.com"
    read = json.loads(
        mod.handler(_sop_event("GET", SECOND, "accrual_review"), None)["body"]
    )
    assert (read["key"], read["pack_version"], read["kind"]) == (key, 2, "exception")
    listing = json.loads(mod.handler(_sop_event("GET", SECOND), None)["body"])
    assert {r["process_type"] for r in listing["sops"]} == {
        "po_accrual",
        "accrual_review",
    }
    again = mod.handler(_sop_event("POST", SECOND, body=body), None)
    assert again["statusCode"] == 409, "a mapped type is replaced with PUT"
    deleted = mod.handler(_sop_event("DELETE", SECOND, "accrual_review"), None)
    assert deleted["statusCode"] == 200, deleted
    assert json.loads(deleted["body"])["version"] == 3
    assert ("sops", key) not in aws.objects and ("sops", sidecar[1]) not in aws.objects
    published = json.loads(aws.objects[("reg", "registry.json")])
    assert (
        "accrual_review"
        not in published["packs"][SECOND]["pack"]["process_type_to_sop"]
    )
    assert len(aws.jobs) == 2, "the delete re-indexes too"


def test_adding_or_removing_a_sop_needs_a_pack_admin(monkeypatch, tree):
    aws = _authoring(monkeypatch, tree)
    mod = _sops_route(monkeypatch, aws, None)
    author = (USERS, AUTHORS)
    body = {"process_type": "accrual_review", "content": "Version 1.0"}
    refused = mod.handler(_sop_event("POST", SECOND, body=body, groups=author), None)
    assert refused["statusCode"] == 403 and PACK_ADMINS in refused["body"]
    gone = mod.handler(_sop_event("DELETE", SECOND, "po_accrual", groups=author), None)
    assert gone["statusCode"] == 403
    assert ("sops", f"{SECOND}/accrual_review.txt") not in aws.objects
    # Replacing a mapped SOP's text is the author's alone.
    put = mod.handler(
        _sop_event(
            "PUT", SECOND, "po_accrual", body={"content": "Version 2"}, groups=author
        ),
        None,
    )
    assert put["statusCode"] == 200, put


def test_a_stale_sop_edit_is_a_409_and_writes_nothing(monkeypatch, tree):
    aws = _authoring(monkeypatch, tree)
    mod = _sops_route(monkeypatch, aws, None)
    key = f"{SECOND}/po_accrual.txt"

    def put(content, expected):
        body = {"content": content, "expected_version": expected}
        return mod.handler(_sop_event("PUT", SECOND, "po_accrual", body=body), None)

    assert put("Version 1.1\nFirst.", 1)["statusCode"] == 200
    stale = put("Version 1.1\nSecond, from the same read.", 1)
    assert stale["statusCode"] == 409, stale
    assert "since you opened it" in stale["body"] and "at v2" in stale["body"]
    assert aws.objects[("sops", key)] == b"Version 1.1\nFirst."
    assert put("Version 1.2\nRead again.", 2)["statusCode"] == 200
    assert put("x", True)["statusCode"] == 400


def test_a_check_lints_like_a_write_and_writes_nothing(monkeypatch, tree):
    aws = _authoring(monkeypatch, tree)
    mod = _sops_route(monkeypatch, aws, None)
    key = f"{SECOND}/dry.txt"

    def check(content):
        event = _sop_event("POST", SECOND, "dry", body={"content": content}, check=True)
        return mod.handler(event, None)

    before = len(_versions(aws, SECOND))
    resp = check("Version 1.0\nThe agent MUST stop.")
    assert resp["statusCode"] == 200, resp
    body = json.loads(resp["body"])
    # A type not mapped yet checks the file a create would write.
    assert body["key"] == key and body["ok"] is False
    assert any(f.startswith("sections ") for f in body["findings"])
    # Nothing stored, no version minted, no index job.
    assert ("sops", key) not in aws.objects
    assert len(_versions(aws, SECOND)) == before and not aws.jobs
    # A refusal comes back exactly as the write's would.
    undeclared = check("Version 1.0 {{NOT_DECLARED_ANYWHERE}}")
    assert (
        undeclared["statusCode"] == 400
        and "NOT_DECLARED_ANYWHERE" in undeclared["body"]
    )
    assert ("sops", key) not in aws.objects


def test_an_alias_reads_its_file_and_is_refused_a_write(monkeypatch, tree):
    _edit(
        tree,
        SECOND,
        process_type_to_sop={
            "po_accrual": f"{SECOND}/po_accrual.txt",
            "accrual": f"{SECOND}/po_accrual.txt",
        },
    )
    aws = _authoring(monkeypatch, tree)
    mod = _sops_route(monkeypatch, aws, None)
    read = json.loads(mod.handler(_sop_event("GET", SECOND, "accrual"), None)["body"])
    assert (read["key"], read["kind"], read["alias_of"]) == (
        f"{SECOND}/po_accrual.txt",
        "alias",
        "po_accrual",
    )
    for method, body in (("PUT", {"content": "Version 2"}), ("DELETE", None)):
        refused = mod.handler(_sop_event(method, SECOND, "accrual", body=body), None)
        assert refused["statusCode"] == 409 and "po_accrual" in refused["body"]
    assert not aws.invokes[1:], "refused before the composer"
    # Deleting the owner takes the alias with it.
    assert (
        mod.handler(_sop_event("DELETE", SECOND, "po_accrual"), None)["statusCode"]
        == 200
    )
    published = json.loads(aws.objects[("reg", "registry.json")])
    assert published["packs"][SECOND]["pack"]["process_type_to_sop"] == {}


def test_the_sop_routes_refuse_a_pdf_and_another_packs_reader(monkeypatch):
    aws = FakeAws({("sops", "closed_pack/x.txt"): b"secret procedure"})
    mod = _sops_route(monkeypatch, aws, PUBLISHED)
    pdf = mod.handler(
        _sop_event(
            "POST",
            "open_pack",
            body={"process_type": "a", "name": "a.pdf", "content": "x"},
        ),
        None,
    )
    assert pdf["statusCode"] == 400
    assert mod.handler(_sop_event("GET", "closed_pack"), None)["statusCode"] == 404
    read = mod.handler(_sop_event("GET", "closed_pack", "x"), None)
    assert read["statusCode"] == 404
    not_author = mod.handler(
        _sop_event(
            "POST",
            "open_pack",
            body={"process_type": "a", "content": "x"},
            groups=(USERS,),
        ),
        None,
    )
    assert not_author["statusCode"] == 403


def test_a_sop_write_citing_an_undeclared_constant_is_refused(monkeypatch, tree):
    _edit(tree, SECOND, constants={"LIMIT_USD": 10})
    aws = _authoring(monkeypatch, tree)
    mod = _sops_route(monkeypatch, aws, None)

    def post(content):
        body = {"process_type": "a", "content": content}
        return mod.handler(_sop_event("POST", SECOND, body=body), None)

    refused = post("Above {{LIMIT_USD}} or {{LIMT_USD}}, mail {{CONTACT_AP_TEAM}}.")
    assert refused["statusCode"] == 400, refused
    error = json.loads(refused["body"])["error"]
    assert "{{LIMT_USD}}" in error and f"'{SECOND}'" in error
    assert "{{LIMIT_USD}}" not in error and "CONTACT" not in error
    assert ("sops", f"{SECOND}/a.txt") not in aws.objects
    assert not aws.jobs and not aws.invokes[1:], "refused before the composer"
    assert post("Above {{LIMIT_USD}}, mail {{CONTACT_AP_TEAM}}.")["statusCode"] == 201
    # The composer is the gate, not the route: it refuses the same on its own.
    direct = aws.composer.handler(
        {
            "pack_id": SECOND,
            "roles": ["author", "pack_admin"],
            "sop": {"op": "put", "name": "b.txt", "content": "{{NOPE}}", "map_as": "b"},
        },
        None,
    )
    assert direct["status"] == 400 and "{{NOPE}}" in direct["error"]
    unauthorized = aws.composer.handler(
        {
            "pack_id": SECOND,
            "sop": {"op": "put", "name": "b.txt", "content": "x"},
        },
        None,
    )
    assert unauthorized["status"] == 403


def test_the_composer_writes_no_sop_file_no_process_type_maps(monkeypatch, tree):
    aws = _authoring(monkeypatch, tree)
    unmapped = aws.composer.handler(
        {
            "pack_id": SECOND,
            "roles": ["author"],
            "sop": {"op": "put", "name": "loose.txt", "content": "Version 1"},
        },
        None,
    )
    assert unmapped["status"] == 400 and "always mapped" in (unmapped["detail"] or "")
    assert ("sops", f"{SECOND}/loose.txt") not in aws.objects


def test_a_throttled_publish_is_retried_then_a_409(monkeypatch):
    import composer_client

    waits: list[float] = []
    monkeypatch.setattr(composer_client.time, "sleep", waits.append)

    class Throttled:
        busy = 99

        def invoke(self, **_):
            if self.busy:
                self.busy -= 1
                raise ClientError(
                    {"Error": {"Code": "TooManyRequestsException"}}, "Invoke"
                )
            return {"Payload": io.BytesIO(b'{"packs": 1}')}

    throttled = Throttled()

    aws = FakeAws({})
    for k, v in {
        "API_ALLOWED_CLIENTS": CONSOLE,
        "USERS_GROUP": USERS,
        "SOP_AUTHOR_GROUPS": AUTHORS,
        "REGISTRY_BUCKET": "reg",
        "PACK_COMPOSER_FUNCTION": "composer",
    }.items():
        monkeypatch.setenv(k, v)
    aws.objects[("reg", "registry.json")] = json.dumps(PUBLISHED).encode()
    import registry

    monkeypatch.setattr(registry, "_get_s3", lambda: aws)
    monkeypatch.setattr(
        boto3, "client", lambda name, *a, **k: throttled if name == "lambda" else aws
    )
    spec = importlib.util.spec_from_file_location(
        "bo_packs", UC / "api" / "packs" / "index.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    event = _event("PATCH", "/packs/{pack_id}", body={"pack": {"display_name": "N"}})
    event["pathParameters"] = {"pack_id": "open_pack"}
    resp = mod.handler(event, None)
    assert resp["statusCode"] == 409, resp
    assert waits == list(composer_client.ROUTE_BACKOFF), "three retries, backing off"
    throttled.busy = 2
    assert mod.handler(event, None)["statusCode"] == 200, "a save that waited"


def test_the_deploy_import_waits_out_a_throttle_and_fails_the_deploy_after(monkeypatch):
    import composer_client

    monkeypatch.setattr(composer_client.time, "sleep", lambda _: None)
    spec = importlib.util.spec_from_file_location(
        "bo_deploy_import_invoker", UC / "pack_composer" / "deploy_import_invoker.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    calls = []

    class Lambda:
        def invoke(self, **kw):
            calls.append(kw)
            if len(calls) < 3:
                raise ClientError(
                    {"Error": {"Code": "TooManyRequestsException"}}, "Invoke"
                )
            return {"Payload": io.BytesIO(b'{"PhysicalResourceId": "pack-seed"}')}

    monkeypatch.setenv("PACK_COMPOSER_FUNCTION", "composer")
    monkeypatch.setattr(boto3, "client", lambda *a, **k: Lambda())
    monkeypatch.setattr(mod, "_proof", lambda: "deploy-proof")
    assert mod.handler({"RequestType": "Update"}, None) == {
        "PhysicalResourceId": "pack-seed"
    }
    calls.clear()
    monkeypatch.setattr(mod, "BACKOFF", (0,))
    with pytest.raises(RuntimeError, match="stayed busy"):
        mod.handler({"RequestType": "Update"}, None)

    class Failed:
        def invoke(self, **_):
            body = b'{"errorMessage": "finance_ops: refused"}'
            return {"FunctionError": "Unhandled", "Payload": io.BytesIO(body)}

    monkeypatch.setattr(boto3, "client", lambda *a, **k: Failed())
    with pytest.raises(RuntimeError, match="finance_ops: refused"):
        mod.handler({"RequestType": "Update"}, None)


@pytest.mark.parametrize("proof", [None, "", "guessed", 7])
def test_the_composer_runs_a_deploy_import_only_with_the_deploys_proof(
    monkeypatch, proof
):
    # A seed publishes the platform prompt and adopts live packs in every role: a
    # holder of the composer's invoke must not replay an old asset through one.
    s3 = FakeS3()
    mod = _composer(monkeypatch, s3)
    event = {
        "RequestType": "Update",
        "ResourceProperties": {"AssetBucket": "assets", "AssetKey": "old.zip"},
    }
    if proof is not None:
        event["DeployImportProof"] = proof
    with pytest.raises(PermissionError, match="import invoker"):
        mod.handler(event, None)
    assert not s3.objects, "it refuses before reading or writing anything"


def test_the_import_invoker_sends_the_proof(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "bo_deploy_import_invoker_proof",
        UC / "pack_composer" / "deploy_import_invoker.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sent = []

    class Lambda:
        def invoke(self, **kw):
            sent.append(json.loads(kw["Payload"]))
            return {"Payload": io.BytesIO(b'{"PhysicalResourceId": "pack-seed"}')}

    monkeypatch.setenv("PACK_COMPOSER_FUNCTION", "composer")
    monkeypatch.setattr(boto3, "client", lambda *a, **k: Lambda())
    monkeypatch.setattr(mod, "_proof", lambda: "deploy-proof")
    mod.handler({"RequestType": "Create"}, None)
    assert sent == [{"RequestType": "Create", "DeployImportProof": "deploy-proof"}]


def test_only_the_import_invoker_and_the_composer_read_the_import_proof():
    t = _stack()
    readers = [
        r["Properties"]["Roles"]
        for r in t["Resources"].values()
        if r["Type"] == "AWS::IAM::Policy"
        and "PackSeedProof" in json.dumps(r["Properties"]["PolicyDocument"])
    ]
    roles = json.dumps(readers)
    assert "PackSeedInvoker" in roles and "PackComposer" in roles
    assert len(readers) == 2


# ── the stack ──


def _stack(fixture="business-operations.yaml") -> dict:
    spec = importlib.util.spec_from_file_location("bo_stack_packs2", UC / "stack.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    platform = load_platform_config(FIXTURES / fixture)
    production = platform.deployment.mode == "production"
    ctx = UseCaseContext(
        project="p", environment="dev", platform=platform, production=production
    )
    ctx, config = bo_sap_helper.load(ctx, {})
    stack = mod.BusinessOperationsStack(
        cdk.App(),
        "p-dev-uc-business-operations",
        ctx=ctx,
        config=config,
        env=cdk.Environment(account="111111111111", region="us-east-1"),
    )
    return Template.from_stack(stack).to_json()


@pytest.mark.parametrize(
    "fixture", ["business-operations.yaml", "business-operations-production.yaml"]
)
def test_only_the_writers_may_write_the_registry_and_sop_buckets(fixture):
    template = _stack(fixture)
    resources = template["Resources"]
    composer = next(
        k
        for k, r in resources.items()
        if r["Type"] == "AWS::Lambda::Function" and k.startswith("PackComposer")
    )
    assert resources[composer]["Properties"]["ReservedConcurrentExecutions"] == 1

    policies = {
        k: r["Properties"]["PolicyDocument"]["Statement"]
        for k, r in resources.items()
        if r["Type"] == "AWS::S3::BucketPolicy"
    }
    registry_deny = sops_deny = text_only = None
    for key, statements in policies.items():
        bucket = resources[key]["Properties"]["Bucket"]["Ref"]
        for s in statements:
            if s.get("Sid") == "DenyWritesButTheWriters":
                writers = s["Condition"]["ArnNotEquals"]["aws:PrincipalArn"]
                if bucket.startswith("Registry"):
                    registry_deny = writers
                elif bucket.startswith("Sops"):
                    sops_deny = writers
            if s.get("Sid") == "DenyNonTextSops":
                text_only = s
    production = fixture.endswith("production.yaml")
    # The composer alone, and the teardown provider outside production.
    for writers in (registry_deny, sops_deny):
        assert len(writers) == (1 if production else 2)
        assert "PackComposer" in json.dumps(writers[0])
        assert "SopsFunction" not in json.dumps(writers)
    # `/sops` reads the bucket and invokes the composer; it holds no write of its own.
    sops_grants = [
        s
        for k, r in resources.items()
        if r["Type"] == "AWS::IAM::Policy" and k.startswith("SopsFunction")
        for s in r["Properties"]["PolicyDocument"]["Statement"]
    ]
    actions = {a for s in sops_grants for a in json.dumps(s["Action"]).split('"')}
    assert not {a for a in actions if a.startswith(("s3:Put", "s3:Delete"))}
    assert "lambda:InvokeFunction" in actions
    (seed_provider,) = [
        r
        for k, r in resources.items()
        if k.startswith("PackSeedProviderframeworkonEvent")
        and r["Type"] == "AWS::Lambda::Function"
    ]
    assert "PackSeedInvoker" in json.dumps(seed_provider["Properties"]["Environment"])
    assert text_only["Action"] == "s3:PutObject"
    assert [r["Fn::Join"][1][-1] for r in text_only["NotResource"]] == [
        "/*.md",
        "/*.txt",
        "/*.md.metadata.json",
        "/*.txt.metadata.json",
    ]


def test_every_pack_key_belongs_to_exactly_one_tier():
    import pack_tiers

    keys = set(
        json.loads((UC / "packs" / "pack.schema.json").read_text())["properties"]
    )
    tiers = [
        pack_tiers.CONTENT_KEYS,
        pack_tiers.STRUCTURE_KEYS,
        pack_tiers.IGNORED_KEYS,
    ]
    assert set().union(*tiers) == keys
    assert sum(len(t) for t in tiers) == len(keys)


def test_an_unreadable_registry_alarms_on_the_dimension_the_handlers_emit():
    template = _stack()
    (alarm,) = [
        r["Properties"]
        for k, r in template["Resources"].items()
        if k.startswith("RegistryUnavailableAlarm")
    ]
    assert alarm["MetricName"] == "RegistryUnavailable"
    assert [d["Name"] for d in alarm["Dimensions"]] == ["Stack"]
    assert alarm["AlarmActions"]


def test_the_deploy_import_re_runs_when_the_composer_changes():
    # CloudFormation re-invokes a custom resource only when its properties change, and
    # otherwise serves the last response: an attribute a new composer adds is absent.
    from business_operations import knowledge

    (seed,) = [
        r
        for r in _stack()["Resources"].values()
        if r["Type"] == "AWS::CloudFormation::CustomResource"
        and "AssetKey" in r["Properties"]
    ]
    assert (
        seed["Properties"]["Composer"]
        == knowledge.tree_hash(
            UC / "pack_composer", UC / "api" / "layer" / "python", pattern="*.py"
        )[:16]
    )


def test_every_disposition_a_sop_names_is_one_its_pack_declares():
    # update_case_state refuses an undeclared label, so an SOP that names one would
    # send every case down that branch into a refused close.
    import re

    packs = UC / "packs"
    for pack_json in sorted(packs.glob("[!_]*/pack.json")):
        declared = json.loads(pack_json.read_text()).get("dispositions")
        if declared is None:
            continue
        for sop in sorted((pack_json.parent / "sops").glob("*.txt")):
            text = " ".join(sop.read_text().split())
            for label in re.findall(r'disposition "([a-z0-9_]+)"', text):
                assert label in declared, (
                    f"{sop.name} names {label!r}; {pack_json.parent.name} declares {declared}"
                )
