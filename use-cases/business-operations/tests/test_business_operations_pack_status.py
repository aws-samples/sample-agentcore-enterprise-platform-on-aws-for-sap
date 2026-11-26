# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""One pack's whole state: the composer's `status`, `GET /packs/status`, the SOP search
state `GET /sops` serves, and `ops.py pack status`."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_business_operations_packs as tp
from test_business_operations_packs import FakeAws, FakeS3

# isort: split
# The import above put the API layer on the path.
import bundle
import sop_store

SOP = "finance_ops/price_variance.txt"


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "packs"
    shutil.copytree(tp.UC / "packs", root, ignore=shutil.ignore_patterns("_seed"))
    tp.second_pack(root)
    return root


def _verb(mod, verb, **kw):
    return mod.handler({"verb": verb, "actor": "tester", **kw}, None)


def _dev(monkeypatch, tree):
    # No SOP search here: a `sop_store` another test imported keeps its own env.
    for name in ("SOPS_KB_ID_PARAM", "SOPS_DATA_SOURCE_ID_PARAM"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delitem(sys.modules, "sop_store", raising=False)
    monkeypatch.setenv("PROMOTION_ROLE", "development")
    s3 = FakeS3()
    mod = tp._composer(monkeypatch, s3)
    tp._seed(mod, s3, tree)
    return s3, mod


def test_status_says_where_each_version_came_from_and_what_was_refused(
    monkeypatch, tree
):
    _, mod = _dev(monkeypatch, tree)
    every = {row["pack_id"]: row for row in _verb(mod, "status")["packs"]}
    assert set(every) == {"finance_ops", tp.SECOND}
    seeded = every["finance_ops"]
    assert seeded["origin"]["via"] == "deploy_import" and seeded["last_refusal"] is None
    assert seeded["routes"] and not seeded["incomplete"] and seeded["search"] == "off"
    assert seeded["files"] == bundle.file_hashes(bundle.from_dir(tree / "finance_ops"))

    # An edit, a rollback over it, then a refused import.
    edit = {
        "pack_id": "finance_ops",
        "roles": ["author"],
        "pack": {"display_name": "A"},
    }
    assert mod.handler(edit, None)["version"] == 2
    exported = _verb(mod, "export", pack_id="finance_ops")["bundle"]
    _verb(mod, "rollback", pack_id="finance_ops", to=1, dry_run=False)
    cites = {"price_variance.txt": "Compare with {{NOPE_PCT}}."}
    newer = {**exported, "sops": {**exported["sops"], **cites}}
    refused = _verb(mod, "import", bundle=newer, dry_run=False)
    assert refused["status"] == 400, refused
    status = _verb(mod, "status", pack_id="finance_ops")
    assert status["origin"]["via"] == "rollback", "a refusal is not where it came from"
    assert "drift" not in status
    assert status["last_refusal"]["verb"] == "import"
    assert "NOPE_PCT" in status["last_refusal"]["error"]

    # A dry run writes nothing, so it records nothing.
    _verb(mod, "import", bundle={**newer, "pack_id": "Not A Pack"})
    assert _verb(mod, "status", pack_id="finance_ops")["last_refusal"]["verb"] == (
        "import"
    )

    # A patch the composer refuses is recorded with the rule that refused it.
    patch = {"pack_id": "finance_ops", "roles": ["author"], "pack": {"model_tier": "x"}}
    assert mod.handler(patch, None)["status"] == 400
    last = _verb(mod, "status", pack_id="finance_ops")["last_refusal"]
    assert last["verb"] == "publish" and "pack-admins" in last["error"]

    assert _verb(mod, "status", pack_id="nope")["status"] == 404


def test_an_incomplete_pack_routes_nothing(monkeypatch, tree):
    _, mod = _dev(monkeypatch, tree)
    spec = {**tp._SHADOW_SPEC, "process_types": ["draft_type"]}
    created = mod.handler(
        {"pack_id": "zz_draft", "tier": "create", "create": spec, "actor": "ana"},
        None,
    )
    assert "error" not in created, created
    status = _verb(mod, "status", pack_id="zz_draft")
    assert status["incomplete"] and status["routes"] == []
    # Why it routes nothing, field by field, so an author can finish the survey rather
    # than guess which values are still markers.
    assert status["unroutable"][:4] == [
        "incomplete at source.entity",
        "incomplete at source.field_map.doc.path",
        "incomplete at source.filter",
        "incomplete at source.key[0].field",
    ], status["unroutable"]
    assert "incomplete at source.service" in status["unroutable"]
    assert status["active"]["version"] == 1
    assert (status["origin"]["via"], status["origin"]["by"]) == ("create", "ana")


def test_search_is_stale_when_a_sop_was_written_after_the_latest_job():
    now = datetime.now(UTC)
    sops = [
        {"key": "a/x.txt", "last_modified": (now - timedelta(hours=2)).isoformat()},
        {"key": "b/y.txt", "last_modified": now.isoformat()},
        {"key": "root.txt", "last_modified": now.isoformat()},
    ]

    def job(status, hours_ago=1):
        return {
            "status": status,
            "started_at": (now - timedelta(hours=hours_ago)).isoformat(),
        }

    assert sop_store.search_states(sops, None) is None
    assert sop_store.search_states(sops, job("COMPLETE")) == {
        "a": "current",
        "b": "stale",
    }
    assert sop_store.search_states(sops, job("IN_PROGRESS"))["a"] == "indexing"
    assert sop_store.search_states(sops, job("FAILED"))["a"] == "stale"
    assert sop_store.search_states(sops, job("COMPLETE", -1)) == {
        "a": "current",
        "b": "current",
    }


def test_the_sop_listing_says_whether_search_is_current(monkeypatch):
    class Indexed(FakeAws):
        def list_ingestion_jobs(self, **_):
            started = datetime.now(UTC) - timedelta(hours=1)
            return {
                "ingestionJobSummaries": [
                    {"ingestionJobId": "j0", "status": "COMPLETE", "startedAt": started}
                ]
            }

    aws = Indexed({("sops", "open_pack/a.txt"): b"Written after the job."})
    published = json.loads(json.dumps(tp.PUBLISHED))
    published["packs"]["open_pack"]["pack"]["process_type_to_sop"] = {
        "a": "open_pack/a.txt"
    }
    mod = tp._sops_route(monkeypatch, aws, published)
    listing = json.loads(mod.handler(tp._sop_event("GET", "open_pack"), None)["body"])
    assert listing["search"] == {"open_pack": "stale"}
    assert [r["key"] for r in listing["sops"]] == ["open_pack/a.txt"]


def test_the_status_route_shows_only_the_packs_a_caller_may_see(monkeypatch):
    aws = FakeAws({})
    for k, v in {
        "API_ALLOWED_CLIENTS": tp.CONSOLE,
        "USERS_GROUP": tp.USERS,
        "SOP_AUTHOR_GROUPS": tp.AUTHORS,
        "REGISTRY_BUCKET": "reg",
        "PACK_COMPOSER_FUNCTION": "composer",
    }.items():
        monkeypatch.setenv(k, v)
    published = json.loads(json.dumps(tp.PUBLISHED))
    published["packs"]["open_pack"]["pack"]["process_type_to_sop"] = {
        "t": "open_pack/t.txt"
    }
    aws.objects.update(
        {
            ("reg", "registry.json"): json.dumps(published).encode(),
            ("reg", "_bundles/open_pack/active.json"): json.dumps(
                {"version": 3, "sha256": "s3"}
            ).encode(),
            ("reg", "_audit/open_pack/20261101T000000000000Z-import.json"): json.dumps(
                {"verb": "import", "at": "a", "to": {"version": 3, "sha256": "s3"}}
            ).encode(),
            ("reg", "_audit/open_pack/20261102T000000000000Z-import.json"): json.dumps(
                {"verb": "import", "state": "refused", "error": "v5 follows v4"}
            ).encode(),
            ("reg", "_refusals/open_pack.json"): json.dumps(
                {
                    "at": "b",
                    "verb": "import",
                    "error": "v5 follows v4",
                    "asserted_actor": "x",
                }
            ).encode(),
        }
    )
    import registry

    monkeypatch.setattr(registry, "_get_s3", lambda: aws)
    registry.reset()
    monkeypatch.setattr(boto3, "client", lambda *a, **k: aws)
    spec = importlib.util.spec_from_file_location(
        "bo_packs_status", tp.UC / "api" / "packs" / "index.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    resp = mod.handler(tp._event("GET", "/packs/status"), None)
    assert resp["statusCode"] == 200, resp
    assert json.loads(resp["body"])["packs"] == [
        {
            "pack_id": "open_pack",
            "version": 3,
            "origin": {"via": "import", "at": "a", "by": None, "change_ref": None},
            "incomplete": False,
            "routes": ["t"],
            "unroutable": [],
            "last_refusal": {
                "at": "b",
                "verb": "import",
                "error": "v5 follows v4",
                "detail": None,
            },
        }
    ]


def test_pack_status_prints_one_line_per_pack_and_compares_the_checkout(
    monkeypatch, capsys
):
    from business_operations import bundle_ops

    live = bundle.file_hashes(bundle.from_dir(tp.UC / "packs" / "finance_ops"))
    row = {
        "pack_id": "finance_ops",
        "active": {"version": 4, "stored": True, "retired": False},
        "origin": {
            "via": "import",
            "at": "2026-11-10T09:30:00+00:00",
            "change_ref": "C1",
        },
        "routes": ["price_variance"],
        "incomplete": False,
        "unroutable": [],
        "drift": [],
        "files": live,
        "search": "stale",
        "last_refusal": {"at": "2026-11-10T10:00:00", "verb": "import", "error": "No."},
    }
    edited = {
        **row,
        "pack_id": "finance_ops",
        "files": {**live, "sops/x.txt": "0"},
        "values": {
            "PRICE_VARIANCE_PCT": {
                "value": 2,
                "kind": "rule",
                "source": "pack v4",
                "guarded_by": ["within_tolerance_or_approved"],
            },
            "MISSING_GR_ESCALATION_DAYS": {
                "value": 5,
                "kind": "rule",
                "source": "pack v4",
                "guarded_by": [],
            },
            "AUTO_WRITE_LIMIT_USD": {
                "value": 500,
                "kind": "limit",
                "source": "stack override (lowered from 100000)",
            },
            "CONTACT_AP_TEAM": {
                "value": "ap@example.com",
                "kind": "contact",
                "source": "platform.yaml",
            },
        },
    }
    answers = {
        None: {
            "packs": [
                row,
                {
                    **row,
                    "pack_id": "not_in_checkout",
                    "incomplete": True,
                    "routes": [],
                    "unroutable": [
                        "incomplete at source.service",
                        "tools unserved: odata_read",
                    ],
                },
            ]
        },
        "finance_ops": edited,
    }
    monkeypatch.setattr(bundle_ops, "_invoke", lambda p: answers[p.get("pack_id")])
    # Never SSM from a test: each pack's mode and the switch, as `pack status` reads them.
    modes = {"finance_ops": "auto"}
    monkeypatch.setattr(
        bundle_ops,
        "_trigger_mode_reader",
        lambda: lambda pack: {"pack": modes.get(pack), "deployment": "auto"},
    )

    assert bundle_ops.pack_cmd(["status"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        (
            "finance_ops: v4 from import 2026-11-10T09:30 (C1); routes price_variance; "
            "checkout matches; search stale; trigger mode auto (deployment auto); "
            "last refused 2026-11-10T10:00 import: No."
        ),
        (
            "not_in_checkout: v4 from import 2026-11-10T09:30 (C1); routable: false, "
            "incomplete at source.service; tools unserved: odata_read; search stale; "
            "trigger mode manual (unset) (deployment auto); "
            "last refused 2026-11-10T10:00 import: No."
        ),
    ]
    assert bundle_ops.pack_cmd(["status", "finance_ops"]) == 0
    out = capsys.readouterr().out
    assert "  checkout differs: sops/x.txt" in out
    # Each value with where it comes from, after the facts.
    # A rule an enabled write guard reads says so.
    assert out.splitlines()[-4:] == [
        "  AUTO_WRITE_LIMIT_USD = 500 (limit, stack override (lowered from 100000))",
        "  CONTACT_AP_TEAM = ap@example.com (contact, platform.yaml)",
        "  MISSING_GR_ESCALATION_DAYS = 5 (rule, pack v4)",
        "  PRICE_VARIANCE_PCT = 2 (rule, pack v4, guarded by within_tolerance_or_approved)",
    ]
    edit = {**row, "origin": {"via": "edit", "at": "2026-11-10T11:00:00", "by": "ana"}}
    assert bundle_ops.status_facts(edit)[0] == "v4 from edit 2026-11-10T11:00 by ana"
    assert bundle_ops.pack_cmd(["status", "a", "b"]) == 2
    assert bundle_ops.pack_cmd(["versions"]) == 2


def test_unroutable_names_the_sap_tools_a_stack_does_not_serve(monkeypatch):
    """The second inert state: a pack whose SAP tools no MCP server here exposes routes
    nothing either, and that was only ever a log line."""
    import registry

    monkeypatch.setenv("SAP_MCP_ENABLED", "false")
    registry.reset()
    registry._cache = {
        "schema": registry.SCHEMA,
        "platform_prompt": "",
        "packs": {
            "p": {
                "pack": {"pack_id": "p", "gateway_tools": ["sap-s4___odata_read"]},
                "source": {"service": "S"},
                "incomplete": False,
            }
        },
    }
    try:
        assert registry.unroutable_reasons("p") == ["tools unserved: odata_read"]
        assert registry.unroutable_reasons("not_deployed_here") == []
    finally:
        registry.reset()
