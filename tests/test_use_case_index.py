# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The platform's entry pages list the installed use cases only in generated blocks, so
deleting a use case's folder and running scripts/gen_use_case_index.py leaves no link to it."""

from __future__ import annotations

import importlib.util
import re
import subprocess  # nosec B404 — running our own generator, no shell
import sys
from pathlib import Path

import pytest

from infra_utils.platform_config import PLATFORM_INTERFACE_VERSION

REPO = Path(__file__).resolve().parents[1]
GEN = REPO / "scripts" / "gen_use_case_index.py"

_spec = importlib.util.spec_from_file_location("gen_use_case_index", GEN)
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)

LINK_INTO_A_USE_CASE = re.compile(r"\]\((?:\.\./)*use-cases/(?!_)[^/)]+/")


def test_the_committed_blocks_are_current():
    result = subprocess.run(  # nosec B603
        [sys.executable, str(GEN), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout


def test_no_platform_page_links_into_a_use_case_outside_a_block():
    found = []
    for pattern in gen.PAGES:
        for page in REPO.glob(pattern):
            text = gen.BLOCK.sub("", page.read_text(encoding="utf-8"))
            if LINK_INTO_A_USE_CASE.search(text):
                found.append(page.relative_to(REPO).as_posix())
    assert found == []


def _use_case(root: Path, name: str, **docs: str) -> None:
    folder = root / "use-cases" / name
    folder.mkdir(parents=True)
    (folder / "walkthrough.md").write_text("# Walkthrough\n")
    lines = "".join(f"  {k}: {v}\n" for k, v in {"title": name.title(), **docs}.items())
    (folder / "manifest.yaml").write_text(
        f"name: {name}\nowner: me\nsummary: The {name} process.\n"
        f"platform_interface: {PLATFORM_INTERFACE_VERSION}\n"
        f"stacks: [uc-{name}]\ndocs:\n  entry: walkthrough.md\n{lines}"
    )


def _tree(tmp_path: Path) -> Path:
    page = tmp_path / "docs" / "start" / "index.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "# Index\n\n<!-- use-cases:begin -->\n<!-- use-cases:end -->\n\n"
        "<!-- use-cases:begin glossary -->\nstale\n<!-- use-cases:end -->\n"
    )
    (tmp_path / "use-cases").mkdir()
    return page


def test_the_blocks_follow_the_use_cases_present(tmp_path):
    page = _tree(tmp_path)
    assert gen.render(tmp_path)[page][1].count("None installed.") == 2

    _use_case(tmp_path, "invoices", glossary="walkthrough.md#terms")
    text = gen.render(tmp_path)[page][1]
    assert (
        "- [Invoices](../../use-cases/invoices/walkthrough.md) (`invoices`): "
        "The invoices process.\n" in text
    )
    assert "(../../use-cases/invoices/walkthrough.md#terms)" in text
    assert "stale" not in text

    _use_case(tmp_path, "orders")
    text = gen.render(tmp_path)[page][1]
    assert "(../../use-cases/orders/walkthrough.md)" in text
    # orders has no glossary, so the glossary block still lists only invoices.
    assert text.count("walkthrough.md#terms") == 1


def test_a_layer_page_links_from_its_own_folder(tmp_path):
    (tmp_path / "use-cases").mkdir()
    page = tmp_path / "layers" / "sap" / "reference" / "page.md"
    page.parent.mkdir(parents=True)
    page.write_text("<!-- use-cases:begin -->\n<!-- use-cases:end -->\n")
    _use_case(tmp_path, "invoices")
    assert (
        "(../../../use-cases/invoices/walkthrough.md)" in gen.render(tmp_path)[page][1]
    )


def test_a_missing_docs_page_names_the_manifest(tmp_path):
    _tree(tmp_path)
    _use_case(tmp_path, "invoices", roles="docs/roles.md")
    with pytest.raises(
        SystemExit, match="use-cases/invoices/manifest.yaml.*docs/roles.md"
    ):
        gen.render(tmp_path)


@pytest.mark.parametrize("path", ["../other/page.md", "/abs.md", "page.txt"])
def test_a_docs_page_must_be_markdown_inside_the_folder(path):
    from infra_utils.platform_config import UseCaseDocs

    with pytest.raises(ValueError, match="inside the use case's folder"):
        UseCaseDocs(title="x", entry=path)
