# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""business-operations: masked in what a run stores or sends, never in what the model reads.

`agent/utils/persist_mask.py` masks the trace, the case tool's free text, tickets
and notifications through the persistence guardrail, and withholds rather than
stores when the guardrail cannot be asked. The stacks create and grant that
guardrail exactly when `security.require_guardrails` is on.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from app_synth import synth_app

REPO = Path(__file__).resolve().parents[3]
UC = REPO / "use-cases" / "business-operations"
import importlib.util

# Loaded by path: `utils` is also scripts/utils.py on the suite's sys.path.
_spec = importlib.util.spec_from_file_location(
    "bo_persist_mask", UC / "agent" / "utils" / "persist_mask.py"
)
persist_mask = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(persist_mask)

ACCOUNT = "00012345678901"


class _Guardrail:
    def __init__(self, entities=(), fail=False):
        self.calls: list[str] = []
        self.entities, self.fail = entities, fail

    def apply_guardrail(self, **kw):
        if self.fail:
            raise RuntimeError("throttled")
        (block,) = kw["content"]
        text = block["text"]["text"]
        self.calls.append(text)
        found = [
            {"type": t, "match": m, "action": "ANONYMIZED"}
            for t, m in self.entities
            if m in text
        ]
        if not found:
            return {"action": "NONE"}
        return {
            "action": "GUARDRAIL_INTERVENED",
            "assessments": [{"sensitiveInformationPolicy": {"piiEntities": found}}],
        }


@pytest.fixture
def guardrail(monkeypatch):
    g = _Guardrail([("US_BANK_ACCOUNT_NUMBER", ACCOUNT)])
    monkeypatch.setenv("PERSIST_GUARDRAIL_ID", "gr-persist")
    monkeypatch.setattr(persist_mask, "_client", g)
    return g


def test_without_the_guardrail_nothing_is_masked_or_asked(monkeypatch):
    monkeypatch.delenv("PERSIST_GUARDRAIL_ID", raising=False)
    monkeypatch.setattr(persist_mask, "_client", _Guardrail(fail=True))
    assert persist_mask.mask([f"acct {ACCOUNT}"]) == [f"acct {ACCOUNT}"]


def test_a_bank_account_is_masked_in_its_own_text_only(guardrail):
    out = persist_mask.mask([f"pay to {ACCOUNT}", "invoice 5105600123", None])
    assert out == ["pay to {US_BANK_ACCOUNT_NUMBER}", "invoice 5105600123", None]
    assert len(guardrail.calls) == 1


def test_a_failed_call_withholds_and_never_stores_the_value(monkeypatch):
    monkeypatch.setenv("PERSIST_GUARDRAIL_ID", "gr-persist")
    monkeypatch.setattr(persist_mask, "_client", _Guardrail(fail=True))
    assert persist_mask.mask([f"acct {ACCOUNT}", ""]) == [persist_mask.WITHHELD, ""]


def test_a_match_across_two_texts_withholds(monkeypatch):
    monkeypatch.setenv("PERSIST_GUARDRAIL_ID", "gr-persist")
    monkeypatch.setattr(
        persist_mask, "_client", _Guardrail([("US_BANK_ACCOUNT_NUMBER", "1\n2")])
    )
    assert persist_mask.mask(["1", "2"]) == [persist_mask.WITHHELD] * 2


def test_long_text_is_split_into_calls_under_the_cap(guardrail):
    big = "x" * (persist_mask._CHUNK_CHARS // 2 + 1)
    persist_mask.mask([big, big, big])
    assert len(guardrail.calls) == 3


def test_a_trace_is_masked_in_its_free_text_and_nowhere_else(guardrail):
    trace = {
        "trace_id": ACCOUNT,
        "prompt": f"Bank change to {ACCOUNT}",
        "segments": [
            {"type": "text", "content": f"The new account is {ACCOUNT}."},
            {
                "type": "tool",
                "tool_name": "get_case_state",
                "tool_input": "{}",
                "tool_result": json.dumps({"BankAccount": ACCOUNT}),
                "evidence": {"rows": [{"BankAccount": ACCOUNT}]},
            },
        ],
    }
    persist_mask.mask_record(trace)
    stored = json.dumps(trace)
    assert stored.count(ACCOUNT) == 1  # the trace id, which is not free text
    assert trace["segments"][1]["type"] == "tool"
    assert trace["segments"][1]["evidence"]["rows"][0]["BankAccount"] == (
        "{US_BANK_ACCOUNT_NUMBER}"
    )


# ── the stacks ──


def _synth(require_guardrails: bool):
    config = yaml.safe_load(
        (UC / "fixtures/contract/business-operations.yaml").read_text()
    )
    config["security"]["require_guardrails"] = require_guardrails
    result, out = synth_app(yaml.safe_dump(config))
    assert result.returncode == 0, result.stderr[-2000:]
    return {
        name: json.loads(
            (out / f"agentcore-workshop-dev-uc-{name}.template.json").read_text()
        )["Resources"]
        for name in ("business-operations", "business-operations-runtime")
    }


def _envs(resources: dict) -> list[dict]:
    return [
        r["Properties"].get("Environment", {}).get("Variables", {})
        for r in resources.values()
        if r["Type"] == "AWS::Lambda::Function"
    ]


def test_a_cached_record_asks_only_about_new_text(guardrail):
    # The live snapshot is masked every few seconds; unchanged text is not resent.
    cache: dict = {}
    first = {"prompt": "run", "segments": [{"tool_result": f"acct {ACCOUNT}"}]}
    persist_mask.mask_record(first, cache=cache)
    assert first["segments"][0]["tool_result"] == "acct {US_BANK_ACCOUNT_NUMBER}"
    asked = len(guardrail.calls)
    second = {
        "prompt": "run",
        "segments": [{"tool_result": f"acct {ACCOUNT}"}, {"content": "next"}],
    }
    persist_mask.mask_record(second, cache=cache)
    assert second["segments"][0]["tool_result"] == "acct {US_BANK_ACCOUNT_NUMBER}"
    assert len(guardrail.calls) == asked + 1 and "next" in guardrail.calls[-1]


def test_a_withheld_text_is_asked_again_next_time(monkeypatch):
    monkeypatch.setenv("PERSIST_GUARDRAIL_ID", "gr-persist")
    failing = _Guardrail(fail=True)
    monkeypatch.setattr(persist_mask, "_client", failing)
    cache: dict = {}
    record = {"content": f"acct {ACCOUNT}"}
    persist_mask.mask_record(record, cache=cache)
    assert record["content"] == persist_mask.WITHHELD and not cache


def test_the_live_snapshot_is_masked_before_it_is_written(guardrail):
    spec = importlib.util.spec_from_file_location(
        "bo_case_persistence_src", UC / "agent" / "utils" / "case_persistence.py"
    )
    source = Path(spec.origin).read_text()
    # The writer is built with the mask whenever this stack has the guardrail.
    assert "mask=mask" in source and "persist_mask.enabled()" in source
    live = (UC / "agent" / "utils" / "live_run.py").read_text()
    assert "snapshot = self._mask(snapshot)" in live
    assert live.index("snapshot = self._mask(snapshot)") < live.index(
        "self._table.update_item("
    )


def test_require_guardrails_creates_the_guardrail_and_hands_it_to_every_writer():
    t = _synth(True)
    main, runtime = t["business-operations"], t["business-operations-runtime"]
    (gid,) = [
        k
        for k, r in main.items()
        if r["Type"] == "AWS::Bedrock::Guardrail" and "Persist" in k
    ]
    pii = main[gid]["Properties"]["SensitiveInformationPolicyConfig"][
        "PiiEntitiesConfig"
    ]
    assert {e["Action"] for e in pii} == {"ANONYMIZE"}
    assert "US_BANK_ACCOUNT_NUMBER" in {e["Type"] for e in pii}
    given = [e for e in _envs(main) if "PERSIST_GUARDRAIL_ID" in e]
    # The case tool, send_notification and (sample ticketing on) the ticket tool.
    assert len(given) >= 2
    (rt,) = [
        r for r in runtime.values() if r["Type"] == "AWS::BedrockAgentCore::Runtime"
    ]
    assert "PERSIST_GUARDRAIL_ID" in rt["Properties"]["EnvironmentVariables"]
    assert "PersistGuardrail" in json.dumps(runtime)


def test_without_require_guardrails_no_writer_masks():
    t = _synth(False)
    main, runtime = t["business-operations"], t["business-operations-runtime"]
    assert not any("Persist" in k for k in main)
    assert not any("PERSIST_GUARDRAIL_ID" in e for e in _envs(main))
    assert "PERSIST_GUARDRAIL_ID" not in json.dumps(runtime)
