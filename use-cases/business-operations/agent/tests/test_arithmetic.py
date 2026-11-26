# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
from utils.arithmetic import calculator


def test_calculator_is_exact_and_refuses_anything_but_arithmetic():
    assert calculator("0.1 + 0.2")["result"] == "0.3"
    assert calculator("(1250.00 - 1200.00) / 1200.00 * 100")["result"].startswith(
        "4.1666"
    )
    assert calculator("round(1250 * 0.035, 2)")["result"] == "43.75"
    assert calculator("abs(-5) + max(1, 2) - min(3, 4) + 7 % 3")["result"] == "5"
    for bad in (
        "1 / 0",
        "__import__('os')",
        "2 ** 999999",
        "x + 1",
        "'a' * 3",
        "True + 1",
    ):
        assert "error" in calculator(bad), bad
