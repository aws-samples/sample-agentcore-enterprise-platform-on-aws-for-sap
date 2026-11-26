# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""One `python app.py` synth per distinct configuration, shared by the session.

A full synth takes about 3 s, and most synth tests read the same fixture, so each
configuration is synthesized once and every later caller gets the same cloud
assembly. Callers read templates from it and never write into it.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import shutil
import subprocess  # nosec B404 — running our own app, no shell
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
_ROOT = Path(tempfile.mkdtemp(prefix="app-synth-"))
atexit.register(shutil.rmtree, _ROOT, ignore_errors=True)
_CACHE: dict[str, tuple[subprocess.CompletedProcess, Path]] = {}


def synth_app(
    config: Path | str, *, region: str = "us-east-1"
) -> tuple[subprocess.CompletedProcess, Path]:
    """Synthesize app.py with `config` (a file, or YAML text) and return the
    process and its cdk.out. A path that does not exist synthesizes with no
    platform.yaml. The result is cached by the parsed configuration and the
    region, so a failed synth is cached too."""
    if isinstance(config, Path):
        text = config.read_text() if config.exists() else ""
    else:
        text = config
    canonical = json.dumps([yaml.safe_load(text), region], sort_keys=True)
    key = hashlib.sha256(canonical.encode()).hexdigest()[:16]
    if key in _CACHE:
        return _CACHE[key]
    run_dir = _ROOT / key
    run_dir.mkdir()
    if not isinstance(config, Path):
        config = run_dir / "platform.yaml"
        config.write_text(text)
    out = run_dir / "cdk.out"
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"AWS_REGION", "AWS_DEFAULT_REGION", "PROJECT_NAME", "ENVIRONMENT"}
    }
    env |= {
        "PLATFORM_CONFIG": str(config),
        "CDK_DEFAULT_ACCOUNT": "111111111111",
        "CDK_DEFAULT_REGION": region,
        "CDK_OUTDIR": str(out),
        "JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION": "1",
    }
    result = subprocess.run(  # nosec B603 — fixed argv
        [sys.executable, "app.py"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    _CACHE[key] = (result, out)
    return _CACHE[key]
