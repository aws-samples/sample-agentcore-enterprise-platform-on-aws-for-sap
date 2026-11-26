# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""GET /packs/{pack_id}/versions and /versions/{version}: a pack's stored versions,
read from what the composer wrote in a development stack after its pack import at deploy."""

from __future__ import annotations

import base64
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import boto3
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_business_operations_packs as tp
from test_business_operations_packs import FakeS3

CLERK = "business-operations-ap-clerk"


@pytest.fixture
def stack(monkeypatch, tmp_path):
    """A development stack: seeded, then a rule edit, then a limit lowered in the pack,
    then a SOP edit, each a version."""
    root = tmp_path / "packs"
    shutil.copytree(tp.UC / "packs", root, ignore=shutil.ignore_patterns("_seed"))
    for name in ("SOPS_KB_ID_PARAM", "SOPS_DATA_SOURCE_ID_PARAM"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delitem(sys.modules, "sop_store", raising=False)
    monkeypatch.setenv("PROMOTION_ROLE", "development")
    s3 = FakeS3()
    composer = tp._composer(monkeypatch, s3)
    tp._seed(composer, s3, root)

    def edit(**change):
        result = composer.handler(
            {
                "pack_id": "finance_ops",
                "roles": ["pack_admin"],
                "actor": "ana",
                **change,
            },
            None,
        )
        assert result.get("result") == "active", result
        return result["version"]

    edit(pack={"constants": {"QTY_VARIANCE_PCT": 7}})
    edit(pack={"constants": {"AUTO_WRITE_LIMIT_USD": 50000}})
    edit(pack={"description": "Accounts payable exceptions"})
    for k, v in {
        "API_ALLOWED_CLIENTS": tp.CONSOLE,
        "USERS_GROUP": tp.USERS,
        "SOP_AUTHOR_GROUPS": tp.AUTHORS,
        "PACK_COMPOSER_FUNCTION": "composer",
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(boto3, "client", lambda *a, **k: s3)
    spec = importlib.util.spec_from_file_location(
        "bo_packs_versions", tp.UC / "api" / "packs" / "index.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def get(resource, version=None, groups=(tp.USERS, CLERK), **query):
        event = tp._event("GET", resource, groups=groups, query=query or None)
        event["pathParameters"] = {
            "pack_id": "finance_ops",
            **({"version": version} if version is not None else {}),
        }
        response = mod.handler(event, None)
        return response["statusCode"], json.loads(response["body"])

    return get, s3


def _walk(get, limit):
    versions, token = [], None
    while True:
        status, body = get(
            "/packs/{pack_id}/versions",
            limit=str(limit),
            **({"next_token": token} if token else {}),
        )
        assert status == 200, body
        versions += body["versions"]
        token = body["next_token"]
        if not token:
            return versions, body


def test_the_versions_list_newest_first_with_who_when_and_parts(stack):
    get, _ = stack
    versions, body = _walk(get, 2)
    numbers = [v["version"] for v in versions]
    assert numbers == sorted(numbers, reverse=True) and len(set(numbers)) == len(
        numbers
    )
    assert body["active"] == numbers[0]
    newest, limit, rule = versions[0], versions[1], versions[2]
    assert newest["made_by"] == "ana" and newest["via"] == "edit" and newest["made_at"]
    assert newest["parts"] == ["procedures"]
    # A changed `constants` reads by symbol: a limit is Controls, a rule Procedures.
    assert limit["parts"] == ["controls"]
    assert rule["parts"] == ["procedures"]
    assert newest["origin"]["via"] == "edit" and newest["origin"]["by"] == "ana"
    assert versions[-1]["origin"] is not None  # the seed brought in the first


def test_one_version_is_its_entry_and_its_content(stack):
    get, _ = stack
    _, body = get("/packs/{pack_id}/versions", limit="1")
    newest = body["versions"][0]["version"]
    status, one = get("/packs/{pack_id}/versions/{version}", version=str(newest - 1))
    assert status == 200, one
    assert one["version"] == newest - 1 and one["parts"] == ["controls"]
    assert one["pack"]["constants"]["AUTO_WRITE_LIMIT_USD"] == 50000
    assert one["sops"] and one["previous"]["version"] == newest - 2
    assert get("/packs/{pack_id}/versions/{version}", version="999")[0] == 404
    assert get("/packs/{pack_id}/versions/{version}", version="-1")[0] == 400
    assert get("/packs/{pack_id}/versions/{version}", version="1e3")[0] == 400


@pytest.mark.parametrize(
    "query,field",
    [
        ({"limit": "0"}, "limit"),
        ({"limit": "51"}, "limit"),
        ({"next_token": "%%"}, "next_token"),
        (
            {
                "next_token": base64.urlsafe_b64encode(
                    json.dumps({"before": -1}).encode()
                ).decode()
            },
            "next_token",
        ),
        (
            {
                "next_token": base64.urlsafe_b64encode(
                    json.dumps({"before": 3, "pack": "hr"}).encode()
                ).decode()
            },
            "next_token",
        ),
        ({"page": "2"}, "page"),
    ],
)
def test_the_listing_checks_its_parameters(stack, query, field):
    get, _ = stack
    status, body = get("/packs/{pack_id}/versions", **query)
    assert (status, body.get("field")) == (400, field)


def test_a_pack_the_caller_may_not_see_is_403_and_one_not_published_404(
    stack, monkeypatch
):
    get, s3 = stack
    import registry

    published = json.loads(s3.objects[("reg", "registry.json")])
    published["packs"]["finance_ops"]["pack"]["entitlement"] = {
        "groups": ["business-operations-ap-manager"]
    }
    s3.objects[("reg", "registry.json")] = json.dumps(published).encode()
    registry.reset()
    assert get("/packs/{pack_id}/versions")[0] == 403
    assert get("/packs/{pack_id}/versions/{version}", version="1")[0] == 403
    del published["packs"]["finance_ops"]
    s3.objects[("reg", "registry.json")] = json.dumps(published).encode()
    registry.reset()
    assert get("/packs/{pack_id}/versions")[0] == 404


def test_the_routes_write_nothing(stack):
    get, s3 = stack
    before = len(s3.puts)
    _walk(get, 1)
    get("/packs/{pack_id}/versions/{version}", version="1")
    assert len(s3.puts) == before


def test_the_routes_are_mounted_for_get_only():
    text = (tp.UC / "business_operations" / "packs.py").read_text()
    assert 'versions_r.add_method("GET"' in text
    assert 'add_resource("{version}").add_method("GET"' in text
