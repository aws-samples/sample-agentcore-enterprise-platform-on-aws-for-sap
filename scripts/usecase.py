#!/usr/bin/env python3

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Use-case scaffolding and operations: `deploy.sh usecase new|add|remove|list|run`.

    usecase.py new <name> [--owner X] [--summary "..."] [--requires a,b]
                          [--template DIR] [--manifest platform.yaml] [--dry-run]
    usecase.py add <git-url|path> [--ref REF]
    usecase.py remove <name>
    usecase.py list
    usecase.py run <name> <verb> [args...]
    usecase.py run --layer <name> <verb> [args...]

`new` copies use-cases/_template/ (or --template) to use-cases/<name>/ with the {{tokens}}
filled in (in file names too), then names the use case in platform.yaml's `use_cases:` block so the
next `deploy.sh build` deploys it and `deploy.sh verify` verifies it. The edit
to platform.yaml is textual, not a YAML round-trip: the file carries comments
worth keeping, and PyYAML would drop every one of them.

`add` copies a use case from another repository (or a folder) into
use-cases/<name>/, the name taken from its manifest, refusing one whose
platform_interface is not this platform's. It links its skills, writes the docs'
use-case blocks, and merges its platform.example.yaml into platform.yaml: it adds
the keys the file lacks and never changes one the operator set. That merge is a
YAML round-trip, so it saves the old file as platform.yaml.<time>.bak first,
comments included. It runs no code from the use case; the next synth does.
.gitignore keeps an added use case out of git; `new` lists its use case there.

`remove` is the repository side of removing a use case: it deletes the folder,
its skill links and its docs links. It refuses while platform.yaml still names
the use case, and never touches a deployment.

`run` hands an operator command to use-cases/<name>/ops.py, with the platform
settings exported the way deploy.sh exports them plus the use case's own
`use_cases.<name>` block as JSON in USE_CASE_CONFIG. With `--layer` (`deploy.sh
layer run`) it runs layers/<name>/ops.py instead, with `layers.<name>` in
LAYER_CONFIG.

Exit codes: 0 ok, 1 failure, 2 usage. Pure stdlib + the repo's own loader; no AWS.
"""

from __future__ import annotations

import argparse
import getpass
import importlib.util
import json
import os
import re
import shutil
import subprocess  # nosec B404 — git with a fixed argv, no shell
import sys
import tempfile
import time
from pathlib import Path

import yaml
from pydantic import ValidationError

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from infra_utils.platform_config import (
    LAYERS_DIR,
    PLATFORM_INTERFACE_VERSION,
    USE_CASES_DIR,
    PlatformConfig,
    UseCaseManifest,
    discover_layers,
    discover_use_cases,
    load_platform_config,
    resolve_region,
    to_env,
)

NAME_RE = re.compile(UseCaseManifest.model_fields["name"].metadata[0].pattern)
TEMPLATE = USE_CASES_DIR / "_template"
# Left out of a copied use case: version control, caches and build output.
NOT_COPIED = (
    ".git",
    ".venv",
    "__pycache__",
    "node_modules",
    "cdk.out",
    ".pytest_cache",
)


def fill(text: str, tokens: dict[str, str]) -> str:
    for key, value in tokens.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def scaffold(
    name: str,
    owner: str,
    summary: str,
    requires: list[str],
    root: Path,
    dry_run: bool,
    template: Path = TEMPLATE,
) -> list[Path]:
    """Copy template/ → root/<name>/ with tokens filled. Returns the files."""
    if not (template / "manifest.yaml").is_file():
        raise SystemExit(f"{template} is not a use-case template: no manifest.yaml")
    dest = root / name
    if dest.exists():
        raise SystemExit(
            f"use-cases/{name}/ already exists; pick another name or remove it"
        )
    tokens = {
        "name": name,
        "owner": owner,
        "summary": summary,
        "platform_interface": str(PLATFORM_INTERFACE_VERSION),
    }
    written: list[Path] = []
    for src in sorted(template.rglob("*")):
        rel = src.relative_to(template).as_posix()
        # A template's .ash.yaml is copied: the new use case owns the suppressions for
        # its own files, such as its test's secret NAME placeholders.
        if src.is_dir() or any(
            (p.startswith(".") and p != ".ash.yaml") or p == "__pycache__"
            for p in rel.split("/")
        ):
            continue
        text = fill(src.read_text(), tokens)
        if rel == "manifest.yaml" and requires:
            text = re.sub(
                r"^requires: \[.*\]$",
                f"requires: [{', '.join(requires)}]",
                text,
                flags=re.MULTILINE,
            )
        # File names take the tokens too: tests/test_{{name}}.py is unique per use case,
        # since every use case's tests/ shares one pytest session.
        target = dest / fill(rel, tokens)
        written.append(target)
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
            if rel == "verify.py":
                target.chmod(0o755)
    return written


def enable_in_manifest(name: str, manifest: Path, dry_run: bool) -> str:
    """Add `<name>: {}` under `use_cases:` textually. Returns what was (or would be) done."""
    entry = f"  {name}: {{}}\n"
    if not manifest.exists():
        return f"no {manifest.name} yet — add this when you create it:\n\nuse_cases:\n{entry}"
    text = manifest.read_text()
    if re.search(rf"^\s+{re.escape(name)}:", text, flags=re.MULTILINE):
        return f"{manifest.name} already names {name}"
    m = re.search(r"^use_cases:[ \t]*(\{\}|)[ \t]*(#.*)?$", text, flags=re.MULTILINE)
    following = text[m.end() :].lstrip("\n") if m else ""
    # Only an EMPTY block is rewritten here: `use_cases:` followed by an indented
    # entry is a populated block and takes the append path below.
    if m and (m.group(1) == "{}" or not following.startswith((" ", "\t"))):
        # `use_cases:` or `use_cases: {}` → the entry goes on the next line.
        head = (
            text[: m.start()]
            + "use_cases:"
            + (f"  {m.group(2)}" if m.group(2) else "")
            + "\n"
        )
        new = head + entry + text[m.end() :].lstrip("\n")
    elif re.search(r"^use_cases:", text, flags=re.MULTILINE):
        # A populated block: insert after the last indented line that follows it.
        lines = text.splitlines(keepends=True)
        start = next(i for i, l in enumerate(lines) if l.startswith("use_cases:"))
        end = start + 1
        while end < len(lines) and (
            lines[end].startswith((" ", "\t")) or not lines[end].strip()
        ):
            end += 1
        lines.insert(end, entry)
        new = "".join(lines)
    else:
        new = text.rstrip("\n") + f"\nuse_cases:\n{entry}"
    if not dry_run:
        manifest.write_text(new)
    return f"{'would add' if dry_run else 'added'} `{name}: {{}}` under use_cases: in {manifest.name}"


def cmd_new(a: argparse.Namespace) -> int:
    if not NAME_RE.match(a.name):
        print(
            f"invalid name {a.name!r}: lowercase, digits, hyphens, 3–41 chars, starts with a letter",
            file=sys.stderr,
        )
        return 2
    root = Path(a.use_cases_dir) if a.use_cases_dir else USE_CASES_DIR
    manifest = Path(os.environ.get("PLATFORM_CONFIG") or a.manifest)
    requires = (
        [r.strip() for r in a.requires.split(",") if r.strip()] if a.requires else []
    )
    files = scaffold(
        a.name, a.owner, a.summary, requires, root, a.dry_run, Path(a.template)
    )
    note = enable_in_manifest(a.name, manifest, a.dry_run)
    tracked = None if a.dry_run else list_in_gitignore(a.name, root, keep=True)
    if not a.dry_run:
        # Prove the result loads before telling the user it worked.
        discover_use_cases.cache_clear()
        found = discover_use_cases(root)
        if a.name not in found:
            return _fail(f"scaffolded but discover_use_cases() does not see {a.name}")
        # The manifest's use_cases validator resolves names against the REAL
        # use-cases/ dir, so this proof only makes sense when that is where we
        # scaffolded (tests point --use-cases-dir elsewhere and check discovery).
        if manifest.exists() and root == USE_CASES_DIR:
            try:
                load_platform_config(manifest)
            except Exception as exc:  # noqa: BLE001 — surface the validator's own message
                return _fail(
                    f"{manifest.name} no longer validates after the edit:\n{exc}"
                )
    verb = "Would create" if a.dry_run else "Created"
    print(f"{verb} use-cases/{a.name}/:")
    for f in files:
        print(f"  {f.relative_to(root.parent) if root.parent in f.parents else f}")
    print(note)
    if tracked:
        print(tracked)
    print(f"Its other platform.yaml entries: use-cases/{a.name}/platform.example.yaml")
    print("\nNext:")
    print("  ./scripts/deploy.sh design     # the plan now lists uc-" + a.name)
    print("  ./scripts/deploy.sh build      # deploys the platform and the use case")
    print(
        "  ./scripts/deploy.sh verify     # runs use-cases/"
        + a.name
        + "/verify.py last"
    )
    return 0


def cmd_list(a: argparse.Namespace) -> int:
    root = Path(a.use_cases_dir) if a.use_cases_dir else USE_CASES_DIR
    manifest = Path(os.environ.get("PLATFORM_CONFIG") or a.manifest)
    enabled = _enabled(manifest)
    found = discover_use_cases(root)
    if not found:
        print("no use cases under use-cases/ (run: deploy.sh usecase new <name>)")
        return 0
    w = max(len(n) for n in found)
    print(f"{'NAME'.ljust(w)}  ENABLED  OWNER        STACKS")
    for n, m in found.items():
        print(
            f"{n.ljust(w)}  {'yes' if n in enabled else 'no':7}  {m.owner[:12].ljust(12)} {', '.join(m.stacks)}"
        )
        print(f"{' ' * w}  {m.summary}")
    return 0


def _enabled(manifest: Path) -> set[str]:
    """The names under platform.yaml's use_cases:.

    Textual, not load_platform_config(): this must work on a manifest that does not
    validate yet, and must not resolve names against the real use-cases/ dir when
    tests point elsewhere."""
    if not manifest.exists():
        return set()
    block = re.search(
        r"^use_cases:\s*\n((?:[ \t]+.*\n?)*)", manifest.read_text(), flags=re.MULTILINE
    )
    if not block:
        return set()
    return set(
        re.findall(r"^[ \t]+([a-z][a-z0-9-]*):", block.group(1), flags=re.MULTILINE)
    )


def list_in_gitignore(name: str, root: Path, keep: bool) -> str | None:
    """Add (keep) or drop the `!use-cases/<name>/` line that keeps a use case in git.

    A use case written here is tracked; one copied in with `add` is not."""
    gitignore = root.parent / ".gitignore"
    text = gitignore.read_text() if gitignore.is_file() else ""
    anchor = "\nuse-cases/*\n"
    if anchor not in text:
        return None
    line = f"!use-cases/{name}/\n"
    if keep == (line in text):
        return None
    if keep:
        last = max(
            (m.end() for m in re.finditer(r"^!use-cases/.*\n", text, re.MULTILINE)),
            default=text.index(anchor) + len(anchor),
        )
        text = text[:last] + line + text[last:]
    else:
        text = text.replace(line, "")
    gitignore.write_text(text)
    if keep:
        return f"listed use-cases/{name}/ in .gitignore, so git tracks it"
    return f"took use-cases/{name}/ out of .gitignore"


def _script(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def refresh_links(root: Path) -> list[str]:
    """The steps after a use case arrives or leaves: `check_repository_structure.py
    --fix` (its skill links) and `gen_use_case_index.py` (the docs' links to it).
    Returns what --fix could not mend, such as two skills with one name."""
    checkout = root.parent
    errors = _script("check_repository_structure").check_skill_links(checkout, fix=True)
    discover_use_cases.cache_clear()
    for page, (text, new) in _script("gen_use_case_index").render(checkout).items():
        if text != new:
            page.write_text(new, encoding="utf-8")
    return errors


def merge_missing(base: dict, extra: dict, at: str = "") -> tuple[list[str], list[str]]:
    """Add to base each key of extra it lacks, recursing into mappings both have.

    Never changes a value base already has. Returns (added, kept): the dotted paths
    added, and those where base keeps a value that differs from extra's."""
    added: list[str] = []
    kept: list[str] = []
    for key, value in extra.items():
        path = f"{at}{key}"
        if base.get(key) is None:
            base[key] = value
            added.append(path)
        elif isinstance(value, dict) and isinstance(base[key], dict):
            more, same = merge_missing(base[key], value, path + ".")
            added += more
            kept += same
        elif base[key] != value:
            kept.append(path)
    return added, kept


def _leaves(value, at: str):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield from _leaves(inner, f"{at}.{key}")
    else:
        yield at, value


PLACEHOLDER = re.compile(r"example\.(com|org|net)|<[^>]+>|123456789012")


def to_fill_in(config: dict, added: list[str]) -> list[str]:
    """The added values an operator must replace: placeholders, and secrets to create."""
    todo = []
    for path in added:
        value = config
        for key in path.split("."):
            value = value[key]
        for leaf, v in _leaves(value, path):
            if leaf.rsplit(".", 1)[-1].endswith("secret_name"):
                todo.append(f"{leaf}: {v} (create this secret in Secrets Manager)")
            elif isinstance(v, str) and PLACEHOLDER.search(v):
                todo.append(f"{leaf}: {v} (a placeholder: set your own)")
    return todo


def merge_example(name: str, root: Path, manifest: Path) -> list[str]:
    """Merge use-cases/<name>/platform.example.yaml into platform.yaml. Returns the
    lines to print."""
    example_file = root / name / "platform.example.yaml"
    if not example_file.is_file():
        return [enable_in_manifest(name, manifest, dry_run=False)]
    shown = f"use-cases/{name}/platform.example.yaml"
    if not manifest.exists():
        return [
            f"no {manifest.name} yet: run `deploy.sh design`, then merge {shown} into it"
        ]
    config = yaml.safe_load(manifest.read_text()) or {}
    added, kept = merge_missing(config, yaml.safe_load(example_file.read_text()) or {})
    out = []
    if added:
        backup = manifest.with_name(
            f"{manifest.name}.{time.strftime('%Y%m%d-%H%M%S')}.bak"
        )
        shutil.copy2(manifest, backup)
        manifest.write_text(
            yaml.safe_dump(config, sort_keys=False, default_flow_style=False)
        )
        out.append(f"{manifest.name}: added {', '.join(added)} from {shown}.")
        out.append(
            f"  Its comments are not kept; {backup.name} is the file as it was, with them."
        )
    else:
        out.append(f"{manifest.name} already has every entry of {shown}.")
    if kept:
        out.append(
            f"  Kept your value at {', '.join(kept)}; the example has another. Compare them."
        )
    todo = to_fill_in(config, added)
    if todo:
        out.append("Still to fill in, in " + manifest.name + ":")
        out += [f"  {line}" for line in todo]
    out.append(f"{shown} explains each entry.")
    return out


def _fetch(source: str, ref: str, into: Path) -> Path:
    """The use case's folder: a plain local folder as it is; a repository, local or
    not, cloned, so only what is committed arrives."""
    folder = Path(source)
    if folder.is_dir() and not ref and not (folder / ".git").exists():
        return folder
    if source.startswith("-") or ref.startswith("-"):
        raise SystemExit(f"not a repository or ref: {source!r} {ref!r}")
    clone = into / "clone"
    steps = [["clone", "--quiet", "--", source, str(clone)]]
    if ref:
        steps.append(["-C", str(clone), "checkout", "--quiet", ref])
    for step in steps:
        argv = ["git", "-c", "advice.detachedHead=false", *step]
        if subprocess.run(argv, check=False).returncode:  # nosec B603 B607
            raise SystemExit(f"git could not fetch {source} {ref}".strip())
    return clone


def _first_errors(exc: ValidationError) -> str:
    return "\n".join(
        ".".join(map(str, e["loc"]))
        + ": " * bool(e["loc"])
        + e["msg"].removeprefix("Value error, ")
        for e in exc.errors()
    )


def cmd_add(a: argparse.Namespace) -> int:
    root = Path(a.use_cases_dir) if a.use_cases_dir else USE_CASES_DIR
    manifest = Path(os.environ.get("PLATFORM_CONFIG") or a.manifest)
    with tempfile.TemporaryDirectory() as tmp:
        src = _fetch(a.source, a.ref, Path(tmp))
        if not (src / "manifest.yaml").is_file():
            return _fail(
                f"{a.source} has no manifest.yaml at its root, so it is not a use case"
            )
        try:
            uc = UseCaseManifest.model_validate(
                yaml.safe_load((src / "manifest.yaml").read_text())
            )
        except ValidationError as exc:
            return _fail(
                f"{a.source}: its manifest.yaml does not load:\n{_first_errors(exc)}"
            )
        dest = root / uc.name
        if dest.exists():
            return _fail(f"use-cases/{uc.name}/ already exists; remove it first")
        shutil.copytree(
            src, dest, symlinks=True, ignore=shutil.ignore_patterns(*NOT_COPIED)
        )
    problems = refresh_links(root)
    if uc.name not in discover_use_cases(root):
        return _fail(f"copied, but discover_use_cases() does not see {uc.name}")
    print(
        f"Added use-cases/{uc.name}/ from {a.source}{' at ' + a.ref if a.ref else ''}."
    )
    print("Linked its skills and listed it in the docs' use-case blocks.")
    for problem in problems:
        print(f"  WARNING: {problem}")
    for line in merge_example(uc.name, root, manifest):
        print(line)
    if manifest.exists() and root == USE_CASES_DIR:
        try:
            load_platform_config(manifest)
        except Exception as exc:  # noqa: BLE001 — show the validator's own message
            print(
                f"\n{manifest.name} does not load yet; fix this before a build:\n{exc}"
            )
    entry = (
        f"use-cases/{uc.name}/{uc.docs.entry}" if uc.docs else f"use-cases/{uc.name}/"
    )
    print(f"\nRead {entry}, then:")
    print("  ./scripts/deploy.sh design     # the plan now lists its stacks")
    print("  ./scripts/deploy.sh build      # deploys the platform and the use case")
    print("  ./scripts/deploy.sh verify     # runs its verify.py last")
    gitignore = root.parent / ".gitignore"
    rules = gitignore.read_text() if gitignore.is_file() else ""
    if "\nuse-cases/*\n" in rules and f"!use-cases/{uc.name}/\n" not in rules:
        print(
            f"git ignores use-cases/{uc.name}/. To commit it to this repository, add "
            f"`!use-cases/{uc.name}/` to .gitignore."
        )
    return 0


def _names_of(value, name: str, at: str = "") -> list[str]:
    """Where a parsed platform.yaml names a use case: keys equal to its name, and
    paths into its folder, such as an interceptor extension's."""
    found = []
    if isinstance(value, dict):
        items = [(f"{at}{key}", key, inner) for key, inner in value.items()]
    elif isinstance(value, list):
        items = [(f"{at.removesuffix('.')}[{i}]", None, v) for i, v in enumerate(value)]
    else:
        folder = f"use-cases/{name}"
        if isinstance(value, str) and (
            value == folder or value.startswith(folder + "/")
        ):
            found.append(f"{at.removesuffix('.')} ({value})")
        return found
    for path, key, inner in items:
        if key == name:
            found.append(path)
        found += _names_of(inner, name, path + ".")
    return found


def cmd_remove(a: argparse.Namespace) -> int:
    root = Path(a.use_cases_dir) if a.use_cases_dir else USE_CASES_DIR
    manifest = Path(os.environ.get("PLATFORM_CONFIG") or a.manifest)
    if a.name not in discover_use_cases(root):
        return _fail(f"no use case {a.name!r} under use-cases/")
    named = (
        _names_of(yaml.safe_load(manifest.read_text()) or {}, a.name)
        if manifest.exists()
        else []
    )
    if named:
        return _fail(
            f"{manifest.name} still names {a.name} at {', '.join(named)}. If it is "
            "deployed, destroy its stacks first; then delete those entries and run this "
            "again (docs/USE_CASE_REMOVAL.md)"
        )
    shutil.rmtree(root / a.name)
    note = list_in_gitignore(a.name, root, keep=False)
    refresh_links(root)
    print(f"Removed use-cases/{a.name}/, its skill links and its links in the docs.")
    if note:
        print(note)
    return 0


def cmd_run(a: argparse.Namespace) -> int:
    kind, default, discover = (
        ("layer", LAYERS_DIR, discover_layers)
        if a.layer
        else ("use case", USE_CASES_DIR, discover_use_cases)
    )
    root = Path(a.use_cases_dir) if a.use_cases_dir else default
    found = discover(root)
    if a.name not in found:
        return _fail(f"no {kind} {a.name!r}; available: {', '.join(found) or 'none'}")
    ops = root / a.name / "ops.py"
    if not ops.is_file():
        return _fail(f"{root.name}/{a.name}/ has no ops.py, so it offers no commands")
    manifest = Path(os.environ.get("PLATFORM_CONFIG") or a.manifest)
    config = load_platform_config(manifest) if manifest.exists() else PlatformConfig()
    # Env wins, as in deploy.sh; resolve the region first so the schema
    # default never overrides the profile's.
    os.environ.setdefault("AWS_REGION", resolve_region())
    for key, value in to_env(config).items():
        os.environ.setdefault(key, value)
    if a.layer:
        os.environ["LAYER_CONFIG"] = json.dumps(config.layers.get(a.name) or {})
    else:
        os.environ["USE_CASE_CONFIG"] = json.dumps(config.use_cases.get(a.name) or {})
    venv = REPO / ".venv" / "bin" / "python"
    python = str(venv) if venv.exists() else sys.executable
    os.execv(python, [python, str(ops), a.verb, *a.args])  # nosec B606 — our own file


def _fail(msg: str) -> int:
    print(f"FAIL: {msg}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="usecase", description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--manifest",
        default=str(REPO / "platform.yaml"),
        help="platform.yaml to edit/read",
    )
    p.add_argument("--use-cases-dir", default="", help=argparse.SUPPRESS)  # tests
    sub = p.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new", help="scaffold a use case and enable it")
    n.add_argument("name")
    n.add_argument("--owner", default=getpass.getuser())
    n.add_argument("--summary", default="")
    n.add_argument(
        "--requires", default="", help="comma-separated core suffixes, default gateway"
    )
    n.add_argument(
        "--template",
        default=str(TEMPLATE),
        help="directory to copy, default use-cases/_template",
    )
    n.add_argument("--dry-run", action="store_true")
    n.set_defaults(fn=cmd_new)
    ad = sub.add_parser(
        "add", help="copy a use case from a repository or folder and merge its config"
    )
    ad.add_argument("source", help="git URL, or a path to a repository or a folder")
    ad.add_argument("--ref", default="", help="branch, tag or commit to check out")
    ad.set_defaults(fn=cmd_add)
    rm = sub.add_parser(
        "remove", help="delete use-cases/<name>/ and its links; not its deployment"
    )
    rm.add_argument("name")
    rm.set_defaults(fn=cmd_remove)
    ls = sub.add_parser(
        "list", help="discovered use cases and whether the manifest enables them"
    )
    ls.set_defaults(fn=cmd_list)
    r = sub.add_parser(
        "run", help="run an operator command from use-cases/<name>/ops.py"
    )
    r.add_argument("--layer", action="store_true", help="a layer's ops.py")
    r.add_argument("name")
    r.add_argument("verb")
    r.add_argument("args", nargs=argparse.REMAINDER)
    r.set_defaults(fn=cmd_run)
    try:
        a = p.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    if a.cmd == "new" and not a.summary:
        a.summary = f"{a.name} use case"
    try:
        return a.fn(a)
    except SystemExit as e:  # scaffold() raises with a message
        if isinstance(e.code, str):
            print(e.code, file=sys.stderr)
            return 1
        return int(e.code or 0)


if __name__ == "__main__":
    sys.exit(main())
