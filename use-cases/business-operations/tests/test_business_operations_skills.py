# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The skills the fork ships, and what makes a clone find them.

Each skill is `use-cases/business-operations/skills/<name>/SKILL.md`, or for one
that is not business-operations', `sap_kit/skills/<name>/SKILL.md`. `.claude/skills/<name>`,
`.agents/skills/<name>` and `.kiro/skills/<name>` are committed symlinks to it, carved out of export-ignore in
`.gitattributes`, so a clone and the published archive both discover it. A skill is a
procedure an agent runs as written, so every command it names must exist.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
UC = ROOT / "use-cases" / "business-operations"
SKILLS = sorted(
    p.parent for d in (UC, ROOT / "sap_kit") for p in (d / "skills").glob("*/SKILL.md")
)
DISCOVERY = (
    ROOT / ".claude" / "skills",
    ROOT / ".agents" / "skills",
    ROOT / ".kiro" / "skills",
)
#: Installed by `bd init`, export-ignored, and not one of ours.
FOREIGN = {"beads"}

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_CODE = re.compile(r"```.*?```|`[^`\n]+`", re.DOTALL)
_LINK = re.compile(r"\]\(([^)\s#]+)(?:#[^)]*)?\)")


def _verbs(ops: Path) -> set[str]:
    for node in ast.parse(ops.read_text()).body:
        if (
            isinstance(node, ast.Assign)
            and getattr(node.targets[0], "id", "") == "VERBS"
        ):
            return {k.value for k in node.value.keys}
    raise AssertionError(f"no VERBS in {ops}")


def _deploy_actions() -> set[str]:
    usage = re.search(
        r"Usage: \$0 \[([a-z|]+)\]", (ROOT / "scripts/deploy.sh").read_text()
    )
    return set(usage.group(1).split("|"))


def _frontmatter(text: str) -> dict[str, str]:
    match = _FRONTMATTER.match(text)
    assert match, "SKILL.md must open with --- frontmatter ---"
    fields = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def test_there_are_skills():
    assert SKILLS, "use-cases/business-operations/skills/ holds no SKILL.md"


@pytest.mark.parametrize("skill", SKILLS, ids=lambda p: p.name)
def test_frontmatter_names_the_directory(skill):
    fields = _frontmatter((skill / "SKILL.md").read_text(encoding="utf-8"))
    assert fields.get("name") == skill.name
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", skill.name) and len(skill.name) <= 64
    assert 0 < len(fields.get("description", "")) <= 1024


@pytest.mark.parametrize("skill", SKILLS, ids=lambda p: p.name)
def test_each_tool_discovers_the_skill(skill):
    for root in DISCOVERY:
        link = root / skill.name
        assert link.is_symlink(), (
            f"{link.relative_to(ROOT)} must be a symlink to {skill}"
        )
        assert link.resolve() == skill.resolve()
        assert not Path(link.readlink()).is_absolute(), "a link that ships is relative"


def test_discovery_holds_only_links_to_shipped_skills():
    names = {s.name for s in SKILLS}
    for root in DISCOVERY:
        for entry in root.iterdir():
            if entry.name in FOREIGN:
                continue
            assert entry.is_symlink() and entry.name in names, (
                f"{entry.relative_to(ROOT)} is not a link to a skill under "
                "use-cases/business-operations/skills/ or sap_kit/skills/"
            )


@pytest.mark.parametrize("skill", SKILLS, ids=lambda p: p.name)
def test_every_command_a_skill_names_exists(skill):
    text = (skill / "SKILL.md").read_text(encoding="utf-8")
    code = " ".join(_CODE.findall(text))
    uc_verbs, sap_verbs, actions = (
        _verbs(UC / "ops.py"),
        _verbs(ROOT / "layers/sap/ops.py"),
        _deploy_actions(),
    )
    named = set(re.findall(r"\bops (?!\()([a-z][a-z-]*)", code))
    named |= set(re.findall(r"usecase run business-operations ([a-z][a-z-]*)", code))
    assert named <= uc_verbs, f"no such ops.py verb: {sorted(named - uc_verbs)}"
    named = set(re.findall(r"layer run sap ([a-z][a-z-]*)", code))
    assert named <= sap_verbs, f"no such SAP layer verb: {sorted(named - sap_verbs)}"
    named = set(re.findall(r"deploy\.sh ([a-z][a-z-]*)", code))
    assert named <= actions, f"no such deploy.sh action: {sorted(named - actions)}"


@pytest.mark.parametrize("skill", SKILLS, ids=lambda p: p.name)
def test_every_relative_link_resolves(skill):
    text = (skill / "SKILL.md").read_text(encoding="utf-8")
    for target in _LINK.findall(_CODE.sub("", text)):
        if "://" in target or target.startswith("mailto:"):
            continue
        assert (skill / target).resolve().exists(), f"{skill.name}: dead link {target}"
