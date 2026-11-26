#!/usr/bin/env python3

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Write the list of installed use cases into the platform's entry pages.

A platform page links to a use case only between these markers, which this script
rewrites from every use-cases/<name>/manifest.yaml that has a `docs` block:

    <!-- use-cases:begin [kind] -->
    <!-- use-cases:end -->

Kinds: `list` (the default) links each use case's entry page; `enable`,
`quickstart`, `glossary`, `roles`, `faq` and `remove` link that page, for the use cases that have one;
`index` adds each use case's pages; `sidebar` writes Docsify sidebar sections.
Deleting a use case's folder and running this again leaves no link to it.

    gen_use_case_index.py          # rewrite the blocks
    gen_use_case_index.py --check  # exit 1 with a diff if a block is stale
"""

from __future__ import annotations

import difflib
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infra_utils import platform_config as pc

PAGES = ("*.md", "docs/**/*.md", "use-cases/*.md", "layers/**/*.md")
BLOCK = re.compile(
    r"(?P<begin><!-- use-cases:begin(?: (?P<kind>[a-z]+))? -->\n)"
    r".*?(?P<end><!-- use-cases:end -->)",
    re.DOTALL,
)
ONE_PAGE = ("enable", "quickstart", "glossary", "roles", "faq", "remove")
NONE = "None installed.\n"


def _href(root: Path, page: Path, name: str, path: str) -> str:
    file, sep, anchor = path.partition("#")
    target = root / "use-cases" / name / file
    return Path(os.path.relpath(target, page.parent)).as_posix() + sep + anchor


def _installed(root: Path) -> dict[str, pc.UseCaseManifest]:
    pc.discover_use_cases.cache_clear()
    found = {
        n: m for n, m in pc.discover_use_cases(root / "use-cases").items() if m.docs
    }
    for name, m in found.items():
        paths = [m.docs.entry, *(getattr(m.docs, k) for k in ONE_PAGE)]
        paths += [p.path for p in m.docs.pages]
        for path in filter(None, paths):
            if not (root / "use-cases" / name / path.partition("#")[0]).is_file():
                raise SystemExit(
                    f"use-cases/{name}/manifest.yaml: docs page not found: {path}"
                )
    return found


def _pages(m: pc.UseCaseManifest) -> list[tuple[str, str]]:
    return [(p.title, p.path) for p in m.docs.pages] or [("Overview", m.docs.entry)]


def render_block(kind: str, page: Path, root: Path, cases: dict) -> str:
    def link(name: str, path: str) -> str:
        return _href(root, page, name, path)

    if kind == "sidebar":
        return "\n".join(
            f"{m.docs.title}\n"
            + "".join(f"- [{t}]({link(n, p)})\n" for t, p in _pages(m))
            for n, m in cases.items()
        )
    lines = []
    for name, m in cases.items():
        path = m.docs.entry if kind in ("list", "index") else getattr(m.docs, kind)
        if path is None:
            continue
        lines.append(
            f"- [{m.docs.title}]({link(name, path)}) (`{name}`): {m.summary}\n"
        )
        if kind == "index" and len(m.docs.pages) > 1:
            pages = ", ".join(f"[{t}]({link(name, p)})" for t, p in _pages(m))
            lines.append(f"  Pages: {pages}\n")
    return "".join(lines) or NONE


def render(root: Path = ROOT) -> dict[Path, tuple[str, str]]:
    """{page: (current text, generated text)} for every page that has a block."""
    cases = _installed(root)
    out = {}
    for pattern in PAGES:
        for page in sorted(root.glob(pattern)):
            text = page.read_text(encoding="utf-8")
            if "<!-- use-cases:begin" not in text:
                continue

            def fill(match: re.Match, page: Path = page) -> str:
                kind = match["kind"] or "list"
                if kind not in ("list", "index", "sidebar", *ONE_PAGE):
                    raise SystemExit(
                        f"{page.relative_to(root)}: unknown block kind {kind!r}"
                    )
                body = render_block(kind, page, root, cases)
                return match["begin"] + body + match["end"]

            out[page] = (text, BLOCK.sub(fill, text))
    return out


def main(argv: list[str]) -> int:
    stale = []
    for page, (text, new) in render().items():
        if text == new:
            continue
        rel = page.relative_to(ROOT).as_posix()
        if "--check" in argv:
            stale.append(rel)
            sys.stdout.writelines(
                difflib.unified_diff(
                    text.splitlines(True),
                    new.splitlines(True),
                    rel,
                    rel + " (generated)",
                )
            )
        else:
            page.write_text(new, encoding="utf-8")
            print(f"wrote {rel}")
    if stale:
        print(
            f"\nstale use-case blocks in {', '.join(stale)}; run scripts/gen_use_case_index.py"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
