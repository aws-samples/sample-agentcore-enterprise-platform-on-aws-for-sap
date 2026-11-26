#!/usr/bin/env python3

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Capture one real run's trace as a committed grading fixture.

`check_expectations` in `run_regression.py` is a pure function over a persisted case,
but every input it has ever been given is either a live stack (which costs a model
call) or a hand-built dict in `tests/test_business_operations_evals.py` (which was
written to match the reader, so it cannot catch the reader and the writer drifting
apart). This script produces the third kind: a **real** trace, captured once, graded
forever for free by `tests/test_business_operations_evals.py`.

Be exact about what that buys, because it is narrower than it looks:

* It catches a **grader** regression — an edit to `check_expectations` that changes the
  verdict on a run whose verdict was recorded when the run happened.
* It grades the grader against the shape the runtime **actually wrote**. A hand-built
  trace agrees with the reader by construction, so it can only ever confirm the
  reader's own assumptions; this one agrees with it because a real run produced it.
  That is why the segment dicts below keep every key the runtime wrote rather than
  only the three the grader reads. It does **not** catch a *future* shape change — the
  fixture is frozen, so the runtime writing `segments[].tool` instead of
  `segments[].tool_name` tomorrow surfaces at the next capture and not before.
* It catches **nothing about the agent.** The recorded verdict is what this run did,
  not what a correct run would do. A model regression needs a model call
  (`evals-run`).

**Everything free-text is elided, and that is a hard requirement, not tidiness.** This
repo is public. A real `tool_result` from `search_sap_sops` carries the SOP bucket URI,
which carries the AWS account number — so an unscrubbed fixture publishes it. The
elision keeps the key and replaces the value with `<elided N chars>`, so the shape
survives and the content does not. `tests/test_business_operations_evals.py` re-checks the invariant on
every fixture at every run, because the guard that only lives in the capturing script
protects only the fixture someone remembered to capture with it.

Usage (PROJECT_NAME, ENVIRONMENT and AWS_REGION name the deployment):
    python evals/capture_fixture.py --case-id ap-7eqp...
    python evals/capture_fixture.py --case-id ... --outcome refused --forbid odata_create
"""

import argparse
import json
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from run_regression import Stack, check_expectations, get_case_record  # noqa: E402

FIXTURE_DIR = _HERE / "trace_fixtures"

# Free text, in the two places the runtime writes it. Elided rather than dropped so a
# reader of the fixture can still see that the segment had a result at all.
# `initiator` is not free text but is elided too: it is the calling app-client id.
_ELIDE_SEGMENT = ("tool_input", "tool_result", "evidence", "content")
_ELIDE_TRACE = ("prompt", "initiator")

# What must never reach a committed fixture. Same two patterns the test re-asserts.
_FORBIDDEN = {
    "an AWS account number": re.compile(r"\b\d{12}\b"),
    "an S3 URI": re.compile(r"s3://"),
    "an ARN": re.compile(r"arn:aws"),
}


def elide(value: object) -> str:
    return f"<elided {len(json.dumps(value, default=str))} chars>"


def scrub(case: dict) -> dict:
    """The subset `check_expectations` reads, with every free-text value elided.

    Top-level keys are an allowlist rather than a denylist: a case record grows fields
    (`attributes`, `cost_summary`, `action_log`), none of them graded, and each new one
    would otherwise arrive in a fixture unreviewed.
    """
    traces = []
    for trace in case.get("agent_traces") or []:
        segments = []
        for seg in trace.get("segments") or []:
            segments.append(
                {k: (elide(v) if k in _ELIDE_SEGMENT else v) for k, v in seg.items()}
            )
        traces.append(
            {
                **{
                    k: (elide(v) if k in _ELIDE_TRACE else v)
                    for k, v in trace.items()
                    if k != "segments"
                },
                "segments": segments,
            }
        )
    return {"status": case.get("status"), "agent_traces": traces}


def assert_publishable(fixture: dict) -> None:
    blob = json.dumps(fixture, default=str)
    for what, pattern in _FORBIDDEN.items():
        hit = pattern.search(blob)
        if hit:
            raise SystemExit(
                f"refusing to write the fixture: it still contains {what} "
                f"({hit.group(0)!r}). Add the field it came from to _ELIDE_SEGMENT or "
                f"_ELIDE_TRACE — this repo is public."
            )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--case-id", required=True)
    p.add_argument(
        "--outcome",
        help="expected.outcome to record. Omit to grade on tool calls alone.",
    )
    p.add_argument("--require", action="append", default=[], metavar="TOOL")
    p.add_argument("--forbid", action="append", default=[], metavar="TOOL")
    p.add_argument("--note", default="", help="why this run is worth keeping")
    args = p.parse_args()

    import os

    stack = Stack(
        os.environ.get("PROJECT_NAME", "agentcore-workshop"),
        os.environ.get("ENVIRONMENT", "dev"),
        os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"),
    )
    case = get_case_record(stack, args.case_id)
    if not case:
        raise SystemExit(f"no case {args.case_id}")
    if not case.get("agent_traces"):
        raise SystemExit(f"case {args.case_id} has no agent_traces — nothing to grade")

    expected = {
        k: v
        for k, v in {
            "outcome": args.outcome,
            "required_tool_calls": args.require,
            "forbidden_tool_calls": args.forbid,
        }.items()
        if v
    }
    scrubbed = scrub(case)
    # The deployment is deliberately NOT persisted: a fixture ships, and stack names
    # are internal. `--note` is the field to describe the run in.
    fixture = {
        "case_id": args.case_id,
        "process_type": case.get("process_type"),
        "pack_id": case.get("pack_id"),
        "note": args.note,
        "expected": expected,
        # The verdict at capture time, recorded rather than asserted to be empty: a run
        # that failed its own expectations is a *better* fixture than a clean one,
        # because it exercises the grader's reporting path over real data.
        "failures": check_expectations(expected, scrubbed),
        "case": scrubbed,
    }
    assert_publishable(fixture)

    FIXTURE_DIR.mkdir(exist_ok=True)
    out = FIXTURE_DIR / f"{args.case_id}.json"
    out.write_text(json.dumps(fixture, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"wrote {out.relative_to(_HERE.parents[1])}")
    print(f"  status={scrubbed['status']} verdict={fixture['failures'] or 'passes'}")


if __name__ == "__main__":
    main()
