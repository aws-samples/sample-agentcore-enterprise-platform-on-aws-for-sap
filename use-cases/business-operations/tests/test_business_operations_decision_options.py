# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Decision options: the agent offers outcomes, a person picks one, and the approval
authorises that option's write and nothing else.

Each link is tested where it lives: the case tool that records the set, the two
decision routes that pick from it, the invoker that builds the grant, the run checks that
match the call, and the prompt the resumed run reads.
"""

from __future__ import annotations

import json

import pytest
from test_business_operations_api import (
    CASE,
    USERS,
    _cases,
    _event,
    _tickets,
)
from test_business_operations_api_invoker import (  # noqa: F401 - fixture
    APPROVED_AT,
    NOW,
    invoker,
)
from test_business_operations_data import CASE_ID, handler  # noqa: F401 - fixture

RELEASE = {
    "op": "function_import",
    "service": "API_SUPPLIERINVOICE_PROCESS_SRV",
    "entity": "Release",
    "key": "5100004416/2026",
    "fields": [
        {"name": "SupplierInvoice", "proposed": "'5100004416'"},
        {"name": "FiscalYear", "proposed": "'2026'"},
    ],
}
SHORT_PAY = {
    "op": "update",
    "service": "API_SUPPLIERINVOICE_PROCESS_SRV",
    "entity": "A_SupplierInvoice",
    "fields": [{"name": "InvoiceGrossAmount", "proposed": "950.00", "current": "1000"}],
}
RAW = [
    {
        "id": "release",
        "label": "Release",
        "effect": "Pays on the due date.",
        "write": RELEASE,
    },
    {
        "id": "short_pay",
        "label": "Short pay",
        "effect": "Pays 950.",
        "write": SHORT_PAY,
    },
    {
        "id": "reject",
        "label": "Reject",
        "effect": "Closes; SAP unchanged.",
        "write": None,
    },
]
APPROVERS = {"ap": {"approve_groups": ["ap-approvers"]}}


def _options():
    import decision_options

    options = decision_options.narrow(RAW)
    return {
        "set_id": decision_options.set_id(options),
        "recorded_at": "t",
        "options": options,
    }


# ── the set the agent records ──


def test_a_set_is_narrowed_and_keeps_its_order():
    import decision_options

    options = decision_options.narrow(json.dumps(RAW))
    assert [o["id"] for o in options] == ["release", "short_pay", "reject"]
    assert "write" not in options[2]
    assert options[1]["write"]["fields"][0] == {
        "name": "InvoiceGrossAmount",
        "proposed": "950.00",
        "current": "1000",
    }


@pytest.mark.parametrize(
    "raw",
    [
        RAW[:1],  # one option is not a choice
        RAW * 3,  # more than six
        [RAW[0], {**RAW[1], "id": "release"}],  # the same id twice
        [RAW[0], {**RAW[1], "write": RELEASE}],  # the same write twice
        [RAW[0], {**RAW[1], "id": "Short Pay"}],
        [RAW[0], {**RAW[1], "label": ""}],
        [RAW[0], {**RAW[1], "label": "x" * 81}],
        [RAW[0], {**RAW[1], "effect": "two\nlines"}],
        [RAW[0], {**RAW[1], "colour": "red"}],
        [RAW[0], {**RAW[1], "write": {**SHORT_PAY, "op": "delete"}}],
        [RAW[0], {**RAW[1], "write": {**SHORT_PAY, "fields": []}}],
        [RAW[0], {**RAW[1], "write": {**SHORT_PAY, "service": None}}],
        [
            RAW[0],
            {
                **RAW[1],
                "write": {**SHORT_PAY, "fields": [{"name": "to_Item", "proposed": {}}]},
            },
        ],
        "not json",
    ],
)
def test_a_set_a_person_could_not_choose_from_is_refused_whole(raw):
    import decision_options

    with pytest.raises(decision_options.OptionsError):
        decision_options.narrow(raw)


def test_the_case_tool_records_a_set_only_as_it_parks_the_case(handler):  # noqa: F811
    reply = handler._update_case(
        {
            "case_id": CASE_ID,
            "updates": json.dumps(
                {"status": "awaiting_human_input", "decision_options": RAW}
            ),
            "action": "asked",
        }
    )
    assert "error" not in reply, reply
    call = handler._table.calls[-1]
    names = call["ExpressionAttributeNames"]
    stored = next(
        call["ExpressionAttributeValues"][k.replace("#", ":")]
        for k, v in names.items()
        if v == "decision_options"
    )
    assert stored["set_id"] == _options()["set_id"] and stored["recorded_at"]
    assert [o["id"] for o in stored["options"]] == ["release", "short_pay", "reject"]
    assert "decision_options" not in call["UpdateExpression"].split("REMOVE")[-1]


@pytest.mark.parametrize("status", ["complete", "manual_review_required", None])
def test_a_set_beside_any_other_status_is_refused_whole(handler, status):  # noqa: F811
    updates = {"decision_options": RAW, **({"status": status} if status else {})}
    reply = handler._update_case(
        {"case_id": CASE_ID, "updates": json.dumps(updates), "action": "a"}
    )
    assert "awaiting_human_input" in reply["error"]
    assert not handler._table.calls


def test_a_bad_set_writes_nothing(handler):  # noqa: F811
    updates = {"status": "awaiting_human_input", "decision_options": RAW[:1]}
    reply = handler._update_case(
        {"case_id": CASE_ID, "updates": json.dumps(updates), "action": "a"}
    )
    assert "2-6" in reply["error"] and not handler._table.calls


@pytest.mark.parametrize("status", ["awaiting_human_input", "complete"])
def test_a_status_written_without_a_set_removes_the_last_one(handler, status):  # noqa: F811
    handler._update_case(
        {"case_id": CASE_ID, "updates": json.dumps({"status": status}), "action": "a"}
    )
    assert (
        "decision_options"
        in handler._table.calls[-1]["UpdateExpression"].split("REMOVE")[1]
    )


def test_a_note_without_a_status_leaves_the_set(handler):  # noqa: F811
    handler._update_case(
        {"case_id": CASE_ID, "updates": json.dumps({"title": "x"}), "action": "a"}
    )
    assert "decision_options" not in handler._table.calls[-1]["UpdateExpression"]


def test_the_tool_spec_tells_the_agent_about_the_set():
    from test_business_operations_data import _stack, _update_tool_text

    text = _update_tool_text(_stack())
    assert "decision_options" in text and "awaiting_human_input" in text


# ── choosing, on the console's reply route ──


def _case(status="awaiting_human_input", options=True):
    return {
        "case_id": CASE,
        "pack_id": "ap",
        "status": status,
        "agent_traces": [],
        "cost_summary": {"invocation_count": 3},
        **({"decision_options": _options()} if options else {}),
    }


def _reply(monkeypatch, body, case=None, groups=(USERS, "ap-approvers")):
    mod, table, sqs = _cases(monkeypatch, APPROVERS, items={CASE: case or _case()})
    event = _event(
        "POST",
        "/cases/{case_id}/reply",
        groups=groups,
        pathParameters={"case_id": CASE},
        body=json.dumps(body),
    )
    event["path"] = f"/cases/{CASE}/reply"
    response = mod.handler(event, None)
    return response["statusCode"], json.loads(response["body"]), table, sqs


def test_choosing_an_option_approves_its_write_alone(monkeypatch):
    from decisions import write_digest

    status, body, table, sqs = _reply(
        monkeypatch, {"option": "short_pay", "message": "ok"}
    )
    assert status == 200, body
    update = table.updates[0]
    entry = update["ExpressionAttributeValues"][":entry"][0]
    assert entry["action"] == "human_approved"
    assert entry["option"] == "short_pay" and entry["option_label"] == "Short pay"
    assert entry["approved_write"] == write_digest(_options()["options"][1]["write"])
    assert entry["option_write"] == _options()["options"][1]["write"]
    # Still the set the person read, and the case still waits on them.
    assert "decision_options.set_id = :set" in update["ConditionExpression"]
    assert update["ExpressionAttributeValues"][":set"] == _options()["set_id"]
    payload = json.loads(sqs.sent[0]["MessageBody"])["payload"]
    assert payload["decision"] == "approved"
    assert payload["option"] == {"id": "short_pay", "label": "Short pay"}
    assert payload["approved_write"] == entry["approved_write"]


def test_an_option_without_a_write_approves_none(monkeypatch):
    status, _, table, sqs = _reply(monkeypatch, {"option": "reject", "message": "dup"})
    assert status == 200
    entry = table.updates[0]["ExpressionAttributeValues"][":entry"][0]
    assert entry["action"] == "human_chose"
    assert "approved_write" not in entry and "option_write" not in entry
    payload = json.loads(sqs.sent[0]["MessageBody"])["payload"]
    assert payload["decision"] == "chose" and "approved_write" not in payload


@pytest.mark.parametrize(
    "body,case,code,field",
    [
        ({"option": "Release", "message": "m"}, None, 400, "option"),
        (
            {"option": "release", "decision": "approved", "message": "m"},
            None,
            400,
            "option",
        ),
        ({"decision": "approved", "message": "m"}, None, 400, "option"),
        ({"decision": "denied", "message": "m"}, None, 400, "option"),
        ({"option": "release", "message": "m"}, _case(options=False), 409, None),
        ({"option": "pay_twice", "message": "m"}, None, 409, None),
        ({"option": "release", "message": "m"}, _case(status="processing"), 409, None),
    ],
)
def test_a_bare_unknown_or_stale_choice_records_nothing(
    monkeypatch, body, case, code, field
):
    status, answer, table, sqs = _reply(monkeypatch, body, case)
    assert status == code, answer
    assert answer.get("field") == field
    assert not table.updates and not sqs.sent
    if code == 409 and case is None:
        assert answer["options"] == ["release", "short_pay", "reject"]


def test_a_reply_on_a_case_with_options_still_authorises_nothing(monkeypatch):
    status, _, table, _sqs = _reply(
        monkeypatch, {"decision": "replied", "message": "?"}
    )
    assert status == 200
    entry = table.updates[0]["ExpressionAttributeValues"][":entry"][0]
    assert entry["action"] == "human_replied" and "approved_write" not in entry


def test_a_case_without_options_keeps_approve(monkeypatch):
    status, _, table, _ = _reply(
        monkeypatch, {"decision": "approved", "message": "ok"}, _case(options=False)
    )
    assert status == 200
    condition = table.updates[0]["ConditionExpression"]
    assert "decision_options" not in condition


def test_a_caller_who_may_not_decide_learns_nothing_of_the_options(monkeypatch):
    status, answer, table, _ = _reply(
        monkeypatch, {"option": "pay_twice", "message": "m"}, groups=(USERS,)
    )
    assert status == 403 and "options" not in answer and not table.updates


def test_a_set_changed_since_the_read_is_a_409(monkeypatch):
    mod, table, sqs = _cases(monkeypatch, APPROVERS, items={CASE: _case()})
    table.refuse_updates = True
    event = _event(
        "POST",
        "/cases/{case_id}/reply",
        groups=(USERS, "ap-approvers"),
        pathParameters={"case_id": CASE},
        body=json.dumps({"option": "release", "message": "m"}),
    )
    event["path"] = f"/cases/{CASE}/reply"
    response = mod.handler(event, None)
    assert response["statusCode"] == 409 and not sqs.sent


# ── choosing, on a ticket ──


def _ticket_action(monkeypatch, body, case=None):
    mod, cases, tickets, sqs = _tickets(monkeypatch, APPROVERS)
    cases.items[CASE] = case or _case()
    event = _event(
        "POST",
        "/tickets/{id}/action",
        groups=(USERS, "ap-approvers"),
        pathParameters={"id": "TKT-1"},
        body=json.dumps(body),
    )
    event["path"] = "/tickets/TKT-1/action"
    response = mod.handler(event, None)
    return response["statusCode"], json.loads(response["body"]), cases, tickets, sqs


@pytest.mark.parametrize(
    "option,ticket_status,case_action",
    [("release", "approved", "human_approved"), ("reject", "denied", "human_chose")],
)
def test_a_ticket_option_records_on_the_case_and_the_ticket(
    monkeypatch, option, ticket_status, case_action
):
    status, body, cases, tickets, sqs = _ticket_action(monkeypatch, {"option": option})
    assert status == 200, body
    entry = cases.updates[0]["ExpressionAttributeValues"][":entry"][0]
    assert entry["action"] == case_action and entry["option"] == option
    values = tickets.updates[0]["ExpressionAttributeValues"]
    assert values[":status"] == ticket_status and values[":option"] == option
    payload = json.loads(sqs.sent[0]["MessageBody"])["payload"]
    assert payload["ticket_decision"] == ticket_status
    assert payload["option"]["id"] == option


@pytest.mark.parametrize(
    "body,code",
    [
        ({"action": "approved"}, 400),
        ({"option": "release", "action": "approved"}, 400),
        ({"option": "pay_twice"}, 409),
    ],
)
def test_a_ticket_takes_only_one_of_the_cases_options(monkeypatch, body, code):
    status, _, cases, tickets, sqs = _ticket_action(monkeypatch, body)
    assert status == code
    assert not cases.updates and not tickets.updates and not sqs.sent


def test_a_ticket_put_cannot_approve_a_case_that_offers_options(monkeypatch):
    mod, cases, tickets, _sqs = _tickets(monkeypatch, APPROVERS)
    cases.items[CASE] = _case()
    event = _event(
        "PUT",
        "/tickets/{id}",
        groups=(USERS, "ap-approvers"),
        pathParameters={"id": "TKT-1"},
        body=json.dumps({"status": "approved"}),
    )
    event["path"] = "/tickets/TKT-1"
    assert mod.handler(event, None)["statusCode"] == 400
    assert not cases.updates and not tickets.updates


# ── the grant, and the run checks ──


def _approval(write, **kw):
    from decisions import write_digest

    return {
        "action": "human_approved",
        "timestamp": APPROVED_AT,
        "actor": "sub-1",
        "source": "console",
        "option": "short_pay",
        "option_label": "Short pay",
        "option_write": write,
        "approved_write": write_digest(write),
        **kw,
    }


def _grant(invoker, action_log, traces=()):  # noqa: F811
    table = invoker.dynamodb.Table.return_value
    table.get_item.side_effect = None
    table.get_item.return_value = {
        "Item": {
            "pack_id": "ap",
            "action_log": action_log,
            "agent_traces": list(traces),
        }
    }
    claim = {
        "case_id": CASE,
        "trigger": "console-action",
        "payload": {"decision": "approved"},
    }
    return invoker._grant_item(claim, "r", attended=False, now=NOW)


def test_the_grant_carries_the_chosen_options_write(invoker):  # noqa: F811
    write = _options()["options"][1]["write"]
    item = _grant(invoker, [_approval(write)])
    assert item["approved"] is True
    assert item["approved_write"]["entity"] == "A_SupplierInvoice"
    assert item["approved_write"]["fields"] == {"InvoiceGrossAmount": "950.00"}


def test_an_option_write_its_digest_does_not_match_grants_nothing(invoker):  # noqa: F811
    write = _options()["options"][1]["write"]
    tampered = {**write, "fields": [{"name": "InvoiceGrossAmount", "proposed": "1.00"}]}
    item = _grant(invoker, [_approval(write, option_write=tampered)])
    assert item["approved"] is False and "approved_write" not in item


def test_the_run_checks_let_through_the_chosen_write_and_no_other_option(invoker):  # noqa: F811
    from business_operations_check.run_checks import is_approved_write

    approved = _grant(invoker, [_approval(_options()["options"][1]["write"])])[
        "approved_write"
    ]
    chosen = {
        "service_name": "API_SUPPLIERINVOICE_PROCESS_SRV",
        "entity_set": "A_SupplierInvoice",
        "payload": {"InvoiceGrossAmount": "950.00"},
    }
    other = {
        "service_name": "API_SUPPLIERINVOICE_PROCESS_SRV",
        "function_name": "Release",
        "parameters": {"SupplierInvoice": "'5100004416'", "FiscalYear": "'2026'"},
    }
    assert is_approved_write(approved, "odata_update", chosen)
    assert not is_approved_write(approved, "odata_function_import", other)
    assert not is_approved_write(
        approved, "odata_update", {**chosen, "payload": {"InvoiceGrossAmount": "1.00"}}
    )


def test_a_run_starting_removes_the_last_set(invoker):  # noqa: F811
    table = invoker.dynamodb.Table.return_value
    invoker._update_status(table, {"case_id": CASE}, "processing", "manual")
    assert (
        "REMOVE status_reason, decision_options"
        in (table.update_item.call_args.kwargs["UpdateExpression"])
    )
    invoker._update_status(table, {"case_id": CASE}, "complete", "manual")
    assert (
        "decision_options" not in table.update_item.call_args.kwargs["UpdateExpression"]
    )


def test_two_approvers_must_choose_the_same_option():
    from decisions import approvals_of, write_digest

    release, short = (_options()["options"][i]["write"] for i in (0, 1))
    log = [
        _approval(release, actor="a", option="release"),
        _approval(short, actor="b"),
    ]
    assert [e["actor"] for _, e in approvals_of(log, write_digest(short))] == ["b"]
    log[0] = _approval(short, actor="a")
    assert {e["actor"] for _, e in approvals_of(log, write_digest(short))} == {"a", "b"}


def test_a_ticket_option_keeps_the_resolution_the_console_sends(monkeypatch):
    status, body, _cases_t, tickets, sqs = _ticket_action(
        monkeypatch,
        {"option": "short_pay", "resolution": "Supplier agreed", "comment": "ok"},
    )
    assert status == 200, body
    values = tickets.updates[0]["ExpressionAttributeValues"]
    assert values[":res"] == "Supplier agreed" and values[":option"] == "short_pay"
    assert values[":comment"][0]["text"] == "ok"
    assert json.loads(sqs.sent[0]["MessageBody"])["payload"]["resolution"] == (
        "Supplier agreed"
    )


def test_without_a_resolution_the_ticket_names_the_option_and_long_text_is_cut(
    monkeypatch,
):
    _, _, _, tickets, _ = _ticket_action(monkeypatch, {"option": "reject"})
    assert tickets.updates[0]["ExpressionAttributeValues"][":res"] == "Chose Reject"
    _, _, _, tickets, _ = _ticket_action(
        monkeypatch, {"option": "reject", "resolution": "x" * 6000}
    )
    assert len(tickets.updates[0]["ExpressionAttributeValues"][":res"]) == 5000
