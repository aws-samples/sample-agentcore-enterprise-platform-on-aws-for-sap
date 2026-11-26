#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""cdk-nag's AwsSolutions pack over synthesized templates, as a gate.

ASH runs cdk-nag too, but 3.2.2 drops every result on the way to its SARIF and
reports the scanner PASSED, so this runs it directly, the way ASH does: each
*.template.json is loaded into its own stack with CfnInclude and the pack applied
as an aspect. Suppressions come from .cdk-nag.yaml and are applied with
NagSuppressions, so cdk-nag itself records each one and its reason.

    check_cdk_nag.py [--config .cdk-nag.yaml] <templates-dir>...

Exit 1 on any unsuppressed error-level finding, on a suppression entry that
matched nothing in one of the stacks it names (a stale entry hides the next
regression on that resource), or
on one past its expiration (a re-review date, as in .ash.yaml).
Warnings are printed, never gating. Needs cdk-nag in the interpreter and Node on
PATH (jsii).
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
REQUIRED = ("rule", "stacks", "reason", "expiration")


def load_config(path: Path) -> list[dict]:
    """The suppression entries, refused unless each is scoped, explained and dated."""
    entries = (yaml.safe_load(path.read_text()) or {}).get("suppressions") or []
    for i, e in enumerate(entries):
        where = f"{path}: suppression {i} ({e.get('rule')})"
        missing = [k for k in REQUIRED if not e.get(k)]
        if missing:
            raise SystemExit(f"{where} lacks {', '.join(missing)}")
        if any("*" in x for x in [e["rule"], *e["stacks"]]):
            raise SystemExit(f"{where}: name the rule and each stack, no globs")
        e.setdefault("resources", ["*"])
        e.setdefault("applies_to", [])
        e.setdefault("where", {})
        e.setdefault("star_actions", [])
    return entries


def default_configs(repo: Path = REPO) -> list[Path]:
    """The root suppressions, then each use case's and layer's own, so a use case's
    entries go with its folder."""
    return [
        repo / ".cdk-nag.yaml",
        *sorted(repo.glob("use-cases/*/.cdk-nag.yaml")),
        *sorted(repo.glob("layers/*/.cdk-nag.yaml")),
    ]


def in_stacks(entry: dict, template: str) -> bool:
    return any(template.endswith(f"-{s}.template.json") for s in entry["stacks"])


def _star_actions(resource: dict) -> list[str]:
    """Every action an Allow statement grants on Resource "*"."""
    if not resource.get("Type", "").startswith("AWS::IAM::"):
        return []
    props = resource.get("Properties", {})
    docs = [props.get("PolicyDocument") or {}]
    docs += [p.get("PolicyDocument") or {} for p in props.get("Policies", [])]
    out: list[str] = []
    for d in docs:
        for st in d.get("Statement", []):
            res = st.get("Resource")
            if st.get("Effect") == "Allow" and "*" in (
                res if isinstance(res, list) else [res]
            ):
                act = st.get("Action", [])
                out += act if isinstance(act, list) else [act]
    return out


def covers_resource(entry: dict, logical_id: str, resource: dict) -> bool:
    """Whether `entry` is applied to one resource. `where` pins top-level
    properties (an L1 entry names the runtime it accepts, so a function on any
    other one still fails); `star_actions` lists what the resource may grant on
    Resource "*" (an IAM5 `Resource::*` entry then hides no new statement)."""
    if not any(fnmatch.fnmatchcase(logical_id, r) for r in entry["resources"]):
        return False
    props = resource.get("Properties", {})
    if any(props.get(k) != v for k, v in entry["where"].items()):
        return False
    return not entry["star_actions"] or all(
        any(fnmatch.fnmatchcase(a, x) for x in entry["star_actions"])
        for a in _star_actions(resource)
    )


def _js_regex(glob: str) -> str:
    """A glob as the `/regex/` string cdk-nag's RegexAppliesTo takes."""
    body = "".join(".*" if c == "*" else re.escape(c) for c in glob)
    return f"/^{body}$/"


def finding_matches(
    entry: dict, template: str, logical_id: str, rule: str, finding: str
) -> bool:
    """Whether `entry` names one suppressed finding. `finding` is cdk-nag's
    granular id (`Resource::*` for IAM5), empty for a rule with none."""
    return (
        in_stacks(entry, template)
        and rule == entry["rule"]
        and any(fnmatch.fnmatchcase(logical_id, r) for r in entry["resources"])
        and (
            not entry["applies_to"]
            or any(fnmatch.fnmatchcase(finding, x) for x in entry["applies_to"])
        )
    )


def scan(template: Path, entries: list[dict], outdir: Path) -> list[dict]:
    """Every finding cdk-nag reports for one template, suppressed or not."""
    import aws_cdk as cdk
    import cdk_nag
    import jsii
    from aws_cdk import cloudformation_include as cfn_inc

    found: list[dict] = []

    # The JSON report drops the granular id an IAM5 suppression is scoped by; a
    # logger receives it.
    @jsii.implements(cdk_nag.INagLogger)
    class Collect:
        def _add(self, data, compliance):
            found.append(
                {
                    "template": template.name,
                    "resource": data.resource.node.id,
                    "rule": data.rule_id,
                    "finding": data.finding_id,
                    "compliance": compliance,
                    "level": str(data.rule_level).rsplit(".", 1)[-1].title(),
                }
            )

        def on_non_compliance(self, data):
            self._add(data, "Non-Compliant")

        def on_suppressed(self, data):
            self._add(data, "Suppressed")
            found[-1]["reason"] = data.suppression_reason

        # A rule that could not evaluate (an intrinsic where it wanted a value)
        # is cdk-nag's CdkNagValidationFailure, suppressed by naming the rule.
        def _failed(self, data, compliance):
            found.append(
                {
                    "template": template.name,
                    "resource": data.resource.node.id,
                    "rule": "CdkNagValidationFailure",
                    "finding": data.rule_id,
                    "compliance": compliance,
                    "level": "Error",
                }
            )

        def on_error(self, data):
            self._failed(data, "Non-Compliant")

        def on_suppressed_error(self, data):
            self._failed(data, "Suppressed")

        def on_compliance(self, data):
            pass

        def on_not_applicable(self, data):
            pass

    app = cdk.App(outdir=str(outdir))
    # The included template already declares BootstrapVersion.
    synth = cdk.DefaultStackSynthesizer(generate_bootstrap_version_rule=False)
    stack = cdk.Stack(app, "Nag", synthesizer=synth)
    include = cfn_inc.CfnInclude(stack, "T", template_file=str(template))
    resources = json.loads(template.read_text()).get("Resources", {})
    for e in entries:
        if not in_stacks(e, template.name):
            continue
        # Typed structs: jsii hands a plain dict in appliesTo to cdk-nag as an
        # object it does not recognize, and the entry then suppresses every
        # finding of the rule on the resource.
        rule = cdk_nag.NagPackSuppression(
            id=e["rule"],
            reason=e["reason"],
            applies_to=[
                cdk_nag.RegexAppliesTo(regex=_js_regex(x)) for x in e["applies_to"]
            ]
            or None,
        )
        for lid, res in resources.items():
            if covers_resource(e, lid, res):
                cdk_nag.NagSuppressions.add_resource_suppressions(
                    include.get_resource(lid), [rule]
                )
    cdk.Aspects.of(stack).add(
        cdk_nag.AwsSolutionsChecks(
            reports=False, log_ignores=True, additional_loggers=[Collect()]
        )
    )
    app.synth()
    return found


# A policy with one wildcard action (suppressed below) and one wildcard resource
# (not). The gate exists because a scanner once reported nothing and passed; this
# proves, on every run, that findings arrive and a granular suppression stays granular.
_CANARY = {
    "Resources": {
        "P": {
            "Type": "AWS::IAM::Policy",
            "Properties": {
                "PolicyName": "canary",
                "Roles": ["canary"],
                "PolicyDocument": {
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": "xray:PutTraceSegments",
                            "Resource": "*",
                        },
                        {
                            "Effect": "Allow",
                            "Action": "s3:GetObject*",
                            "Resource": "arn:aws:s3:::b/k",
                        },
                    ]
                },
            },
        }
    }
}


def canary(tmp: Path) -> bool:
    template = tmp / "x-canary.template.json"
    template.write_text(json.dumps(_CANARY))
    entry = {
        "rule": "AwsSolutions-IAM5",
        "stacks": ["canary"],
        "resources": ["*"],
        "applies_to": ["Action::s3:GetObject*"],
        "where": {},
        "star_actions": [],
        "reason": "canary: the action wildcard only",
    }
    got = {
        (x["finding"], x["compliance"]) for x in scan(template, [entry], tmp / "canary")
    }
    return got == {
        ("Resource::*", "Non-Compliant"),
        ("Action::s3:GetObject*", "Suppressed"),
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("dirs", nargs="+", type=Path)
    ap.add_argument(
        "--config",
        type=Path,
        action="append",
        help="a suppressions file; repeat for more. Default: .cdk-nag.yaml and each "
        "use case's and layer's own .cdk-nag.yaml",
    )
    ap.add_argument(
        "--report",
        type=Path,
        help="write every finding, suppressed or not, as JSON here",
    )
    a = ap.parse_args(argv)
    try:
        import cdk_nag  # noqa: F401
    except ImportError:
        print(
            "cdk-nag is not installed in this interpreter: refusing to pass",
            file=sys.stderr,
        )
        return 1
    entries = [e for path in a.config or default_configs() for e in load_config(path)]
    templates = sorted(p for d in a.dirs for p in d.rglob("*.template.json"))
    if not templates:
        print(
            f"no *.template.json under {', '.join(map(str, a.dirs))}", file=sys.stderr
        )
        return 1

    lines: list[dict] = []
    with tempfile.TemporaryDirectory() as tmp:
        if not canary(Path(tmp)):
            print(
                "cdk-nag canary failed: findings or suppressions are not reported as "
                "expected, so this scan proves nothing",
                file=sys.stderr,
            )
            return 1
        for i, t in enumerate(templates):
            lines += scan(t, entries, Path(tmp) / str(i))
    if a.report:
        a.report.write_text(json.dumps(lines, indent=1))

    used = Counter()
    for line in lines:
        if line["compliance"] == "Suppressed":
            for i, e in enumerate(entries):
                if finding_matches(
                    e, line["template"], line["resource"], line["rule"], line["finding"]
                ):
                    for st in e["stacks"]:
                        used[i, st] += line["template"].endswith(f"-{st}.template.json")
    errors = [
        x for x in lines if x["compliance"] == "Non-Compliant" and x["level"] == "Error"
    ]
    warnings = [
        x for x in lines if x["compliance"] == "Non-Compliant" and x["level"] != "Error"
    ]
    # Per stack: an entry naming a stack it no longer matches there is stale too.
    stale = [
        (e, st) for i, e in enumerate(entries) for st in e["stacks"] if not used[i, st]
    ]
    expired = [
        e
        for e in entries
        if str(e["expiration"]) < datetime.now(UTC).date().isoformat()
    ]

    by_rule = Counter(x["rule"] for x in errors)
    for x in errors:
        print(f"ERROR {x['rule']}[{x['finding']}] {x['template']} {x['resource']}")
    for x in warnings:
        print(f"warn  {x['rule']}[{x['finding']}] {x['template']} {x['resource']}")
    for e, st in stale:
        print(
            f"STALE suppression {e['rule']} on {st}: {e['resources']} {e['applies_to']}"
        )
    for e in expired:
        print(
            f"EXPIRED suppression {e['rule']} {e['stacks']} ({e['expiration']}): re-review it"
        )
    suppressed = sum(1 for x in lines if x["compliance"] == "Suppressed")
    print(
        f"cdk-nag AwsSolutions: {len(templates)} templates, {len(errors)} errors "
        f"{dict(by_rule)}, {len(warnings)} warnings, {suppressed} suppressed, "
        f"{len(stale)} stale and {len(expired)} expired suppressions"
    )
    return 1 if errors or stale or expired else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
