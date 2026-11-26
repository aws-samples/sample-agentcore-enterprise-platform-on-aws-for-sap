# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""business-operations' tests, collected with the platform suite (pytest.ini testpaths)."""

import runpy
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_TESTS = _REPO / "tests"

# Run on their own, these get the platform suite's setup (tests/conftest.py) and
# its helpers (app_synth, test_layers); in the full suite both are already there.
if str(_TESTS) not in sys.path:
    runpy.run_path(str(_TESTS / "conftest.py"))
    sys.path.append(str(_TESTS))

# The API's Lambda layer, which its handlers import as top-level modules (a
# deployed layer's python/ is on the Lambda's path). Appended, so it never
# shadows anything else.
_LAYER = str(_REPO / "use-cases" / "business-operations" / "api" / "layer" / "python")
if _LAYER not in sys.path:
    sys.path.append(_LAYER)
