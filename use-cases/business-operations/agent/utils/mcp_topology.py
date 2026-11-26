# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Which Gateway tools a run holds, and the audit headers every Gateway call carries.

The Gateway is shared by every use case on the platform, so a tool is matched by its
full, target-prefixed name (`business-operations-case-management___get_case_state`),
never by the bare tool name another tenant's target may also use. A run only ever sees
this use case's own targets and the SAP layer's, where the layer holds it to its claim.

Pure stdlib so the unit suite can import it (basic_agent.py pulls in strands and mcp).
"""

from __future__ import annotations

#: Every target of this use case starts with one: its own, and the SAP layer's
#: (business_operations/names.py, layers/sap/lib/sap_layer/names.py).
TARGET_PREFIXES = ("business-operations-", "sap-")
TOOL_SEPARATOR = "___"
#: The SAP layer's targets, one per system: `sap-<system>`.
SAP_TARGET_PREFIX = "sap-"

# The bare tool names the agent must never hold, on any of our targets: the
# SOP management tools, which read, rewrite and delete SOPs across every pack. No target
# here serves them (SOP writes go through `/packs/{pack_id}/sops`); the denial stays so
# a target that
# ever does is never the agent's. A routed run passes its pack's `gateway_tools` as an
# allowlist, but a discovery run passes None, so this is the only thing that would
# withhold them there.
_AGENT_DENIED_TOOLS = frozenset(
    {"list_sops", "get_sop", "put_sop", "delete_sop", "sync_sops"}
)

#: The tools that change a document in SAP. Reused by `write_gate.py`, so a fifth one
#: cannot be withheld from a read-only run while the write guards ignore it.
#: `odata_delete` is here too although the SAP layer refuses it for every caller.
SAP_WRITE_TOOLS = frozenset(
    {"odata_create", "odata_update", "odata_delete", "odata_function_import"}
)

# Every tool that leaves a mark outside the run, withheld from a read-only run (a
# rehearsal, or a conversation turn), so the model is never offered them. The boundary
# is the run grant's `rehearsal` or `ask`, on which the run checks refuse the same tools
# (business_operations_check/run_checks.py).
_READ_ONLY_DENIED_TOOLS = SAP_WRITE_TOOLS | {
    "update_case_state",
    "demo_create_ticket",
    "send_notification",
}

# A discovery run (no routed pack) reads: it has no case to write for, so no SAP write
# and no notification.
_DISCOVERY_DENIED_TOOLS = SAP_WRITE_TOOLS | {"send_notification"}


#: The run grant's secret arrives from the invoker on this header (the runtime allowlists
#: it in business_operations/runtime.py) and goes to the Gateway on the next one, which
#: the run checks read and no target's header allowlist names. It is not an audit header: it
#: never reaches a target, the prompt, the trace or Memory.
RUNTIME_GRANT_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Run-Grant"
GATEWAY_GRANT_HEADER = "x-business-operations-run-grant"


def audit_headers(audit_context: dict | None) -> dict[str, str]:
    """The `x-audit-*` headers. The run checks refuse a gated call without the correlation id,
    which names the run grant the invoker wrote."""
    ctx = audit_context or {}
    return {
        f"x-audit-{name}": ctx[key]
        for name, key in (
            ("correlation-id", "correlation_id"),
            ("initiator", "initiator"),
            ("trigger", "trigger"),
        )
        if ctx.get(key)
    }


def permitted(
    full_name: str,
    allowed_tools: list[str] | None,
    read_only: bool = False,
    sap_system: str | None = None,
) -> bool:
    """Whether a run may hold the Gateway tool `full_name` (`<target>___<tool>`).

    A pack that names its `sap_system` holds that system's SAP tools and no other's.
    """
    target, sep, tool = full_name.partition(TOOL_SEPARATOR)
    if not sep or not target.startswith(TARGET_PREFIXES):
        return False
    if (
        sap_system
        and target.startswith(SAP_TARGET_PREFIX)
        and target != f"{SAP_TARGET_PREFIX}{sap_system}"
    ):
        return False
    denied = _AGENT_DENIED_TOOLS | (
        _READ_ONLY_DENIED_TOOLS if read_only else frozenset()
    )
    if allowed_tools is None:
        denied |= _DISCOVERY_DENIED_TOOLS
    if tool in denied:
        return False
    return allowed_tools is None or full_name in set(allowed_tools)


def bare_names(registered: list[str]) -> dict[str, str]:
    """`{full name: bare name}` for the Gateway tools the model sees by their bare name,
    which is what SOP text names.

    A name only one target serves is made bare. A name several serve keeps its target:
    every SAP system serves the same tools, and a run with no `sap_system` (a discovery
    run over two systems) is offered both. Raises when a bare name would replace a tool
    already registered under it.
    """
    by_bare: dict[str, list[str]] = {}
    for name in registered:
        _target, sep, bare = name.partition(TOOL_SEPARATOR)
        if sep:
            by_bare.setdefault(bare, []).append(name)
    for bare, full in by_bare.items():
        if bare in registered:
            raise ValueError(
                f"Gateway tool {full[0]!r} would replace the tool {bare!r}"
            )
    return {full[0]: bare for bare, full in by_bare.items() if len(full) == 1}


def build_tool_filters(
    allowed_tools: list[str] | None,
    read_only: bool = False,
    sap_system: str | None = None,
) -> dict:
    """Strands `tool_filters`: the pack's allowlist of full names, minus what is denied.

    `allowed_tools=None` means no pack was routed: every tool on this use case's own
    targets, minus the denylist, the SAP writes and send_notification, rather than
    nothing.
    """
    # An empty list grants nothing; only None (no pack routed) means the denylists alone.
    allowed = None if allowed_tools is None else list(allowed_tools)

    def permit(tool) -> bool:
        return permitted(tool.mcp_tool.name, allowed, read_only, sap_system)

    return {"allowed": [permit]}
