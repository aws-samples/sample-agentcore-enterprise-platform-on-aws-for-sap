# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Fail when the published documentation has broken local navigation."""

from __future__ import annotations

import re
import subprocess
import sys
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_ROOT = REPO_ROOT / "docs"
# Every shipped page outside docs/ is checked too (README files, use cases, layers, kits),
# so a link into a deleted use case fails here. Templates hold links that resolve only
# after `deploy.sh usecase new` copies them.
NOT_CHECKED = (
    "internal/",
    ".claude/",
    ".agents/",
    ".kiro/",
    ".beads/",
    "memory.md",
    "sap_kit/templates/",
    "use-cases/_template/",
)
LINK_RE = re.compile(
    r"!?\[[^\]]*]\((?P<target><[^>]+>|[^)\s]+)(?:\s+['\"][^)]*['\"])?\)"
)
FENCED_CODE_RE = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
EXTERNAL_SCHEMES = {"http", "https", "mailto", "tel"}
REQUIRED_SIDEBAR_ROUTES = {
    "home.md",
    "MIGRATION_RUNBOOK.md",
    "PLATFORM_YAML.md?id=migration",
}
REQUIRED_DOCSIFY_CONFIG = {
    '"/.*/_sidebar.md": "/_sidebar.md",',
    'const docsBasePath = window.location.pathname.replace(/[^/]*$/, "");',
    "basePath: docsBasePath,",
}


class _LocalAssetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.assets: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attribute = "href" if tag == "link" else "src" if tag == "script" else None
        if attribute is None:
            return
        values = dict(attrs)
        value = values.get(attribute)
        if value:
            self.assets.append(value)


def _markdown_targets(text: str) -> list[str]:
    content = HTML_COMMENT_RE.sub("", FENCED_CODE_RE.sub("", text))
    return [
        match.group("target").removeprefix("<").removesuffix(">")
        for match in LINK_RE.finditer(content)
    ]


def _is_external(target: str) -> bool:
    parsed = urlsplit(target)
    return parsed.scheme.lower() in EXTERNAL_SCHEMES or target.startswith("//")


def _resolved_target(source: Path, target: str) -> Path | None:
    if _is_external(target) or target.startswith("#"):
        return None
    path = unquote(urlsplit(target).path)
    if not path:
        return None
    resolved = (source.parent / path).resolve()
    if resolved.is_dir() and (resolved / "README.md").is_file():
        resolved /= "README.md"
    return resolved  # a directory with no README is a valid link: GitHub lists it


def _shipped_pages() -> list[Path]:
    tracked = subprocess.run(  # nosec B603 B607 — fixed argv
        ["git", "ls-files", "*.md"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    pages = [REPO_ROOT / f for f in tracked if not f.startswith(NOT_CHECKED)]
    return [p for p in pages if p.is_file()]  # tracked but deleted in the tree: skip


def check_markdown_links() -> tuple[list[str], int, int]:
    errors: list[str] = []
    local_links = 0
    markdown_files = sorted(set(DOCS_ROOT.rglob("*.md")) | set(_shipped_pages()))

    for source in markdown_files:
        for target in _markdown_targets(source.read_text(encoding="utf-8")):
            if "?id=" in target and source.name != "_sidebar.md":
                errors.append(
                    f"{source.relative_to(REPO_ROOT)}: Docsify-only section link "
                    f"(GitHub ignores ?id=; link to #<heading> instead): {target}"
                )
            resolved = _resolved_target(source, target)
            if resolved is None:
                continue
            local_links += 1
            try:
                resolved.relative_to(REPO_ROOT)
            except ValueError:
                errors.append(
                    f"{source.relative_to(REPO_ROOT)}: link escapes the repository: {target}"
                )
                continue
            if not resolved.exists():
                errors.append(
                    f"{source.relative_to(REPO_ROOT)}: missing local target: {target}"
                )

    return errors, len(markdown_files), local_links


def check_sidebar() -> list[str]:
    sidebar = DOCS_ROOT / "_sidebar.md"
    targets = [
        target
        for target in _markdown_targets(sidebar.read_text(encoding="utf-8"))
        if not _is_external(target) and not target.startswith("#")
    ]
    errors = [
        f"docs/_sidebar.md: required route is missing: {route}"
        for route in sorted(REQUIRED_SIDEBAR_ROUTES - set(targets))
    ]
    for route, count in sorted(Counter(targets).items()):
        if count > 1:
            errors.append(f"docs/_sidebar.md: route appears {count} times: {route}")
    return errors


def check_site_assets() -> list[str]:
    errors: list[str] = []
    index = DOCS_ROOT / "index.html"
    index_text = index.read_text(encoding="utf-8")
    parser = _LocalAssetParser()
    parser.feed(index_text)
    for target in parser.assets:
        resolved = _resolved_target(index, target)
        if resolved is not None and not resolved.is_file():
            errors.append(f"docs/index.html: missing local asset: {target}")

    for snippet in sorted(REQUIRED_DOCSIFY_CONFIG):
        if snippet not in index_text:
            errors.append(
                "docs/index.html: missing project-path-safe Docsify configuration: "
                f"{snippet}"
            )

    if not (DOCS_ROOT / "_404.md").is_file():
        errors.append("docs/_404.md: required Docsify not-found page is missing")
    return errors


def main() -> int:
    link_errors, markdown_files, local_links = check_markdown_links()
    errors = link_errors + check_sidebar() + check_site_assets()
    if errors:
        print("Documentation integrity checks failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print(
        "Documentation integrity checks passed "
        f"({markdown_files} Markdown files, {local_links} local links)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
