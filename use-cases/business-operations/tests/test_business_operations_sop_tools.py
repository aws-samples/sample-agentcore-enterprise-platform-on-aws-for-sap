# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""business-operations `load_sop`: the handler, the evidence it leaves, and its target.

The handler reads the published registry and the SOP bucket through stand-ins; the
target and its Cedar statement are synthesized from the main stack.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import aws_cdk as cdk
import boto3
import pytest
from aws_cdk.assertions import Template
from botocore.exceptions import ClientError

from infra_utils.platform_config import load_platform_config
from infra_utils.use_case_context import UseCaseContext

REPO = Path(__file__).resolve().parents[3]
UC = REPO / "use-cases" / "business-operations"
sys.path.insert(0, str(UC))

from business_operations import config as uc_config
from business_operations import names

REGISTRY = {
    "schema": "pack-registry/1",
    "packs": {
        "finance_ops": {
            "pack": {
                "process_type_to_sop": {"short_pay": "finance_ops/short_pay.txt"},
                "constants": {"TOL_PCT": 5},
            }
        },
        "draft": {
            "pack": {"process_type_to_sop": {"x": "draft/x.txt"}},
            "source": {"entity": "<AWAITING_SURVEY: q>"},
        },
    },
}


class S3:
    class exceptions:
        NoSuchKey = type("NoSuchKey", (ClientError,), {})

    def __init__(self, objects):
        self.objects = objects

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise self.exceptions.NoSuchKey({"Error": {"Code": "NoSuchKey"}}, "Get")
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)]), "VersionId": "v7"}


def _ctx(tool="load_sop"):
    return SimpleNamespace(
        client_context=SimpleNamespace(
            custom={"bedrockAgentCoreToolName": f"{names.SOPS_TARGET}___{tool}"}
        )
    )


@pytest.fixture
def tool(monkeypatch):
    objects = {
        ("reg", "registry.json"): json.dumps(REGISTRY).encode(),
        # The pack maps .txt; the corpus holds .md, which the agent also accepts.
        ("sops", "finance_ops/short_pay.md"): b"Short pay within {{TOL_PCT}}%.",
    }
    s3 = S3(objects)
    monkeypatch.setenv("SOPS_BUCKET", "sops")
    monkeypatch.setenv("REGISTRY_BUCKET", "reg")
    monkeypatch.delenv("CONFIG_TABLE", raising=False)
    monkeypatch.setattr(boto3, "client", lambda *a, **k: s3)
    for name in ("registry", "config_overrides"):
        sys.modules.pop(name, None)
    sys.path.insert(0, str(UC / "sop_tools"))
    try:
        spec = importlib.util.spec_from_file_location(
            "bo_sop_tools", UC / "sop_tools" / "handler.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(UC / "sop_tools"))
    monkeypatch.setattr(mod.registry, "_get_s3", lambda: s3)
    yield mod, objects
    for name in ("registry", "config_overrides"):
        sys.modules.pop(name, None)


def test_load_sop_returns_the_whole_sop_resolved_and_headed_by_its_version(tool):
    mod, _ = tool
    text = mod.handler({"process_type": "short_pay"}, _ctx())["content"][0]["text"]
    assert text == "[sop finance_ops/short_pay.md version v7]\nShort pay within 5%."


def test_load_sop_reads_the_registry_on_every_call(tool):
    mod, objects = tool
    assert "error" in mod.handler({"process_type": "new_type"}, _ctx())
    published = json.loads(objects[("reg", "registry.json")])
    published["packs"]["finance_ops"]["pack"]["process_type_to_sop"]["new_type"] = (
        "finance_ops/short_pay.txt"
    )
    objects[("reg", "registry.json")] = json.dumps(published).encode()
    assert "content" in mod.handler({"process_type": "new_type"}, _ctx())


@pytest.mark.parametrize(
    "event, ctx, error",
    [
        ({"process_type": "x"}, _ctx(), "Unknown process_type 'x'"),  # Incomplete pack
        ({}, _ctx(), "process_type is required"),
        ({"process_type": "short_pay"}, _ctx("put_sop"), "Unknown tool"),
        ({"process_type": "short_pay"}, None, "AgentCore Gateway"),
    ],
)
def test_load_sop_refuses(tool, event, ctx, error):
    mod, _ = tool
    assert error in mod.handler(event, ctx)["error"]


def test_a_mapped_sop_missing_from_the_bucket_is_an_error_not_an_empty_answer(tool):
    mod, objects = tool
    del objects[("sops", "finance_ops/short_pay.md")]
    assert "not found" in mod.handler({"process_type": "short_pay"}, _ctx())["error"]


def test_the_evidence_records_the_loaded_sops_key_and_version(tool):
    mod, _ = tool
    # The agent's helpers are the package `utils`, as is a module in scripts/: hold
    # the binding only for the import.
    ours = [k for k in sys.modules if k == "utils" or k.startswith("utils.")]
    held = {k: sys.modules.pop(k) for k in ours}
    sys.path.insert(0, str(UC / "agent"))
    try:
        evidence = importlib.import_module("utils.evidence")
    finally:
        sys.path.remove(str(UC / "agent"))
        for k in [k for k in sys.modules if k == "utils" or k.startswith("utils.")]:
            del sys.modules[k]
        sys.modules.update(held)
    reply = mod.handler({"process_type": "short_pay"}, _ctx())
    for result in (
        {"status": "success", "content": reply["content"]},
        # As the Gateway may hand it over: the Lambda's reply, JSON-encoded.
        {"status": "success", "content": [{"text": json.dumps(reply)}]},
    ):
        ev = evidence.extract_evidence(
            f"{names.SOPS_TARGET}___load_sop",
            {"process_type": "short_pay"},
            result,
            at="t",
        )
        assert ev["source"] == {"key": "finance_ops/short_pay.md"}
        assert ev["fields"] == [{"name": "version_id", "value": "v7"}]


def test_the_target_is_in_the_main_stack_with_a_read_scoped_permit():
    spec = importlib.util.spec_from_file_location("bo_stack_sops", UC / "stack.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    platform = load_platform_config(
        REPO
        / "use-cases"
        / "business-operations"
        / "fixtures"
        / "contract"
        / "business-operations.yaml"
    )
    ctx = UseCaseContext(
        project="p",
        environment="dev",
        platform=platform,
    )
    stack = mod.BusinessOperationsStack(
        cdk.App(),
        "p-dev-uc-business-operations",
        ctx=ctx,
        config=uc_config.load({}),
        env=cdk.Environment(account="111111111111", region="us-east-1"),
    )
    resources = Template.from_stack(stack).to_json()["Resources"]
    targets = [
        r["Properties"]
        for r in resources.values()
        if r["Type"] == "AWS::BedrockAgentCore::GatewayTarget"
        and r["Properties"]["Name"] == names.SOPS_TARGET
    ]
    assert len(targets) == 1
    tools = targets[0]["TargetConfiguration"]["Mcp"]["Lambda"]["ToolSchema"]
    assert [t["Name"] for t in tools["InlinePayload"]] == ["load_sop"]
    statements = [
        r["Properties"]["Definition"]
        for r in resources.values()
        if r["Type"] == "AWS::BedrockAgentCore::Policy"
    ]
    action = names.tool_name(names.SOPS_TARGET, "load_sop")
    (permit,) = [s for s in statements if action in json.dumps(s)]
    assert "uc-business-operations/read" in json.dumps(permit)
    # The created packs and the shipped ones name the tool on the target that serves it.
    assert action in names.created_pack_tools()
    finance = json.loads((UC / "packs" / "finance_ops" / "pack.json").read_text())
    assert action in finance["gateway_tools"]
