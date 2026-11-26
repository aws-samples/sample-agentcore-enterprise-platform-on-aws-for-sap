# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Validate repository capability roots and dependency boundaries."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parents[1]

ALLOWED_TOP_LEVEL_DIRECTORIES = {
    ".agentcore",
    ".github",
    "agent-code",
    "control-library",
    "dashboard",
    "docker",
    "docs",
    "infra_utils",
    "migration",
    "presets",
    "scripts",
    "stacks",
    "tests",
    "tools",
    "use-cases",
}
# The fork's own top-level directories, one per line.
ALLOWED_TOP_LEVEL_DIRECTORIES |= set(
    (REPO_ROOT / "sap_kit/roots.txt").read_text().split()
)

LEGACY_CAPABILITY_ROOTS = {
    "migration-adapter",
    "migration-network-probe",
    "terraform",
    "workshop-simulation",
}

FORBIDDEN_IMPORTS = {
    "agent-code": {"infra_utils", "stacks"},
    "infra_utils": {"stacks"},
    "layers": {"stacks"},
    "migration": {"infra_utils", "stacks"},
    "use-cases": {"stacks"},
}

# Modules a capability may import despite the rule above: RuntimeStack is the
# one core class a use case or a layer may build on (docs/PLATFORM_INTERFACE.md).
ALLOWED_IMPORTS = {
    "layers": {"stacks.runtime_stack"},
    "use-cases": {"stacks.runtime_stack"},
}

REQUIRED_COMPATIBILITY_LINKS = {
    "terraform": "control-library/terraform",
}

AGENT_SHARED_DIRECTORIES = {"shared"}
AGENT_REQUIRED_FILES = {"Dockerfile", "agent.py", "requirements.txt"}


def repository_paths(repo_root: Path = REPO_ROOT) -> list[PurePosixPath]:
    """Return committed and new, non-ignored files that currently exist."""

    result = subprocess.run(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    paths = [
        PurePosixPath(raw.decode())
        for raw in result.stdout.split(b"\0")
        if raw and (repo_root / raw.decode()).is_file()
    ]
    unshipped = _export_ignored_roots(paths, repo_root)
    return sorted(p for p in paths if p.parts[0] not in unshipped)


def _export_ignored_roots(paths: list[PurePosixPath], repo_root: Path) -> set[str]:
    """Top-level directories `git archive` leaves out: not part of what ships."""

    roots = sorted({p.parts[0] for p in paths if len(p.parts) > 1})
    if not roots:
        return set()
    out = subprocess.run(
        ["git", "check-attr", "-z", "export-ignore", "--", *(f"{r}/" for r in roots)],
        cwd=repo_root,
        check=True,
        capture_output=True,
    ).stdout.split(b"\0")
    # -z output is path, attribute, value triples.
    return {
        out[i].decode().rstrip("/")
        for i in range(0, len(out) - 2, 3)
        if out[i + 2] == b"set"
    }


# Not read in a dropped-in use case: version control, environments and build output.
SKIPPED_DIRECTORIES = {".git", ".venv", "__pycache__", "node_modules", "cdk.out"}


def dropped_in_paths(
    paths: list[PurePosixPath], repo_root: Path = REPO_ROOT
) -> list[PurePosixPath]:
    """Python files of use cases that .gitignore keeps out of git (`usecase add`).

    They do not ship, but they run in this platform, so its import rules apply."""

    tracked = {
        p.parts[1] for p in paths if p.parts[0] == "use-cases" and len(p.parts) > 2
    }
    found: list[PurePosixPath] = []
    for folder in sorted((repo_root / "use-cases").glob("*/")):
        if folder.name in tracked or folder.name.startswith(("_", ".")):
            continue
        for dirpath, dirnames, filenames in os.walk(folder):
            dirnames[:] = [d for d in dirnames if d not in SKIPPED_DIRECTORIES]
            found += [
                PurePosixPath(Path(dirpath, f).relative_to(repo_root).as_posix())
                for f in filenames
                if f.endswith(".py")
            ]
    return found


def check_top_level_directories(paths: list[PurePosixPath]) -> list[str]:
    roots = {path.parts[0] for path in paths if len(path.parts) > 1}
    errors = [
        f"unregistered top-level directory: {root}"
        for root in sorted(
            roots - ALLOWED_TOP_LEVEL_DIRECTORIES - LEGACY_CAPABILITY_ROOTS
        )
    ]
    errors.extend(
        f"legacy capability root must not return: {root}"
        for root in sorted(roots & LEGACY_CAPABILITY_ROOTS)
    )
    return errors


def _import_roots(tree: ast.AST, allowed: set[str] = frozenset()) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return {m.partition(".")[0] for m in modules - allowed}


def check_dependency_boundaries(
    paths: list[PurePosixPath], repo_root: Path = REPO_ROOT
) -> list[str]:
    errors: list[str] = []
    for path in paths:
        capability = path.parts[0]
        forbidden = FORBIDDEN_IMPORTS.get(capability)
        # A test beside its owner may import the platform it is tested against.
        if not forbidden or path.suffix != ".py" or "tests" in path.parts[1:-1]:
            continue
        try:
            tree = ast.parse((repo_root / path).read_text(encoding="utf-8"))
        except (OSError, SyntaxError) as exc:
            errors.append(f"{path}: cannot validate Python imports: {exc}")
            continue
        violations = (
            _import_roots(tree, ALLOWED_IMPORTS.get(capability, set())) & forbidden
        )
        if violations:
            errors.append(
                f"{path}: imports forbidden package(s): {', '.join(sorted(violations))}"
            )
    return errors


def check_agent_runtime_shape(paths: list[PurePosixPath]) -> list[str]:
    path_names = {str(path) for path in paths}
    runtime_directories = {
        path.parts[1]
        for path in paths
        if len(path.parts) > 2
        and path.parts[0] == "agent-code"
        and path.parts[1] not in AGENT_SHARED_DIRECTORIES
    }
    errors: list[str] = []
    for directory in sorted(runtime_directories):
        missing = {
            required
            for required in AGENT_REQUIRED_FILES
            if f"agent-code/{directory}/{required}" not in path_names
        }
        if missing:
            errors.append(
                f"agent-code/{directory}: missing runtime contract file(s): "
                f"{', '.join(sorted(missing))}"
            )
    return errors


def check_compatibility_links(repo_root: Path = REPO_ROOT) -> list[str]:
    errors: list[str] = []
    for link_name, expected_target in REQUIRED_COMPATIBILITY_LINKS.items():
        link = repo_root / link_name
        if not link.is_symlink() or link.readlink() != Path(expected_target):
            errors.append(
                f"{link_name}: required compatibility link must target "
                f"{expected_target}"
            )
    return errors


# Each tool finds skills in its own directory, so every shipped skill has a committed,
# relative link in all three. The skills live beside what they belong to; deleting a use
# case leaves its links dangling until `--fix` removes them, and dropping one in needs
# `--fix` to add them.
SKILL_LINK_DIRS = (".claude/skills", ".agents/skills", ".kiro/skills")
SKILL_SOURCES = (
    "use-cases/*/skills/*/SKILL.md",
    "layers/*/skills/*/SKILL.md",
    "sap_kit/skills/*/SKILL.md",
)


def _shipped_skills(repo_root: Path) -> tuple[dict[str, Path], list[str]]:
    skills: dict[str, Path] = {}
    errors: list[str] = []
    for pattern in SKILL_SOURCES:
        for skill_md in sorted(repo_root.glob(pattern)):
            skill = skill_md.parent
            if skill.name in skills:
                errors.append(
                    f"skill {skill.name!r} is defined twice: {skills[skill.name]} and {skill}"
                )
            skills[skill.name] = skill
    return skills, errors


def check_skill_links(repo_root: Path = REPO_ROOT, fix: bool = False) -> list[str]:
    skills, errors = _shipped_skills(repo_root)
    for directory in SKILL_LINK_DIRS:
        base = repo_root / directory
        if not base.is_dir():
            continue
        for name, skill in skills.items():
            link = base / name
            want = os.path.relpath(skill, base)
            if link.is_symlink() and os.readlink(link) == want:
                continue
            if fix and not link.exists():  # missing, or dangling after a skill moved
                link.unlink(missing_ok=True)
                link.symlink_to(want)
                continue
            errors.append(f"{directory}/{name}: must be a link to {want}")
        for entry in sorted(base.iterdir()):
            if entry.is_symlink() and not entry.exists():
                if fix:
                    entry.unlink()
                else:
                    errors.append(
                        f"{directory}/{entry.name}: dangling link (run with --fix)"
                    )
    return errors


def run_checks(repo_root: Path = REPO_ROOT) -> list[str]:
    paths = repository_paths(repo_root)
    return (
        check_top_level_directories(paths)
        + check_dependency_boundaries(
            paths + dropped_in_paths(paths, repo_root), repo_root
        )
        + check_agent_runtime_shape(paths)
        + check_compatibility_links(repo_root)
        + check_skill_links(repo_root)
    )


def main() -> int:
    if "--fix" in sys.argv[1:]:
        check_skill_links(fix=True)
    try:
        errors = run_checks()
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"Repository structure check could not run: {exc}", file=sys.stderr)
        return 1

    if errors:
        print("Repository structure checks failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print("Repository structure checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
