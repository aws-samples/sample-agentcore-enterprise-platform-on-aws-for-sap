# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Set one `evals-run --results-file` against another: what a change did, in numbers.

    python compare_results.py <this run's results> <the baseline's results>

Prints the assertion pass rate, each judge's mean, cost per case and p90 wall time for
both runs and the difference. It reports and never gates: always exit 0, and a missing
baseline (the first run) says so.
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from run_regression import summarize  # noqa: E402


def rows(current: dict, baseline: dict) -> list[tuple[str, float, float | None]]:
    """(metric, this run, the baseline or None) for every number worth comparing."""

    def rate(s: dict) -> float:
        return s["asserted"] / s["cases"] if s["cases"] else 0.0

    out = [("assertion pass rate", rate(current), rate(baseline) if baseline else None)]
    then = baseline.get("judge_means", {})
    for name, mean in current["judge_means"].items():
        out.append((f"judge {name}", mean, then.get(name)))
    for key, label in (
        ("cost_per_case_usd", "cost per case (USD)"),
        ("p90_wall_s", "p90 wall (s)"),
    ):
        out.append((label, current[key], baseline.get(key)))
    return out


def render(current: dict, baseline: dict) -> str:
    lines = [f"{'metric':<40} {'this run':>10} {'baseline':>10} {'delta':>10}"]
    for metric, now, then in rows(current, baseline):
        if then is None:
            lines.append(f"{metric:<40} {now:>10.4f} {'-':>10} {'-':>10}")
        else:
            lines.append(
                f"{metric:<40} {now:>10.4f} {then:>10.4f} {now - then:>+10.4f}"
            )
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.split("\n\n")[1].strip(), file=sys.stderr)
        return 2
    current = summarize(json.loads(Path(argv[0]).read_text(encoding="utf-8")))
    base = Path(argv[1])
    if not base.is_file():
        print(f"No baseline at {base}: nothing to compare with yet.")
        baseline = {}
    else:
        baseline = summarize(json.loads(base.read_text(encoding="utf-8")))
    print(render(current, baseline))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
