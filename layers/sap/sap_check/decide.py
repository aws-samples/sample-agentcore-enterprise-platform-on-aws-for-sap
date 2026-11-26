# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The SAP layer's interceptor check, as a pure decision.

`__init__.py` does the reads and hands them in, so every branch is a unit test. The
platform interceptor runs it for the SAP targets only, before Cedar, for every
caller. It refuses:

1. `odata_delete` on any SAP target, for everyone.
2. A target the layer does not serve, or a tool it does not know (fail closed).
3. A call when the claims cannot be read.
4. A caller no claim names: no use case claims SAP access for this client.
5. A caller more than one claim names: ownership must be unambiguous.
6. A system or operation the caller's claim, capped by the operator's ceiling for
   that use case, does not cover.
7. A write whose claim names no governing check, unless Cedar enforces: then the
   use case's write permit is the gate. Under LOG_ONLY nothing else would stop it.

Otherwise it allows, and the claiming use case's own check, if any, decides next.
"""

from __future__ import annotations

from collections.abc import Callable

READ_TOOLS = frozenset(
    {
        "find_sap_services",
        "get_metadata",
        "get_service_hints",
        "odata_read",
        "odata_count",
    }
)
WRITE_OPS = {
    "odata_create": "create",
    "odata_update": "update",
    "odata_function_import": "function_import",
}
DELETE = "odata_delete"
#: sap_layer.names TARGET_PREFIX and LEGACY_TARGET_PREFIX. Any other target is not the
#: layer's, so its call passes here without a read, whatever prefix the check runs on.
TARGET_PREFIXES = ("sap-", "business-operations-sap-")


def split(tool: str) -> tuple[str, str]:
    """`<target>___<tool>` to (target, tool)."""
    target, _, name = tool.partition("___")
    return (target, name) if name else ("", target)


def decide(
    *,
    tool: str,
    settings: Callable[[], dict],
    claims: Callable[[], list[dict]],
    caller: Callable[[], str],
) -> str | None:
    """The refusal reason for this call, or None to let it through.

    `settings` is the layer's `check` parameter, `claims` every claim of a use case
    the settings name, `caller` the calling app's id. Callables, so a call refused
    on its name alone reads nothing.
    """
    target, name = split(tool)
    if not target.startswith(TARGET_PREFIXES):
        return None
    if name == DELETE:
        return "odata_delete is not available through the SAP layer"
    s = settings()
    system = s["targets"].get(target)
    if system is None:
        return f"'{target}' is not a SAP target the SAP layer serves"
    op = "read" if name in READ_TOOLS else WRITE_OPS.get(name)
    if op is None:
        return f"'{name}' is not a SAP tool the SAP layer offers"

    who = caller()
    mine = [c for c in claims() if who in (c.get("callers") or [])]
    if not mine:
        return "no use case claims SAP access for this client"
    if len(mine) > 1:
        return (
            "more than one use case claims this client; SAP access must have one owner"
        )
    (claim,) = mine
    use_case = claim.get("use_case")
    ceiling = s["ceilings"].get(use_case) or {}
    if op == "read":
        covered = system in (claim.get("read") or []) and system in ceiling.get(
            "read", []
        )
    else:
        covered = op in ((claim.get("write") or {}).get(system) or []) and op in (
            ceiling.get("write", {}).get(system) or []
        )
    if not covered:
        return f"use case '{use_case}' does not claim {op} on SAP system '{system}'"
    if (
        op != "read"
        and not claim.get("governed_by")
        and s.get("cedar_enforced") is not True
    ):
        return (
            f"use case '{use_case}' writes to SAP with no check of its own, which the "
            "layer allows only while Cedar enforces its write permit "
            "(security.cedar.mode: ENFORCE in platform.yaml)"
        )
    return None
