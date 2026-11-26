# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""business-operations pipeline: SAP push and poll, the sweeper.

Handlers run in-process against fakes; the stack is synthesized from the contract fixtures.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import sys
import urllib.parse
from pathlib import Path

import aws_cdk as cdk
import bo_sap_helper
import pytest
from aws_cdk.assertions import Template
from botocore.exceptions import ClientError

from infra_utils.platform_config import load_platform_config
from infra_utils.use_case_context import UseCaseContext

REPO = Path(__file__).resolve().parents[3]
UC = REPO / "use-cases" / "business-operations"
FIXTURES = REPO / "use-cases" / "business-operations" / "fixtures" / "contract"
for path in (
    UC / "pipeline",
    UC / "agent_knowledge",
    UC / "api" / "layer" / "python",
    REPO / "layers" / "sap" / "lib" / "sap_client",
):
    sys.path.insert(0, str(path))
# The handlers build boto3 clients at import; a region is all they need here.
_had_region = "AWS_DEFAULT_REGION" in os.environ
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
from datetime import UTC

import case_store
import erp_events
import polling_engine
import sap_http
import sap_ingest
import sap_poller
import warm_stagger

if not _had_region:
    del os.environ["AWS_DEFAULT_REGION"]

PACK = {
    "pack_id": "ap",
    "short_id": "ap",
    "service": "API_SUPPLIERINVOICE_PROCESS_SRV",
    "entity": "A_SupplierInvoice",
    "kind": "filter",
    "push": "records",
    "key": [{"field": "SupplierInvoice"}, {"field": "FiscalYear"}],
    "title": "Invoice {SupplierInvoice}",
    "field_map": {"amount": {"path": "InvoiceGrossAmount", "cast": "decimal2"}},
}


class FakeTable:
    def __init__(self, fail_put=False):
        self.items, self.fail_put = {}, fail_put

    def get_item(self, Key, **_):
        item = self.items.get(Key["case_id"])
        return {"Item": item} if item else {}

    def put_item(self, Item, ConditionExpression=None):
        if self.fail_put:
            raise ClientError(
                {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "PutItem"
            )
        if Item["case_id"] in self.items:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem"
            )
        self.items[Item["case_id"]] = Item


def _process(table, records, sent):
    return polling_engine.process_entities(
        PACK,
        records,
        table,
        case_store.case_exists,
        case_store.put_case,
        lambda case_id, pack_id, pt: sent.append(case_id),
    )


RECORD = {
    "SUPPLIERINVOICE": "5100000001",
    "FiscalYear": "2026",
    "InvoiceGrossAmount": "12.5",
}


# ── One mapping for both transports ──


def test_a_record_becomes_one_detected_case_and_a_second_sight_is_skipped():
    table, sent = FakeTable(), []
    assert _process(table, [RECORD], sent) == (1, 0, 0)
    (case,) = table.items.values()
    assert case["status"] == "detected" and case["pack_id"] == "ap"
    assert case["key"] == {"SupplierInvoice": "5100000001", "FiscalYear": "2026"}
    assert sent == [case["case_id"]]
    assert _process(table, [RECORD], sent) == (0, 1, 0)
    assert len(sent) == 1


def test_a_failed_case_write_raises_instead_of_reading_as_skipped():
    with pytest.raises(ClientError):
        _process(FakeTable(fail_put=True), [RECORD], [])


def test_a_key_with_a_blank_field_makes_no_case():
    table = FakeTable()
    assert _process(table, [{**RECORD, "FiscalYear": ""}], []) == (0, 1, 0)
    assert not table.items


BAD = {**RECORD, "SUPPLIERINVOICE": "5100000002", "InvoiceGrossAmount": "n/a"}
LATER = {**RECORD, "SUPPLIERINVOICE": "5100000003"}


def test_a_malformed_record_is_rejected_and_the_records_after_it_still_ingest(capsys):
    table, sent = FakeTable(), []
    assert _process(table, [RECORD, BAD, LATER], sent) == (2, 0, 1)
    assert {c["key"]["SupplierInvoice"] for c in table.items.values()} == {
        "5100000001",
        "5100000003",
    }
    out = capsys.readouterr().out
    # The pack and the error's type, never the record's content.
    assert "Rejected one ap record" in out and "ValueError" in out
    assert "n/a" not in out


def test_a_rejected_record_that_already_has_a_case_is_only_skipped():
    table = FakeTable()
    _process(table, [{**BAD, "InvoiceGrossAmount": "1"}], [])
    assert _process(table, [BAD], []) == (0, 1, 0)


# ── POST /erp-events: validates and enqueues ──


class Secret:
    """The ingest secret: what it holds now, and how often it was read."""

    def __init__(self, token):
        self.token, self.reads = token, 0

    def __call__(self):
        self.reads += 1
        return self.token


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def secret(monkeypatch):
    s = Secret("t" * 48)
    monkeypatch.setattr(erp_events, "_load_token", s)
    monkeypatch.setattr(erp_events, "_cached", ["", float("-inf")])
    return s


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(erp_events.time, "monotonic", c)
    return c


@pytest.fixture
def push(monkeypatch, secret):
    """What the route put on the ingest queue, one body per message."""
    sent = []
    monkeypatch.setattr(erp_events, "load_pack_sources", lambda: [PACK])
    monkeypatch.setattr(erp_events.registry, "refresh_for_request", lambda: None)
    monkeypatch.setattr(erp_events, "_send", sent.append)
    monkeypatch.setattr(sap_ingest, "SAP_SYSTEMS", {"s4": {}})
    for module in (erp_events, sap_ingest):
        monkeypatch.setattr(module, "system_of", lambda cfg: "s4")
    return sent


def _post(body, token="t" * 48, scheme="Bearer"):
    if scheme == "Basic":
        auth = "Basic " + base64.b64encode(f"SM59:{token}".encode()).decode()
    else:
        auth = f"Bearer {token}"
    raw = body if isinstance(body, str) else json.dumps(body)
    return erp_events.handler({"headers": {"Authorization": auth}, "body": raw}, None)


ENVELOPE = {"pack_id": "ap", "timestamp": "2026-11-02T00:00:00Z", "records": [RECORD]}
NUDGE = {
    "pack_id": "ap",
    "keys": [{"SupplierInvoice": "5100000001", "FiscalYear": "2026"}],
}


@pytest.mark.parametrize("body", [ENVELOPE, NUDGE, {"pack_id": "ap"}])
def test_push_enqueues_the_event_the_bus_would_deliver_and_answers_202(push, body):
    resp = _post(body, scheme="Basic")
    assert resp["statusCode"] == 202
    (sent,) = push
    assert json.loads(sent) == {
        "source": "sap.s4",
        "detail-type": "business-operations",
        "detail": body,
    }
    # The consumer takes it as it takes the rule's.
    assert sap_ingest.parse(sent)[:2] == (
        "s4",
        "records" if "records" in body else "nudge",
    )


@pytest.mark.parametrize(
    "body, reason",
    [
        ({**ENVELOPE, "extra": 1}, "shape"),
        ({**ENVELOPE, "records": []}, "shape"),
        ({**ENVELOPE, "system": "e1"}, "system"),
        ({**NUDGE, "keys": [{"SupplierInvoice": "1"}]}, "key-fields"),
        (
            {**NUDGE, "keys": [{"SupplierInvoice": "1'", "FiscalYear": "2"}]},
            "key-value",
        ),
        ({"pack_id": "ap", "version": 2}, "version"),
        ({"pack_id": 7}, "shape"),
        ([1], "shape"),
    ],
)
def test_push_refuses_what_the_consumer_would_and_enqueues_nothing(push, body, reason):
    resp = _post(body)
    assert resp["statusCode"] == 400
    assert json.loads(resp["body"])["reason"] == reason
    assert push == []


def test_push_refuses_a_wrong_token_an_unknown_pack_and_bad_json(push):
    assert _post(ENVELOPE, token="x")["statusCode"] == 401
    assert _post({**ENVELOPE, "pack_id": "nope"})["statusCode"] == 400
    assert _post("{")["statusCode"] == 400
    assert _post({"pack_id": "ap", "x": "y" * 1_000_001})["statusCode"] == 413
    assert _post({"pack_id": "ap", "x": "y" * 17_000})["statusCode"] == 413  # a nudge
    assert push == []


def test_push_refuses_a_pack_on_a_system_it_does_not_serve(push, monkeypatch):
    monkeypatch.setattr(erp_events, "system_of", lambda cfg: None)
    assert _post(NUDGE)["statusCode"] == 400 and push == []


def test_push_refuses_a_non_ascii_token_as_a_wrong_one(push):
    assert _post(ENVELOPE, token="t\u00e9" * 24)["statusCode"] == 401


def test_push_honours_the_packs_push_opt_in(push, monkeypatch):
    # Typed records set the case's key and amount, which the approval limit reads, so
    # the HTTP door honours the pack's `push` opt-in exactly as the ingest queue does.
    for mode, typed, nudge in (
        ({}, 400, 202),
        ({"push": "nudge"}, 400, 202),
        ({"push": "off"}, 400, 400),
    ):
        pack = {k: v for k, v in PACK.items() if k != "push"} | mode
        monkeypatch.setattr(erp_events, "load_pack_sources", lambda p=pack: [p])
        assert _post(ENVELOPE)["statusCode"] == typed
        assert _post(NUDGE)["statusCode"] == nudge
    assert len(push) == 2


def test_push_without_a_token_configured_refuses_everyone(push, secret):
    secret.token = ""
    assert _post(ENVELOPE, token="")["statusCode"] == 503


def test_a_rotated_token_is_accepted_at_once_and_the_old_one_refused(
    push, secret, clock
):
    assert _post(ENVELOPE)["statusCode"] == 202
    secret.token = "n" * 48
    clock.now += erp_events.REREAD_FLOOR_SECONDS + 1
    # The new token mismatches the cached one, so the secret is re-read before refusing.
    assert _post(ENVELOPE, token="n" * 48)["statusCode"] == 202
    assert _post(ENVELOPE)["statusCode"] == 401


def test_the_old_token_stops_working_within_the_ttl(push, secret, clock):
    assert _post(ENVELOPE)["statusCode"] == 202
    secret.token = "n" * 48
    clock.now += 1
    assert _post(ENVELOPE)["statusCode"] == 202  # still cached
    clock.now += erp_events.TOKEN_TTL_SECONDS
    assert _post(ENVELOPE)["statusCode"] == 401


def test_a_flood_of_wrong_tokens_rereads_at_most_once_per_floor(push, secret, clock):
    for _ in range(20):
        assert _post(ENVELOPE, token="x")["statusCode"] == 401
    assert secret.reads == 1
    clock.now += erp_events.REREAD_FLOOR_SECONDS + 1
    assert _post(ENVELOPE, token="x")["statusCode"] == 401
    assert secret.reads == 2


def test_a_failed_enqueue_answers_500_so_sap_resends(push, monkeypatch):
    def fail(body):
        raise ClientError({"Error": {"Code": "KMS.AccessDenied"}}, "SendMessage")

    monkeypatch.setattr(erp_events, "_send", fail)
    assert _post(ENVELOPE)["statusCode"] == 500


# The ingest client's access token: a Cognito client-credentials JWT.
ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_pool"


@pytest.fixture
def oauth(monkeypatch, push):
    """Sign a token like the pool's: `mint(**claims)`."""
    from types import SimpleNamespace

    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(erp_events, "ISSUER", ISSUER)
    monkeypatch.setattr(erp_events, "CLIENT_ID", "ingest-client")
    monkeypatch.setattr(erp_events, "SCOPE", "business-operations-ingest/send")
    jwks = SimpleNamespace(
        get_signing_key_from_jwt=lambda token: SimpleNamespace(key=key.public_key())
    )
    monkeypatch.setattr(erp_events, "_jwks", lambda: jwks)

    def mint(signer=key, alg="RS256", **claims):
        base = {
            "iss": ISSUER,
            "exp": 4_000_000_000,
            "token_use": "access",
            "client_id": "ingest-client",
            "scope": "business-operations-ingest/send",
        }
        return jwt.encode({**base, **claims}, signer, algorithm=alg)

    return mint


def test_the_ingest_clients_token_gets_in_beside_the_shared_one(oauth, push):
    assert _post(NUDGE, token=oauth())["statusCode"] == 202
    assert _post(NUDGE)["statusCode"] == 202  # the shared token still does
    assert len(push) == 2


@pytest.mark.parametrize(
    "claims",
    [
        {"client_id": "another-client"},
        {"scope": "uc-business-operations/read"},
        {"token_use": "id"},
        {"iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_other"},
        {"exp": 1},
    ],
)
def test_another_clients_or_a_stale_token_is_refused(oauth, push, claims):
    assert _post(NUDGE, token=oauth(**claims))["statusCode"] == 401
    assert push == []


def test_a_token_not_signed_by_the_pool_is_refused(oauth, push):
    from cryptography.hazmat.primitives.asymmetric import rsa

    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert _post(NUDGE, token=oauth(signer=other))["statusCode"] == 401
    # A token signed with a shared secret, the classic algorithm confusion.
    assert _post(NUDGE, token=oauth(signer="s" * 32, alg="HS256"))["statusCode"] == 401
    # Basic carries the shared token only.
    assert _post(NUDGE, token=oauth(), scheme="Basic")["statusCode"] == 401
    assert push == []


# ── The poller ──


class FakeSap:
    """Answers each GET with the next page; records what was asked."""

    name, base_url = "s4", "https://sap.example.com"

    def __init__(self, pages):
        self.pages, self.asked = list(pages), []

    def get_json(self, url, params=None):
        self.asked.append((url, params))
        return {"d": self.pages.pop(0) if self.pages else {"results": []}}


def test_the_poller_follows_next_and_sends_its_query_only_once():
    sap = FakeSap(
        [
            {"results": [1], "__next": "https://sap.example.com/n?$skiptoken=1"},
            {"results": [2]},
        ]
    )
    assert polling_engine._fetch_pages(
        sap, "https://sap.example.com/e", {"$top": 1}, "ap"
    ) == [1, 2]
    assert sap.asked == [
        ("https://sap.example.com/e", {"$top": 1}),
        ("https://sap.example.com/n?$skiptoken=1", None),
    ]


def test_a_next_that_never_clears_stops_at_the_page_cap_and_says_so(capsys):
    loop = {"results": [0], "__next": "https://sap.example.com/again"}
    sap = FakeSap([loop] * (polling_engine._MAX_POLL_PAGES + 5))
    got = polling_engine._fetch_pages(sap, "https://sap.example.com/e", {}, "ap")
    assert len(got) == len(sap.asked) == polling_engine._MAX_POLL_PAGES
    assert (
        f"stopped after {polling_engine._MAX_POLL_PAGES} pages"
        in capsys.readouterr().out
    )


def test_an_aging_pack_gets_its_cutoff_as_a_date_beside_its_own_filter():
    from datetime import datetime

    cfg = {
        "kind": "aging",
        "filter": "Status eq 'O'",
        "aging": {"field": "DueDate", "older_than_days": 30},
    }
    now = datetime(2026, 11, 3, 17, 0, tzinfo=UTC)
    assert polling_engine.compose_filter(cfg, now) == (
        "(Status eq 'O') and (DueDate lt datetime'2026-10-04T00:00:00')"
    )
    assert (
        polling_engine.compose_filter({**cfg, "kind": "filter"}, now) == "Status eq 'O'"
    )
    v4 = {**cfg, "odata_version": "v4"}
    assert polling_engine.compose_filter(v4, now).endswith("(DueDate lt 2026-10-04)")


def test_a_stack_cutoff_narrows_a_pack_with_a_created_field_to_newer_records(
    monkeypatch,
):
    """`pipeline.poll_created_since` reaches the poller as POLL_CREATED_SINCE. A pack that
    names `source.created_field` then reads only records created on or after it; a pack
    that names none still reads its whole backlog."""
    cfg = {"kind": "filter", "filter": "Status eq 'O'", "created_field": "CreationDate"}
    assert polling_engine.compose_filter(cfg, since="2026-11-11") == (
        "(Status eq 'O') and (CreationDate ge datetime'2026-11-11T00:00:00')"
    )
    v4 = {**cfg, "odata_version": "v4"}
    assert polling_engine.compose_filter(v4, since="2026-11-11").endswith(
        "(CreationDate ge 2026-11-11)"
    )
    no_field = {"kind": "filter", "filter": "Status eq 'O'"}
    assert (
        polling_engine.compose_filter(no_field, since="2026-11-11") == "Status eq 'O'"
    )
    assert polling_engine.compose_filter(cfg) == "Status eq 'O'"
    monkeypatch.setenv("POLL_CREATED_SINCE", "2026-11-11")
    assert polling_engine.compose_filter(cfg).endswith(
        "(CreationDate ge datetime'2026-11-11T00:00:00')"
    )


def test_a_v4_pack_reads_odata4_and_follows_a_relative_next_link():
    cfg = {
        "odata_version": "v4",
        "service": "sap/api_x/srvd_a2x/sap/x/0001",
        "entity": "Invoice",
        "select": "Id,_Item",
        "expand": "_Item",
    }
    url, params = polling_engine.query(cfg, FakeSap([]))
    assert (
        url
        == "https://sap.example.com/sap/opu/odata4/sap/api_x/srvd_a2x/sap/x/0001/Invoice"
    )
    assert params == {"$expand": "_Item", "$select": "Id,_Item"}
    v2_url, v2 = polling_engine.query(
        {**cfg, "odata_version": None, "service": "S"}, FakeSap([])
    )
    assert v2_url.endswith("/sap/opu/odata/sap/S/Invoice") and v2["$format"] == "json"

    class V4Sap(FakeSap):
        def get_json(self, url, params=None):
            self.asked.append((url, params))
            return self.pages.pop(0)

    sap = V4Sap(
        [
            {"value": [{"Id": "1"}], "@odata.nextLink": "Invoice?$skiptoken=1"},
            {"value": [{"Id": "2"}]},
        ]
    )
    assert polling_engine._fetch_pages(sap, url, params, "ap") == [
        {"Id": "1"},
        {"Id": "2"},
    ]
    assert sap.asked[1] == (f"{url}?$skiptoken=1", None)


def test_the_stagger_sends_each_packs_first_case_at_once_and_holds_the_rest(
    monkeypatch,
):
    sent, slept = [], []
    monkeypatch.setattr(warm_stagger.time, "sleep", slept.append)
    stagger = warm_stagger.Stagger(
        pack_of=lambda m: m[1], send=lambda m: sent.append(m[0]) or True, warm_delay=15
    )
    for msg in [("a1", "ap"), ("a2", "ap"), ("g1", "gr"), ("a3", "ap")]:
        stagger.offer(msg)
    assert sent == ["a1", "g1"]
    assert stagger.flush(remaining_seconds=600) == 4
    assert sent == ["a1", "g1", "a2", "a3"] and len(slept) == 1 and 0 < slept[0] <= 15


def test_a_skip_when_rule_skips_the_record_it_matches():
    cfg = {**PACK, "skip_when": [{"op": "blank", "field": "PaymentBlockingReason"}]}
    table = FakeTable()
    blocked = {**LATER, "PaymentBlockingReason": "R"}
    assert polling_engine.process_entities(
        cfg,
        [RECORD, blocked],
        table,
        case_store.case_exists,
        case_store.put_case,
        lambda *_: None,
    ) == (1, 1, 0)
    (case,) = table.items.values()
    assert case["key"]["SupplierInvoice"] == "5100000003"


def test_a_poll_reads_the_switch_once_and_logs_what_it_rejected(monkeypatch, capsys):
    table, reads, sent = FakeTable(), [], []
    sap = FakeSap([{"results": [RECORD, BAD, LATER]}])
    monkeypatch.setattr(sap_poller, "SAP_SYSTEMS", {"s4": SPEC})
    monkeypatch.setattr(sap_poller, "WARM_DELAY_SECONDS", 0)
    monkeypatch.setattr(sap_poller, "load_pack_sources", lambda: [PACK])
    monkeypatch.setattr(sap_poller.registry, "refresh_for_request", lambda: None)
    monkeypatch.setattr(sap_poller.registry, "packs", dict)
    monkeypatch.setattr(sap_poller, "get_cases_table", lambda: table)
    monkeypatch.setattr(sap_poller, "system", lambda name, spec: sap)
    monkeypatch.setattr(case_store, "QUEUE_URL", "q")
    monkeypatch.setattr(
        case_store, "send", lambda case_id, trigger: sent.append(trigger)
    )
    monkeypatch.setattr(
        case_store, "get_trigger_mode", lambda: reads.append(1) or "auto"
    )
    pack_reads = []
    monkeypatch.setattr(
        case_store, "get_pack_mode", lambda pack: pack_reads.append(pack) or "auto"
    )
    result = sap_poller.handler({}, None)
    assert result["packs"]["ap"] == {
        "system": "s4",
        "created": 2,
        "skipped": 0,
        "rejected": 1,
    }
    # The switch once per poll, and the pack's mode once, however many cases.
    assert len(reads) == 1 and pack_reads == ["ap"] and sent == ["poller", "poller"]
    assert "2 created, 0 skipped, 1 rejected" in capsys.readouterr().out


def _missed(monkeypatch, capsys, packs, existing=(), senders=("s4",)) -> list[dict]:
    """The `IngestMissed` lines one poll of `packs` on `s4` prints."""
    table = FakeTable()
    for case_id in existing:
        table.items[case_id] = {"case_id": case_id}
    monkeypatch.setattr(sap_poller, "SAP_SYSTEMS", {"s4": SPEC})
    monkeypatch.setattr(sap_poller, "INGEST_SYSTEMS", frozenset(senders))
    monkeypatch.setattr(sap_poller, "METRICS_NAMESPACE", "p-dev/business-operations")
    monkeypatch.setattr(sap_poller, "load_pack_sources", lambda: packs)
    monkeypatch.setattr(sap_poller.registry, "refresh_for_request", lambda: None)
    monkeypatch.setattr(sap_poller.registry, "packs", dict)
    monkeypatch.setattr(sap_poller, "get_cases_table", lambda: table)
    monkeypatch.setattr(
        sap_poller, "system", lambda name, spec: FakeSap([{"results": [RECORD]}])
    )
    monkeypatch.setattr(case_store, "get_trigger_mode", lambda: "manual")
    sap_poller.handler({}, None)
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines() if "{" in x]
    return [x for x in lines if x.get("IngestMissed")]


def test_a_case_the_poller_makes_for_a_pack_that_takes_pushes_is_a_missed_push(
    monkeypatch, capsys
):
    (line,) = _missed(monkeypatch, capsys, [{**PACK, "push": "nudge"}])
    (metric,) = line["_aws"]["CloudWatchMetrics"]
    assert metric["Namespace"] == "p-dev/business-operations"
    assert metric["Dimensions"] == [[], ["Pack"]]
    assert line["Pack"] == "ap" and line["IngestMissed"] == 1
    assert line["key"] == {"SupplierInvoice": "5100000001", "FiscalYear": "2026"}


def test_every_poll_of_a_pushed_pack_writes_a_zero_so_the_alarm_can_clear(
    monkeypatch, capsys
):
    case_id = polling_engine.derive_case_id(
        "ap", "ap", {"SupplierInvoice": "5100000001", "FiscalYear": "2026"}
    )
    _missed(monkeypatch, capsys, [{**PACK, "push": "nudge"}], existing=[case_id])
    # _missed consumed the output; poll again and read the raw lines.
    table_lines = []
    monkeypatch.setattr(sap_poller, "print", table_lines.append, raising=False)
    sap_poller.handler({}, None)
    zeros = [json.loads(x) for x in table_lines if '"IngestMissed": 0' in x]
    assert [z["Pack"] for z in zeros] == ["ap"]


def test_a_pack_with_push_off_a_skipped_case_or_no_sender_counts_nothing(
    monkeypatch, capsys
):
    assert _missed(monkeypatch, capsys, [{**PACK, "push": "off"}]) == []
    case_id = polling_engine.derive_case_id(
        "ap", "ap", {"SupplierInvoice": "5100000001", "FiscalYear": "2026"}
    )
    pushed = {**PACK, "push": "nudge"}
    assert _missed(monkeypatch, capsys, [pushed], existing=[case_id]) == []
    # No sender on the pack's system: the poller is the only producer there.
    assert _missed(monkeypatch, capsys, [pushed], senders=()) == []
    # The default push: nothing says SAP sends this pack, so its cases are not missed.
    default = {k: v for k, v in PACK.items() if k != "push"}
    assert _missed(monkeypatch, capsys, [default]) == []


def _polled(monkeypatch, event) -> list[str]:
    """Which packs one poller invocation polls, over packs on each `poll` value."""
    packs = [
        {**PACK, "pack_id": "every"},
        {**PACK, "pack_id": "sched", "poll": "schedule"},
        {**PACK, "pack_id": "swept", "poll": "sweep"},
        {**PACK, "pack_id": "never", "poll": "off"},
        {**PACK, "pack_id": "pushed", "kind": "event"},
    ]
    polled = []
    monkeypatch.setattr(sap_poller, "SAP_SYSTEMS", {"s4": SPEC})
    monkeypatch.setattr(sap_poller, "load_pack_sources", lambda: packs)
    monkeypatch.setattr(sap_poller.registry, "refresh_for_request", lambda: None)
    monkeypatch.setattr(sap_poller.registry, "packs", dict)
    monkeypatch.setattr(sap_poller, "get_cases_table", lambda: None)
    monkeypatch.setattr(sap_poller, "system", lambda name, spec: None)
    monkeypatch.setattr(case_store, "get_trigger_mode", lambda: "manual")
    monkeypatch.setattr(
        sap_poller,
        "poll_pack",
        lambda cfg, *_: polled.append(cfg["pack_id"]) or (0, 0, 0),
    )
    sap_poller.handler(event, None)
    return polled


def test_each_run_polls_only_the_packs_whose_poll_names_it(monkeypatch):
    # The schedule's own EventBridge event carries no `sweep`.
    assert _polled(monkeypatch, {"source": "aws.events"}) == ["every", "sched"]
    assert _polled(monkeypatch, {"sweep": True}) == ["swept"]
    for pack_id in ("swept", "never", "sched"):
        assert _polled(monkeypatch, {"pack_id": pack_id}) == [pack_id]


def test_poll_defaults_to_schedule_for_a_polled_kind_and_off_for_event():
    assert polling_engine.poll_mode({"kind": "aging"}) == "schedule"
    assert polling_engine.poll_mode({}) == "schedule"
    assert polling_engine.poll_mode({"kind": "event"}) == "off"
    assert polling_engine.poll_mode({"kind": "filter", "poll": "sweep"}) == "sweep"


def test_poll_sweep_and_off_need_push_and_event_takes_no_poll():
    from business_operations import validate_packs
    from pack_schema import SCHEMA, schema_errors

    schema = SCHEMA["properties"]["source"]
    source = {"short_id": "ap", "kind": "filter", "key": [{"field": "K"}]}
    for poll in ("schedule", "sweep", "off"):
        assert not validate_packs._source("p", {**source, "poll": poll}, {})
        assert not schema_errors({**source, "poll": poll}, schema)
    assert schema_errors({**source, "poll": "hourly"}, schema)
    assert not validate_packs._source("p", {**source, "push": "off"}, {})
    for poll in ("sweep", "off"):
        (error,) = validate_packs._source(
            "p", {**source, "poll": poll, "push": "off"}, {}
        )
        assert f"poll {poll!r}" in error and "push 'off'" in error
    event = {**source, "kind": "event", "push": "records", "poll": "off"}
    (error,) = validate_packs._source("p", event, {})
    assert "poll is read only for a polled kind" in error


# ── The SAP client ──


def test_the_sap_client_sends_its_credential_only_to_its_own_origin():
    sap = sap_http.SapSystem("s4", "https://sap.example.com", "Basic x")
    with pytest.raises(sap_http.SapError, match="outside"):
        sap.get_json("https://evil.example.com/sap/opu/odata/sap/X?$skiptoken=1")
    assert sap_http.normalize_base_url("https://h/sap/opu/odata/sap/") == "https://h"
    with pytest.raises(ValueError):
        sap_http.normalize_base_url("http://h/sap/opu/odata/sap/")


class _Secrets:
    def __init__(self, password):
        self.password, self.reads = password, 0

    def get_secret_value(self, SecretId):
        self.reads += 1
        return {
            "SecretString": json.dumps({"username": "u", "password": self.password})
        }


class _Opener:
    """Answers 401 to any Authorization but the one `accepts`."""

    def __init__(self, accepts):
        self.accepts, self.sent = accepts, []

    def open(self, req, timeout):
        import io
        import urllib.error

        self.sent.append(req.get_header("Authorization"))
        if req.get_header("Authorization") != self.accepts:
            raise urllib.error.HTTPError(req.full_url, 401, "no", {}, None)
        return io.BytesIO(b'{"d": {}}')


def _basic(password):
    return "Basic " + base64.b64encode(f"u:{password}".encode()).decode()


SPEC = {"base_url": "https://sap.example.com", "secret": "sap-s4"}


def test_a_rotated_sap_secret_is_re_read_on_a_refusal_and_retried_once(monkeypatch):
    secrets = _Secrets("old")
    opener = _Opener(accepts=_basic("old"))
    monkeypatch.setattr(sap_http, "_secrets", secrets)
    monkeypatch.setattr(sap_http, "_opener", opener)
    monkeypatch.setattr(sap_http, "_cache", {})
    sap = sap_http.system("s4", SPEC)
    sap.get_json("https://sap.example.com/x")

    secrets.password, opener.accepts = "new", _basic("new")
    assert sap_http.system("s4", SPEC).get_json("https://sap.example.com/x") == {
        "d": {}
    }
    assert opener.sent[1:] == [_basic("old"), _basic("new")]
    assert sap_http.system("s4", SPEC).authorization == _basic("new")

    # Refused with a secret that has not changed: one request, not a replay.
    opener.accepts = "nothing"
    opener.sent.clear()
    with pytest.raises(sap_http.SapError, match="HTTP 401"):
        sap.get_json("https://sap.example.com/x")
    assert opener.sent == [_basic("new")]


def test_the_sap_credential_cache_expires(monkeypatch):
    secrets = _Secrets("p")
    monkeypatch.setattr(sap_http, "_secrets", secrets)
    monkeypatch.setattr(sap_http, "_cache", {})
    now = [1000.0]
    monkeypatch.setattr(sap_http.time, "monotonic", lambda: now[0])
    sap_http.system("s4", SPEC)
    now[0] += sap_http.CREDENTIAL_TTL_SECONDS - 1
    sap_http.system("s4", SPEC)
    assert secrets.reads == 1
    now[0] += 2
    sap_http.system("s4", SPEC)
    assert secrets.reads == 2


# ── The SAP client's token flows ──

M2M_SAP = {
    "base_url": "https://sap.example.com",
    "secret": "sap-m2m",
    "flow": "m2m-sap",
    "token_url": "https://sap.example.com/sap/bc/sec/oauth2/token",
    "scopes": ["ZAPI_A_0001", "ZAPI_B_0001"],
}
M2M_OIDC = {
    "base_url": "https://sap.example.com",
    "secret": "sap-m2m",
    "flow": "m2m-oidc",
    "discovery_url": "https://idp.example.com/.well-known/openid-configuration",
    "scopes": ["api://sap/.default"],
}


class _ClientSecrets:
    def __init__(self):
        self.reads = 0

    def get_secret_value(self, SecretId):
        self.reads += 1
        return {"SecretString": json.dumps({"clientId": "c", "clientSecret": "s+/1"})}


class _TokenOpener:
    """An IdP and a SAP host: SAP accepts only the token issued last, until revoked."""

    def __init__(self, expires_in=3600):
        self.expires_in, self.issued, self.revoked = expires_in, 0, False
        self.requests: list[tuple[str, str | None, bytes | None]] = []

    def open(self, req, timeout):
        import io
        import urllib.error

        auth = req.get_header("Authorization")
        self.requests.append((req.full_url, auth, req.data))
        if req.full_url.endswith("/openid-configuration"):
            body = {"token_endpoint": "https://idp.example.com/token"}
        elif req.full_url.endswith("/token"):
            self.issued += 1
            body = {"access_token": f"t{self.issued}", "expires_in": self.expires_in}
        elif self.revoked or auth != f"Bearer t{self.issued}":
            raise urllib.error.HTTPError(req.full_url, 401, "no", {}, None)
        else:
            body = {"d": {}}
        return io.BytesIO(json.dumps(body).encode())


@pytest.fixture
def token_sap(monkeypatch):
    secrets, opener, now = _ClientSecrets(), _TokenOpener(), [1000.0]
    monkeypatch.setattr(sap_http, "_secrets", secrets)
    monkeypatch.setattr(sap_http, "_opener", opener)
    monkeypatch.setattr(sap_http, "_cache", {})
    monkeypatch.setattr(sap_http.time, "monotonic", lambda: now[0])
    return secrets, opener, now


@pytest.mark.parametrize("spec", [M2M_SAP, M2M_OIDC], ids=["m2m-sap", "m2m-oidc"])
def test_a_token_flow_fetches_a_client_credentials_token_and_sends_bearer(
    token_sap, spec
):
    _, opener, _ = token_sap
    assert sap_http.system("s4", spec).get_json("https://sap.example.com/x") == {
        "d": {}
    }
    *sign_in, (url, auth, _) = opener.requests
    if spec is M2M_OIDC:
        (discovery, token) = sign_in
        assert discovery[0] == M2M_OIDC["discovery_url"] and discovery[1] is None
        assert token[0] == "https://idp.example.com/token"
    else:
        (token,) = sign_in
        assert token[0] == M2M_SAP["token_url"]
    # The client authenticates with Basic. An IdP gets the pair form-encoded first, as
    # RFC 6749 asks; SAP's SOAUTH2 does not decode it, so it gets the pair as it is.
    pair = b"c:s+/1" if spec is M2M_SAP else b"c:s%2B%2F1"
    assert token[1] == "Basic " + base64.b64encode(pair).decode()
    form = urllib.parse.parse_qs(token[2].decode())
    assert form == {
        "grant_type": ["client_credentials"],
        "scope": [" ".join(spec["scopes"])],
    }
    assert (url, auth) == ("https://sap.example.com/x", "Bearer t1")


def test_a_token_is_reused_until_shortly_before_it_expires(token_sap):
    secrets, opener, now = token_sap
    opener.expires_in = 600  # inside CREDENTIAL_TTL_SECONDS, so the token decides
    sap = sap_http.system("s4", M2M_SAP)
    sap.get_json("https://sap.example.com/x")
    now[0] += 600 - sap_http.TOKEN_MARGIN_SECONDS - 1
    sap.get_json("https://sap.example.com/x")
    sap_http.system("s4", M2M_SAP).get_json("https://sap.example.com/x")
    assert opener.issued == 1 and secrets.reads == 1
    # Within the margin, a held system fetches a new token before it sends.
    now[0] += 2
    sap.get_json("https://sap.example.com/x")
    assert opener.issued == 2
    # Fetched ahead of time, so no request was refused to get there.
    assert [(u.rsplit("/", 1)[1], a) for u, a, _ in opener.requests][1:] == [
        ("x", "Bearer t1"),
        ("x", "Bearer t1"),
        ("x", "Bearer t1"),
        ("token", opener.requests[0][1]),
        ("x", "Bearer t2"),
    ]


def test_a_refused_token_is_fetched_again_once_and_never_looped(token_sap):
    _, opener, _ = token_sap
    sap = sap_http.system("s4", M2M_SAP)
    sap.get_json("https://sap.example.com/x")
    opener.issued += 1  # SAP now accepts only a newer token: t1 was revoked
    assert sap.get_json("https://sap.example.com/x") == {"d": {}}
    assert sap.authorization == "Bearer t3" and opener.issued == 3

    opener.revoked = True
    opener.requests.clear()
    with pytest.raises(sap_http.SapError, match="HTTP 401"):
        sap.get_json("https://sap.example.com/x")
    # One refusal, one new token, one retry, then the error.
    assert [u.rsplit("/", 1)[1] for u, _, _ in opener.requests] == ["x", "token", "x"]


def test_a_token_request_refuses_http_and_hides_the_response(token_sap, monkeypatch):
    import urllib.error

    with pytest.raises(sap_http.SapError, match="not https"):
        sap_http.system("s4", {**M2M_SAP, "token_url": "http://sap.example.com/t"})

    def refuse(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "invalid_client", {}, None)

    monkeypatch.setattr(sap_http._opener, "open", refuse)
    with pytest.raises(sap_http.SapError) as e:
        sap_http.system("s4", M2M_SAP)
    assert str(e.value) == "token request: HTTP 401"


def test_basic_never_fetches_a_token(token_sap, monkeypatch):
    _, opener, _ = token_sap
    monkeypatch.setattr(sap_http, "_secrets", _Secrets("p"))
    sap = sap_http.system("s4", SPEC)
    assert sap.oauth is None and sap.authorization == _basic("p")
    assert opener.requests == []


CASE_ID = "ap-" + "a" * 26


# ── The stack ──


def _stack(settings: dict | None = None, fixture="business-operations.yaml") -> dict:
    return Template.from_stack(_synth(settings, fixture)).to_json()


def _synth(
    settings: dict | None = None, fixture="business-operations.yaml"
) -> cdk.Stack:
    spec = importlib.util.spec_from_file_location("bo_stack_pipeline", UC / "stack.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    platform = load_platform_config(FIXTURES / fixture)
    ctx = UseCaseContext(
        project="p",
        environment="dev",
        platform=platform,
    )

    ctx, config = bo_sap_helper.load(ctx, settings)
    stack = mod.BusinessOperationsStack(
        cdk.App(),
        "p-dev-uc-business-operations",
        ctx=ctx,
        config=config,
        env=cdk.Environment(account="111111111111", region="us-east-1"),
    )
    return stack


def _of(t: dict, kind: str) -> list[dict]:
    return [r["Properties"] for r in t["Resources"].values() if r["Type"] == kind]


def _functions(t: dict) -> dict[str, dict]:
    return {p.get("Handler"): p for p in _of(t, "AWS::Lambda::Function")}


SAP = {
    "sap": {
        "systems": {
            "s4": {
                "system_role": "sandbox",
                "base_url": "https://sap.example.com/sap/opu/odata/sap/",
                "secret_name": "x/sap/s4",
            }
        }
    }
}


def test_one_fifo_queue_feeds_the_invoker_one_case_at_a_time():
    t = _stack()
    fifo = [q for q in _of(t, "AWS::SQS::Queue") if q.get("FifoQueue")]
    assert len(fifo) == 2
    (esm,) = _of(t, "AWS::Lambda::EventSourceMapping")
    assert esm["BatchSize"] == 1 and esm["ScalingConfig"] == {"MaximumConcurrency": 5}
    assert esm["FunctionResponseTypes"] == ["ReportBatchItemFailures"]
    (cases,) = [
        p
        for p in _of(t, "AWS::DynamoDB::Table")
        if p["KeySchema"][0]["AttributeName"] == "case_id"
    ]
    assert "status-index" in {g["IndexName"] for g in cases["GlobalSecondaryIndexes"]}


def test_the_sweeper_always_exists_and_the_poller_only_with_a_basic_system():
    assert "batch_runner.handler" in _functions(_stack())
    assert "sap_poller.handler" not in _functions(_stack())
    poller = _functions(_stack(SAP))["sap_poller.handler"]
    systems = poller["Environment"]["Variables"]["SAP_SYSTEMS"]
    assert "x/sap/s4" in json.dumps(systems) and "??????" not in json.dumps(systems)


def test_the_poller_runs_every_15_minutes_by_default():
    rules = _of(_stack(SAP), "AWS::Events::Rule")
    assert {"rate(15 minutes)", "rate(1 hour)"} <= {
        r["ScheduleExpression"] for r in rules if "ScheduleExpression" in r
    }


def test_the_sweep_runs_the_same_poller_hourly_with_its_own_input():
    t = _stack(SAP)
    (sweep,) = [
        r for r in _of(t, "AWS::Events::Rule") if "sweeps SAP" in r["Description"]
    ]
    assert sweep["ScheduleExpression"] == "rate(1 hour)"
    (target,) = sweep["Targets"]
    assert json.loads(target["Input"]) == {"sweep": True}
    (poll,) = [
        r for r in _of(t, "AWS::Events::Rule") if "polls SAP" in r["Description"]
    ]
    assert (
        poll["Targets"][0]["Arn"] == target["Arn"] and "Input" not in poll["Targets"][0]
    )
    rules = _of(
        _stack({**SAP, "pipeline": {"sweep_schedule": "rate(6 hours)"}}),
        "AWS::Events::Rule",
    )
    assert "rate(6 hours)" in {r.get("ScheduleExpression") for r in rules}


def test_a_checkout_pack_that_never_polls_is_named(tmp_path):
    from business_operations import packs

    for pack_id, source in {
        "a": {"poll": "off"},
        "b": {"poll": "sweep"},
        "c": {},
    }.items():
        (tmp_path / pack_id).mkdir()
        (tmp_path / pack_id / "pack.json").write_text(json.dumps({"source": source}))
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "pack.json").write_text("{}")
    assert packs.unpolled_packs(tmp_path) == ["a"]
    assert packs.unpolled_packs() == []


def _unpolled(stack: cdk.Stack, text: str = "are not polled") -> list[str]:
    """The stack's warnings that say `text` ("not polled" by default), read as JSON
    from the cloud assembly.

    Not `Annotations.find_warning`: in one lane, with the same packages, jsii raised
    "Unknown interface: ...MetadataEntry" deserializing the matched entry.
    """
    directory = Path(cdk.Stage.of(stack).synth().directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    artifact = manifest["artifacts"][stack.artifact_id]
    metadata = artifact.get("metadata") or json.loads(
        (directory / artifact["additionalMetadataFile"]).read_text()
    )
    return [
        str(entry["data"])
        for entries in metadata.values()
        for entry in entries
        if entry["type"] == "aws:cdk:warning" and text in str(entry["data"])
    ]


def test_a_development_deployment_reaching_production_sap_is_a_synth_warning():
    prd = {
        "sap": {
            "systems": {
                "s4": {**SAP["sap"]["systems"]["s4"], "system_role": "production"}
            }
        }
    }
    (warning,) = _unpolled(_synth(prd), "whose system role is production")
    assert "['s4']" in warning
    assert not _unpolled(_synth(SAP), "whose system role is production")


def test_a_token_flow_system_is_polled_with_its_token_settings():
    m2m = {
        "system_role": "sandbox",
        "base_url": "https://m2m.example.com",
        "secret_name": "x/sap/m2m",
        "flow": "m2m-sap",
        "scopes": ["s"],
        "token_url": "https://m2m.example.com/t",
    }
    stack = _synth({"sap": {"systems": {**SAP["sap"]["systems"], "m2m": m2m}}})
    assert not _unpolled(stack)
    (poller,) = [
        f
        for f in _of(Template.from_stack(stack).to_json(), "AWS::Lambda::Function")
        if f["Handler"] == "sap_poller.handler"
    ]
    systems = json.loads(poller["Environment"]["Variables"]["SAP_SYSTEMS"])
    assert systems["m2m"] == {
        "base_url": "https://m2m.example.com",
        "secret": "x/sap/m2m",
        "client": None,
        "system_role": "sandbox",
        "flow": "m2m-sap",
        "token_url": "https://m2m.example.com/t",
        "discovery_url": None,
        "scopes": ["s"],
    }
    # Basic's entry is as it was: no token settings.
    assert "flow" not in systems["s4"]


UNATTENDED = {
    "flow": "m2m-sap",
    "secret_name": "x/sap/oauth",
    "token_url": "https://sap.example.com/t",
    "scopes": ["s"],
}
OBO = {
    "flow": "obo",
    "scopes": ["api://sap-app/SAP.Access"],
    "gateway_scopes": ["api://mcp-app/.default"],
}


@pytest.mark.parametrize("own", [{}, OBO], ids=["basic", "obo"])
def test_an_unattended_block_signs_the_poller_in_whatever_the_servers_flow(own):
    s4 = {**SAP["sap"]["systems"]["s4"], **own, "unattended": UNATTENDED}
    stack = _synth({"sap": {"systems": {"s4": s4}}})
    assert not _unpolled(stack)
    t = Template.from_stack(stack).to_json()
    poller = _functions(t)["sap_poller.handler"]
    systems = json.loads(poller["Environment"]["Variables"]["SAP_SYSTEMS"])
    assert systems["s4"] == {
        "base_url": "https://sap.example.com/sap/opu/odata/sap/",
        "secret": "x/sap/oauth",
        "client": None,
        "system_role": "sandbox",
        "flow": "m2m-sap",
        "token_url": "https://sap.example.com/t",
        "discovery_url": None,
        "scopes": ["s"],
    }
    # Its role reads the unattended secret, not the server's.
    grants = json.dumps(_of(t, "AWS::IAM::Policy"))
    assert "x/sap/oauth-??????" in grants and "x/sap/s4-" not in grants


def test_an_obo_system_without_an_unattended_block_is_not_polled():
    s4 = {**SAP["sap"]["systems"]["s4"], **OBO}
    (warning,) = _unpolled(_synth({"sap": {"systems": {"s4": s4}}}))
    assert "['s4']" in warning and "unattended" in warning


def test_a_sap_system_the_poller_cannot_sign_in_to_is_a_synth_warning():
    # Bring your own runtime: no base_url or secret, so no direct caller reaches it.
    byo = {
        "system_role": "sandbox",
        "runtime_arn": "arn:aws:bedrock-agentcore:us-east-1:111111111111:runtime/x",
    }
    stack = _synth({"sap": {"systems": {**SAP["sap"]["systems"], "byo": byo}}})
    (warning,) = _unpolled(stack)
    assert "['byo']" in warning
    assert not _unpolled(_synth(SAP))


def test_the_invoker_is_never_retried_by_lambda_on_its_async_path():
    t = _stack()
    (cfg,) = _of(t, "AWS::Lambda::EventInvokeConfig")
    assert cfg["MaximumRetryAttempts"] == 0
    (invoker,) = [
        k
        for k, r in t["Resources"].items()
        if r["Type"] == "AWS::Lambda::Function" and k.startswith("AgentInvoker")
    ]
    assert cfg["FunctionName"] == {"Ref": invoker}


def test_erp_events_endpoint_gets_the_real_handler_and_a_generated_token():
    t = _stack({"api": {"erp_events_endpoint": True}})
    push = _functions(t)["erp_events.handler"]
    env = push["Environment"]["Variables"]
    assert {"INGEST_SECRET_ARN", "INGEST_QUEUE_URL", "REGISTRY_BUCKET"} <= set(env)
    assert any(
        s.get("GenerateSecretString", {}).get("GenerateStringKey") == "token"
        for s in _of(t, "AWS::SecretsManager::Secret")
    )


def test_business_metrics_carry_the_stack_in_their_namespace():
    seen = [
        (fn.get("Environment") or {}).get("Variables", {}).get("METRICS_NAMESPACE")
        for fn in _of(_stack(), "AWS::Lambda::Function")
    ]
    assert [ns for ns in seen if ns] and all(
        ns in (None, "p-dev/business-operations") for ns in seen
    )


def test_the_exemplar_builder_keys_precedents_on_the_readers_band(monkeypatch):
    import queries

    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    for k in ("CASES_TABLE", "EXEMPLAR_BUCKET", "MODEL_ID"):
        monkeypatch.setenv(k, "x")
    import exemplar_builder

    assert exemplar_builder.amount_band is queries.amount_band


def _deny(policy: dict, sid: str) -> dict:
    (statement,) = [s for s in policy["Statement"] if s.get("Sid") == sid]
    assert statement["Effect"] == "Deny" and statement["Principal"] == {"AWS": "*"}
    return statement


def test_only_the_producers_may_send_to_the_agent_queue():
    t = _stack({**SAP, "api": {"erp_events_endpoint": True}})
    (policy,) = [
        p["PolicyDocument"]
        for p in _of(t, "AWS::SQS::QueuePolicy")
        if any(
            s.get("Sid") == "OnlyTheProducersSend"
            for s in p["PolicyDocument"]["Statement"]
        )
    ]
    statement = _deny(policy, "OnlyTheProducersSend")
    senders = statement["Condition"]["ArnNotEquals"]["aws:PrincipalArn"]
    roles = {r["Fn::GetAtt"][0] for r in senders}
    # The cases and tickets routes, the ingest consumer, the poller, the sweeper and
    # the operator re-run; never the invoker, which only receives, nor push ingest,
    # which only fills the ingest queue.
    assert len(roles) == 6 and not any("Invoker" in r for r in roles)
    assert any("SapIngest" in r for r in roles)
    assert not any("ErpEvents" in r for r in roles)
    assert any("OperatorRerun" in r for r in roles)


def test_only_the_invoker_writes_a_run_grant():
    """verify.py's probe grants come from the invoker too, so no key is exempt."""
    t = _stack()
    (grants,) = [
        p
        for p in _of(t, "AWS::DynamoDB::Table")
        if p["KeySchema"][0]["AttributeName"] == "run_id"
    ]
    policy = grants["ResourcePolicy"]["PolicyDocument"]
    statement = _deny(policy, "OnlyTheInvokerWritesGrants")
    assert {"dynamodb:PutItem", "dynamodb:UpdateItem"} <= set(statement["Action"])
    assert set(statement["Condition"]) == {"ArnNotEquals"}
    role = statement["Condition"]["ArnNotEquals"]["aws:PrincipalArn"]
    assert "AgentInvoker" in role["Fn::GetAtt"][0]
    assert "verify-" not in json.dumps(policy)
    # Revoking: the invoker as a run ends, and the emergency stop; nobody else.
    revoke = _deny(policy, "OnlyTheInvokerAndTheStopRevokeGrants")
    assert set(revoke["Action"]) == {"dynamodb:DeleteItem", "dynamodb:PartiQLDelete"}
    assert not set(revoke["Action"]) & set(statement["Action"])
    roles = {
        r["Fn::GetAtt"][0]
        for r in revoke["Condition"]["ArnNotEquals"]["aws:PrincipalArn"]
    }
    assert len(roles) == 2
    assert any("AgentInvoker" in r for r in roles)
    assert any("StopFunction" in r for r in roles)


def _role_actions(t: dict, function_fragment: str) -> list[dict]:
    """Every statement on the role of the one function whose logical id has this."""
    (role,) = {
        r["Properties"]["Role"]["Fn::GetAtt"][0]
        for k, r in t["Resources"].items()
        if r["Type"] == "AWS::Lambda::Function" and function_fragment in k
    }
    return [
        s
        for r in t["Resources"].values()
        if r["Type"] == "AWS::IAM::Policy"
        and {"Ref": role} in r["Properties"].get("Roles", [])
        for s in r["Properties"]["PolicyDocument"]["Statement"]
    ]


def test_the_stop_may_write_the_switch_and_its_record_and_end_runs_only():
    t = _stack()
    statements = _role_actions(t, "StopFunction")
    actions = {a for s in statements for a in _as_list(s["Action"])}
    assert "ssm:PutParameter" in actions
    # It never mints a grant, never writes an override, never deletes a case.
    assert not actions & {
        "dynamodb:PutItem*",
        "dynamodb:BatchWriteItem",
        "dynamodb:DeleteTable",
    }
    (ssm_put,) = [s for s in statements if "ssm:PutParameter" in _as_list(s["Action"])]
    names = json.dumps(ssm_put["Resource"])
    assert "autonomy/trigger-mode" in names and "packs" not in names
    assert "autonomy/emergency-stop" in names
    # It pauses and resumes the agent queue's consumer, and only that mapping.
    (pause,) = [
        s
        for s in statements
        if "lambda:UpdateEventSourceMapping" in _as_list(s["Action"])
    ]
    assert "event-source-mapping" in json.dumps(pause["Resource"])
    assert _as_list(pause["Action"]) == ["lambda:UpdateEventSourceMapping"]
    (config_put,) = [
        s
        for s in statements
        if _as_list(s["Action"]) == ["dynamodb:PutItem", "dynamodb:UpdateItem"]
    ]
    assert config_put["Condition"] == {
        "ForAllValues:StringEquals": {"dynamodb:LeadingKeys": ["deployment"]}
    }
    # The switch it writes is the one the audit-change alert watches.
    (stop_fn,) = [
        r["Properties"]
        for k, r in t["Resources"].items()
        if r["Type"] == "AWS::Lambda::Function" and "StopFunction" in k
    ]
    switch = (
        stop_fn["Environment"]["Variables"]["TRIGGER_MODE_PREFIX"] + "/trigger-mode"
    )
    watched = [
        json.dumps(r["EventPattern"])
        for r in _of(t, "AWS::Events::Rule")
        if "ssm.amazonaws.com" in json.dumps(r.get("EventPattern", {}))
    ]
    assert any(json.dumps(switch) in w for w in watched), (switch, watched)


def _as_list(value) -> list:
    return value if isinstance(value, list) else [value]


# ── The operator re-run ──


class _Ctx:
    aws_request_id = "req-1"


@pytest.fixture
def rerun(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    import operator_rerun

    table, sent = FakeTable(), []
    monkeypatch.setattr(operator_rerun, "QUEUE_URL", "q")
    monkeypatch.setattr(operator_rerun, "get_cases_table", lambda: table)
    monkeypatch.setattr(
        operator_rerun, "send", lambda *a, **kw: sent.append((a, kw)) or "m-1"
    )
    return operator_rerun, table, sent


def test_a_rerun_queues_the_case_alone_and_claims_no_decision(rerun):
    mod, table, sent = rerun
    table.items[CASE_ID] = {"case_id": CASE_ID, "status": "error"}
    out = mod.handler({"case_id": CASE_ID, "payload": {"decision": "approved"}}, _Ctx())
    assert out["queued"] and out["status"] == "error" and out["message_id"] == "m-1"
    # Whatever the event carried: the message is the case id and the trigger, no payload.
    assert sent == [((CASE_ID, "operator-rerun"), {"dedup_id": "req-1"})]


@pytest.mark.parametrize(
    "status,force,refused",
    [
        (None, True, "no such case"),
        ("complete", True, "settled"),
        ("sap_updated", True, "settled"),
        ("awaiting_human_input", False, "--force"),
        ("manual_review_required", False, "--force"),
        ("awaiting_human_input", True, None),
        ("detected", False, None),
        ("processing", False, None),
    ],
)
def test_a_rerun_refuses_a_missing_or_settled_case_and_a_parked_one_without_force(
    rerun, status, force, refused
):
    mod, table, sent = rerun
    if status:
        table.items[CASE_ID] = {"case_id": CASE_ID, "status": status}
    out = mod.handler({"case_id": CASE_ID, "force": force}, _Ctx())
    if refused:
        assert not out["queued"] and refused in out["error"] and not sent
    else:
        assert out["queued"] and len(sent) == 1


@pytest.mark.parametrize("case_id", [None, "", 7, "not a case id at all!"])
def test_a_rerun_refuses_what_is_not_a_case_id(rerun, case_id):
    mod, _, sent = rerun
    assert not mod.handler({"case_id": case_id}, _Ctx())["queued"] and not sent


def test_the_rerun_verb_parses_its_arguments():
    sys.path.insert(0, str(UC))
    from ops import rerun_args

    assert rerun_args(["ap-x"]) == ("ap-x", 0, False)
    assert rerun_args(["ap-x", "--force"]) == ("ap-x", 0, True)
    assert rerun_args(["--dlq"]) == (None, 10, False)
    assert rerun_args(["--dlq", "--max", "3", "--force"]) == (None, 3, True)
    for bad in (
        [],
        ["a", "b"],
        ["--dlq", "--max"],
        ["--dlq", "--max", "0"],
        ["--max", "3"],
    ):
        assert rerun_args(bad) is None


def test_a_dead_letter_becomes_its_case_and_says_what_it_drops():
    sys.path.insert(0, str(UC))
    from ops import dead_letter_case

    reply = {
        "case_id": "ap-1",
        "trigger": "console-action",
        "payload": {"decision": "approved"},
    }
    assert dead_letter_case(json.dumps(reply)) == (
        "ap-1",
        "trigger 'console-action', decision 'approved'",
    )
    assert dead_letter_case(json.dumps({"case_id": "ap-1", "trigger": "batch"})) == (
        "ap-1",
        "trigger 'batch'",
    )
    for body in ("{", "[]", json.dumps({"case_id": ""}), json.dumps({"trigger": "x"})):
        assert dead_letter_case(body)[0] == ""


def test_the_dlq_redrive_deletes_a_dead_letter_only_once_its_case_is_queued(
    monkeypatch, capsys
):
    import io

    import boto3

    sys.path.insert(0, str(UC))
    import ops

    letters = [
        {
            "MessageId": "1",
            "ReceiptHandle": "h1",
            "Body": json.dumps({"case_id": "ap-ok", "trigger": "batch"}),
        },
        {
            "MessageId": "2",
            "ReceiptHandle": "h2",
            "Body": json.dumps({"case_id": "ap-done"}),
        },
        {"MessageId": "3", "ReceiptHandle": "h3", "Body": "{"},
    ]
    deleted, invoked = [], []

    class Fake:
        def get_parameter(self, Name):
            return {"Parameter": {"Value": Name.rsplit("/", 1)[1]}}

        def receive_message(self, QueueUrl, MaxNumberOfMessages, **_):
            batch, letters[:] = (
                letters[:MaxNumberOfMessages],
                letters[MaxNumberOfMessages:],
            )
            return {"Messages": batch}

        def delete_message(self, QueueUrl, ReceiptHandle):
            deleted.append(ReceiptHandle)

        def invoke(self, FunctionName, Payload):
            event = json.loads(Payload)
            invoked.append(event)
            ok = event["case_id"] == "ap-ok"
            body = (
                {
                    "queued": True,
                    "case_id": "ap-ok",
                    "status": "error",
                    "trigger": "operator-rerun",
                    "message_id": "m",
                }
                if ok
                else {"queued": False, "error": "settled as 'complete'"}
            )
            return {"Payload": io.BytesIO(json.dumps(body).encode())}

    monkeypatch.setattr(boto3, "client", lambda *a, **kw: Fake())
    assert ops.rerun(["--dlq", "--max", "5"]) == 1
    assert deleted == ["h1"]
    assert [e["case_id"] for e in invoked] == ["ap-ok", "ap-done"]
    assert all(set(e) == {"case_id", "force"} for e in invoked)
    assert "3 dead letter(s) read, 1 re-queued" in capsys.readouterr().out


@pytest.mark.parametrize(
    "source",
    [
        {"service": "API_X_SRV/../../sap/bc/adt", "entity": "A_X"},
        {"service": "API_X_SRV?sap-client=000", "entity": "A_X"},
        {"service": "API_X_SRV", "entity": "A_X/../$batch"},
        {"service": "API_X_SRV", "entity": "A_X#frag"},
        {"service": "API_X_SRV", "entity": "<AWAITING_SURVEY>"},
        {
            "service": "sap/api_x/srvd_a2x/sap/x/0001/..",
            "entity": "A_X",
            "odata_version": "v4",
        },
    ],
)
def test_the_poller_refuses_a_source_that_is_not_an_odata_name(source):
    # The poller reads as the system's service account, outside the Gateway's checks:
    # a pack's names may not step out of its service or add a query of their own.
    sap = type("Sap", (), {"base_url": "https://sap.example.com"})()
    with pytest.raises(ValueError, match="is not an OData name"):
        polling_engine.query(source, sap)


def test_the_poller_reads_v2_and_v4_names():
    sap = type("Sap", (), {"base_url": "https://sap.example.com"})()
    url, _ = polling_engine.query({"service": "API_X_SRV", "entity": "A_X"}, sap)
    assert url == "https://sap.example.com/sap/opu/odata/sap/API_X_SRV/A_X"
    v4 = {
        "service": "sap/api_x/srvd_a2x/sap/x/0001",
        "entity": "X",
        "odata_version": "v4",
    }
    url, _ = polling_engine.query(v4, sap)
    assert (
        url == "https://sap.example.com/sap/opu/odata4/sap/api_x/srvd_a2x/sap/x/0001/X"
    )


def test_the_schema_refuses_a_source_that_is_not_an_odata_name():
    import pack_schema

    def errors(source):
        pack = json.loads((UC / "packs" / "finance_ops" / "pack.json").read_text())
        pack["source"].update(source)
        return pack_schema.errors(pack)

    assert errors({"entity": "A_X/../$batch"})
    assert errors({"service": "API_X_SRV?x=1"})
    assert not errors({"entity": "<AWAITING_SURVEY: which entity>"})
