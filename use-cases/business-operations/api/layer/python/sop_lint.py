#!/usr/bin/env python3

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Lint a SOP before it is written: its structure and, given the pack, its symbols.

    python3 lint_sop.py <sop file> [--pack-json <pack.json, or a JSON with pack_id,
                                    constants and process_type_to_sop>]

Standard library only, so it runs with no checkout and no virtual environment. It is
an early warning: the synth, `bundle import` and the SOP routes re-check the symbols with
`pack_tiers.undeclared_symbols` and refuse what this misses. Prints `OK:` or one `FAIL:`
per problem, and exits 1 on any failure.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Equal to pack_tiers.SYMBOL_RE (a test pins it): what the agent substitutes.
SYMBOL_RE = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")
SECTIONS = [
    "PURPOSE",
    "SCOPE",
    "PRECONDITIONS",
    "STEP",
    "ESCALATION RULES",
    "CONTACTS",
    "TOLERANCES",
    "REVISION HISTORY",
]


def problems(text: str, name: str, pack: dict | None) -> list[str]:
    lines = text.splitlines()
    heads = [
        lines[i].strip()
        for i in range(1, len(lines) - 1)
        if set(lines[i - 1].strip()) == {"="} and set(lines[i + 1].strip()) == {"="}
    ]
    seen: list[str] = []
    for h in heads:
        h = "STEP" if h.startswith("STEP ") else h.split(" (")[0]
        if not seen or seen[-1] != h:
            seen.append(h)
    found = []
    if not re.search(r"^Version\s+\S+", text, re.MULTILINE):
        found.append("no 'Version X.Y' line")
    if "RFC 2119" not in text or not re.search(r"\bMUST\b", text):
        found.append("no RFC 2119 boilerplate or no MUST")
    if seen != SECTIONS:
        found.append(f"sections {seen}, want {SECTIONS}")
    if pack is not None:
        key = f"{pack.get('pack_id')}/{name}"
        constants = pack.get("constants") or {}
        missing = sorted(
            {
                s
                for s in SYMBOL_RE.findall(text)
                if not s.startswith("CONTACT_") and s not in constants
            }
        )
        if missing:
            cited = ", ".join("{{" + s + "}}" for s in missing)
            found.append(f"{key} cites {cited}, which the pack does not declare")
        if key not in (pack.get("process_type_to_sop") or {}).values():
            found.append(f"{key} is not mapped by any process type (the pack maps it)")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lint_sop.py")
    parser.add_argument("sop")
    parser.add_argument("--pack-json")
    args = parser.parse_args(argv)
    sop = Path(args.sop)
    pack = (
        json.loads(Path(args.pack_json).read_text(encoding="utf-8"))
        if args.pack_json
        else None
    )
    found = problems(sop.read_text(encoding="utf-8"), sop.name, pack)
    for f in found:
        print("FAIL:", f)
    if found:
        return 1
    print("OK:", sop.name if pack is None else f"{pack.get('pack_id')}/{sop.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
