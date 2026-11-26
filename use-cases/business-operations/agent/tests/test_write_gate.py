# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The wrapper the write guards and the verifier ride on.

The amount a human must approve above is the run checks' (business_operations_check), at the
Gateway; this wrapper only carries the checks that need SAP reads or a model. Two
things have to hold, and both fail silently on their own:

  * a deployment where no pack declares a guard or a verifier wraps nothing;
  * the wrapper survives `ToolRegistry.process_tools`, or it deletes the write tools
    from the model's config and looks exactly like a guard that works.
"""

from unittest import mock

from utils import write_gate as wg

_CASE = "ap-bchzfkch6kxm6uwqvqe5hhlkvx"
_WRITE_TOOLS = ("odata_create", "odata_update", "odata_delete", "odata_function_import")


class _FakeTool:
    """Stands in for a renamed external-MCP write tool."""

    def __init__(self, name):
        self._agent_tool_name = name

    @property
    def tool_name(self):
        return self._agent_tool_name

    @property
    def tool_spec(self):
        return {"name": self._agent_tool_name}

    @property
    def tool_type(self):
        return "fake"


def _agent(*names):
    registry = {name: _FakeTool(name) for name in names}
    return registry, mock.Mock(tool_registry=mock.Mock(registry=registry))


def test_a_deployment_that_declares_no_guard_or_verifier_wraps_nothing(monkeypatch):
    monkeypatch.setattr(wg, "guards_by_pack", dict)
    monkeypatch.setattr(wg, "verifiers_by_pack", dict)
    registry, agent = _agent(*_WRITE_TOOLS)

    assert wg.wrap_write_gate_tools(agent, {"case_id": _CASE}) == 0
    assert all(isinstance(t, _FakeTool) for t in registry.values())


def test_only_the_sap_write_tools_are_wrapped(monkeypatch):
    monkeypatch.setattr(wg, "guards_by_pack", lambda: {"finance_ops": ["po_reference"]})
    monkeypatch.setattr(wg, "verifiers_by_pack", dict)
    registry, agent = _agent(*_WRITE_TOOLS, "odata_read", "get_case_state")

    assert wg.wrap_write_gate_tools(agent, {"case_id": _CASE}) == 4
    assert all(isinstance(registry[n], wg.WriteGateTool) for n in _WRITE_TOOLS)
    assert isinstance(registry["odata_read"], _FakeTool), "a read is not a write"
    assert isinstance(registry["get_case_state"], _FakeTool)


def test_a_write_that_kept_its_target_is_wrapped_too(monkeypatch):
    """Two SAP systems on one run keep their prefixes; neither write escapes the gate."""
    monkeypatch.setattr(wg, "guards_by_pack", lambda: {"finance_ops": ["po_reference"]})
    monkeypatch.setattr(wg, "verifiers_by_pack", dict)
    names = ("sap-s4___odata_update", "sap-ecc___odata_update", "sap-s4___odata_read")
    registry, agent = _agent(*names)

    assert wg.wrap_write_gate_tools(agent, {"case_id": _CASE}) == 2
    assert isinstance(registry["sap-s4___odata_read"], _FakeTool)


def test_a_wrapper_keeps_the_name_and_spec_the_model_sees():
    gate = wg.WriteGateTool(_FakeTool("odata_function_import"), _CASE)

    assert gate.tool_name == "odata_function_import"
    assert gate.tool_spec == {"name": "odata_function_import"}
    assert gate.is_dynamic is False


def test_a_wrapper_survives_being_re_registered_as_a_tool():
    # `ag_ui_strands` snapshots the registry and passes the values as `tools=` to the
    # per-thread clone that serves the turn. `process_tools` drops anything that is not
    # an `AgentTool` with only a log line, so a duck-typed wrapper would delete every
    # write tool from the model's config, which looks exactly like a guard working.
    from strands.tools.registry import ToolRegistry

    gate = wg.WriteGateTool(_FakeTool("odata_function_import"), _CASE)

    assert ToolRegistry().process_tools([gate]) == ["odata_function_import"]
