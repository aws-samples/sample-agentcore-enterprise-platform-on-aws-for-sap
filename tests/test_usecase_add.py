# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Drop-in use cases: the interface version check, `deploy.sh usecase add|remove`,
and the .gitignore that keeps an added use case out of git.

Every test works in tmp_path: the repo's own use-cases/ and platform.yaml are never
touched.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess  # nosec B404 — git with a fixed argv, no shell
from pathlib import Path

import pytest
import yaml

from infra_utils.platform_config import (
    PLATFORM_INTERFACE_VERSION,
    USE_CASES_DIR,
    UseCaseManifest,
    discover_use_cases,
)

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "usecase", REPO / "scripts" / "usecase.py"
)
usecase = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(usecase)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(  # nosec B603 B607
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A checkout's use-cases/ and .gitignore, and a greenfield platform.yaml."""
    root = tmp_path / "checkout" / "use-cases"
    root.mkdir(parents=True)
    shutil.copy(REPO / ".gitignore", root.parent / ".gitignore")
    manifest = root.parent / "platform.yaml"
    manifest.write_text((REPO / "presets" / "greenfield.yaml").read_text())
    monkeypatch.setenv("PLATFORM_CONFIG", str(manifest))
    return root, manifest


@pytest.fixture
def source(tmp_path, monkeypatch):
    """A use case in its own git repository, scaffolded from the template."""
    made = tmp_path / "made"
    with monkeypatch.context() as m:
        m.setenv("PLATFORM_CONFIG", str(tmp_path / "none.yaml"))
        assert usecase.main(["--use-cases-dir", str(made), "new", "invoice-match"]) == 0
    repo = made / "invoice-match"
    _git(repo, "init", "--quiet")
    _git(repo, "add", ".")
    _git(repo, "commit", "--quiet", "-m", "first")
    return repo


def run(root: Path, *args: str) -> int:
    return usecase.main(["--use-cases-dir", str(root), *args])


def _manifest(**kw) -> dict:
    return {
        "name": "old-one",
        "owner": "o",
        "summary": "s",
        "stacks": ["uc-old-one"],
    } | kw


@pytest.mark.parametrize("declared", [PLATFORM_INTERFACE_VERSION - 1, None])
def test_a_manifest_for_another_interface_names_both_versions(declared):
    with pytest.raises(ValueError) as e:
        UseCaseManifest.model_validate(_manifest(platform_interface=declared))
    message = str(e.value)
    assert "'old-one'" in message
    assert f"interface version {PLATFORM_INTERFACE_VERSION}" in message
    want = (
        "no platform_interface"
        if declared is None
        else f"platform_interface {declared}"
    )
    assert want in message


def test_the_version_in_code_is_the_one_the_interface_page_states():
    page = (REPO / "docs" / "PLATFORM_INTERFACE.md").read_text()
    stated = re.search(r"^Interface version: \*\*(\d+)\*\*", page, re.MULTILINE)
    assert int(stated.group(1)) == PLATFORM_INTERFACE_VERSION


def test_add_copies_a_repository_at_a_ref_and_merges_its_example(
    checkout, source, capsys
):
    root, manifest = checkout
    assert run(root, "add", str(source), "--ref", "HEAD") == 0
    added = root / "invoice-match"
    assert (added / "manifest.yaml").is_file()
    assert not (added / ".git").exists()
    assert "invoice-match" in discover_use_cases(root)
    config = yaml.safe_load(manifest.read_text())
    assert config["use_cases"]["invoice-match"] == {"label": "invoice-match"}
    assert config["gateway"]["tools"] == ["sample-tool"]  # the preset's own, intact
    (backup,) = root.parent.glob("platform.yaml.*.bak")
    assert "# Greenfield" in backup.read_text()  # the comments live on in the copy
    out = capsys.readouterr().out
    assert "platform.yaml: added use_cases from" in out
    assert "git ignores use-cases/invoice-match/" in out


def test_add_never_overwrites_what_the_operator_set(checkout, source, capsys):
    root, manifest = checkout
    manifest.write_text(
        manifest.read_text() + "use_cases:\n  invoice-match:\n    label: mine\n"
    )
    assert run(root, "add", str(source)) == 0
    config = yaml.safe_load(manifest.read_text())
    assert config["use_cases"]["invoice-match"] == {"label": "mine"}
    out = capsys.readouterr().out
    assert "already has every entry" in out
    assert "Kept your value at use_cases.invoice-match.label" in out
    assert not list(root.parent.glob("platform.yaml.*.bak"))  # nothing rewritten


def test_merge_adds_missing_keys_only():
    base = {"security": {}, "gateway": {"tools": ["a"]}, "use_cases": None}
    extra = {
        "security": {"cedar": {"enabled": True}},
        "gateway": {"tools": ["b"], "interceptor_extensions": [{"path": "p"}]},
        "use_cases": {"x": {}},
    }
    added, kept = usecase.merge_missing(base, extra)
    assert added == [
        "security.cedar",
        "gateway.interceptor_extensions",
        "use_cases",
    ]
    assert kept == ["gateway.tools"]
    assert base["gateway"]["tools"] == ["a"]


def test_placeholders_and_secrets_are_left_to_fill_in():
    config = {
        "layers": {"l": {"systems": {"s": {"base_url": "https://x.example.com/"}}}},
        "use_cases": {"x": {"secret_name": "x/s", "label": "fine"}},
    }
    todo = usecase.to_fill_in(config, ["layers", "use_cases.x"])
    assert todo == [
        "layers.l.systems.s.base_url: https://x.example.com/ (a placeholder: set your own)",
        "use_cases.x.secret_name: x/s (create this secret in Secrets Manager)",
    ]


def test_add_refuses_another_interface_version_and_copies_nothing(
    checkout, source, capsys
):
    root, _ = checkout
    text = (source / "manifest.yaml").read_text()
    (source / "manifest.yaml").write_text(
        text.replace(
            f"platform_interface: {PLATFORM_INTERFACE_VERSION}",
            "platform_interface: 2",
        )
    )
    _git(source, "commit", "--quiet", "-am", "older")
    assert run(root, "add", str(source)) == 1
    err = capsys.readouterr().err
    assert "'invoice-match' declares platform_interface 2" in err
    assert f"implements interface version {PLATFORM_INTERFACE_VERSION}" in err
    assert not (root / "invoice-match").exists()


def test_add_refuses_a_use_case_already_there(checkout, source, capsys):
    root, _ = checkout
    assert run(root, "add", str(source)) == 0
    assert run(root, "add", str(source)) == 1
    assert "already exists" in capsys.readouterr().err


def test_remove_refuses_while_platform_yaml_names_it_then_removes(
    checkout, source, capsys
):
    root, manifest = checkout
    assert run(root, "add", str(source)) == 0
    assert run(root, "remove", "invoice-match") == 1
    assert "still names invoice-match at use_cases.invoice-match" in (
        capsys.readouterr().err
    )
    config = yaml.safe_load(manifest.read_text())
    del config["use_cases"]["invoice-match"]
    manifest.write_text(yaml.safe_dump(config))
    assert run(root, "remove", "invoice-match") == 0
    assert not (root / "invoice-match").exists()


def test_new_lists_its_use_case_in_gitignore_and_remove_takes_it_out(checkout):
    root, manifest = checkout
    gitignore = root.parent / ".gitignore"
    assert run(root, "new", "release-notes") == 0
    assert "!use-cases/release-notes/\n" in gitignore.read_text()
    manifest.write_text((REPO / "presets" / "greenfield.yaml").read_text())
    assert run(root, "remove", "release-notes") == 0
    assert "release-notes" not in gitignore.read_text()


def test_gitignore_keeps_every_shipped_use_case_tracked_and_ignores_a_new_one():
    # Only what is on disk: CI's without-use-cases job deletes shipped folders.
    def on_disk(*args: str) -> list[str]:
        return [
            p
            for p in _git(REPO, "ls-files", *args, "use-cases").split()
            if (REPO / p).exists() or (REPO / p).is_symlink()
        ]

    tracked = on_disk()
    shipped = {Path(p).parts[1] for p in tracked if len(Path(p).parts) > 2}
    assert shipped and shipped <= {p.name for p in USE_CASES_DIR.iterdir()}
    # No tracked file matches an ignore rule, so a new file beside it is not ignored.
    assert on_disk("-ci", "--exclude-standard") == []
    probes = [f"use-cases/{name}/new-file.py" for name in sorted(shipped)]
    ignored = subprocess.run(  # nosec B603 B607
        ["git", "check-ignore", "--no-index", *probes, "use-cases/dropped-in/x.py"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,  # exits 1 when nothing is ignored
    ).stdout.split()
    assert ignored == ["use-cases/dropped-in/x.py"]


def test_structure_check_reads_a_dropped_in_use_cases_imports(tmp_path):
    structure = usecase._script("check_repository_structure")
    folder = tmp_path / "use-cases" / "dropped" / "lib"
    folder.mkdir(parents=True)
    (folder / "bad.py").write_text("import stacks.gateway_stack\n")
    (tmp_path / "use-cases" / "dropped" / "node_modules").mkdir()
    (tmp_path / "use-cases" / "dropped" / "node_modules" / "x.py").write_text("")
    found = structure.dropped_in_paths([], tmp_path)
    assert [p.as_posix() for p in found] == ["use-cases/dropped/lib/bad.py"]
    errors = structure.check_dependency_boundaries(found, tmp_path)
    assert errors and "stacks" in errors[0]


def test_remove_finds_every_place_platform_yaml_names_the_use_case():
    config = {
        "use_cases": {"x-uc": {}, "other": {}},
        "layers": {"l": {"access": {"x-uc": {"read": "*"}}}},
        "gateway": {"interceptor_extensions": [{"path": "use-cases/x-uc/check"}]},
    }
    assert usecase._names_of(config, "x-uc") == [
        "use_cases.x-uc",
        "layers.l.access.x-uc",
        "gateway.interceptor_extensions[0].path (use-cases/x-uc/check)",
    ]
