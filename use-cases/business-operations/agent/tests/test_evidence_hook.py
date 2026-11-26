# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The status the evidence hook records is what the trace, the console and the evals
read, so a call SAP refused must be recorded as the failure it is."""

import json
from types import SimpleNamespace

import pytest
from utils import hooks


def _call(text: str, status: str = "success") -> SimpleNamespace:
    return SimpleNamespace(
        tool_use={"toolUseId": "t1", "name": "odata_read", "input": {}},
        result={"toolUseId": "t1", "status": status, "content": [{"text": text}]},
    )


@pytest.mark.parametrize(
    "text,status,recorded",
    [
        (
            json.dumps({"success": False, "error_type": "authentication_required"}),
            "success",
            "error",
        ),
        (
            json.dumps({"error": "Notification channel unavailable: x"}),
            "success",
            "error",
        ),
        ("Refused by the Gateway: this run is a rehearsal", "error", "error"),
        (json.dumps({"success": True, "data": []}), "success", "success"),
    ],
    ids=["sap-401", "lambda-error", "runchecks", "ok"],
)
def test_a_refused_call_is_recorded_as_an_error(monkeypatch, text, status, recorded):
    monkeypatch.setenv("CEDAR_ENFORCEMENT_MODE", "OFF")
    hook = hooks.EvidenceHook()
    hook.on_tool_call(_call(text, status))

    assert hook.status_for("t1") == recorded
    assert hook.evidence_by_tool_call_id()["t1"]["evidence"]["authz"]["mode"] == "OFF"
