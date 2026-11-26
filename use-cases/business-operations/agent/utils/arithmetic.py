# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Arithmetic for the agent: the `calculator` tool the SOPs name for every variance.

strands-agents-tools 0.9.0 removed its calculator and points at a shell tool instead,
which an agent that posts to SAP must not hold. This evaluates one expression against
an AST allowlist, in Decimal so amounts do not drift (0.1 + 0.2 is 0.3). Stdlib only.
"""

from __future__ import annotations

import ast
import operator
from decimal import Decimal

_BIN = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS = {"abs": abs, "min": min, "max": max, "round": round}


def _eval(node: ast.AST) -> Decimal:
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return Decimal(str(node.value))
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
        return _BIN[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_eval(node.operand))
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _FUNCS
        and not node.keywords
    ):
        args = [_eval(a) for a in node.args]
        if node.func.id == "round" and len(args) == 2:
            return round(args[0], int(args[1]))
        return Decimal(_FUNCS[node.func.id](*args))
    raise ValueError(f"unsupported: {ast.dump(node)[:60]}")


def calculator(expression: str) -> dict:
    """Evaluate one arithmetic expression exactly, for amounts, variances and percentages.

    Supports numbers, + - * / %, parentheses, and abs(), min(), max(), round(x, n).
    Example: "(1250.00 - 1200.00) / 1200.00 * 100" for a price variance in percent.
    Dates are not numbers here: use days_between for those.
    """
    if len(expression) > 500:
        return {
            "expression": expression[:80],
            "error": "expression longer than 500 characters",
        }
    try:
        result = _eval(ast.parse(expression, mode="eval").body)
    except (SyntaxError, ValueError, TypeError, ArithmeticError, RecursionError) as exc:
        return {"expression": expression, "error": str(exc) or type(exc).__name__}
    return {"expression": expression, "result": str(result)}
