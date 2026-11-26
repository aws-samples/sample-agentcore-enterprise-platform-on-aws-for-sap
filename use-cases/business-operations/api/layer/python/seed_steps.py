# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""A pack's declarative seed: the `seed.steps` of its `pack.json`, rendered and posted in order.

Stdlib only, and no SAP: the seeder (`test_data/index.py`) hands `run` the SAP layer's
`post`, and `validate_packs` checks every template at synth with `template_names`.

A **template** is any string in a step holding `{{expression}}`. A string that is one
expression and nothing else takes the value's type when it is a boolean, and when it is
`null` its key is left out of the payload; every other value renders as text, which is
what OData V2 JSON wants for a decimal. An expression is arithmetic (`+ - * /`),
comparison, `and`/`or`/`not`, `a if c else b`, and three functions: `round(x, n)`,
`sap_date(iso_date, days)` (`/Date(ms)/`, `days` later) and `year(iso_date)`. A name is
a declared input, a step that ran before it (`po.PurchaseOrder` is a field SAP returned
for step `po`), or `posting_date`.

`posting_date` is the input of that name, `YYYY-MM-DD`, or when it is blank whatever
`open_date` answers: the seeder reads the latest date SAP has open for the seed's
`company_code`, since no fixed rule (today, the end of last month) lands in an open
period on every system.

A step whose `when` is false is skipped. The first step SAP refuses ends the chain:
its error comes back as `<step>_error` beside what the earlier steps created, which is
real accounting documents nobody may lose track of.
"""

from __future__ import annotations

import ast
import math
import operator
import re
import time
from datetime import datetime, timedelta, timezone

TEMPLATE_RE = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
#: A step POSTs to `<service>/<entity>` under the OData root, and nowhere else on SAP.
SERVICE_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
ENTITY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
DATE_FORMAT = "%Y-%m-%d"
#: SAP's update task commits a document after its create returns; the next document
#: in the chain references it, so it waits this long first.
PAUSE_SECONDS = 3
BUILTINS = frozenset({"posting_date"})


class TemplateError(ValueError):
    """A template that is not allowed, or that names something the scope lacks."""


def _date(iso) -> datetime:
    try:
        return datetime.strptime(str(iso), DATE_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        raise TemplateError(f"{iso!r} is not a date (YYYY-MM-DD)") from None


def _number(x):
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        raise TemplateError(f"{x!r} is not a number")
    return x


def _sap_date(iso, days=0) -> str:
    return f"/Date({int((_date(iso) + timedelta(days=_number(days))).timestamp() * 1000)})/"


FUNCTIONS = {
    "round": lambda x, n=0: round(_number(x), int(_number(n))),
    "sap_date": _sap_date,
    "year": lambda iso: str(_date(iso).year),
}

_ARITHMETIC = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}
_COMPARE = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}
_ALLOWED = (
    ast.Expression, ast.Constant, ast.Name, ast.Load, ast.Attribute, ast.BinOp,
    ast.UnaryOp, ast.USub, ast.Not, ast.BoolOp, ast.And, ast.Or, ast.Compare,
    ast.IfExp, ast.Call, *_ARITHMETIC, *_COMPARE,
)  # fmt: skip


def _parse(expression: str) -> ast.Expression:
    """The expression's tree, or a TemplateError naming what is not allowed."""
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError:
        raise TemplateError(f"{{{{{expression}}}}} is not an expression") from None
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED):
            raise TemplateError(
                f"{{{{{expression}}}}}: {type(node).__name__} is not allowed"
            )
        if isinstance(node, ast.Call) and (
            node.keywords
            or not isinstance(node.func, ast.Name)
            or node.func.id not in FUNCTIONS
        ):
            raise TemplateError(
                f"{{{{{expression}}}}}: only {sorted(FUNCTIONS)} may be called"
            )
    return tree


def _eval(node, scope: dict):
    if isinstance(node, ast.Expression):
        return _eval(node.body, scope)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in scope:
            raise TemplateError(f"{node.id} is not an input, a step or a function")
        return scope[node.id]
    if isinstance(node, ast.Attribute):
        owner = _eval(node.value, scope)
        if not isinstance(owner, dict) or node.attr not in owner:
            raise TemplateError(f"{ast.unparse(node)}: SAP returned no {node.attr}")
        return owner[node.attr]
    if isinstance(node, ast.BinOp):
        left, right = _eval(node.left, scope), _eval(node.right, scope)
        if not (
            isinstance(node.op, ast.Add)
            and isinstance(left, str)
            and isinstance(right, str)
        ):
            left, right = _number(left), _number(right)
        try:
            return _ARITHMETIC[type(node.op)](left, right)
        except ZeroDivisionError:
            raise TemplateError(f"{ast.unparse(node)} divides by zero") from None
    if isinstance(node, ast.UnaryOp):
        value = _eval(node.operand, scope)
        return not value if isinstance(node.op, ast.Not) else -_number(value)
    if isinstance(node, ast.BoolOp):
        value = None
        for operand in node.values:
            value = _eval(operand, scope)
            if bool(value) is isinstance(node.op, ast.Or):
                break
        return value
    if isinstance(node, ast.Compare):
        left = _eval(node.left, scope)
        for op, right_node in zip(node.ops, node.comparators):
            right = _eval(right_node, scope)
            try:
                if not _COMPARE[type(op)](left, right):
                    return False
            except TypeError:
                raise TemplateError(
                    f"{ast.unparse(node)} compares unlike values"
                ) from None
            left = right
        return True
    if isinstance(node, ast.IfExp):
        return _eval(node.body if _eval(node.test, scope) else node.orelse, scope)
    # A Call: _parse admitted only FUNCTIONS, by name.
    try:
        return FUNCTIONS[node.func.id](*(_eval(a, scope) for a in node.args))
    except TypeError as e:
        raise TemplateError(f"{ast.unparse(node)}: {e}") from None


def evaluate(expression: str, scope: dict):
    return _eval(_parse(expression), {**FUNCTIONS, **scope})


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def render(value, scope: dict):
    """`value` with every template in it evaluated against `scope`."""
    if isinstance(value, dict):
        rendered = {k: render(v, scope) for k, v in value.items()}
        return {k: v for k, v in rendered.items() if v is not None}
    if isinstance(value, list):
        return [render(v, scope) for v in value]
    if not isinstance(value, str):
        return value
    whole = TEMPLATE_RE.fullmatch(value)
    if whole:
        result = evaluate(whole[1], scope)
        return result if result is None or isinstance(result, bool) else _text(result)
    return TEMPLATE_RE.sub(lambda m: _text(evaluate(m[1], scope)), value)


def template_names(value) -> set[str]:
    """Every name the templates in `value` read. Raises TemplateError on a template that
    is not allowed, so a pack's seed fails at synth rather than in SAP."""
    if isinstance(value, dict):
        return set().union(*(template_names(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(template_names(v) for v in value))
    if not isinstance(value, str):
        return set()
    names = set()
    for expression in TEMPLATE_RE.findall(value):
        tree = _parse(expression)
        names |= {
            n.id
            for n in ast.walk(tree)
            if isinstance(n, ast.Name) and n.id not in FUNCTIONS
        }
    return names


def inputs(seed: dict, given: dict, open_date=None) -> dict:
    """The declared defaults overlaid with `given`, each checked against its declaration.

    Raises ValueError (the caller's mistake) on an undeclared name or a value its
    input cannot take. The declaration is the contract: the steps read nothing else.
    A blank `posting_date` is `open_date()`, called only then; with none, or when it
    raises, the error names `posting_date` so the caller knows what to set.
    """
    declared = {i["id"]: i for i in seed.get("inputs") or []}
    if unknown := sorted(set(given) - set(declared)):
        raise ValueError(f"{', '.join(unknown)} is not an input of this seed")
    values = {k: i["default"] for k, i in declared.items() if "default" in i}
    values |= given
    for name, spec in declared.items():
        value, kind = values.get(name), spec["type"]
        if value is None:
            raise ValueError(f"{name} is required")
        if kind == "number":
            try:
                _number(value)
            except TemplateError:
                raise ValueError(f"{name} must be a number") from None
            if spec.get("min") is not None and value < spec["min"]:
                raise ValueError(f"{name} must be at least {spec['min']}")
        elif kind == "boolean" and not isinstance(value, bool):
            raise ValueError(f"{name} must be true or false")
        elif kind == "select" and value not in [o["value"] for o in spec["options"]]:
            raise ValueError(f"{name} must be one of the declared options")
        elif kind == "string" and not isinstance(value, str):
            raise ValueError(f"{name} must be a string")
    posting = values.get("posting_date")
    if not posting:
        try:
            posting = open_date() if open_date else None
        except Exception as e:  # noqa: BLE001 - SAP's reason goes back to the caller
            raise ValueError(
                f"posting_date is blank and SAP's open period could not be read: {e}. "
                "Set posting_date (YYYY-MM-DD) to a date SAP has open"
            ) from None
        if not posting:
            raise ValueError("posting_date is required (YYYY-MM-DD)")
    try:
        _date(posting)
    except TemplateError:
        raise ValueError("posting_date must be YYYY-MM-DD") from None
    return {**values, "posting_date": posting}


def run(seed: dict, values: dict, post, pause: float | None = None) -> dict:
    """Post the steps in order. `post(service, entity, payload) -> SAP's created object`.

    Returns what landed: each step's `returns`, the `case` the poller will open for it
    (`seed.case`, rendered), and `<step>_error` for the step that stopped the chain.
    """
    scope = dict(values)
    result: dict = {"inputs": dict(values)}
    posted = False
    for step in seed["steps"]:
        name = step["id"]
        try:
            if "when" in step and not evaluate(step["when"], scope):
                scope[name] = None
                continue
            if not (
                SERVICE_RE.fullmatch(str(step["service"]))
                and ENTITY_RE.fullmatch(str(step["entity"]))
            ):
                raise TemplateError("a step's service and entity must be OData names")
            payload = render(step["payload"], scope)
            if posted:
                time.sleep(PAUSE_SECONDS if pause is None else pause)
            created = post(step["service"], step["entity"], payload) or {}
            posted = True
        except Exception as e:  # noqa: BLE001 - reported beside what landed, not raised
            result[f"{name}_error"] = str(e)
            return result
        scope[name] = created
        for key, field in (step.get("returns") or {}).items():
            result[key] = created.get(field)
    if seed.get("case"):
        try:
            result["case"] = render(seed["case"], scope)
        except TemplateError as e:
            result["case_error"] = str(e)
    return result
