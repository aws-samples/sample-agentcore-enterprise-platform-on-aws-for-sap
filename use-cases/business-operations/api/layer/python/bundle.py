# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""A pack version as one JSON document: the unit a pack moves between environments in.

A **bundle** carries `pack.json` (without `$schema`), `base_prompt.txt`, the pack's SOPs
and its eval cases, plus the hashes that identify it. A checkout pack directory is the
same thing in another encoding: `from_dir` reads one and `to_dir` writes one.

**Sealed or a draft.** A bundle whose `version` is set, whose `sha256` matches its
content and whose `seal_sha256` matches everything else it carries (`version`,
`previous`, `base`, `provenance`, `requires`, `changes`) is *sealed*: DEV minted it, and it
may be imported anywhere. Anything else (a checkout directory, or a sealed bundle someone
edited, its `previous` included) is a *draft*, which only DEV imports, minting the next
version from it.

**Versions chain over published versions.** `previous` is the version DEV held when it minted this
one; `base` is the last version DEV *published* (exported) before it. Every DEV edit mints a
version and only an export travels, so another environment checks `base` against what it
holds (`chain_link`). A bundle minted before publishing existed carries no `base`, and its `previous`
is the link.

**No signature yet.** `signatures` stays an empty list; the hashes detect corruption and
an edit made by hand, not a deliberate forgery that recomputes them.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import registry
from pack_tiers import (
    CONTACT_PREFIX,
    SYMBOL_RE,
    TEXT_EXTENSIONS,
)

FORMAT = "bo-bundle/1"

#: The longest change reference an import records (P11).
CHANGE_REF_MAX = 128


def canonical(value) -> bytes:
    """One byte string per value, whatever the key order or whitespace it arrived in."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_sha(b: dict) -> str:
    """The bundle's identity: its content, never its version or provenance."""
    return sha256(
        canonical(
            {
                "pack": b.get("pack"),
                "base_prompt": b.get("base_prompt") or "",
                "sops": b.get("sops") or {},
                "evals": b.get("evals") or [],
                "retired": bool(b.get("retired")),
            }
        )
    )


#: What `seal_sha` leaves out: itself, and what is derived from what it covers.
_UNSEALED = ("seal_sha256", "sha256", "files", "signatures")


def seal_sha(b: dict) -> str:
    """Everything a sealed bundle says about itself, so an edited `previous` cannot
    skip versions without `force` and the audit cannot be told who exported it."""
    return sha256(canonical({k: v for k, v in b.items() if k not in _UNSEALED}))


def file_hashes(b: dict) -> dict[str, str]:
    """Per file, so drift and a diff can name the file rather than the bundle."""
    if b.get("retired"):
        return {}
    files = {"pack.json": sha256(canonical(b.get("pack")))}
    if b.get("base_prompt"):
        files["base_prompt.txt"] = sha256(b["base_prompt"].encode("utf-8"))
    for name, text in sorted((b.get("sops") or {}).items()):
        files[f"sops/{name}"] = sha256(text.encode("utf-8"))
    if b.get("evals"):
        files["evals.json"] = sha256(canonical(b["evals"]))
    return files


def named_tiers(cfg: dict) -> list[tuple[str, str]]:
    """Every `(key, tier)` the pack names; the agent and the write verifier read each."""
    verifier = cfg.get("verifier") if isinstance(cfg.get("verifier"), dict) else {}
    named = {
        "model_tier": cfg.get("model_tier", "sonnet"),
        "verifier.model_tier": verifier.get("model_tier"),
    }
    return [(k, t) for k, t in named.items() if isinstance(t, str)]


def requires(b: dict) -> dict[str, list[str]]:
    """The facts the target environment must supply. The SAP systems are not listed on
    their own: a missing system is missing tools, which `tools` already names."""
    pack = b.get("pack") or {}
    if b.get("retired"):
        return {"tools": [], "groups": [], "contacts": [], "model_tiers": []}
    groups = {
        str(g)
        for named in (pack.get("entitlement") or {}).values()
        if isinstance(named, list)
        for g in named
    }
    contacts = {
        s.removeprefix(CONTACT_PREFIX).lower()
        for text in (b.get("sops") or {}).values()
        for s in SYMBOL_RE.findall(text)
        if s.startswith(CONTACT_PREFIX)
    }
    return {
        "tools": sorted(set(pack.get("gateway_tools") or [])),
        "groups": sorted(groups),
        "contacts": sorted(contacts),
        "model_tiers": sorted({t for _, t in named_tiers(pack)}),
    }


def unsealed(pack_id: str, pack, base_prompt: str, sops: dict, evals: list) -> dict:
    """A draft: content with no version. What `from_dir` and a live snapshot return."""
    if isinstance(pack, dict):
        pack = {k: v for k, v in pack.items() if k != "$schema"}
    return {
        "format": FORMAT,
        "pack_id": pack_id,
        "version": None,
        "previous": None,
        "schema": registry.SCHEMA,
        "pack": pack,
        "base_prompt": base_prompt or "",
        "sops": dict(sorted((sops or {}).items())),
        "evals": list(evals or []),
        "signatures": [],
    }


def seal(
    b: dict,
    version: int,
    previous: dict | None,
    provenance: dict,
    changes=None,
    base: dict | None = None,
) -> dict:
    """Stamp a version onto content; the hashes are recomputed, never trusted."""
    sealed = {**b, "version": version, "previous": previous, "base": base}
    sealed["requires"] = requires(sealed)
    sealed["sha256"] = content_sha(sealed)
    sealed["files"] = file_hashes(sealed)
    sealed["provenance"] = provenance
    sealed["changes"] = changes or {}
    sealed["signatures"] = []
    sealed["seal_sha256"] = seal_sha(sealed)
    return sealed


def chain_link(b: dict) -> dict | None:
    """`{version, sha256}` of the version an importing environment must hold: `base`,
    or `previous` for a bundle minted before publishing existed. None when it names none."""
    link = b["base"] if "base" in b else b.get("previous")
    if isinstance(link, dict) and {"version", "sha256"} <= link.keys():
        return {"version": link["version"], "sha256": link["sha256"]}
    return None


def is_sealed(b: dict) -> bool:
    return (
        isinstance(b.get("version"), int)
        and not isinstance(b.get("version"), bool)
        and b.get("sha256") == content_sha(b)
        and b.get("seal_sha256") == seal_sha(b)
    )


def shape_errors(b: object) -> list[str]:
    """What makes a document not a bundle at all (import checks 0 and 2)."""
    if not isinstance(b, dict):
        return ["A bundle is a JSON object"]
    if b.get("format") != FORMAT:
        return [f"format is {b.get('format')!r}, this code reads {FORMAT!r}"]
    errors = []
    if b.get("schema") != registry.SCHEMA:
        errors.append(
            f"The bundle targets schema {b.get('schema')!r}; this code reads "
            f"{registry.SCHEMA!r}. Deploy the code the bundle needs first."
        )
    if not b.get("retired") and not isinstance(b.get("pack"), dict):
        errors.append("`pack` must be a JSON object")
    if not isinstance(b.get("base_prompt") or "", str):
        errors.append("`base_prompt` must be a string")
    sops = b.get("sops") or {}
    if not isinstance(sops, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in sops.items()
    ):
        errors.append("`sops` must map a file name to its text")
    else:
        for name in sops:
            if "/" in name or not name.endswith(TEXT_EXTENSIONS) or name[:1] == ".":
                errors.append(
                    f"SOP {name!r}: a SOP is a {' or '.join(TEXT_EXTENSIONS)} file "
                    "directly under the pack"
                )
    if not isinstance(b.get("evals") or [], list):
        errors.append("`evals` must be a list")
    return errors


def diff(old: dict | None, new: dict) -> dict:
    """What `new` changes against `old`: pack keys and files, added, removed, changed."""
    old_pack = (old or {}).get("pack") or {}
    new_pack = new.get("pack") or {}
    old_files = file_hashes(old) if old else {}
    new_files = file_hashes(new)

    def split(a: dict, b: dict) -> dict:
        return {
            "added": sorted(set(b) - set(a)),
            "removed": sorted(set(a) - set(b)),
            "changed": sorted(k for k in set(a) & set(b) if a[k] != b[k]),
        }

    return {"keys": split(old_pack, new_pack), "files": split(old_files, new_files)}


def from_dir(path: Path) -> dict:
    """A checkout pack directory as a draft bundle."""
    path = Path(path)
    pack = json.loads((path / "pack.json").read_text(encoding="utf-8"))
    prompt = path / "base_prompt.txt"
    sops = {
        p.name: p.read_text(encoding="utf-8")
        for p in sorted((path / "sops").glob("*"))
        if p.is_file()
    }
    evals_file = path / "evals.json"
    evals = (
        json.loads(evals_file.read_text(encoding="utf-8"))
        if evals_file.is_file()
        else []
    )
    return unsealed(
        path.name,
        pack,
        prompt.read_text(encoding="utf-8") if prompt.is_file() else "",
        sops,
        evals,
    )


def to_dir(b: dict, path: Path) -> None:
    """Write a bundle as a checkout pack directory (the inverse of `from_dir`)."""
    path = Path(path)
    (path / "sops").mkdir(parents=True, exist_ok=True)
    pack = {"$schema": "../pack.schema.json", **(b.get("pack") or {})}
    (path / "pack.json").write_text(json.dumps(pack, indent=2) + "\n", encoding="utf-8")
    if b.get("base_prompt"):
        (path / "base_prompt.txt").write_text(b["base_prompt"], encoding="utf-8")
    for name, text in (b.get("sops") or {}).items():
        (path / "sops" / name).write_text(text, encoding="utf-8")
    if b.get("evals"):
        (path / "evals.json").write_text(
            json.dumps(b["evals"], indent=2) + "\n", encoding="utf-8"
        )
