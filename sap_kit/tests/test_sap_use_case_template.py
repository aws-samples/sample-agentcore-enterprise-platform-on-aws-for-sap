# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""`usecase new --template sap_kit/templates/sap-use-case` and its example, hello-sap.

use-cases/hello-sap/ is what the command generates, byte for byte, so the example
is the template's proof; its synth is read-only on SAP: a claim with no write, and
Cedar permits naming only the read tools.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import yaml
from test_layers import synth, template

REPO = Path(__file__).resolve().parents[2]
TEMPLATE = REPO / "sap_kit" / "templates" / "sap-use-case"
HELLO = REPO / "use-cases" / "hello-sap"

_spec = importlib.util.spec_from_file_location(
    "usecase", REPO / "scripts" / "usecase.py"
)
usecase = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(usecase)


def test_hello_sap_is_what_the_template_generates(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_CONFIG", str(tmp_path / "platform.yaml"))
    manifest = yaml.safe_load((HELLO / "manifest.yaml").read_text())
    args = ["--owner", manifest["owner"], "--summary", manifest["summary"]]
    root = tmp_path / "use-cases"
    argv = ["--use-cases-dir", str(root), "new", "hello-sap", *args]
    assert usecase.main([*argv, "--template", str(TEMPLATE)]) == 0
    made = _files(root / "hello-sap")
    assert made == _files(HELLO)
    for name in made:
        assert (root / "hello-sap" / name).read_text() == (HELLO / name).read_text()


def _files(folder: Path) -> list[str]:
    return sorted(
        p.relative_to(folder).as_posix()
        for p in folder.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    )


def test_a_directory_without_a_manifest_is_not_a_template(tmp_path, capsys):
    root = tmp_path / "use-cases"
    argv = ["--use-cases-dir", str(root), "new", "some-name", "--dry-run"]
    assert usecase.main([*argv, "--template", str(tmp_path)]) == 1
    assert "is not a use-case template" in capsys.readouterr().err


def test_hello_sap_claims_and_permits_reads_only(tmp_path):
    sap = {
        "systems": {
            "s4": {
                "system_role": "sandbox",
                "base_url": "https://sap.example.com/sap/opu/odata/sap/",
                "secret_name": "x/sap/s4",
                "writes": ["update"],
            }
        },
        "access": {"hello-sap": {"read": "*"}},
    }
    check = {
        "path": "layers/sap/sap_check",
        "target_prefixes": ["sap-"],
        "request_headers": True,
    }
    config = {
        "security": {"cedar": {"enabled": True}},
        "gateway": {"interceptor_extensions": [check]},
        "layers": {"sap": sap},
        "use_cases": {"hello-sap": {}},
    }
    result = synth(tmp_path, config)
    assert result.returncode == 0, result.stderr[-2000:]
    resources = template(tmp_path, "agentcore-workshop-dev-uc-hello-sap")["Resources"]
    (claim,) = [
        r["Properties"]["Value"]["Fn::Join"][1]
        for r in resources.values()
        if r["Type"] == "AWS::SSM::Parameter"
    ]
    assert "hellosapreaderclientid" in claim[1]["Ref"]
    assert claim[2] == '"],"read":["s4"],"write":{}}'
    (permit,) = [
        r["Properties"]
        for r in resources.values()
        if r["Type"] == "AWS::BedrockAgentCore::Policy"
    ]
    statement = json.dumps(permit["Definition"]["Cedar"]["Statement"])
    assert '\\"uc-hello-sap/read\\"' in statement and "odata_read" in statement
    assert "odata_update" not in statement
