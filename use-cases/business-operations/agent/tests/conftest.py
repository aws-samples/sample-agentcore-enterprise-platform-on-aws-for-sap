# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The agent's own tests: they import strands, ag_ui and bedrock_agentcore, so they
run against agent/requirements.txt in their own CI job, never in the root suite.

Puts the agent directory on sys.path so a test imports `utils.x` the way the
container does.
"""

import os
import sys

# No .pyc beside the sources: Lambda and image assets fingerprint their whole
# directory, so bytecode from a test run changes asset hashes and redeploys them.
sys.dont_write_bytecode = True
__import__("os").environ["PYTHONDONTWRITEBYTECODE"] = "1"  # and in subprocesses
from pathlib import Path

AGENT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT))

# boto3 clients built at import time need a region; a bare checkout has none.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
