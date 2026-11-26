# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""build_tool_filters: what a run may load, as Strands applies it.

tests/test_business_operations_runtime.py covers `permitted()` name by name; this pins
the filter wrapper and the full read-only and denylist sets.
"""

import pytest
from utils.mcp_topology import build_tool_filters

SAP = "sap-s4___"
CASES = "business-operations-case-management___"


class _Tool:
    """Stands in for a strands MCPAgentTool: only the Gateway-qualified name matters."""

    def __init__(self, name):
        self.mcp_tool = type("_Spec", (), {"name": name})()


def _permits(allowed_tools, name, read_only=False):
    (permit,) = build_tool_filters(allowed_tools, read_only=read_only)["allowed"]
    return permit(_Tool(name))


def test_a_routed_pack_gets_the_tools_it_declares_and_no_others():
    allowed = [f"{CASES}get_case_state", f"{SAP}odata_read"]
    assert _permits(allowed, f"{CASES}get_case_state")
    assert _permits(allowed, f"{SAP}odata_read")
    assert not _permits(allowed, f"{SAP}odata_update")


def test_a_discovery_run_loads_every_own_tool_and_an_empty_list_none():
    assert _permits(None, f"{CASES}get_case_state")
    assert not _permits([], f"{SAP}odata_read")


@pytest.mark.parametrize(
    "tool",
    ["odata_create", "odata_update", "odata_delete", "odata_function_import",
     "send_notification"],
)  # fmt: skip
def test_a_discovery_run_writes_nothing_to_sap_and_sends_nothing(tool):
    assert not _permits(None, f"{SAP}{tool}")
    assert _permits([f"{SAP}{tool}"], f"{SAP}{tool}")
    assert _permits(None, f"{SAP}odata_read")


@pytest.mark.parametrize(
    "tool", ["list_sops", "get_sop", "put_sop", "delete_sop", "sync_sops"]
)
def test_the_sop_corpus_tools_are_denied_even_when_a_pack_names_them(tool):
    name = f"business-operations-sop-management___{tool}"
    assert not _permits(None, name)
    assert not _permits([name], name)


@pytest.mark.parametrize(
    "tool",
    [
        "odata_create",
        "odata_update",
        "odata_delete",
        "odata_function_import",
        "update_case_state",
        "demo_create_ticket",
        "send_notification",
    ],
)
def test_a_rehearsal_loads_no_tool_that_leaves_a_mark(tool):
    # Not even one the rehearsed pack declares: that pack is the one being rehearsed.
    assert not _permits([f"{SAP}{tool}"], f"{SAP}{tool}", read_only=True)
    assert _permits([f"{SAP}{tool}"], f"{SAP}{tool}")


@pytest.mark.parametrize("tool", ["odata_read", "odata_count", "get_metadata"])
def test_a_rehearsal_still_reads(tool):
    assert _permits(None, f"{SAP}{tool}", read_only=True)
