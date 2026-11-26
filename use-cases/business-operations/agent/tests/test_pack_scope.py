# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Pack entitlement inside the agent runtime, past the API floor.

`test_entitlement_floor.py` pins the REST boundary. This pins the part of the boundary
that lives behind it, and the specific leak it exists for: `POST /runs` gates on
`body.case_id`, so a chat turn that carries none is only refused if the caller is
entitled to nothing at all — and the runtime then recovers a case id from the *prompt
text* and routes on it. An AR-only operator typing "take a look at case ap-…" was
getting the AP pack's SOP with its tolerances and contacts already substituted, plus the
case row itself, having touched no gated route.

Three things have to hold, and each fails silently on its own; the first two are here:

  * the entitled set is **server-resolved and forwarded**, never read off the body;
  * an out-of-scope `process_type` does not route, whether the caller stated it or the
    prompt implied it;
  * a tool naming an out-of-scope case or process type is refused at the Gateway, by
    the run checks, against the same set on the run grant (tests/test_business_operations_run_checks.py).

Absence is the shipped default and is load-bearing in the other direction: no stamped
set means no restriction, so an undeclared deployment and every unattended queued run
behave exactly as they did before this control existed.
"""

import json

import pytest
from utils import payload as pl
from utils import skill_router as sr

# Two packs, one entitled each — the pair the whole file turns on.
_AP_PACK = "finance_ops"
_AP_PROCESS_TYPE = "price_variance"
_GL_PACK = "gl_ops"
_GL_PROCESS_TYPE = "po_accrual"

_AP_CASE = "ap-bchzfkch6kxm6uwqvqe5hhlkvx"
_AP_ONLY = frozenset({_AP_PACK})
_GL_ONLY = frozenset({_GL_PACK})

_CORPUS = {
    _AP_PACK: [_AP_PROCESS_TYPE],
    _GL_PACK: [_GL_PROCESS_TYPE, "tooling_accrual"],
}


@pytest.fixture
def local_packs(monkeypatch, tmp_path):
    """A synthetic two-pack corpus read from disk rather than S3."""
    for pack_id, process_types in _CORPUS.items():
        (tmp_path / pack_id / "sops").mkdir(parents=True)
        (tmp_path / pack_id / "pack.json").write_text(
            json.dumps(
                {
                    "pack_id": pack_id,
                    "process_type_to_sop": {
                        pt: f"{pack_id}/{pt}.txt" for pt in process_types
                    },
                }
            )
        )
        (tmp_path / pack_id / "base_prompt.txt").write_text(
            "{PLATFORM_MECHANICS}\n{SOP_CONTENT}\n"
        )
        for pt in process_types:
            (tmp_path / pack_id / "sops" / f"{pt}.txt").write_text(
                f"Version 1.0\nProcedure for {pt}."
            )
    (tmp_path / "_platform_prompt.txt").write_text("Shared mechanics.")
    sr._skills_index = None
    sr.config_overrides._contacts = None
    sr.registry.reset()
    monkeypatch.delenv("REGISTRY_BUCKET", raising=False)
    monkeypatch.delenv("SOP_BUCKET", raising=False)
    monkeypatch.setenv("PACKS_DIR", str(tmp_path))
    yield
    sr._skills_index = None
    sr.config_overrides._contacts = None
    sr.registry.reset()


# --------------------------------------------------------------------------- #
# The stamped set: absent means inert
# --------------------------------------------------------------------------- #


def test_a_payload_with_no_stamped_set_is_unrestricted():
    # The shipped default and every queued run. None, not an empty set: empty would
    # refuse every pack, which would break an undeclared deployment's own agent.
    assert pl.scoped_packs({"case_id": _AP_CASE}) is None


def test_a_stamped_set_becomes_the_scope():
    assert pl.scoped_packs({"visible_packs": ["finance_ops"]}) == _AP_ONLY


def test_a_malformed_stamped_set_is_ignored_rather_than_half_read():
    # A string is what a caller hand-rolling the envelope sends. Reading it as a
    # sequence of characters would produce a scope matching no pack — refusing
    # everything on a typo, in the runtime, with no error anywhere.
    assert pl.scoped_packs({"visible_packs": "finance_ops"}) is None


# --------------------------------------------------------------------------- #
# Resolving what a process type belongs to
# --------------------------------------------------------------------------- #


def test_a_process_type_is_scoped_by_the_pack_that_declares_it(local_packs):
    assert pl.may_see_process_type(_AP_PROCESS_TYPE, _AP_ONLY) is True
    assert pl.may_see_process_type(_AP_PROCESS_TYPE, _GL_ONLY) is False
    assert pl.may_see_process_type("not_a_process_type", _AP_ONLY) is False


# --------------------------------------------------------------------------- #
# Routing — the leak this exists for
# --------------------------------------------------------------------------- #


def _resolve(monkeypatch, payload, process_type=_AP_PROCESS_TYPE):
    """`_resolve_system_prompt` with the case-id lookup answered from memory."""
    monkeypatch.setattr(pl, "_lookup_process_type", lambda _c: (process_type, None))
    return pl._resolve_system_prompt(payload)


def test_a_case_named_in_prompt_text_does_not_route_outside_the_scope(
    local_packs, monkeypatch
):
    # The whole reason this control exists. No case_id in the body, so `POST /runs` had
    # no pack to check; the id comes out of the sentence and resolves to the AP pack.
    skill = _resolve(
        monkeypatch,
        {
            "user_prompt": f"take a look at case {_AP_CASE}",
            "visible_packs": [_GL_PACK],
        },
    )

    assert skill.get("discovery") is True, (
        "an out-of-scope process type must not resolve to a skill — a routed skill IS "
        "that pack's SOP, tolerances and contacts, handed over with no tool call"
    )
    assert "pack_id" not in skill


def test_a_caller_stated_process_type_is_checked_too(local_packs, monkeypatch):
    # The body can state one directly, which skips the case lookup entirely.
    skill = _resolve(
        monkeypatch,
        {"process_type": _AP_PROCESS_TYPE, "visible_packs": [_GL_PACK]},
    )

    assert skill.get("discovery") is True


def test_an_entitled_caller_still_routes(local_packs, monkeypatch):
    skill = _resolve(
        monkeypatch,
        {"user_prompt": f"take a look at case {_AP_CASE}", "visible_packs": [_AP_PACK]},
    )

    assert skill["pack_id"] == _AP_PACK
    assert skill["visible_packs"] == _AP_ONLY


def test_an_unstamped_payload_routes_as_it_always_did(local_packs, monkeypatch):
    skill = _resolve(monkeypatch, {"user_prompt": f"take a look at case {_AP_CASE}"})

    assert skill["pack_id"] == _AP_PACK
    assert skill["visible_packs"] is None


def test_the_general_prompt_names_only_entitled_use_cases(local_packs, monkeypatch):
    # "What can you do?" is an enumeration of the estate. An AP-only operator being
    # told a GL pack exists is the same leak as listing its cases.
    skill = _resolve(
        monkeypatch, {"user_prompt": "what can you do?"}, process_type=None
    )

    assert _AP_PROCESS_TYPE in skill["system_prompt"]
    assert _GL_PROCESS_TYPE in skill["system_prompt"]

    scoped = _resolve(
        monkeypatch,
        {"user_prompt": "what can you do?", "visible_packs": [_AP_PACK]},
        process_type=None,
    )

    assert _AP_PROCESS_TYPE in scoped["system_prompt"]
    assert _GL_PROCESS_TYPE not in scoped["system_prompt"]


# --------------------------------------------------------------------------- #
# Rehearsing a pack — the same resolver, with every write withheld
# --------------------------------------------------------------------------- #


def test_a_rehearsal_marks_the_skill_and_tells_the_agent(local_packs, monkeypatch):
    # `basic_agent._create_agent` reads `rehearsal` off the skill to pick the tool
    # filter, and the note is what stops the agent planning a write, failing on a tool
    # that is simply absent, and reporting that as a blocked case.
    skill = _resolve(monkeypatch, {"process_type": _AP_PROCESS_TYPE, "rehearse": True})

    assert skill["pack_id"] == _AP_PACK
    assert skill["rehearsal"] is True
    assert skill["system_prompt"].endswith(pl.REHEARSAL_NOTE)


def test_a_rehearsal_that_falls_to_general_mode_keeps_its_tools_withheld(
    local_packs, monkeypatch
):
    # The hole this closes: the scope check drops an out-of-scope process type and
    # returns the general-mode skill instead. Stamping only the routed return would hand
    # that run its writes back on the way past.
    skill = _resolve(
        monkeypatch,
        {
            "process_type": _AP_PROCESS_TYPE,
            "visible_packs": [_GL_PACK],
            "rehearse": True,
        },
    )

    assert skill.get("discovery") is True
    assert skill["rehearsal"] is True
    assert skill["system_prompt"].endswith(pl.REHEARSAL_NOTE)


def test_an_ordinary_run_is_not_marked(local_packs, monkeypatch):
    skill = _resolve(monkeypatch, {"process_type": _AP_PROCESS_TYPE})

    assert "rehearsal" not in skill and "read_only" not in skill and "ask" not in skill
    assert pl.REHEARSAL_NOTE not in skill["system_prompt"]
    assert pl.ASK_NOTE not in skill["system_prompt"]


def test_a_rehearsal_reads_only(local_packs, monkeypatch):
    skill = _resolve(monkeypatch, {"process_type": _AP_PROCESS_TYPE, "rehearse": True})

    assert skill["read_only"] is True
    assert "ask" not in skill and pl.ASK_NOTE not in skill["system_prompt"]


# --------------------------------------------------------------------------- #
# Asking — a conversation turn, with every write withheld
# --------------------------------------------------------------------------- #


def test_a_question_on_a_case_routes_to_its_sop_and_reads_only(
    local_packs, monkeypatch
):
    # It may read everything a run may read today, its case's SOP included; only the
    # tools that leave a mark are withheld, and the note says where the action is.
    skill = _resolve(monkeypatch, {"process_type": _AP_PROCESS_TYPE, "ask": True})

    assert skill["pack_id"] == _AP_PACK
    assert (skill["read_only"], skill["ask"]) == (True, True)
    assert "rehearsal" not in skill
    assert skill["system_prompt"].endswith(pl.ASK_NOTE)
    assert "Process" in pl.ASK_NOTE and "decision" in pl.ASK_NOTE


def test_a_case_less_question_reads_only_in_general_mode(local_packs, monkeypatch):
    skill = _resolve(
        monkeypatch, {"user_prompt": "brief me", "ask": True}, process_type=None
    )

    assert skill.get("discovery") is True
    assert skill["read_only"] is True
    assert skill["system_prompt"].endswith(pl.ASK_NOTE)


def test_only_a_true_ask_flag_marks_a_question(local_packs, monkeypatch):
    skill = _resolve(monkeypatch, {"process_type": _AP_PROCESS_TYPE, "ask": "yes"})

    assert "read_only" not in skill
    assert pl.is_ask({"ask": True}) and not pl.is_ask({"ask": 1})


def test_discovery_skills_are_offered_only_for_entitled_packs(local_packs):
    # Instructions here are pre-substituted, so an offered skill hands over that pack's
    # thresholds the moment the model activates it.
    offered = {s.name for s in sr.discovery_skills(packs=_GL_ONLY)}

    assert offered == {
        sr._skill_name(pt) for pt in (_GL_PROCESS_TYPE, "tooling_accrual")
    }
