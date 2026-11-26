# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""{{name}}: its stack synthesizes from platform.example.yaml on the SAP layer, and
verify.py can fail.

Add this use case's own tests beside this one; pytest.ini collects use-cases/*/tests.
"""

from __future__ import annotations

import os
import subprocess  # nosec B404 — running our own scripts, no shell
import sys
from pathlib import Path

import pytest
import yaml

UC = Path(__file__).resolve().parents[1]
if UC.name.startswith("_"):
    pytest.skip("the template itself, not a use case", allow_module_level=True)

from app_synth import synth_app  # tests/, on the path through conftest.py

# What the operator sets once for the SAP layer: one sandbox system, the layer's check, Cedar.
LAYER = {
    "gateway": {
        "interceptor_extensions": [
            {
                "path": "layers/sap/sap_check",
                "target_prefixes": ["sap-"],
                "request_headers": True,
            }
        ]
    },
    "security": {"cedar": {"enabled": True}},
    "layers": {
        "sap": {
            "systems": {
                "s4": {
                    "system_role": "sandbox",
                    "base_url": "https://sap.example.com/sap/opu/odata/sap/",
                    "client": "100",
                    "secret_name": "sap/s4",
                }
            }
        }
    },
}


def test_the_stack_synthesizes_from_the_example_config():
    example = yaml.safe_load((UC / "platform.example.yaml").read_text())
    config = {**LAYER, "use_cases": example["use_cases"]}
    config["layers"] = {"sap": {**LAYER["layers"]["sap"], **example["layers"]["sap"]}}
    result, out = synth_app(yaml.safe_dump(config))
    assert result.returncode == 0, result.stderr[-2000:]
    for suffix in yaml.safe_load((UC / "manifest.yaml").read_text())["stacks"]:
        assert (out / f"agentcore-workshop-dev-{suffix}.template.json").is_file()


def test_verify_fails_without_a_deployment():
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", "/tmp"),  # nosec B108 — only a fallback HOME
        "AWS_CONFIG_FILE": os.devnull,
        "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_REGION": "us-east-1",
    }
    result = subprocess.run(  # nosec B603 — fixed argv
        [sys.executable, str(UC / "verify.py")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 1, result.stdout
    assert result.stderr.startswith("FAIL:"), result.stderr
    assert "Traceback" not in result.stderr
