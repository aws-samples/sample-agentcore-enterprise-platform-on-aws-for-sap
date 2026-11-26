# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""No code in this use case reads a JWT without verifying it.

Every bearer the agent or a Lambda believes is verified first (`utils/auth.py`
`verify_runtime_bearer`; API Gateway's Cognito authorizer for the routes). A new
unverified decode is a new place a forged token's claims could be believed.
"""

from pathlib import Path

USE_CASE = Path(__file__).resolve().parents[2]
_MARKERS = (
    "verify_signature",
    "get_unverified_claims",
    "get_unverified_header",
    "decode_unverified",
)
# Reads that verify nothing but believe no claim either: the API authorizer reads the
# header only to pin RS256 and pick the key by kid, and jwt.decode then verifies the
# signature before any claim is read.
_ALLOWED = {"api_authorizer/index.py": {"get_unverified_header"}}


def test_nothing_turns_off_signature_verification():
    offenders = []
    for path in USE_CASE.rglob("*.py"):
        if "tests" in path.parts or "node_modules" in path.parts:
            continue
        name = path.relative_to(USE_CASE).as_posix()
        text = path.read_text(encoding="utf-8")
        if any(m in text for m in set(_MARKERS) - _ALLOWED.get(name, set())):
            offenders.append(name)
    assert offenders == []
