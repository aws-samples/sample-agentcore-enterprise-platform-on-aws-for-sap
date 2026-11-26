# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""A use case's stacks depend on the core stacks its manifest `requires`.

It reads the platform through SSM dynamic references, which give CDK no edge,
so without these deploy --all and destroy --all would order by definition
alone. Synthesized: the dependency must be in the cloud assembly.
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 — running our own app, no shell
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PREFIX = "agentcore-workshop-dev"


def test_use_case_stack_depends_on_the_core_stacks_it_requires(tmp_path):
    env = {
        **os.environ,
        "PLATFORM_CONFIG": str(REPO / "tests/fixtures/contract/usecase.yaml"),
        "CDK_DEFAULT_ACCOUNT": "111111111111",
        "CDK_DEFAULT_REGION": "us-east-1",
        "CDK_OUTDIR": str(tmp_path),
        "JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION": "1",
    }
    subprocess.run(  # nosec B603 — fixed argv
        [sys.executable, "app.py"], cwd=REPO, env=env, check=True, capture_output=True
    )
    artifacts = json.loads((tmp_path / "manifest.json").read_text())["artifacts"]
    deps = artifacts[f"{PREFIX}-uc-hello-platform"]["dependencies"]
    # hello-platform requires [gateway]; memory is in this footprint but not
    # required, so it is not an edge.
    assert f"{PREFIX}-gateway" in deps
    assert f"{PREFIX}-memory" not in deps
