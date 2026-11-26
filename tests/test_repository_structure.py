# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Regression tests for the capability-oriented repository layout."""

import os
from pathlib import Path, PurePosixPath

from scripts.check_repository_structure import (
    check_agent_runtime_shape,
    check_compatibility_links,
    check_skill_links,
    check_top_level_directories,
    run_checks,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_current_repository_structure_passes():
    assert run_checks(REPO_ROOT) == []


def test_unregistered_top_level_directory_fails():
    errors = check_top_level_directories([PurePosixPath("new-component/file.py")])
    assert errors == ["unregistered top-level directory: new-component"]


def test_legacy_capability_root_fails_with_migration_guidance():
    errors = check_top_level_directories(
        [PurePosixPath("migration-adapter/adapter.py")]
    )
    assert errors == ["legacy capability root must not return: migration-adapter"]


def test_terraform_compatibility_link_is_preserved():
    assert check_compatibility_links(REPO_ROOT) == []


def test_agent_runtime_requires_build_contract():
    errors = check_agent_runtime_shape([PurePosixPath("agent-code/example/agent.py")])
    assert errors == [
        (
            "agent-code/example: missing runtime contract file(s): "
            "Dockerfile, requirements.txt"
        )
    ]


def test_agent_runtime_requires_entrypoint():
    errors = check_agent_runtime_shape(
        [
            PurePosixPath("agent-code/example/Dockerfile"),
            PurePosixPath("agent-code/example/requirements.txt"),
        ]
    )
    assert errors == ["agent-code/example: missing runtime contract file(s): agent.py"]


def test_a_use_case_may_import_runtime_stack_and_no_other_core_stack(tmp_path):
    from scripts.check_repository_structure import check_dependency_boundaries

    path = PurePosixPath("use-cases/tenant/stack.py")
    (tmp_path / path.parent).mkdir(parents=True)
    (tmp_path / path).write_text("from stacks.runtime_stack import RuntimeStack\n")
    assert check_dependency_boundaries([path], tmp_path) == []
    (tmp_path / path).write_text("from stacks.gateway_stack import GatewayStack\n")
    assert check_dependency_boundaries([path], tmp_path) == [
        f"{path}: imports forbidden package(s): stacks"
    ]


def test_skill_links_follow_the_use_cases_present(tmp_path):
    """Dropping a use case in and taking it out again changes its skill links only."""
    skill = tmp_path / "use-cases" / "demo" / "skills" / "demo-run"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: demo-run\n---\n")
    links = tmp_path / ".claude" / "skills"
    links.mkdir(parents=True)

    assert check_skill_links(tmp_path) == [
        ".claude/skills/demo-run: must be a link to ../../use-cases/demo/skills/demo-run"
    ]
    assert check_skill_links(tmp_path, fix=True) == []
    assert (links / "demo-run").resolve() == skill.resolve()

    (skill / "SKILL.md").unlink()
    skill.rmdir()
    assert check_skill_links(tmp_path) == [
        ".claude/skills/demo-run: dangling link (run with --fix)"
    ]
    assert check_skill_links(tmp_path, fix=True) == []
    assert not (links / "demo-run").is_symlink()


def test_fix_relinks_a_skill_that_moved(tmp_path):
    """A skill moved out of a use case to sap_kit/ is relinked by one --fix."""
    old = tmp_path / "use-cases" / "demo" / "skills" / "start"
    new = tmp_path / "sap_kit" / "skills" / "start"
    new.mkdir(parents=True)
    (new / "SKILL.md").write_text("---\nname: start\n---\n")
    links = tmp_path / ".claude" / "skills"
    links.mkdir(parents=True)
    (links / "start").symlink_to(os.path.relpath(old, links))

    assert check_skill_links(tmp_path, fix=True) == []
    assert (links / "start").resolve() == new.resolve()
