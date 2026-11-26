# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""business-operations code shared by stack.py, ops.py and the tests."""

import sys
from pathlib import Path

# ops.py and evals/ run standalone, without the repo root on sys.path; names.py
# and policies.py build on the platform's infra_utils, and the SAP modules on the
# SAP layer's lib/ (app.py puts it on the path at synth; this use case requires it).
_REPO = Path(__file__).resolve().parents[3]
for _path in (_REPO, _REPO / "layers" / "sap" / "lib"):
    if str(_path) not in sys.path:
        sys.path.append(str(_path))

from sap_kit.functions import ASSET_EXCLUDE  # noqa: F401 - re-exported
