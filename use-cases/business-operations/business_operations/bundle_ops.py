# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""`ops.py bundle …` and `ops.py add-group`: move packs between environments over IAM.

Every verb invokes the stack's `pack_composer` directly, the same writer the console's
routes invoke, so nothing here writes a bucket. The composer knows its own
`promotion.role` and enforces it; this module only reads files and prints results.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from business_operations.names import NAME

UC = Path(__file__).resolve().parents[1]

USAGE = """usage: ops.py bundle export <pack_id> [-o file | --to-dir packs_dir] [--note text]
       ops.py bundle import <file|pack dir> [--change-ref X] [--apply] [--force] [--yes]
       ops.py bundle wait <import_id>
       ops.py bundle versions <pack_id>
       ops.py bundle status <pack_id>
       ops.py bundle rollback <pack_id> --to N [--change-ref X] [--apply]
       ops.py bundle retire <pack_id> [--change-ref X] [--apply]
import, rollback and retire are dry runs unless --apply; an --apply with --force asks
first, unless --yes. --to-dir writes the export as <packs_dir>/<pack_id>/, the
directory form `bundle import` reads back."""

_VALUED = {"-o", "--note", "--change-ref", "--to", "--to-dir"}
_FLAGS = {"--apply", "--force", "--json", "--yes"}
_GROUP = re.compile(rf"^{NAME}-[a-z0-9-]{{1,80}}$")


def confirmed(args: list[str], question: str) -> bool:
    """`--yes` or NON_INTERACTIVE=1 (deploy.sh's rule), else a [y/N] on a terminal."""
    if "--yes" in args or os.environ.get("NON_INTERACTIVE") == "1":
        return True
    if not sys.stdin.isatty():
        print(f"FAIL: {question} needs a terminal, or --yes", file=sys.stderr)
        return False
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def _bundle_module():
    layer = str(UC / "api" / "layer" / "python")
    if layer not in sys.path:
        sys.path.insert(0, layer)
    import bundle

    return bundle


def parse(argv: list[str]) -> tuple[list[str], dict] | None:
    """`(positional, options)`, or None on an unknown or valueless option."""
    positional, options = [], {}
    it = iter(argv)
    for arg in it:
        if arg in _VALUED:
            value = next(it, None)
            if value is None:
                return None
            options[arg] = value
        elif arg in _FLAGS:
            options[arg] = True
        elif arg.startswith("-"):
            return None
        else:
            positional.append(arg)
    return positional, options


def _deployment() -> tuple[str, str, str | None]:
    return (
        os.environ.get("PROJECT_NAME", "agentcore-workshop"),
        os.environ.get("ENVIRONMENT", "dev"),
        os.environ.get("AWS_REGION"),
    )


def _actor(region: str | None) -> str:
    import boto3

    try:
        return boto3.client("sts", region_name=region).get_caller_identity()["Arn"]
    except Exception:  # noqa: BLE001 - the record says unknown rather than failing
        return "unknown"


def _invoke(payload: dict) -> dict:
    import boto3
    from botocore.config import Config

    project, env, region = _deployment()
    name = f"/{project}/{env}/use-cases/{NAME}/pack-composer-function"
    fn = boto3.client("ssm", region_name=region).get_parameter(Name=name)["Parameter"][
        "Value"
    ]
    # No retry: a retried apply after a timeout would import twice. `bundle versions`
    # reads the audit trail when the answer was lost.
    lam = boto3.client(
        "lambda",
        region_name=region,
        config=Config(read_timeout=300, retries={"total_max_attempts": 1}),
    )
    out = lam.invoke(
        FunctionName=fn,
        Payload=json.dumps({**payload, "actor": _actor(region)}).encode(),
    )
    result = json.loads(out["Payload"].read() or b"{}")
    if out.get("FunctionError"):
        return {"status": 502, "error": "The composer failed", "detail": str(result)}
    return result


def _load(source: str) -> dict:
    path = Path(source)
    # `deploy.sh usecase run` starts at the repository root, so `packs/<id>` means the
    # use case's own packs, as the walkthrough writes it.
    if not path.exists() and (UC / source).exists():
        path = UC / source
    if path.is_dir():
        return _bundle_module().from_dir(path)
    return json.loads(path.read_text(encoding="utf-8"))


def _report(result: dict, as_json: bool) -> int:
    """Print the composer's answer; exit 1 on a refusal."""
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    status = int(result.get("status") or 200)
    if status >= 400:
        detail = f" {result['detail']}" if result.get("detail") else ""
        print(f"FAIL: {result.get('error')}.{detail}", file=sys.stderr)
        if not as_json:
            for key in ("diff", "warnings"):
                if result.get(key):
                    print(f"  {key}: {json.dumps(result[key])}", file=sys.stderr)
        return 1
    if not as_json:
        shown = {k: v for k, v in result.items() if k not in ("bundle", "status")}
        print(json.dumps(shown, indent=2, sort_keys=True))
    return 0


def _write_bundle(b: dict, out: str | None) -> str:
    version = b.get("version")
    path = Path(
        out or f"{b['pack_id']}@{'live' if version is None else version}.bundle.json"
    )
    path.write_text(json.dumps(b, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return str(path)


def _write_dir(b: dict, packs_dir: str) -> str:
    """The checkout form, `<packs_dir>/<pack_id>/`; never over an existing directory,
    whose stale SOPs `from_dir` would carry into the next import."""
    path = Path(packs_dir) / b["pack_id"]
    if path.exists() and any(path.iterdir()):
        raise FileExistsError(f"{path} is not empty; move it aside first")
    _bundle_module().to_dir(b, path)
    return f"{path}/"


def bundle_cmd(argv: list[str]) -> int:
    parsed = parse(argv[1:]) if argv else None
    verb = argv[0] if argv else ""
    if parsed is None or not parsed[0]:
        print(USAGE, file=sys.stderr)
        return 2
    (subject, *rest), opts = parsed
    if rest:
        print(USAGE, file=sys.stderr)
        return 2
    as_json = bool(opts.get("--json"))
    # A forced apply skips the version chain.
    if (
        opts.get("--apply")
        and opts.get("--force")
        and not confirmed(argv, "Apply with --force, over the live pack's checks?")
    ):
        return 1
    common = {
        "dry_run": not opts.get("--apply"),
        "change_ref": opts.get("--change-ref"),
        "force": bool(opts.get("--force")),
    }
    try:
        if verb == "export":
            result = _invoke(
                {
                    "verb": "export",
                    "pack_id": subject,
                    "change_note": opts.get("--note"),
                }
            )
            if "bundle" in result:
                b = result["bundle"]
                written = (
                    _write_dir(b, opts["--to-dir"])
                    if opts.get("--to-dir")
                    else _write_bundle(b, opts.get("-o"))
                )
                print(
                    f"wrote {written}: v{b.get('version')} {b.get('sha256') or '(live, unversioned)'}"
                )
            return _report(result, as_json)
        if verb == "import":
            return _report(
                _invoke({"verb": "import", "bundle": _load(subject), **common}), as_json
            )
        if verb == "wait":
            return _report(
                _invoke({"verb": "import_status", "import_id": subject}), as_json
            )
        if verb in ("versions", "status"):
            return _report(_invoke({"verb": verb, "pack_id": subject}), as_json)
        if verb == "rollback":
            if not str(opts.get("--to", "")).isdigit():
                print("FAIL: rollback needs --to <version>", file=sys.stderr)
                return 2
            payload = {
                "verb": "rollback",
                "pack_id": subject,
                "to": int(opts["--to"]),
                **common,
            }
            return _report(_invoke(payload), as_json)
        if verb == "retire":
            return _report(
                _invoke({"verb": "retire", "pack_id": subject, **common}), as_json
            )
    except Exception as e:  # noqa: BLE001 - no creds, no stack: say so
        print(f"FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(USAGE, file=sys.stderr)
    return 2


PACK_USAGE = "usage: ops.py pack status [<pack_id>] [--json]"


def checkout_drift(row: dict, packs_dir: Path = UC / "packs") -> list[str] | None:
    """The files of the checkout's `packs/<id>` that differ from what is live, or None
    when the checkout has no such pack."""
    path = packs_dir / row["pack_id"]
    if not (path / "pack.json").is_file():
        return None
    ours, live = (
        _bundle_module().file_hashes(_bundle_module().from_dir(path)),
        row["files"],
    )
    return sorted(k for k in set(ours) | set(live) if ours.get(k) != live.get(k))


def status_facts(row: dict) -> list[str]:
    """One pack's state as short phrases, in the order `pack status` prints them."""
    active = row["active"]
    origin = row.get("origin") or {}
    version = f"v{active['version']}" + (
        "" if active.get("stored") else " (not stored)"
    )
    came = " ".join(
        str(x) for x in (origin.get("via"), (origin.get("at") or "")[:16]) if x
    )
    if origin.get("by"):
        came += f" by {origin['by']}"
    if origin.get("change_ref"):
        came += f" ({origin['change_ref']})"
    facts = [f"{version} from {came or 'unknown'}"]
    if active.get("retired"):
        facts.append("retired")
    elif unroutable := row.get("unroutable"):
        # Why it routes nothing, not just that it does: the survey fields still to fill
        # in, or the SAP tools this deployment does not serve.
        facts.append(f"routable: false, {'; '.join(unroutable)}")
    else:
        facts.append(f"routes {', '.join(row.get('routes') or []) or 'nothing'}")
    checkout = row.get("checkout")
    if checkout is not None:
        facts.append(
            f"checkout differs: {', '.join(checkout)}"
            if checkout
            else "checkout matches"
        )
    facts.append(f"search {row.get('search') or 'unknown'}")
    if mode := row.get("trigger_mode"):
        facts.append(
            f"trigger mode {mode.get('pack') or 'manual (unset)'} "
            f"(deployment {mode.get('deployment') or 'manual (unset)'})"
        )
    refusal = row.get("last_refusal")
    facts.append(
        f"last refused {(refusal.get('at') or '')[:16]} {refusal.get('verb')}: "
        f"{refusal.get('error')}"
        if refusal
        else "no refusals"
    )
    return facts


def value_lines(row: dict) -> list[str]:
    """Each `{{SYMBOL}}` the pack's SOPs can cite, with the value a case resolves it to
    here and where that comes from: `pack v7`, `stack override (lowered from …)`,
    `platform.yaml` or `stack contact override`, and the enabled write guards that read
    it."""
    return [
        f"{symbol} = {v.get('value')} ({v.get('kind')}, {v.get('source')}"
        + (f", guarded by {', '.join(v['guarded_by'])}" if v.get("guarded_by") else "")
        + ")"
        for symbol, v in sorted((row.get("values") or {}).items())
    ]


def _trigger_mode_reader():
    """`pack_id -> {pack, deployment}`, each the stored mode or None for unset; None
    when the parameters cannot be read, so `pack status` prints what it can."""
    try:
        import boto3

        from business_operations import names

        project, env, region = _deployment()
        params = names.trigger_mode_parameters(f"/{project}/{env}")
        ssm = boto3.client("ssm", region_name=region)
    except Exception:  # noqa: BLE001 - no deployment named: leave the fact out
        return None

    def read(name: str) -> str | None:
        try:
            return ssm.get_parameter(Name=name)["Parameter"]["Value"]
        except Exception:  # noqa: BLE001 - absent or unreadable reads as unset
            return None

    switch = read(params["switch"])
    return lambda pack_id: {
        "pack": read(f"{params['packs']}/{pack_id}"),
        "deployment": switch,
    }


def pack_cmd(argv: list[str]) -> int:
    """`ops.py pack status [<pack_id>]`: the composer's read-only `status`, with the
    checkout compared. One line per pack with no argument, one fact per line with one."""
    parsed = parse(argv[1:]) if argv[:1] == ["status"] else None
    if parsed is None or len(parsed[0]) > 1:
        print(PACK_USAGE, file=sys.stderr)
        return 2
    ids, opts = parsed
    try:
        result = _invoke({"verb": "status", **({"pack_id": ids[0]} if ids else {})})
    except Exception as e:  # noqa: BLE001 - no creds, no stack: say so
        print(f"FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    if int(result.get("status") or 200) >= 400:
        return _report(result, bool(opts.get("--json")))
    rows = result["packs"] if not ids else [result]
    modes = _trigger_mode_reader()
    for row in rows:
        row["checkout"] = checkout_drift(row)
        if modes is not None:
            row["trigger_mode"] = modes(row["pack_id"])
    if opts.get("--json"):
        print(json.dumps(rows if not ids else rows[0], indent=2, sort_keys=True))
    elif ids:
        lines = [*status_facts(rows[0]), *value_lines(rows[0])]
        print("\n".join([rows[0]["pack_id"], *(f"  {f}" for f in lines)]))
    else:
        for row in rows:
            print(f"{row['pack_id']}: {'; '.join(status_facts(row))}")
        if not rows:
            print("no packs")
    return 0


def add_group(argv: list[str]) -> int:
    """Create a group a bundle requires on the shared pool. Never part of an import."""
    import boto3

    if len(argv) != 1 or not _GROUP.match(argv[0]):
        print(f"usage: ops.py add-group {NAME}-<name>", file=sys.stderr)
        return 2
    group = argv[0]
    project, env, region = _deployment()
    ssm = boto3.client("ssm", region_name=region)
    try:
        mode = ssm.get_parameter(Name=f"/{project}/{env}/auth/mode")["Parameter"][
            "Value"
        ]
        if mode == "direct":
            print(
                f"identity.mode direct: create the app role {group} on the platform app "
                "in Entra instead.",
                file=sys.stderr,
            )
            return 2
        pool = ssm.get_parameter(Name=f"/{project}/{env}/auth/user-pool-id")[
            "Parameter"
        ]["Value"]
        boto3.client("cognito-idp", region_name=region).create_group(
            UserPoolId=pool,
            GroupName=group,
            Description=f"{NAME}: created by add-group {datetime.now(UTC):%Y-%m-%d}",
        )
    except Exception as e:  # noqa: BLE001 - an existing group, no creds: say so
        print(f"FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(f"OK: {group} created in {pool}")
    return 0
