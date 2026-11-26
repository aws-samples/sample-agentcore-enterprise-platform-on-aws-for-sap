# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""A pack's example cases (`evals.json`) and the last evaluation run published for it.

The example cases are the Evaluations part of a pack: each seeds SAP, runs the agent and
asserts what it did (`evals/run_regression.py`). They live in the pack's bundle beside
its SOPs, so a version carries the cases it was graded against, and `pack_composer`,
the one writer of packs, is the only thing that changes them. `check` is the rule a case
must pass, applied by `PUT /packs/{pack_id}/evaluations/{test_id}` for a readable
refusal and again by the composer, which is the gate.

A run's results are published by `evals-run --publish` through the composer's
`record_evaluation` verb, under `_evaluations/<pack_id>/` in the registry bucket, which
no corpus rebuild reads.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

import oracle
import seed_steps

#: An example case's id, also its path segment.
TEST_ID = re.compile(r"^[a-z][a-z0-9_]{2,79}$")
#: The keys an example case may carry; `payload` is a webhook's body, for the
#: security cases.
KEYS = frozenset(
    {"test_id", "description", "process_type", "tags", "seed", "payload", "expected"}
)
#: What `expected` may assert (`run_regression.check_expectations`).
EXPECTED_KEYS = frozenset(
    {
        "outcome",
        "reason",
        "required_tool_calls",
        "forbidden_tool_calls",
        "sap_state",
        "gateway",
        "guardrail",
        "guardrail_stop_ok",
    }
)
OUTCOMES = frozenset(oracle.OUTCOME_TO_STATUS)
MAX_CASES = 200
_TEXT_MAX = 2000
_CASE_BYTES = 16_000
#: Published results: one object per run and the newest, per pack.
RESULTS_PREFIX = "_evaluations"
RESULTS_BYTES = 400_000
#: The price of one case when no run has been published to price it from.
DEFAULT_CASE_USD = 0.20


def results_key(pack_id: str, run_id: str | None = None) -> str:
    return f"{RESULTS_PREFIX}/{pack_id}/" + (
        f"runs/{run_id}.json" if run_id else "latest.json"
    )


def group(case: dict) -> str:
    """`security` for a red-team case, `boundaries` for one at the edge of a rule, else
    `behaviour`: how the Evaluations tab groups the file."""
    if "red-team" in (case.get("tags") or []):
        return "security"
    if re.search(r"_(under|at|over)(_|$)", str(case.get("test_id") or "")):
        return "boundaries"
    return "behaviour"


def _strings(value, cap: int) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= cap
        and all(isinstance(v, str) and 0 < len(v) <= 200 for v in value)
    )


def check(test_id: str, case: object, document: dict) -> list[str]:
    """Why `case` is not an example case of the pack `document` describes, or [].

    `test_id` is the path's; a `test_id` in the body must agree with it.
    """
    if not TEST_ID.fullmatch(str(test_id)):
        return ["test_id must be 3 to 80 lowercase letters, digits and underscores"]
    if not isinstance(case, dict):
        return ["the example case must be an object"]
    errors = []
    if unknown := sorted(set(case) - KEYS):
        errors.append(f"{', '.join(unknown)} is not a key of an example case")
    if case.get("test_id", test_id) != test_id:
        errors.append("test_id in the body must be the path's")
    description = case.get("description")
    if not isinstance(description, str) or not description.strip():
        errors.append("description is required")
    elif len(description) > _TEXT_MAX:
        errors.append(f"description is longer than {_TEXT_MAX} characters")
    process_types = document.get("process_type_to_sop") or {}
    if case.get("process_type") not in process_types:
        errors.append(
            "process_type must be one this pack maps: "
            + ", ".join(sorted(process_types))
        )
    if "tags" in case and not _strings(case["tags"], 10):
        errors.append("tags must be a list of at most 10 short strings")
    if "payload" in case and not isinstance(case["payload"], dict):
        errors.append("payload must be an object")
    expected = case.get("expected")
    if not isinstance(expected, dict):
        errors.append("expected is required")
    else:
        if unknown := sorted(set(expected) - EXPECTED_KEYS):
            errors.append(f"expected.{', expected.'.join(unknown)} is not checked")
        if expected.get("outcome") not in OUTCOMES:
            errors.append(
                "expected.outcome must be one of " + ", ".join(sorted(OUTCOMES))
            )
        reason = expected.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            errors.append("expected.reason is required: it is the judges' ground truth")
        for key in ("required_tool_calls", "forbidden_tool_calls"):
            if key in expected and not _strings(expected[key], 30):
                errors.append(f"expected.{key} must be a list of tool names")
    seed = document.get("seed") or {}
    given = (case.get("seed") or {}).get("inputs") if "seed" in case else None
    attributes = (
        (case.get("seed") or {}).get("case_attributes") if "seed" in case else None
    )
    if "seed" in case and (
        not isinstance(case["seed"], dict)
        or set(case["seed"]) - {"inputs", "case_attributes"}
    ):
        errors.append("seed must be {inputs: {...}}, with case_attributes beside it")
    elif attributes is not None and not (
        isinstance(attributes, dict)
        and all(
            isinstance(k, str) and isinstance(v, str) and len(v) <= _TEXT_MAX
            for k, v in attributes.items()
        )
    ):
        errors.append("seed.case_attributes must map attribute names to text")
    elif given is not None:
        if not seed.get("inputs"):
            errors.append("this pack declares no seed inputs")
        elif not isinstance(given, dict):
            errors.append("seed.inputs must be an object")
        else:
            try:
                # A blank posting date is the seeder's to fill at run time.
                seed_steps.inputs(seed, given, lambda: "2000-01-01")
            except ValueError as error:
                errors.append(f"seed.inputs: {error}")
    if len(json.dumps(case, default=str)) > _CASE_BYTES:
        errors.append(f"the example case is larger than {_CASE_BYTES} bytes")
    return errors


def apply(cases: list, op: str, test_id: str, case: dict | None) -> list | None:
    """The pack's cases with one put or deleted, or None for a delete of a case it does
    not have. A put replaces a case of the same id where it stands, or adds one."""
    kept = [c for c in cases if c.get("test_id") != test_id]
    if op == "delete":
        return kept if len(kept) < len(cases) else None
    stored = {
        "test_id": test_id,
        **{k: v for k, v in (case or {}).items() if k != "test_id"},
    }
    for index, existing in enumerate(cases):
        if existing.get("test_id") == test_id:
            return [*cases[:index], stored, *cases[index + 1 :]]
    return [*cases, stored]


def check_results(pack_id: str, record: object) -> list[str]:
    """Why `record` is not a run's results `record_evaluation` may publish, or []."""
    if not isinstance(record, dict):
        return ["the results must be an object"]
    errors = []
    run_id = record.get("run_id")
    if not isinstance(run_id, str) or not re.fullmatch(r"[0-9TZ:\-]{16,25}", run_id):
        errors.append("run_id must be the run's UTC start, e.g. 2026-11-13T10:02:11Z")
    if record.get("pack_id") != pack_id:
        errors.append("pack_id must be the pack the results are for")
    for key in ("total", "passed", "failed"):
        if not isinstance(record.get(key), int) or isinstance(record.get(key), bool):
            errors.append(f"{key} must be a whole number")
    if not isinstance(record.get("results"), dict):
        errors.append("results must be an object keyed by test_id")
    if len(json.dumps(record, default=str)) > RESULTS_BYTES:
        errors.append(f"the results are larger than {RESULTS_BYTES} bytes")
    return errors


def estimate(case_count: int, last_run: dict | None) -> float:
    """What a run of every case would cost, from the last run's cost per case."""
    ran = (last_run or {}).get("total") or 0
    cost = (last_run or {}).get("cost_usd")
    per_case = (
        cost / ran if ran and isinstance(cost, (int, float)) else DEFAULT_CASE_USD
    )
    return round(per_case * case_count, 2)


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
