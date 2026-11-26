# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The business-operations run checks (its interceptor check) and its Cedar statements.

run_checks.decide is pure, so its table is tested directly; check() is tested with its
reads stubbed. The Cedar statements are synthesized against a stand-in target.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[3]
UC = REPO / "use-cases" / "business-operations"
sys.path.insert(0, str(UC))

import business_operations_check as check_module
from business_operations import cedar, names, policies
from business_operations_check import run_checks
from sap_layer import names as sap_names

from sap_kit import run_grant

CASE = names.CASE_TARGET
SAP = sap_names.sap_target("s4")
NOW = 1_000
NONCE = "the-invokers-nonce"


def _grant(**kw) -> run_checks.Grant:
    return run_checks.Grant(
        **{
            "run_id": "run-1",
            "client_id": "agent",
            "case_id": "case-1",
            "packs": None,
            "approved": False,
            "expires_at": NOW + 60,
            "secret_sha256": hashlib.sha256(NONCE.encode()).hexdigest(),
            **kw,
        }
    )


KEY = {"SupplierInvoice": "5100004416", "FiscalYear": "2026"}
CASE_DOCUMENT_CHECK = {"SupplierInvoice": "SupplierInvoice", "FiscalYear": "FiscalYear"}
DEFAULT_CASE = run_checks.Case("ap", "10", "USD", KEY)


def _lookups(
    grant=None,
    *,
    limit=None,
    case=DEFAULT_CASE,
    pack="ap",
    pt=None,
    document_check=CASE_DOCUMENT_CHECK,
    ticket_case="case-1",
    amount_field=None,
    systems=None,
):
    def case_document_check(p):
        if isinstance(document_check, Exception):
            raise document_check
        return document_check

    return run_checks.Lookups(
        grant=lambda run_id: grant,
        limit=lambda p: limit,
        any_limit_declared=lambda: limit is not None,
        case=lambda c: case,
        case_pack=lambda c: pack,
        ticket_case=lambda t: ticket_case,
        process_type_pack=lambda t: pt,
        case_document_check=case_document_check,
        amount_field=lambda p: amount_field,
        pack_system=lambda p: (systems or {})[p],
    )


def _no_read(*_):
    raise AssertionError("read on a call decided without it")


NO_READS = run_checks.Lookups(*[_no_read] * 8)


def decide(
    tool,
    *,
    args=None,
    client="agent",
    run_id="run-1",
    secret=NONCE,
    lookups=None,
    app=None,
    sap_callers=("agent", "mcp"),
    agent_callers=(),
):
    return run_checks.decide(
        tool=tool,
        arguments={"case_id": "case-1"} if args is None else args,
        client_id=lambda: client,
        run_id=run_id,
        grant_secret=secret,
        app_id=lambda: client if app is None else app,
        sap_callers=lambda: frozenset(sap_callers),
        agent_callers=lambda: frozenset(agent_callers),
        now=NOW,
        lookups=lookups or _lookups(_grant()),
    )


# ── What is gated at all, decided with no reads ──


@pytest.mark.parametrize(
    "tool",
    [
        f"{SAP}___odata_read",
        f"{SAP}___get_metadata",
        f"{names.KNOWLEDGE_TARGET}___search_sap_api_docs",
        # A SAP write tool name on a target that is not a SAP target is not a SAP write.
        f"{CASE}___odata_create",
    ],
)
def test_ungated_tools_pass_without_a_read(tool):
    assert decide(tool, run_id=None, lookups=NO_READS) is None


def test_odata_delete_is_the_sap_layers_to_refuse():
    # The SAP layer's check refuses it for every caller; the run checks do not gate it.
    assert decide(f"{SAP}___odata_delete", lookups=NO_READS) is None


def test_another_use_cases_sap_calls_pass_untouched():
    # The SAP layer already held that caller to its own use case's claim.
    for name in ("odata_update", "odata_create"):
        assert (
            decide(f"{SAP}___{name}", app="other-app", run_id=None, lookups=NO_READS)
            is None
        )
    # A person's token: the run grant names the person, the claim names the console app.
    assert (
        decide(f"{SAP}___odata_update", client="user:1", app="agent", run_id=None)
        == run_checks.NO_RUN
    )


def test_the_constants_the_check_copies_match_names():
    # The check is bundled into the interceptor alone, so it cannot import names.
    assert run_checks.SAP_TARGET_PREFIX == sap_names.TARGET_PREFIX
    assert run_checks.LEGACY_SAP_TARGET_PREFIX == sap_names.LEGACY_TARGET_PREFIX


def test_a_write_on_the_old_target_name_is_gated_while_it_is_renamed():
    tool = f"{run_checks.LEGACY_SAP_TARGET_PREFIX}s4___odata_update"
    assert decide(tool, run_id=None) == run_checks.NO_RUN
    assert run_checks.SAP_WRITE_TOOLS == frozenset(sap_names.SAP_WRITE_TOOLS)


# ── The run grant ──


@pytest.mark.parametrize(
    "tool",
    [
        f"{SAP}___odata_create",
        f"{CASE}___update_case_state",
        f"{CASE}___get_case_state",
    ],
)
def test_no_run_header_is_refused(tool):
    assert decide(tool, run_id=None) == run_checks.NO_RUN


@pytest.mark.parametrize(
    "grant",
    [None, _grant(client_id="someone-else"), _grant(expires_at=NOW)],
    ids=["absent", "other-client", "expired"],
)
def test_a_grant_must_exist_be_this_clients_and_be_live(grant):
    assert (
        decide(f"{CASE}___update_case_state", lookups=_lookups(grant))
        == run_checks.NO_RUN
    )


def test_a_valid_grant_passes_a_case_tool():
    assert decide(f"{CASE}___update_case_state") is None


@pytest.mark.parametrize(
    "secret,grant",
    [
        (None, _grant(client_id="user:1")),
        ("guessed", _grant(client_id="user:1")),
        # A grant the invoker wrote no secret for matches nothing, not everything.
        ("", _grant(client_id="user:1", secret_sha256="")),
        (NONCE, _grant(client_id="user:1", secret_sha256="")),
    ],
    ids=["run-id-only", "wrong-secret", "both-empty", "grant-without-one"],
)
def test_the_run_id_alone_does_not_use_a_grant(secret, grant):
    # Under direct mode with on-behalf-of SAP the person's own token is the grant's
    # caller key, and POST /runs hands them the run id: only the secret stops them.
    for tool in (f"{CASE}___update_case_state", f"{SAP}___odata_update"):
        # app: the console app, which the SAP claim names.
        assert (
            decide(
                tool,
                client="user:1",
                app="agent",
                secret=secret,
                lookups=_lookups(grant),
            )
            == run_checks.NO_RUN
        )
    # The same grant with its secret passes.
    ok = _lookups(_grant(client_id="user:1"))
    assert decide(f"{CASE}___update_case_state", client="user:1", lookups=ok) is None


def test_a_person_and_an_app_both_need_a_grant_to_read_a_ticket():
    tool = f"{names.TICKETS_TARGET}___demo_get_ticket"
    args = {"ticket_id": "t-1"}
    # The desktop proxy's reader: an app's token, no run.
    assert decide(tool, args=args, run_id=None) == run_checks.NO_RUN
    assert decide(tool, args=args, client="user:1", run_id=None) == run_checks.NO_RUN
    grant = _lookups(_grant(client_id="user:1"))
    assert decide(tool, args=args, client="user:1", lookups=grant) is None


# ── Rehearsal ──


@pytest.mark.parametrize(
    "tool",
    [
        f"{SAP}___odata_function_import",
        f"{SAP}___odata_update",
        f"{CASE}___update_case_state",
        f"{names.NOTIFICATION_TARGET}___send_notification",
        f"{names.TICKETS_TARGET}___demo_create_ticket",
    ],
)
def test_a_rehearsal_cannot_leave_a_mark(tool):
    lookups = _lookups(_grant(rehearsal=True))
    assert decide(tool, lookups=lookups) == run_checks.REHEARSAL


def test_a_rehearsal_refuses_a_marking_tool_no_other_set_gates(monkeypatch):
    # Listing a tool in READ_ONLY_DENIED_TOOLS is enough: it need not also name a case.
    monkeypatch.setattr(
        run_checks,
        "READ_ONLY_DENIED_TOOLS",
        run_checks.READ_ONLY_DENIED_TOOLS | {"post_memo"},
    )
    tool = f"{names.NOTIFICATION_TARGET}___post_memo"
    assert (
        decide(tool, lookups=_lookups(_grant(rehearsal=True))) == run_checks.REHEARSAL
    )
    assert decide(tool, args={}) is None
    assert decide(tool, run_id=None) == run_checks.NO_RUN


def test_a_rehearsal_still_reads_its_case():
    lookups = _lookups(_grant(rehearsal=True))
    assert decide(f"{CASE}___get_case_state", lookups=lookups) is None


# ── A question ──


@pytest.mark.parametrize(
    "tool",
    [
        f"{SAP}___odata_function_import",
        f"{SAP}___odata_update",
        f"{SAP}___odata_create",
        f"{CASE}___update_case_state",
        f"{names.NOTIFICATION_TARGET}___send_notification",
        f"{names.TICKETS_TARGET}___demo_create_ticket",
    ],
)
def test_a_question_cannot_leave_a_mark_even_on_its_own_case(tool):
    # The grant names the case, as a Process run's would: only `ask` refuses.
    lookups = _lookups(_grant(ask=True))
    assert decide(tool, lookups=lookups) == run_checks.ASKING
    assert "Process" in run_checks.ASKING and "decision" in run_checks.ASKING


def test_a_question_still_reads_its_case_and_sap():
    lookups = _lookups(_grant(ask=True))
    assert decide(f"{CASE}___get_case_state", lookups=lookups) is None
    assert decide(f"{SAP}___odata_read", lookups=lookups) is None


def test_an_approval_does_not_lift_a_questions_refusal():
    # A resume after a decision is a queued run and never carries `ask`; were one to,
    # the approved write is still refused.
    write = {"op": "function_import", "service": "S", "entity": "Release", "fields": {}}
    lookups = _lookups(_grant(ask=True, approved=True, approved_write=write))
    assert (
        decide(f"{SAP}___odata_function_import", lookups=lookups) == run_checks.ASKING
    )


# ── The amount gate ──


# The write the E2E run's human approved, as the invoker puts it on the grant.
RELEASE = {
    "op": "function_import",
    "service": "API_SUPPLIERINVOICE_PROCESS_SRV",
    "entity": "Release",
    "fields": {
        "SupplierInvoice": "'5100004416'",
        "FiscalYear": "'2026'",
        "DiscountDaysHaveToBeShifted": "false",
    },
}
RELEASE_CALL = {
    "service_name": "API_SUPPLIERINVOICE_PROCESS_SRV",
    "function_name": "Release",
    "parameters": dict(RELEASE["fields"]),
    "method": "POST",
}


def _approved(**kw):
    grant = _grant(approved=True, approved_write=RELEASE)
    return decide(
        f"{SAP}___odata_function_import",
        args=kw.get("args", RELEASE_CALL),
        lookups=_lookups(grant, limit=Decimal(5)),
    )


def _write(**kw):
    return decide(f"{SAP}___odata_function_import", args=RELEASE_CALL, **kw)


def test_amount_over_the_limit_is_refused_without_approval():
    over = _lookups(_grant(), limit=Decimal(5))
    reason = _write(lookups=over)
    assert "over the 5 USD" in reason
    # The model is told what to do next, so a refusal ends in a question to a person.
    assert 'response_type "approval"' in reason and "awaiting_human_input" in reason
    # `approved` with no approved write lifts nothing: an approval is of one write.
    bare = _lookups(_grant(approved=True), limit=Decimal(5))
    assert "not the write the human approved" in _write(lookups=bare)


def test_an_approval_lifts_the_gate_for_the_approved_write_only():
    assert _approved() is None
    # The parameters may arrive as a JSON string; a JSON boolean reads as the card did.
    as_text = {**RELEASE_CALL, "parameters": json.dumps(RELEASE["fields"])}
    assert _approved(args=as_text) is None
    flag = {**RELEASE["fields"], "DiscountDaysHaveToBeShifted": False}
    assert _approved(args={**RELEASE_CALL, "parameters": flag}) is None


_NOT_APPROVED = "not the write the human approved"
_OTHER_DOCUMENT = "only its own case's document"


@pytest.mark.parametrize(
    "args,expected",
    [
        (
            {
                **RELEASE_CALL,
                "parameters": {
                    **RELEASE["fields"],
                    "DiscountDaysHaveToBeShifted": "true",
                },
            },
            _NOT_APPROVED,
        ),
        (
            {**RELEASE_CALL, "parameters": {**RELEASE["fields"], "Extra": "x"}},
            _NOT_APPROVED,
        ),
        ({**RELEASE_CALL, "function_name": "Post"}, _NOT_APPROVED),
        ({**RELEASE_CALL, "service_name": "API_OTHER_SRV"}, _NOT_APPROVED),
        (
            {**RELEASE_CALL, "parameters": {**RELEASE["fields"], "to_Item": [{}]}},
            _NOT_APPROVED,
        ),
        # The case document check refuses these first: they are not this case's document at all.
        (
            {
                **RELEASE_CALL,
                "parameters": {**RELEASE["fields"], "SupplierInvoice": "'1'"},
            },
            _OTHER_DOCUMENT,
        ),
        (
            {**RELEASE_CALL, "parameters": {"SupplierInvoice": "'5100004416'"}},
            "names no FiscalYear",
        ),
        ({**RELEASE_CALL, "parameters": "not json"}, "names no"),
    ],
    ids=[
        "other-value",
        "extra-field",
        "other-function",
        "other-service",
        "nested",
        "other-document",
        "missing-field",
        "unparseable",
    ],
)
def test_a_write_other_than_the_approved_one_is_refused_over_the_limit(args, expected):
    assert expected in _approved(args=args)


def test_an_approval_does_not_carry_across_ops():
    grant = _grant(approved=True, approved_write={**RELEASE, "op": "update"})
    lookups = _lookups(grant, limit=Decimal(5))
    reason = decide(
        f"{SAP}___odata_function_import", args=RELEASE_CALL, lookups=lookups
    )
    assert "not the write the human approved" in reason


@pytest.mark.parametrize(
    "case,expected",
    [
        (None, "could not be read"),
        (run_checks.Case("ap", "", "USD", KEY), "no readable amount"),
        (run_checks.Case("ap", "NaN", "USD", KEY), "no readable amount"),
        (run_checks.Case("ap", "1", "EUR", KEY), "cannot be compared"),
        (run_checks.Case("ap", "1", "", KEY), "cannot be compared"),
        (run_checks.Case("ap", "5", "USD", KEY), None),
    ],
)
def test_amount_gate_refuses_on_doubt(case, expected):
    reason = _write(lookups=_lookups(_grant(), limit=Decimal(5), case=case))
    assert reason is None if expected is None else expected in reason


def test_a_pushed_case_amount_never_lifts_the_limit():
    pushed = run_checks.Case("ap", "1", "USD", KEY, pushed=True)
    reason = _write(lookups=_lookups(_grant(), limit=Decimal(5), case=pushed))
    assert "records a sender pushed" in reason and 'response_type "approval"' in reason
    # The approved write still goes through: a human read the document.
    approved = _grant(approved=True, approved_write=RELEASE)
    assert _write(lookups=_lookups(approved, limit=Decimal(5), case=pushed)) is None
    # A pack with no limit has no amount gate to lift.
    assert _write(lookups=_lookups(_grant(), limit=None, case=pushed)) is None


def test_no_limit_declared_anywhere_passes_a_sap_write():
    assert _write() is None


# ── The write is on the case's own document ──


@pytest.mark.parametrize(
    "parameters,expected",
    [
        (
            {"SupplierInvoice": "'5100004417'", "FiscalYear": "'2026'"},
            "SupplierInvoice 5100004417",
        ),
        (
            {"SupplierInvoice": "'5100004416'", "FiscalYear": "'2025'"},
            "FiscalYear 2025",
        ),
        ({"FiscalYear": "'2026'"}, "names no SupplierInvoice"),
    ],
    ids=["other-invoice", "other-year", "unnamed"],
)
def test_a_write_on_another_document_is_refused(parameters, expected):
    reason = decide(
        f"{SAP}___odata_function_import",
        args={**RELEASE_CALL, "parameters": parameters},
    )
    assert expected in reason
    if "names no" not in reason:
        assert "only its own case's document" in reason


def test_a_checked_value_matches_as_sap_compares_it():
    # Quotes, leading zeros and a JSON string are the same document.
    as_text = json.dumps({"SupplierInvoice": "'05100004416'", "FiscalYear": 2026})
    assert _write() is None
    assert (
        decide(
            f"{SAP}___odata_function_import",
            args={**RELEASE_CALL, "parameters": as_text},
        )
        is None
    )


def test_every_copy_of_a_checked_field_must_match():
    update = {
        "service_name": "API_SUPPLIERINVOICE_PROCESS_SRV",
        "entity_set": "A_SupplierInvoice",
        "identifier_fields": {"SupplierInvoice": "5100004416", "FiscalYear": "2026"},
        "payload": {"SupplierInvoice": "5100009999", "PaymentBlockingReason": ""},
    }
    assert "SupplierInvoice 5100009999" in decide(f"{SAP}___odata_update", args=update)
    update["payload"] = {"PaymentBlockingReason": ""}
    assert decide(f"{SAP}___odata_update", args=update) is None


@pytest.mark.parametrize(
    "lookups,expected",
    [
        (_lookups(_grant(), document_check=None), "declares no case_document_check"),
        (_lookups(_grant(), document_check={}), "declares no case_document_check"),
        (_lookups(_grant(), document_check=ValueError("x")), "is unreadable"),
        (_lookups(_grant(case_id=None)), "names no case"),
        (_lookups(_grant(), case=None), "could not be read"),
        (
            _lookups(
                _grant(), case=run_checks.Case("ap", "1", "USD", {"FiscalYear": "2026"})
            ),
            "has no SupplierInvoice",
        ),
    ],
    ids=[
        "undeclared",
        "empty",
        "unreadable",
        "no-case",
        "unreadable-case",
        "keyless-case",
    ],
)
def test_a_write_the_case_document_check_cannot_match_is_refused(lookups, expected):
    assert expected in _write(lookups=lookups)


def test_an_approval_does_not_lift_the_case_document_check():
    grant = _grant(approved=True, approved_write=RELEASE)
    other = run_checks.Case("ap", "1", "USD", {**KEY, "SupplierInvoice": "1"})
    reason = _write(lookups=_lookups(grant, limit=Decimal(5), case=other))
    assert "only its own case's document" in reason


# ── Pack scope ──


def test_a_case_outside_the_grants_packs_is_refused():
    lookups = _lookups(_grant(packs=frozenset({"ar"})), pack="ap")
    assert "not in a use case" in decide(f"{CASE}___get_case_state", lookups=lookups)
    inside = _lookups(_grant(packs=frozenset({"ap"})), pack="ap")
    assert decide(f"{CASE}___get_case_state", lookups=inside) is None


def test_a_ticket_outside_the_grants_packs_is_refused():
    tool = f"{names.TICKETS_TARGET}___demo_get_ticket"
    args = {"ticket_id": "TKT-1"}
    assert decide(tool, args=args, run_id=None) == run_checks.NO_RUN
    outside = _lookups(_grant(packs=frozenset({"ar"})), pack="ap")
    assert "ticket 'TKT-1'" in decide(tool, args=args, lookups=outside)
    # A ticket that names no case, or is missing, is in no pack the run may touch.
    caseless = _lookups(_grant(packs=frozenset({"ap"})), ticket_case=None)
    assert "ticket 'TKT-1'" in decide(tool, args=args, lookups=caseless)
    inside = _lookups(_grant(packs=frozenset({"ap"})), pack="ap")
    assert decide(tool, args=args, lookups=inside) is None


def test_filing_a_ticket_needs_the_write_scope():
    statement = next(s for s in policies.STATEMENTS if s.tool == "demo_create_ticket")
    assert '"uc-business-operations/write"' in statement.text
    assert "/read" not in statement.text


def test_a_process_type_outside_the_grants_packs_is_refused():
    tool = f"{names.AGENT_KNOWLEDGE_TARGET}___get_precedent"
    lookups = _lookups(_grant(packs=frozenset({"ap"})), pt=None)
    assert "process type" in decide(tool, args={"process_type": "x"}, lookups=lookups)


def test_a_sop_search_outside_the_grants_packs_is_refused():
    tool = f"{names.KNOWLEDGE_TARGET}___search_sap_sops"
    assert decide(tool, run_id=None) == run_checks.NO_RUN
    lookups = _lookups(_grant(packs=frozenset({"ap"})))
    assert "pack 'ar'" in decide(tool, args={"pack_id": "ar"}, lookups=lookups)
    assert decide(tool, args={"pack_id": "ap"}, lookups=lookups) is None
    assert decide(tool, args={"pack_id": "ar"}) is None  # a grant naming no packs


def test_load_sop_is_held_to_the_grants_packs():
    tool = f"{names.SOPS_TARGET}___load_sop"
    assert decide(tool, run_id=None) == run_checks.NO_RUN
    inside = _lookups(_grant(packs=frozenset({"ap"})), pt="ap")
    assert decide(tool, args={"process_type": "x"}, lookups=inside) is None
    outside = _lookups(_grant(packs=frozenset({"ar"})), pt="ap")
    assert "process type 'x'" in decide(
        tool, args={"process_type": "x"}, lookups=outside
    )


# ── check(): the reads ──


def _bearer(client_id: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"client_id": client_id}).encode())
    return f"Bearer h.{payload.decode().rstrip('=')}.s"


@pytest.fixture
def reads(monkeypatch):
    monkeypatch.setattr(check_module, "_config", lambda: {"sap_callers": ["agent"]})
    grants = {}
    monkeypatch.setattr(
        check_module,
        "_LOOKUPS",
        run_checks.Lookups(
            grant=grants.get,
            limit=lambda p: None,
            any_limit_declared=lambda: False,
            case=lambda c: None,
            case_pack=lambda c: None,
            ticket_case=lambda t: None,
            process_type_pack=lambda t: None,
            case_document_check=lambda p: None,
        ),
    )
    return grants


def test_check_reads_the_client_and_run_from_the_headers(reads):
    body = {"params": {"name": f"{CASE}___update_case_state", "arguments": {}}}
    refused = check_module.check(body, {"Authorization": _bearer("agent")})
    assert (
        refused.startswith(run_checks.NO_RUN)
        and "retrying will not change it" in refused
    )
    reads["run-1"] = _grant(expires_at=2**40)
    headers = {"Authorization": _bearer("agent"), "X-Audit-Correlation-Id": "run-1"}
    assert check_module.check(body, headers).startswith(run_checks.NO_RUN)
    headers["X-Business-Operations-Run-Grant"] = NONCE
    assert check_module.check(body, headers) is None


def test_check_names_the_run_it_saw_and_nothing_else(reads, caplog):
    """The interceptor's verdict line names no run; this line, same invocation, does."""
    body = {"params": {"name": f"{CASE}___update_case_state", "arguments": {}}}
    headers = {"Authorization": _bearer("agent"), "X-Audit-Correlation-Id": "run-1"}
    with caplog.at_level("INFO"):
        check_module.check(body, {**headers, "X-Business-Operations-Run-Grant": NONCE})
        check_module.check(body, {"Authorization": _bearer("agent")})
    assert [r.getMessage() for r in caplog.records] == [
        'interceptor run {"run_id": "run-1"}'
    ]


def test_check_raises_on_a_malformed_token(reads):
    # The interceptor turns a raise into a refusal for this use case's targets.
    reads["run-1"] = _grant(expires_at=2**40)
    body = {"params": {"name": f"{CASE}___get_case_state"}}
    headers = {"Authorization": "Bearer not-a-jwt", "X-Audit-Correlation-Id": "run-1"}
    with pytest.raises(IndexError):
        check_module.check(body, headers)


# How GetParameterHistory names a stack deploy's write (layers/sap/tests/test_sap_layer.py).
CFN_EXEC_ROLE = (
    "arn:aws:iam::111111111111:role/cdk-hnb659fds-cfn-exec-role-111111111111-us-west-2"
)
CLOUDFORMATION = (
    "arn:aws:sts::111111111111:assumed-role/"
    "cdk-hnb659fds-cfn-exec-role-111111111111-us-west-2/AWSCloudFormation"
)
RUN_CHECKS = "/p/e/use-cases/business-operations/run-checks"


class _History:
    """get_parameter_history for the run checks parameter: a list of (writer, value)
    versions, oldest first, served one per page; or the error it raises."""

    def __init__(self, versions):
        self.versions = versions
        self.calls = 0

    def get_parameter_history(self, Name, MaxResults, NextToken=None):
        assert Name == RUN_CHECKS
        self.calls += 1
        if isinstance(self.versions, Exception):
            raise self.versions
        i = int(NextToken or 0)
        writer, value = self.versions[i]
        page = {
            "Parameters": [
                {
                    "Version": i + 1,
                    "Value": json.dumps(value),
                    "LastModifiedUser": writer,
                }
            ]
        }
        if i + 1 < len(self.versions):
            page["NextToken"] = str(i + 1)
        return page


@pytest.fixture
def history(monkeypatch):
    monkeypatch.setenv("SSM_PREFIX", "/p/e")
    monkeypatch.setenv("CFN_EXEC_ROLE_ARN", CFN_EXEC_ROLE)
    monkeypatch.setattr(check_module, "_settings", None)
    ssm = _History([(CLOUDFORMATION, {"sap_callers": ["agent"]})])
    monkeypatch.setattr(check_module, "_client", lambda service: ssm)
    return ssm


def _sap_write() -> str | None:
    body = {"params": {"name": f"{SAP}___odata_update", "arguments": {}}}
    return check_module.check(body, {"Authorization": _bearer("agent")})


def test_settings_cloudformation_wrote_are_trusted(history):
    assert check_module._config() == {"sap_callers": ["agent"]}
    # The settings name this caller, so its write meets the run grant.
    assert _sap_write().startswith(run_checks.NO_RUN)


def test_settings_written_after_cloudformation_refuse_every_gated_call(history):
    # `sap_callers: []` would let this use case's SAP writes skip the grant.
    history.versions.append(
        ("arn:aws:sts::111111111111:assumed-role/Admin/someone", {"sap_callers": []})
    )
    with pytest.raises(check_module.trusted.Untrusted, match="last written by"):
        _sap_write()


def test_unreadable_settings_history_refuses(history):
    history.versions = RuntimeError("AccessDeniedException")
    with pytest.raises(RuntimeError):
        _sap_write()


def test_settings_are_kept_for_the_ttl_and_a_refusal_is_not(history, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(check_module.time, "monotonic", lambda: clock[0])
    good = history.versions[0]
    assert check_module._config() == {"sap_callers": ["agent"]}
    clock[0] += check_module.SETTINGS_TTL_SECONDS - 1
    assert check_module._config() and history.calls == 1  # within the TTL: no read
    history.versions.append(("arn:aws:iam::111111111111:user/x", {"sap_callers": []}))
    clock[0] += 1
    with pytest.raises(check_module.trusted.Untrusted):
        check_module._config()  # expired: the tampered version is seen at once
    history.versions.append(good)  # CloudFormation writes it again
    assert check_module._config() == {"sap_callers": ["agent"]}  # refusal not kept


@pytest.mark.parametrize(
    ("name", "target"),
    [
        # The SAP layer's own writer check.
        ("trusted.py", "layers/sap/sap_check/trusted.py"),
        # The run-grant mechanics the fork's use cases share.
        ("run_grant.py", "sap_kit/run_grant.py"),
    ],
)
def test_the_check_links_its_shared_modules(name, target):
    # Linked, not copied, and the interceptor bundle carries the file, not the link.
    from stacks.gateway_stack import GatewayStack

    link = UC / "business_operations_check" / name
    target = REPO / target
    assert link.is_symlink() and not Path(link.readlink()).is_absolute()
    assert link.resolve() == target.resolve()
    staged = Path(
        GatewayStack._bundle_extensions(
            "tools/egress_interceptor",
            [{"path": "use-cases/business-operations/business_operations_check"}],
        )
    )
    try:
        bundled = staged / "extensions" / "business_operations_check" / name
        assert not bundled.is_symlink()
        assert bundled.read_text() == target.read_text()
    finally:
        shutil.rmtree(staged)


@pytest.mark.parametrize("override, limit", [("60", 60), ("250", 100)])
def test_a_limit_override_only_lowers_the_packs_ceiling(monkeypatch, override, limit):
    # 250 over a ceiling of 100: a row left from before a version lowered the ceiling,
    # which must not widen what the agent writes without a human.
    class Table:
        def get_item(self, Key):
            assert Key == {
                "namespace": "constant#ap",
                "config_key": run_checks.LIMIT_SYMBOL,
            }
            return {"Item": {"value": override}}

    monkeypatch.setattr(
        check_module,
        "_packs",
        lambda: {"ap": {"pack": {"constants": {run_checks.LIMIT_SYMBOL: 100}}}},
    )
    monkeypatch.setattr(check_module, "_table", lambda key: Table())
    assert check_module._limit("ap") == Decimal(limit)
    assert check_module._limit("other") is None


def test_the_grant_carries_rehearsal_only_when_it_says_true(monkeypatch):
    items = {
        "r": {"run_id": "r", "client_id": "a", "expires_at": 1, "rehearsal": True},
        "s": {"run_id": "s", "client_id": "a", "expires_at": 1, "rehearsal": "yes"},
        "q": {"run_id": "q", "client_id": "a", "expires_at": 1, "ask": True},
        "t": {"run_id": "t", "client_id": "a", "expires_at": 1, "ask": "true"},
    }

    class Table:
        def get_item(self, Key, ConsistentRead):
            return {"Item": items[Key["run_id"]]}

    monkeypatch.setattr(check_module, "_table", lambda key: Table())
    assert check_module._grant("r").rehearsal is True
    assert check_module._grant("s").rehearsal is False
    assert check_module._grant("q").ask is True
    assert check_module._grant("t").ask is False
    assert check_module._grant("r").ask is False


def test_the_case_is_read_with_its_key(monkeypatch):
    class Table:
        def get_item(self, Key, ProjectionExpression, ExpressionAttributeNames):
            assert "#k" in ProjectionExpression
            assert ExpressionAttributeNames["#k"] == "key"
            return {
                "Item": {
                    "pack_id": "ap",
                    "key": KEY,
                    "attributes": {"amount": "12.5", "currency": "USD"},
                }
            }

    monkeypatch.setattr(check_module, "_table", lambda key: Table())
    assert check_module._case("c") == run_checks.Case("ap", "12.5", "USD", KEY)


def test_case_document_check_is_read_from_the_registry_and_refuses_on_doubt(
    monkeypatch,
):
    packs = {
        "ap": {"pack": {"case_document_check": CASE_DOCUMENT_CHECK}},
        "none": {"pack": {}},
        "bad": {"pack": {"case_document_check": {"SupplierInvoice": 1}}},
    }
    monkeypatch.setattr(check_module, "_packs", lambda: packs)
    assert check_module._case_document_check("ap") == CASE_DOCUMENT_CHECK
    assert check_module._case_document_check("none") is None
    with pytest.raises(ValueError):
        check_module._case_document_check("bad")


def test_finance_ops_checks_its_writes_against_the_case_key():
    pack = json.loads((UC / "packs" / "finance_ops" / "pack.json").read_text())
    key_fields = {k.get("name", k["field"]) for k in pack["source"]["key"]}
    assert pack["case_document_check"] == CASE_DOCUMENT_CHECK
    assert set(pack["case_document_check"].values()) <= key_fields


def test_the_registry_is_kept_for_the_ttl_and_a_failed_read_is_not(monkeypatch):
    bodies = [
        {"packs": {}},
        RuntimeError("throttled"),
        {"packs": {"ap": {}}},
        {"packs": {"ap": {}, "ar": {}}},
    ]

    class S3:
        def get_object(self, Bucket, Key, **_):
            body = bodies.pop(0)
            if isinstance(body, Exception):
                raise body
            return {"Body": SimpleNamespace(read=lambda: json.dumps(body).encode())}

    clock = [100.0]
    monkeypatch.setattr(check_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(check_module, "_client", lambda service: S3())
    monkeypatch.setattr(check_module, "_config", lambda: {"registry_bucket": "reg"})
    monkeypatch.setattr(check_module, "_registry", None)
    ttl = check_module.REGISTRY_TTL_SECONDS

    assert check_module._packs() == {}  # an empty registry is a real answer
    clock[0] += ttl - 1
    assert check_module._packs() == {}  # still within the TTL: no read
    clock[0] += 1
    with pytest.raises(RuntimeError):
        check_module._packs()  # expired, and the read fails: refused, not "empty"
    assert set(check_module._packs()) == {"ap"}  # the failure was not kept
    clock[0] += ttl
    assert set(check_module._packs()) == {"ap", "ar"}  # a later publish is seen
    assert bodies == []


def test_an_incomplete_pack_routes_no_process_type(monkeypatch):
    """So does an entry with no flag at all: a registry.json from before it was named."""
    packs = {
        "old": {"pack": {"process_type_to_sop": {"w": "s"}}},
        "draft": {
            "pack": {"process_type_to_sop": {"x": "s"}},
            "source": {"entity": "<AWAITING_SURVEY: q>"},
            "incomplete": True,
        },
        "ap": {"pack": {"process_type_to_sop": {"y": "s"}}, "incomplete": False},
    }
    monkeypatch.setattr(check_module, "_packs", lambda: packs)
    assert check_module._process_type_pack("x") is None
    assert check_module._process_type_pack("w") is None
    assert check_module._process_type_pack("y") == "ap"


def test_a_process_type_two_complete_packs_claim_routes_nowhere(monkeypatch):
    """A tie is refused rather than given to whichever pack the dict yields first; an
    incomplete claimant never takes a complete pack's type."""
    packs = {
        "ap": {
            "pack": {"process_type_to_sop": {"y": "s", "z": "s"}},
            "incomplete": False,
        },
        "rogue": {"pack": {"process_type_to_sop": {"y": "s"}}, "incomplete": False},
        "draft": {
            "pack": {"process_type_to_sop": {"z": "s"}},
            "source": {"entity": "<AWAITING_SURVEY>"},
            "incomplete": True,
        },
    }
    monkeypatch.setattr(check_module, "_packs", lambda: packs)
    assert check_module._process_type_pack("y") is None
    assert check_module._process_type_pack("z") == "ap"


# ── The tools a browser-created pack gets ──


def test_a_created_pack_gets_no_notification_and_no_sap_write():
    tools = names.created_pack_tools("s4")
    assert not [t for t in tools if t.endswith("___send_notification")]
    assert not [
        t
        for t in tools
        if t.split("___")[1] in (*sap_names.SAP_WRITE_TOOLS, "odata_delete")
    ]
    assert all(
        t.startswith((f"{names.NAME}-", sap_names.TARGET_PREFIX)) and "___" in t
        for t in tools
    )
    assert f"{SAP}___odata_read" in tools
    assert not [
        t for t in names.created_pack_tools() if t.startswith(sap_names.TARGET_PREFIX)
    ]


# ── Cedar statements ──


def test_every_statement_names_a_valid_target_prefixed_action():
    ids = [s.id for s in policies.STATEMENTS]
    assert len(ids) == len(set(ids))
    for s in policies.STATEMENTS:
        assert f'AgentCore::Action::"{s.action}"' in s.text
        assert s.action.startswith(f"{names.NAME}-")
        names.policy_name("agentcore-workshop", "dev", s.id)


def test_every_permit_names_a_scope():
    for s in policies.STATEMENTS:
        if s.text.startswith("permit"):
            assert 'principal.getTag("scope") == "uc-business-operations/' in s.text


def test_scoped_permit_text_is_unchanged():
    # The deployed read_cases and write_cases policies carry exactly this text;
    # any change is an update to a live policy.
    assert cedar.scoped_permit("t___x", "read", "arn:g") == (
        "permit(\n  principal is AgentCore::OAuthUser,\n"
        '  action == AgentCore::Action::"t___x",\n'
        '  resource == AgentCore::Gateway::"arn:g"\n)\nwhen {\n'
        '  principal.hasTag("scope") &&\n'
        '  (principal.getTag("scope") == "uc-business-operations/read" ||\n'
        '   principal.getTag("scope") like "uc-business-operations/read *" ||\n'
        '   principal.getTag("scope") like "* uc-business-operations/read" ||\n'
        '   principal.getTag("scope") like "* uc-business-operations/read *")\n'
        "};"
    )


def _synth_policies(targets: list[str], **kw) -> dict:
    import aws_cdk as cdk
    from aws_cdk import aws_bedrockagentcore as agentcore

    app = cdk.App()
    stack = cdk.Stack(
        app, "uc", env=cdk.Environment(account="111111111111", region="us-east-1")
    )
    attached = {
        t: agentcore.CfnGatewayTarget(
            stack,
            f"T{i}",
            name=t,
            gateway_identifier="g",
            target_configuration={"mcp": {}},
        )
        for i, t in enumerate(targets)
    }
    policies.create(
        stack,
        targets=attached,
        engine_id="e",
        gateway_arn="arn:g",
        project="p",
        environment="e",
        prefix="p-e",
        **kw,
    )
    return app.synth().get_stack_by_name("uc").template


def _of(template: dict, type_: str) -> dict:
    return {k: r for k, r in template["Resources"].items() if r["Type"] == type_}


def test_only_statements_on_attached_targets_are_created_one_at_a_time():
    template = _synth_policies([CASE])
    created = _of(template, "AWS::BedrockAgentCore::Policy")
    assert sorted(created) == ["Policyreadcases", "Policywritecases"]
    assert "Policyreadcases" in created["Policywritecases"]["DependsOn"]
    assert not _of(template, "AWS::IAM::Policy")


def test_mail_needs_write_and_cedar_copies_no_lambda_check():
    """One write permit, at any priority. The recipient allowlist and the PII check are
    the notification Lambda's, in every mode, so Cedar holds no forbid and grants the
    shared Gateway role nothing."""
    template = _synth_policies([names.NOTIFICATION_TARGET])
    created = _of(template, "AWS::BedrockAgentCore::Policy")
    assert sorted(created) == ["Policynotifications"]
    text = created["Policynotifications"]["Properties"]["Definition"]["Cedar"][
        "Statement"
    ]
    assert text.startswith("permit(") and "priority" not in text
    assert '== "uc-business-operations/write"' in text
    assert not any(s.text.startswith("forbid") for s in policies.STATEMENTS)
    assert not _of(template, "AWS::IAM::Policy")


def test_the_prompt_says_what_the_run_checks_do_about_odata_delete():
    prompt = (UC / "packs" / "_platform_prompt.txt").read_text()
    assert (
        "the Gateway refuses `odata_delete` for every caller, in every Cedar mode"
        in prompt
    )
    sys.path.insert(0, str(REPO / "layers" / "sap"))
    from sap_check import decide as layer

    reason = layer.decide(
        tool=f"{SAP}___odata_delete",
        settings=dict,
        claims=list,
        caller=lambda: "",
    )
    assert reason.startswith("odata_delete is not available")


def test_an_expired_registry_is_revalidated_on_its_etag(monkeypatch):
    from botocore.exceptions import ClientError

    calls = []

    class S3:
        def get_object(self, **kw):
            calls.append(kw)
            if kw.get("IfNoneMatch") == '"v1"':
                raise ClientError(
                    {
                        "Error": {"Code": "304"},
                        "ResponseMetadata": {"HTTPStatusCode": 304},
                    },
                    "GetObject",
                )
            body = json.dumps({"packs": {"ap": {}}}).encode()
            return {"Body": SimpleNamespace(read=lambda: body), "ETag": '"v1"'}

    clock = [100.0]
    monkeypatch.setattr(check_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(check_module, "_client", lambda service: S3())
    monkeypatch.setattr(check_module, "_config", lambda: {"registry_bucket": "reg"})
    monkeypatch.setattr(check_module, "_registry", None)
    assert set(check_module._packs()) == {"ap"}
    clock[0] += check_module.REGISTRY_TTL_SECONDS
    assert set(check_module._packs()) == {"ap"}
    assert [c.get("IfNoneMatch") for c in calls] == [None, '"v1"']
    clock[0] += check_module.REGISTRY_TTL_SECONDS - 1
    check_module._packs()
    assert len(calls) == 2, "a 304 restarts the TTL"


def _dual(approvers: set[str], dual):
    grant = _grant(
        approved=True, approved_write=RELEASE, approvers=frozenset(approvers)
    )
    lookups = _lookups(grant, limit=Decimal(5))
    lookups = run_checks.Lookups(**{**lookups.__dict__, "dual_limit": lambda p: dual})
    return decide(f"{SAP}___odata_function_import", args=RELEASE_CALL, lookups=lookups)


def test_an_approved_write_over_the_dual_limit_needs_two_different_approvers():
    # The case is 10 USD: over a dual limit of 8, under one of 20.
    assert "needs a second approver" in _dual({"alice"}, Decimal(8))
    assert _dual({"alice", "bob"}, Decimal(8)) is None
    assert _dual({"alice"}, Decimal(20)) is None
    assert (
        _dual({"alice"}, None) is None
    )  # no dual limit declared: one approval, as before


def test_a_pushed_case_needs_two_approvers_over_any_declared_dual_limit():
    # A sender's amount is its own word: 10 USD reads as under a dual limit of 20,
    # but the document in SAP may be far larger, so one approval never releases it.
    grant = _grant(approved=True, approved_write=RELEASE, approvers=frozenset({"a"}))
    pushed = run_checks.Case("ap", "10", "USD", KEY, pushed=True)
    base = _lookups(grant, limit=Decimal(5), case=pushed).__dict__
    lookups = run_checks.Lookups(**{**base, "dual_limit": lambda p: Decimal(20)})
    reason = decide(
        f"{SAP}___odata_function_import", args=RELEASE_CALL, lookups=lookups
    )
    assert "needs a second approver" in reason
    two = _grant(approved=True, approved_write=RELEASE, approvers=frozenset("ab"))
    lookups = run_checks.Lookups(
        **{**base, "grant": lambda r: two, "dual_limit": lambda p: Decimal(20)}
    )
    assert (
        decide(f"{SAP}___odata_function_import", args=RELEASE_CALL, lookups=lookups)
        is None
    )
    # No dual limit declared: one approval, as for a polled case.
    lookups = run_checks.Lookups(**{**base, "dual_limit": lambda p: None})
    assert (
        decide(f"{SAP}___odata_function_import", args=RELEASE_CALL, lookups=lookups)
        is None
    )


def test_a_dual_limit_in_doubt_needs_the_second_approver():
    def unreadable(p):
        raise ValueError("not a number")

    grant = _grant(approved=True, approved_write=RELEASE, approvers=frozenset({"a"}))
    base = _lookups(grant, limit=Decimal(5)).__dict__
    lookups = run_checks.Lookups(**{**base, "dual_limit": unreadable})
    reason = decide(
        f"{SAP}___odata_function_import", args=RELEASE_CALL, lookups=lookups
    )
    assert "dual-approval limit" in reason and "unreadable" in reason
    eur = run_checks.Case("ap", "10", "EUR", KEY)
    lookups = run_checks.Lookups(
        **{**base, "case": lambda c: eur, "dual_limit": lambda p: Decimal(20)}
    )
    assert "second approver" in decide(
        f"{SAP}___odata_function_import", args=RELEASE_CALL, lookups=lookups
    )


# ── The amount the write carries ──

UPDATE = f"{SAP}___odata_update"


def _update(amount, **kw):
    args = {
        "service_name": "API_SUPPLIERINVOICE_SRV",
        "entity_set": "A_SupplierInvoice",
        "identifier_fields": dict(KEY),
        "payload": {} if amount is None else {"InvoiceGrossAmount": amount},
    }
    lookups = _lookups(
        _grant(),
        limit=kw.pop("limit", Decimal(100)),
        case=kw.pop("case", run_checks.Case("ap", "50", "USD", KEY)),
        amount_field=kw.pop("field", "InvoiceGrossAmount"),
    )
    return decide(UPDATE, args=args, lookups=lookups, **kw)


def test_a_write_amount_within_the_case_and_the_limit_passes():
    assert _update("50") is None
    assert _update("'49.99'") is None
    assert _update(-50) is None  # a payable's sign is not its size


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        ("50.01", "more than case"),
        ("1e9", "more than case"),
        ("abc", "not a number"),
        ("NaN", "not a number"),
        ("Infinity", "not a number"),
        (None, "names no InvoiceGrossAmount"),
    ],
)
def test_a_write_amount_over_the_case_or_unreadable_is_refused(amount, expected):
    assert expected in _update(amount)


def test_a_write_amount_over_the_limit_is_refused_even_when_the_case_is_not_small():
    # The case row is 5 USD under a 1000 limit, but a write of 6 posts more than the case.
    assert "more than case" in _update("6", case=run_checks.Case("ap", "5", "USD", KEY))
    reason = _update(
        "150", case=run_checks.Case("ap", "150", "USD", KEY), limit=Decimal(100)
    )
    assert "over the 100 USD" in reason and "awaiting_human_input" in reason


def test_a_write_amount_is_checked_with_no_limit_declared_anywhere():
    # No pack declares a limit: the old gate is skipped, this one still refuses.
    assert "more than case" in _update("51", limit=None)


def test_a_pack_without_the_field_behaves_as_before():
    assert _update("999999", field=None) is None
    assert _update(None, field=None) is None


def test_a_function_import_need_not_carry_the_amount_but_one_that_does_is_checked():
    lookups = _lookups(_grant(), limit=Decimal(100), amount_field="InvoiceGrossAmount")
    assert (
        decide(f"{SAP}___odata_function_import", args=RELEASE_CALL, lookups=lookups)
        is None
    )
    dear = {
        **RELEASE_CALL,
        "parameters": {**RELEASE["fields"], "InvoiceGrossAmount": "11"},
    }
    assert "more than case" in decide(
        f"{SAP}___odata_function_import", args=dear, lookups=lookups
    )


def test_an_approved_write_lifts_the_payload_amount_check_too():
    # Same rule as the case-row gate: the exact approved write is the human's call.
    approved = {
        **RELEASE,
        "fields": {**RELEASE["fields"], "InvoiceGrossAmount": "900"},
    }
    call = {**RELEASE_CALL, "parameters": dict(approved["fields"])}
    lookups = _lookups(
        _grant(approved=True, approved_write=approved),
        limit=Decimal(100),
        amount_field="InvoiceGrossAmount",
    )
    assert decide(f"{SAP}___odata_function_import", args=call, lookups=lookups) is None


def test_an_unreadable_amount_field_declaration_refuses():
    lookups = _lookups(_grant())
    broken = run_checks.Lookups(
        **{
            **lookups.__dict__,
            "amount_field": lambda p: (_ for _ in ()).throw(ValueError("x")),
        }
    )
    assert "write_amount_field" in decide(
        f"{SAP}___odata_function_import", args=RELEASE_CALL, lookups=broken
    )


# ── SAP reads by the agent's client ──

READ = f"{SAP}___odata_read"


def _read(*, packs=None, systems=None, **kw):
    kw.setdefault("agent_callers", ("agent",))
    lookups = _lookups(_grant(packs=packs), systems=systems)
    return decide(READ, lookups=kw.pop("lookups", lookups), **kw)


def test_an_agent_read_needs_a_live_grant():
    assert _read(run_id=None, lookups=NO_READS) == run_checks.NO_RUN
    assert _read(secret="wrong") == run_checks.NO_RUN
    assert _read(client="other", app="agent") == run_checks.NO_RUN
    assert _read() is None


def test_an_agent_read_of_every_sap_read_tool_is_gated_but_delete_is_the_layers():
    for tool in ("odata_read", "odata_count", "get_metadata"):
        assert (
            decide(
                f"{SAP}___{tool}",
                run_id=None,
                agent_callers=("agent",),
                lookups=NO_READS,
            )
            == run_checks.NO_RUN
        )
    assert (
        decide(f"{SAP}___odata_delete", agent_callers=("agent",), lookups=NO_READS)
        is None
    )


def test_a_rehearsal_may_read():
    lookups = _lookups(_grant(rehearsal=True))
    assert decide(READ, agent_callers=("agent",), lookups=lookups) is None


def test_other_callers_reads_stay_as_they_were():
    # The operator's reader is a SAP caller but not the agent; another use case's
    # client is neither. Neither needs a grant, and neither touches a lookup.
    assert (
        decide(
            READ,
            app="mcp",
            client="mcp",
            run_id=None,
            agent_callers=("agent",),
            lookups=NO_READS,
        )
        is None
    )
    assert (
        decide(
            READ,
            app="other-app",
            run_id=None,
            agent_callers=("agent",),
            lookups=NO_READS,
        )
        is None
    )


def test_a_grant_with_packs_reads_only_their_sap_system():
    systems = {"ap": "s4"}
    assert _read(packs=frozenset({"ap"}), systems=systems) is None
    refused = _read(packs=frozenset({"ap"}), systems={"ap": "ecc"})
    assert "'s4'" in refused
    # A pack that names no system reads the only one there is.
    assert _read(packs=frozenset({"ap"}), systems={"ap": None}) is None
    two = {"ap": "ecc", "ar": "s4"}
    assert _read(packs=frozenset({"ap", "ar"}), systems=two) is None


def test_a_discovery_grant_reads_any_system_with_the_live_grant():
    assert _read(packs=frozenset()) is None
    assert _read(packs=None) is None


def test_an_unknown_pack_on_a_grant_refuses_the_read():
    def unknown(pack):
        raise ValueError(pack)

    lookups = _lookups(_grant(packs=frozenset({"ghost"})))
    lookups = run_checks.Lookups(**{**lookups.__dict__, "pack_system": unknown})
    assert "could not be read" in decide(
        READ, agent_callers=("agent",), lookups=lookups
    )


def test_check_reads_agent_callers_only_for_this_use_cases_own_callers(
    monkeypatch, reads
):
    monkeypatch.setattr(
        check_module, "_config", lambda: {"sap_callers": ["agent", "mcp"]}
    )
    body = {"params": {"name": READ, "arguments": {}}}
    # Another use case's client never reads `agent_callers`, which an older parameter
    # lacks, so its reads are untouched.
    assert check_module.check(body, {"Authorization": _bearer("other")}) is None
    # This use case's own callers do, and a parameter without it refuses (fail closed).
    for caller in ("mcp", "agent"):
        with pytest.raises(KeyError):
            check_module.check(body, {"Authorization": _bearer(caller)})
    monkeypatch.setattr(
        check_module,
        "_config",
        lambda: {"sap_callers": ["agent", "mcp"], "agent_callers": ["agent"]},
    )
    assert check_module.check(body, {"Authorization": _bearer("mcp")}) is None
    assert check_module.check(body, {"Authorization": _bearer("agent")}).startswith(
        run_checks.NO_RUN
    )


def test_business_operations_follows_the_header_convention():
    assert check_module.RUN_ID_HEADER == run_grant.RUN_ID_HEADER
    assert check_module.GRANT_HEADER == run_grant.grant_header("business-operations")


def test_another_use_cases_tool_of_the_same_name_is_not_gated():
    # The interceptor hands the check only these prefixes; the check holds to them too.
    assert run_checks.TARGET_PREFIX == f"{names.NAME}-"
    assert run_checks.SAP_TARGET_PREFIX == sap_names.TARGET_PREFIX
    assert run_checks.gated(f"{CASE}___update_case_state")
    assert not run_checks.gated("another-use-case___update_case_state")
    assert decide("another-use-case___update_case_state", run_id=None) is None
