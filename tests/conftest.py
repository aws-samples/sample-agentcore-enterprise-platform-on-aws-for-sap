# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
import sys

# No .pyc beside the sources: Lambda and image assets fingerprint their whole
# directory, so bytecode from a test run changes asset hashes and redeploys them.
sys.dont_write_bytecode = True
__import__("os").environ["PYTHONDONTWRITEBYTECODE"] = "1"  # and in subprocesses
from pathlib import Path

# Make repo root importable so tests can `import infra_utils...`
_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

# Each layer's lib/, which its use cases import as top-level modules. Appended,
# so they never shadow anything else.
for _lib in sorted(_REPO.glob("layers/*/lib")):
    if _lib.is_dir() and str(_lib) not in sys.path:
        sys.path.append(str(_lib))

import os

# The shipped presets carry placeholders (sentinel account ids, REPLACE_ME client
# ids) on purpose, and a real deploy refuses them (tests/test_placeholders.py).
# Every other test that loads a preset describes it as it ships, so the suite
# runs the way scripts/check-contract.sh does. test_placeholders.py deletes this
# for its own strict cases.
os.environ.setdefault("PLATFORM_ALLOW_PLACEHOLDERS", "1")

# The CDK CLI tells an app which stacks to bundle; `python app.py` alone means
# all of them, which would build a use case's console (npm) on every
# in-process synth. Tests assert on templates, never on bundled assets.
os.environ.setdefault("CDK_CONTEXT_JSON", '{"aws:cdk:bundling-stacks": []}')

# Synthesizing a VPC pins its AZs from the account's AZ ids, an AWS call. The suite
# synthesizes offline, as scripts/check-contract.sh does; the tests of the pinning
# itself pass their own environ.
os.environ.setdefault("AGENTCORE_SKIP_AZ_PINNING", "1")
