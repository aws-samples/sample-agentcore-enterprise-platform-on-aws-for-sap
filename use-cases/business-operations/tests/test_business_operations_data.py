# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""business-operations: its tables and buckets, and what update_case_state may write.

The stack is built in-process from the two contract fixtures, so the posture
switch (CMK and retention under production, AWS-managed and teardown otherwise)
and the platform's KMS guard are checked without a full app synth.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from infra_utils.platform_config import load_platform_config
from infra_utils.shared_role_grants import foreign_key_grant_violations
from infra_utils.use_case_context import UseCaseContext

REPO = Path(__file__).resolve().parents[3]
UC = REPO / "use-cases" / "business-operations"
FIXTURES = REPO / "use-cases" / "business-operations" / "fixtures" / "contract"


def _template(fixture: str) -> dict:
    spec = importlib.util.spec_from_file_location("bo_stack", UC / "stack.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    platform = load_platform_config(FIXTURES / fixture)
    production = platform.deployment.mode == "production"
    ctx = UseCaseContext(
        project="p", environment="dev", platform=platform, production=production
    )
    app = cdk.App()
    stack = mod.BusinessOperationsStack(
        app,
        "p-dev-uc-business-operations",
        ctx=ctx,
        env=cdk.Environment(account="111111111111", region="us-east-1"),
    )
    return Template.from_stack(stack).to_json()


@pytest.fixture(scope="module")
def workshop() -> dict:
    return _template("business-operations.yaml")


@pytest.fixture(scope="module")
def production() -> dict:
    return _template("business-operations-production.yaml")


def _of(template: dict, kind: str) -> dict:
    return {k: r for k, r in template["Resources"].items() if r["Type"] == kind}


def test_the_scaffold_cases_table_is_kept_not_replaced(workshop):
    tables = _of(workshop, "AWS::DynamoDB::Table")
    # The logical id the live scaffold deployed; a new one replaces the table.
    assert "Cases80582F3E" in tables
    cases = tables["Cases80582F3E"]["Properties"]
    assert cases["KeySchema"] == [{"AttributeName": "case_id", "KeyType": "HASH"}]
    assert cases["TimeToLiveSpecification"]["AttributeName"] == "ttl"
    # One index per update on a table that exists: add the others one at a time.
    # pack-status-updated-index (the case list) came after status-index, and
    # pack-status-amount-index (sort=amount) after it.
    assert [i["IndexName"] for i in cases["GlobalSecondaryIndexes"]] == [
        "pack-status-index",
        "status-index",
        "pack-status-updated-index",
        "pack-status-amount-index",
    ]
    for query, sort in zip(
        cases["GlobalSecondaryIndexes"][2:], ("updated_at", "amount_sort")
    ):
        assert query["KeySchema"] == [
            {"AttributeName": "pack_id", "KeyType": "HASH"},
            {"AttributeName": "status", "KeyType": "HASH"},
            {"AttributeName": sort, "KeyType": "RANGE"},
        ]
        # Not ALL: an entry stays small, and the case's traces never ride on the index.
        assert query["Projection"]["ProjectionType"] == "INCLUDE"
        assert "agent_traces" not in query["Projection"]["NonKeyAttributes"]
    amount = {
        d["AttributeName"]: d["AttributeType"] for d in cases["AttributeDefinitions"]
    }["amount_sort"]
    assert amount == "N"
    assert len(tables) == 4


def test_an_upgrade_from_before_both_indexes_can_defer_the_amount_index(monkeypatch):
    # The first of the two deploys docs/operating.md gives: one new index only.
    monkeypatch.setenv("DEFER_CASES_AMOUNT_INDEX", "1")
    cases = _of(_template("business-operations.yaml"), "AWS::DynamoDB::Table")[
        "Cases80582F3E"
    ]["Properties"]
    assert [i["IndexName"] for i in cases["GlobalSecondaryIndexes"]] == [
        "pack-status-index",
        "status-index",
        "pack-status-updated-index",
    ]


def test_workshop_uses_aws_managed_keys_and_tears_down(workshop):
    for r in _of(workshop, "AWS::DynamoDB::Table").values():
        assert r["Properties"]["SSESpecification"] == {"SSEEnabled": True}
        assert r["DeletionPolicy"] == "Delete"
    for r in _of(workshop, "AWS::S3::Bucket").values():
        rule = r["Properties"]["BucketEncryption"]["ServerSideEncryptionConfiguration"]
        assert rule[0]["ServerSideEncryptionByDefault"]["SSEAlgorithm"] == "AES256"
        assert r["DeletionPolicy"] == "Delete"
    assert "kms-key-arn" not in json.dumps(workshop)


def test_production_uses_the_platform_cmk_and_retains(production):
    key = json.dumps(production["Parameters"])
    assert "/p/dev/security/kms-key-arn" in key
    for r in _of(production, "AWS::DynamoDB::Table").values():
        sse = r["Properties"]["SSESpecification"]
        assert sse["SSEType"] == "KMS" and "Ref" in sse["KMSMasterKeyId"]
        assert r["DeletionPolicy"] == "Retain"
        assert r["Properties"]["DeletionProtectionEnabled"] is True
    for r in _of(production, "AWS::S3::Bucket").values():
        rule = r["Properties"]["BucketEncryption"]["ServerSideEncryptionConfiguration"]
        assert rule[0]["ServerSideEncryptionByDefault"]["SSEAlgorithm"] == "aws:kms"
        assert r["DeletionPolicy"] == "Retain"
    assert not _of(production, "Custom::S3AutoDeleteObjects")


def test_production_grants_pass_the_platform_key_guard(production):
    assert (
        foreign_key_grant_violations({"p-dev-uc-business-operations": production}) == []
    )
    # And the function really is granted the key, through DynamoDB.
    assert "kms:ViaService" in json.dumps(_of(production, "AWS::IAM::Policy"))


def test_queues_take_the_platform_cmk_only_in_production(workshop, production):
    for r in _of(workshop, "AWS::SQS::Queue").values():
        assert r["Properties"]["SqsManagedSseEnabled"] is True
    queues = _of(production, "AWS::SQS::Queue")
    assert len(queues) == 2  # the agent queue and its dead-letter queue
    for r in queues.values():
        assert "SqsManagedSseEnabled" not in r["Properties"]
        assert "Ref" in r["Properties"]["KmsMasterKeyId"]
    # Whoever sends to or drains the agent queue holds the key through SQS only.
    via_sqs = {
        role["Ref"]
        for p in _of(production, "AWS::IAM::Policy").values()
        for s in p["Properties"]["PolicyDocument"]["Statement"]
        if "sqs.us-east-1.amazonaws.com"
        in json.dumps(s.get("Condition", {}).get("StringEquals", {}))
        for role in p["Properties"]["Roles"]
    }
    sends = {
        role["Ref"]
        for p in _of(production, "AWS::IAM::Policy").values()
        for s in p["Properties"]["PolicyDocument"]["Statement"]
        if "sqs:SendMessage" in s["Action"] or "sqs:ReceiveMessage" in s["Action"]
        for role in p["Properties"]["Roles"]
    }
    assert sends and sends <= via_sqs


@pytest.mark.parametrize("posture", ["workshop", "production"])
def test_no_one_deletes_a_sop_or_registry_version(posture, request):
    template = request.getfixturevalue(posture)
    denies = [
        s
        for p in _of(template, "AWS::S3::BucketPolicy").values()
        for s in p["Properties"]["PolicyDocument"]["Statement"]
        if s.get("Sid") == "DenyVersionDeletes"
    ]
    assert len(denies) == 2  # sops and registry; exemplars are unversioned
    for s in denies:
        assert s["Effect"] == "Deny" and s["Principal"] == {"AWS": "*"}
        assert s["Action"] == "s3:DeleteObjectVersion"
        # Outside production only the teardown provider is exempt; in it, no one.
        assert ("Condition" in s) == (posture == "workshop")


def test_every_table_and_bucket_is_published(workshop):
    names = {
        r["Properties"]["Name"].rsplit("/", 1)[-1]
        for r in _of(workshop, "AWS::SSM::Parameter").values()
    }
    assert {
        "cases-table",
        "config-table",
        "run-grants-table",
        "tickets-table",
        "sops-bucket",
        "registry-bucket",
        "exemplars-bucket",
    } <= names


# ── update_case_state writes an allowlist ──


CASE_ID = "ap-bchzfkch6kxm6uwqvqe5hhlkvx"


class _Table:
    """One case, `ap-…`, in pack `ap`; any other id does not exist."""

    def __init__(self):
        self.calls = []
        self.status = "processing"

    def get_item(self, Key, **kw):
        return {"Item": {"pack_id": "ap"}} if Key["case_id"] == CASE_ID else {}

    def update_item(self, **kw):
        from botocore.exceptions import ClientError

        self.calls.append(kw)
        failed = {"Error": {"Code": "ConditionalCheckFailedException"}}
        if kw["Key"]["case_id"] != CASE_ID:
            raise ClientError(failed, "UpdateItem")
        # What DynamoDB answers when the settled-status condition fails: the item as
        # it was, in AttributeValue form, because the write asks for ALL_OLD.
        if self.status in ("complete", "sap_updated"):
            item = {"case_id": {"S": CASE_ID}, "status": {"S": self.status}}
            raise ClientError({**failed, "Item": item}, "UpdateItem")
        return {"Attributes": {"case_id": kw["Key"]["case_id"]}}


@pytest.fixture
def handler(monkeypatch):
    monkeypatch.setenv("CASES_TABLE", "cases")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.syspath_prepend(str(UC / "api" / "layer" / "python"))
    spec = importlib.util.spec_from_file_location(
        "bo_case_management", UC / "case_management" / "handler.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._table = _Table()
    return mod


def _update(handler, updates, case_id=CASE_ID):
    return handler._update_case(
        {"case_id": case_id, "updates": json.dumps(updates), "action": "a"}
    )


def _written(table: _Table) -> set[str]:
    return set(table.calls[-1].get("ExpressionAttributeNames", {}).values())


def test_ttl_user_rating_and_the_audit_fields_are_never_written(handler):
    updates = {
        "status": "complete",
        "disposition": "released",
        "amount": 1.5,
        "ttl": 1,
        "user_rating": "positive",
        "ratings": {"agent": {"rating": "positive"}},
        "updated_at": "x",
        "action_log": [],
        "pack_id": "other",
    }
    reply = handler._update_case(
        {"case_id": CASE_ID, "updates": json.dumps(updates), "action": "done"}
    )
    assert _written(handler._table) == {"status", "disposition"}
    note = reply["content"][0]["text"]
    for refused in ("amount", "ttl", "user_rating", "ratings", "pack_id"):
        assert refused in note
    expr = handler._table.calls[-1]["UpdateExpression"]
    assert "updated_at = :ts" in expr and "list_append" in expr


def test_a_field_passed_beside_updates_is_applied_not_dropped(handler):
    # Live: the agent closed a released case with `status` beside `updates`, the
    # status was dropped, and the case was parked for manual review.
    handler._update_case(
        {
            "case_id": CASE_ID,
            "action": "case_closed",
            "status": "complete",
            "title": "beside",
            "updates": '{"disposition": "released", "title": "inside"}',
        }
    )
    values = handler._table.calls[-1]["ExpressionAttributeValues"]
    names = handler._table.calls[-1]["ExpressionAttributeNames"]
    written = {names[k.replace(":", "#")]: v for k, v in values.items() if k[1] == "f"}
    assert written == {
        "disposition": "released",
        "title": "inside",
        "status": "complete",
    }
    reply = handler._update_case(
        {"case_id": CASE_ID, "action": "x", "updates": "{}", "status": "processing"}
    )
    assert "error" in reply  # beside or inside, a platform status is refused


def test_an_allowed_update_names_nothing_back_and_keeps_decimals(handler):
    reply = handler._update_case(
        {"case_id": CASE_ID, "updates": '{"priority": 2.5}', "action": ""}
    )
    assert len(reply["content"]) == 1
    values = handler._table.calls[-1]["ExpressionAttributeValues"]
    assert type(values[":f0"]).__name__ == "Decimal"
    assert values[":entry"][0]["action"] == "update"


def _stack():
    sys.path.insert(0, str(UC))
    spec = importlib.util.spec_from_file_location("bo_stack_tools", UC / "stack.py")
    stack = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stack)
    return stack


def _update_tool_text(stack) -> str:
    tool = next(t for t in stack.TOOLS if t["Name"] == "update_case_state")
    return (
        tool["Description"]
        + tool["InputSchema"]["Properties"]["updates"]["Description"]
    )


def test_the_tool_description_names_exactly_the_writable_fields(handler):
    text = _update_tool_text(_stack())
    for field in handler.WRITABLE:
        assert field in text
    assert handler.WRITABLE == {
        "status",
        "title",
        "description",
        "priority",
        "disposition",
    }


@pytest.mark.parametrize("status", ["detected", "processing", "sap_posted", None])
def test_a_status_the_agent_may_not_set_is_refused_whole(handler, status):
    reply = _update(handler, {"status": status, "title": "t"})
    assert "error" in reply and not handler._table.calls


def test_an_unknown_or_malformed_case_id_is_never_created(handler):
    unknown = _update(handler, {"status": "complete"}, "ap-" + "a" * 26)
    assert unknown == {"error": f"No case found for ap-{'a' * 26}"}
    assert "error" in _update(handler, {"status": "complete"}, "scope-probe")
    assert len(handler._table.calls) == 1  # the malformed id never reached DynamoDB


@pytest.mark.parametrize("settled", ["complete", "sap_updated"])
@pytest.mark.parametrize("updates", [{"status": "error"}, {"title": "a note"}])
def test_a_settled_case_takes_no_further_update(handler, settled, updates):
    handler._table.status = settled
    reply = _update(handler, updates)
    assert reply == {
        "error": f"Not applied: case {CASE_ID} is settled as {settled!r}, and a "
        "settled case takes no further update. Put the disposition and the note in "
        "the update that settles a case."
    }
    kw = handler._table.calls[-1]
    assert kw["ConditionExpression"].startswith("attribute_exists(case_id) AND NOT (")
    assert kw["ExpressionAttributeNames"]["#st"] == "status"
    values = kw["ExpressionAttributeValues"]
    assert {values[":settled0"], values[":settled1"]} == {"complete", "sap_updated"}
    assert kw["ReturnValuesOnConditionCheckFailure"] == "ALL_OLD"


def test_the_case_id_is_normalized_before_the_write(handler):
    _update(handler, {"title": "t"}, f"  {CASE_ID} ")
    assert handler._table.calls[-1]["Key"] == {"case_id": CASE_ID}


def test_inquiry_sent_at_is_stamped_on_entry_and_cleared_on_leaving(handler):
    _update(handler, {"status": "awaiting_human_input"})
    expr = handler._table.calls[-1]["UpdateExpression"]
    assert "inquiry_sent_at = if_not_exists(inquiry_sent_at, :ts)" in expr
    # A status written without options removes the last set (decision options).
    assert expr.endswith(" REMOVE status_reason, decision_options")
    _update(handler, {"status": "complete"})
    # The reason an earlier status was parked with goes with it.
    assert handler._table.calls[-1]["UpdateExpression"].endswith(
        " REMOVE inquiry_sent_at, status_reason, decision_options"
    )
    _update(handler, {"title": "no status"})
    expr = handler._table.calls[-1]["UpdateExpression"]
    assert "inquiry_sent_at" not in expr and "status_reason" not in expr
    # Never taken from the model.
    reply = _update(handler, {"inquiry_sent_at": "2020-01-01"})
    assert "inquiry_sent_at" in reply["content"][0]["text"]


def test_the_agent_statuses_are_the_schema_enum_less_the_platforms_own(handler):
    schema = json.loads((UC / "types" / "cases.schema.json").read_text())
    enum = set(schema["definitions"]["CaseStatus"]["enum"])
    assert handler.AGENT_STATUSES == enum - {"detected", "processing"}


def test_the_advertised_statuses_are_exactly_the_ones_the_handler_accepts(handler):
    # The model was refused `processing` on 17 of 24 eval cases while the spec it read
    # listed a hand-copied set; the spec and the refusal now come from one schema.
    stack = _stack()
    assert set(stack.AGENT_STATUSES) == handler.AGENT_STATUSES
    assert len(stack.AGENT_STATUSES) == len(handler.AGENT_STATUSES)
    text = _update_tool_text(stack)
    assert f"`status` is one of {', '.join(stack.AGENT_STATUSES)}." in text
    refusal = _update(handler, {"status": "processing"})["error"]
    for status in handler.AGENT_STATUSES:
        assert status in refusal


@pytest.mark.parametrize(
    "declared,disposition,ok",
    [
        (None, "anything_at_all", True),
        (["released", "rejected_duplicate"], "released", True),
        (["released", "rejected_duplicate"], "sap_already_released", False),
        # A blank disposition sets nothing: refusing it cost a live run a retry.
        (["released", "rejected_duplicate"], "", True),
    ],
)
def test_a_pack_that_declares_dispositions_accepts_only_those(
    handler, monkeypatch, declared, disposition, ok
):
    pack = {"pack_id": "ap", **({"dispositions": declared} if declared else {})}
    monkeypatch.setattr(handler.registry, "refresh_for_request", lambda: None)
    monkeypatch.setattr(handler.registry, "packs", lambda: {"ap": {"pack": pack}})
    reply = _update(handler, {"status": "complete", "disposition": disposition})
    assert ("error" not in reply) is ok
    assert bool(handler._table.calls) is ok


@pytest.mark.parametrize("fixture", ["workshop", "production"])
def test_no_role_may_scan_delete_or_batch_write_the_cases_table(fixture, request):
    # A writer gets the item verbs its handler calls, not CDK's read-write set.
    template = request.getfixturevalue(fixture)
    writers = 0
    for name, policy in _of(template, "AWS::IAM::Policy").items():
        actions = set()
        for st in policy["Properties"]["PolicyDocument"]["Statement"]:
            if "Cases80582F3E" not in json.dumps(st.get("Resource")):
                continue
            a = st["Action"]
            actions |= {a} if isinstance(a, str) else set(a)
        assert not actions & {"dynamodb:DeleteItem", "dynamodb:BatchWriteItem"}, name
        if actions & {"dynamodb:PutItem", "dynamodb:UpdateItem"}:
            writers += 1
            assert "dynamodb:Scan" not in actions, name
    assert writers >= 3
