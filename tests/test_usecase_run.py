# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""`deploy.sh usecase run <name> <verb>` hands the verb to
use-cases/<name>/ops.py with the platform settings and the use case's config
block in the environment, and refuses clearly when there is nothing to run."""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404 — running our own scripts, no shell
import sys
from pathlib import Path

from infra_utils.platform_config import USE_CASES_DIR

REPO = Path(__file__).resolve().parents[1]


def _run(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(  # nosec B603 — fixed argv
        [sys.executable, str(REPO / "scripts/usecase.py"), *args],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_run_refuses_an_unknown_use_case():
    result = _run("run", "no-such-thing", "publish")
    assert result.returncode == 1
    assert "no-such-thing" in result.stderr and "hello-platform" in result.stderr


def test_run_refuses_a_use_case_without_ops():
    assert not (USE_CASES_DIR / "hello-platform" / "ops.py").exists()
    result = _run("run", "hello-platform", "publish")
    assert result.returncode == 1
    assert "ops.py" in result.stderr


def test_run_hands_the_verb_and_config_to_ops(tmp_path):
    uc = tmp_path / "use-cases"
    shutil.copytree(USE_CASES_DIR / "hello-platform", uc / "hello-platform")
    (uc / "hello-platform" / "ops.py").write_text(
        "import json, os, sys\n"
        "print(json.dumps({'argv': sys.argv[1:],"
        " 'project': os.environ['PROJECT_NAME'],"
        " 'config': json.loads(os.environ['USE_CASE_CONFIG'])}))\n"
    )
    manifest = tmp_path / "platform.yaml"
    manifest.write_text(
        "project: ops-check\nuse_cases:\n  hello-platform:\n    greeting: hi\n"
    )
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"PROJECT_NAME", "ENVIRONMENT", "PLATFORM_CONFIG"}
    }
    env["AWS_REGION"] = "us-west-2"
    result = _run(
        "--manifest",
        str(manifest),
        "--use-cases-dir",
        str(uc),
        "run",
        "hello-platform",
        "publish",
        "--force",
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "argv": ["publish", "--force"],
        "project": "ops-check",
        "config": {"greeting": "hi"},
    }


def test_layer_run_looks_in_layers_and_refuses_an_unknown_one(tmp_path):
    result = _run("--use-cases-dir", str(tmp_path), "run", "--layer", "nope", "x")
    assert result.returncode == 1
    assert "no layer 'nope'" in result.stderr
