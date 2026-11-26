# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""hello-sap's tests, collected with the platform suite (pytest.ini testpaths)."""

import runpy
import sys
from pathlib import Path

_TESTS = Path(__file__).resolve().parents[3] / "tests"

# Run on their own, these get the platform suite's setup (tests/conftest.py) and
# its helpers (app_synth); in the full suite both are already there.
if str(_TESTS) not in sys.path:
    runpy.run_path(str(_TESTS / "conftest.py"))
    sys.path.append(str(_TESTS))
