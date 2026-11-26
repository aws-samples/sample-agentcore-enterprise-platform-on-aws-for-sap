# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""business-operations knowledge: the flags, the search and agent-knowledge tools, the synth.

The stack is `<prefix>-uc-business-operations-knowledge`; see
use-cases/business-operations/business_operations/knowledge.py.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess  # nosec B404 — running our own app, no shell
import sys
from pathlib import Path
from types import SimpleNamespace

import botocore.session
import pytest
from app_synth import synth_app

REPO = Path(__file__).resolve().parents[3]
UC = REPO / "use-cases" / "business-operations"
sys.path.insert(0, str(UC))

from business_operations import config as uc_config
from business_operations import names

STACK = "agentcore-workshop-dev-uc-business-operations-knowledge"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ctx(tool: str):
    return SimpleNamespace(
        client_context=SimpleNamespace(
            custom={
                "bedrockAgentCoreToolName": f"business-operations-knowledge___{tool}"
            }
        )
    )


# ── Flags ──


def test_defaults_sop_search_on_api_docs_and_aurora_off():
    k = uc_config.load({}).knowledge
    assert (k.sop_search, k.api_docs_search, k.agent_knowledge.enabled) == (
        True,
        False,
        False,
    )


def test_knowledge_block_refuses_typos_and_inverted_capacity():
    with pytest.raises(ValueError, match="use_cases.business-operations"):
        uc_config.load({"knowledge": {"sops_search": True}})
    with pytest.raises(ValueError, match="min_acu must not exceed max_acu"):
        uc_config.load({"knowledge": {"agent_knowledge": {"min_acu": 8, "max_acu": 4}}})


def test_knowledge_base_name_matches_the_service_model():
    model = botocore.session.get_session().get_service_model("bedrock-agent")
    for op in ("CreateKnowledgeBase", "CreateDataSource"):
        meta = model.operation_model(op).input_shape.members["name"].metadata
        assert meta["pattern"] == names.KB_NAME.pattern
    assert (
        names.knowledge_base_name("acme-dev", "sops")
        == "acme-dev-business-operations-sops"
    )
    with pytest.raises(ValueError, match="Shorten"):
        names.knowledge_base_name("x" * 80, "sops")


# ── search_sap_sops is scoped to one pack ──


@pytest.fixture
def search(monkeypatch):
    monkeypatch.setenv("SOPS_KB_ID", "KB1")
    monkeypatch.delenv("API_DOCS_KB_ID", raising=False)
    return _load("bo_knowledge_search", UC / "knowledge_search" / "handler.py")


def _hit(uri: str, text: str = "t") -> dict:
    return {"content": {"text": text}, "location": {"s3Location": {"uri": uri}}}


def test_shape_heads_each_hit_with_its_uri_and_caps_the_count(search):
    hits = [_hit("s3://b/finance_ops/a.txt", "one"), _hit("", "two"), _hit("x", "3")]
    assert search.shape(hits, 2) == "[s3://b/finance_ops/a.txt]\none\n\ntwo"
    assert search.shape([], 2) == "No relevant results found."


def test_search_requires_a_pack_and_refuses_what_is_off(search, monkeypatch):
    calls = []
    fake = SimpleNamespace(
        retrieve=lambda **kw: calls.append(kw) or {"retrievalResults": []}
    )
    monkeypatch.setattr(search, "_client", fake)
    assert (
        "pack_id is required"
        in search.handler({"query": "q"}, _ctx("search_sap_sops"))["error"]
    )
    assert (
        "pack_id is required"
        in search.handler({"query": "q", "pack_id": "a/b"}, _ctx("search_sap_sops"))[
            "error"
        ]
    )
    assert search.handler({"query": "q"}, _ctx("search_sap_api_docs"))["error"] == (
        "Unknown tool: search_sap_api_docs"
    )
    assert "AgentCore Gateway" in search.handler({"query": "q"}, None)["error"]
    assert calls == []
    search.handler({"query": "q", "pack_id": "finance_ops"}, _ctx("search_sap_sops"))
    assert calls[0]["knowledgeBaseId"] == "KB1"
    # Scoped in the query, so another pack's SOPs never take a slot.
    assert calls[0]["retrievalConfiguration"]["vectorSearchConfiguration"] == {
        "numberOfResults": search.MAX_RESULTS["search_sap_sops"],
        "filter": {"equals": {"key": "pack_id", "value": "finance_ops"}},
    }


def test_a_sop_hit_carries_its_contacts_and_constants_filled(search, monkeypatch):
    """As `load_sop` serves the same text: a raw `{{CONTACT_*}}` is what the model
    copies into a ticket's assignee. The checkout's packs stand in for the registry."""
    monkeypatch.delenv("REGISTRY_BUCKET", raising=False)
    monkeypatch.delenv("PACKS_DIR", raising=False)
    monkeypatch.delenv("CONFIG_TABLE", raising=False)
    monkeypatch.setenv("CONTACTS_JSON", json.dumps({"finance_approvals": "fa@x.test"}))
    monkeypatch.setattr(search.config_overrides, "_contacts", None)
    search.registry.reset()
    hit = _hit(
        "s3://b/finance_ops/a.txt",
        "Assign {{CONTACT_FINANCE_APPROVALS}} "
        "above {{PRICE_VARIANCE_PCT}}%; {{UNDECLARED}} stays visible.",
    )
    fake = SimpleNamespace(retrieve=lambda **kw: {"retrievalResults": [hit]})
    monkeypatch.setattr(search, "_client", fake)

    def run(tool):
        return search.handler({"query": "q", "pack_id": "finance_ops"}, _ctx(tool))

    text = run("search_sap_sops")["content"][0]["text"]
    assert text == (
        "[s3://b/finance_ops/a.txt]\nAssign fa@x.test above 2%; "
        "{{UNDECLARED}} stays visible."
    )

    def unreadable():
        raise search.registry.RegistryUnavailable("down")

    monkeypatch.setattr(search.registry, "constants", unreadable)
    assert "could not be read" in run("search_sap_sops")["error"]


# ── A SOP write while a job runs is re-indexed on its own ──

FLAG = "/p/sops-reindex-pending"


class _Kb:
    """SSM and one Amazon Bedrock data source, which runs one ingestion job at a time."""

    def __init__(self):
        self.params = {"/p/sops-kb-id": "KB1", "/p/sops-data-source-id": "DS1"}
        self.jobs: list[dict] = []
        self.running = False

    @staticmethod
    def _error(code: str):
        from botocore.exceptions import ClientError

        return ClientError({"Error": {"Code": code}}, "op")

    def get_parameter(self, Name):
        if Name not in self.params:
            raise self._error("ParameterNotFound")
        return {"Parameter": {"Value": self.params[Name]}}

    def put_parameter(self, Name, Value, Type, Overwrite):
        self.params[Name] = Value

    def delete_parameter(self, Name):
        if self.params.pop(Name, None) is None:
            raise self._error("ParameterNotFound")

    def start_ingestion_job(self, **kw):
        if self.running:
            raise self._error("ConflictException")
        self.running = True
        self.jobs.append(kw)
        return {
            "ingestionJob": {
                "ingestionJobId": f"j{len(self.jobs)}",
                "status": "STARTING",
            }
        }


@pytest.fixture
def kb(monkeypatch):
    import boto3

    fake = _Kb()
    monkeypatch.setattr(boto3, "client", lambda *a, **k: fake)
    for k, v in {
        "SOPS_KB_ID_PARAM": "/p/sops-kb-id",
        "SOPS_DATA_SOURCE_ID_PARAM": "/p/sops-data-source-id",
        "SOPS_REINDEX_FLAG_PARAM": FLAG,
        "REINDEX_FLAG_PARAM": FLAG,
        "SOPS_KB_ID": "KB1",
        "SOPS_DATA_SOURCE_ID": "DS1",
    }.items():
        monkeypatch.setenv(k, v)
    sys.modules.pop("sop_store", None)
    import sop_store

    tick = _load("bo_sop_reindex", UC / "knowledge_search" / "reindex.py").handler
    yield fake, sop_store, tick
    sys.modules.pop("sop_store", None)


def test_an_idle_write_starts_one_job_and_sets_no_flag(kb):
    fake, sop_store, tick = kb
    assert sop_store.start_ingestion("a written")["status"] == "STARTING"
    assert len(fake.jobs) == 1 and FLAG not in fake.params
    fake.running = False
    assert tick() == {"status": "IDLE"}
    assert len(fake.jobs) == 1


def test_a_write_during_a_job_is_indexed_by_one_follow_up(kb):
    fake, sop_store, tick = kb
    sop_store.start_ingestion("a written")
    queued = sop_store.start_ingestion("b written")
    assert queued["status"] == "QUEUED" and "Sync" not in queued["detail"]
    sop_store.start_ingestion("c written")  # a second write queues no second job
    assert FLAG in fake.params
    # While the job runs, the follow-up waits and keeps the flag.
    assert tick() == {"status": "WAITING"}
    assert FLAG in fake.params and len(fake.jobs) == 1
    fake.running = False
    assert tick()["status"] == "STARTING"
    assert len(fake.jobs) == 2 and FLAG not in fake.params
    assert fake.jobs[1]["knowledgeBaseId"] == "KB1"
    assert fake.jobs[1]["dataSourceId"] == "DS1"
    # One follow-up per finished job: nothing more until another write queues one.
    fake.running = False
    assert tick() == {"status": "IDLE"}
    assert len(fake.jobs) == 2


def test_a_write_falls_back_to_sync_when_the_flag_cannot_be_set(kb, monkeypatch):
    fake, sop_store, _ = kb
    monkeypatch.setattr(sop_store, "REINDEX_FLAG_PARAM", "")
    fake.running = True
    assert sop_store.start_ingestion("a written")["status"] == "ALREADY_RUNNING"
    with pytest.raises(sop_store.SopRefusal) as refused:
        sop_store.sync("by hand")
    assert refused.value.status == 409


# ── Agent knowledge ──


@pytest.fixture
def queries():
    return _load("bo_ak_queries", UC / "agent_knowledge" / "queries.py")


def test_queries_self_check(queries):
    queries.demo()


def test_load_statements_orders_vendors_first_and_stubs_edge_ends(queries):
    statements = queries.load_statements(
        {
            "vendors": [{"supplier_number": "100", "name": "Acme"}],
            "vendor_edges": [
                {
                    "from_vendor": "100",
                    "to_vendor": "200",
                    "edge_type": "shared_bank_account",
                    "evidence": "same IBAN",
                }
            ],
            "lessons": [
                {
                    "process_type": "duplicate_invoice",
                    "lesson": "x",
                    "authored_by": "me",
                }
            ],
        }
    )
    sqls = [s for s, _ in statements]
    assert sqls == [
        queries.VENDOR_UPSERT_SQL,
        queries.VENDOR_UPSERT_SQL,
        queries.EDGE_UPSERT_SQL,
        queries.LESSON_INSERT_SQL,
    ]
    assert statements[0][1] == {"supplier_number": "100", "name": "Acme", "country": ""}
    assert statements[1][1]["supplier_number"] == "200"
    assert statements[3][1]["scope_vendor"] == ""
    # Every value is a bound parameter, never SQL text.
    assert all(":" + k in s for s, p in statements for k in p)


@pytest.mark.parametrize(
    "doc, reason",
    [
        ({"vendor": []}, "expected an object"),
        ({"vendors": {}}, "must be a list"),
        ({"vendors": [{"supplier_number": "1", "nmae": "x"}]}, "allowed keys"),
        ({"vendors": [{"supplier_number": 1}]}, "must be a string"),
        (
            {"lessons": [{"process_type": "p", "lesson": " ", "authored_by": "a"}]},
            "missing",
        ),
        (
            {
                "vendor_edges": [
                    {
                        "from_vendor": "1",
                        "to_vendor": "1",
                        "edge_type": "e",
                        "evidence": "x",
                    }
                ]
            },
            "itself",
        ),
    ],
)
def test_load_statements_refuses_a_bad_file(queries, doc, reason):
    with pytest.raises(ValueError, match=reason):
        queries.load_statements(doc)


def test_agent_knowledge_tools_need_the_gateway(monkeypatch):
    for k in ("CLUSTER_ARN", "SECRET_ARN", "DATABASE_NAME"):
        monkeypatch.setenv(k, "x")
    monkeypatch.syspath_prepend(str(UC / "agent_knowledge"))
    handler = _load("bo_ak_handler", UC / "agent_knowledge" / "handler.py")
    assert "AgentCore Gateway" in handler.handler({}, None)["error"]
    assert handler.handler({}, _ctx("check_vendor_risk"))["error"] == (
        "supplier_number is required"
    )


def test_knowledge_load_refuses_a_bad_file_before_touching_aws(tmp_path):
    bad = tmp_path / "load.json"
    bad.write_text('{"vendors": [{"supplier_number": 1}]}')
    result = subprocess.run(  # nosec B603 — fixed argv
        [sys.executable, str(UC / "ops.py"), "knowledge-load", str(bad)],
        cwd=UC,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "AWS_REGION": "us-east-1"},
    )
    assert result.returncode == 1
    assert "must be a string" in result.stderr


# ── Synth ──


def _synth(tmp_path: Path, knowledge: dict | None, stack: str = STACK) -> dict:
    block = json.dumps({"knowledge": knowledge}) if knowledge is not None else "{}"
    result, out = synth_app(
        (
            REPO
            / "use-cases/business-operations/fixtures/contract/business-operations.yaml"
        )
        .read_text()
        .replace("business-operations: {}", f"business-operations: {block}")
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads((out / f"{stack}.template.json").read_text())


def _types(template: dict) -> list[str]:
    return [r["Type"] for r in template["Resources"].values()]


def test_default_synth_is_sop_search_alone(tmp_path):
    template = _synth(tmp_path, None)
    types = _types(template)
    kb = "AWS::Bedrock::KnowledgeBase"  # publication-gate: allow wording
    assert types.count(kb) == 1
    assert "AWS::RDS::DBCluster" not in types and "AWS::S3::Bucket" not in types
    ssm = [p for p in template["Parameters"].values() if "Default" in p]
    assert any(
        p["Default"]
        == "/agentcore-workshop/dev/use-cases/business-operations/sops-bucket"
        for p in ssm
    )


def test_the_sop_follow_up_polls_every_five_minutes_with_scoped_grants(tmp_path):
    resources = _synth(tmp_path, None)["Resources"]
    (fn_id,) = [
        k
        for k, r in resources.items()
        if r["Type"] == "AWS::Lambda::Function"
        and r["Properties"]["Handler"] == "reindex.handler"
    ]
    (rule,) = [r for r in resources.values() if r["Type"] == "AWS::Events::Rule"]
    assert rule["Properties"]["ScheduleExpression"] == "rate(5 minutes)"
    assert rule["Properties"]["Targets"][0]["Arn"] == {"Fn::GetAtt": [fn_id, "Arn"]}
    (policy,) = [
        r["Properties"]["PolicyDocument"]["Statement"]
        for k, r in resources.items()
        if r["Type"] == "AWS::IAM::Policy" and k.startswith(f"{fn_id[:-8]}ServiceRole")
    ]
    grants = {
        " ".join(
            sorted(st["Action"] if isinstance(st["Action"], list) else [st["Action"]])
        ): json.dumps(st["Resource"])
        for st in policy
    }
    ssm = grants["ssm:DeleteParameter ssm:GetParameter ssm:PutParameter"]
    assert ssm.endswith('use-cases/business-operations/sops-reindex-pending"]]}')
    bedrock = grants["bedrock:StartIngestionJob"]
    assert "knowledge-base/" in bedrock and "SopsKnowledgeBase" in bedrock
    assert len(grants) == 3  # the two above and X-Ray
    # The two SOP writers may set the flag, and only set it.
    main = _synth(tmp_path, None, STACK.removesuffix("-knowledge"))["Resources"]
    flag_grants = [
        st["Action"]
        for r in main.values()
        if r["Type"] == "AWS::IAM::Policy"
        for st in r["Properties"]["PolicyDocument"]["Statement"]
        if "sops-reindex-pending" in json.dumps(st["Resource"])
    ]
    assert flag_grants == ["ssm:PutParameter", "ssm:PutParameter"]


def test_all_off_leaves_one_parameter(tmp_path):
    off = {"sop_search": False, "api_docs_search": False}
    assert _types(_synth(tmp_path, off)) == ["AWS::SSM::Parameter"]


def test_all_on_chains_four_scoped_policies(tmp_path):
    on = {"api_docs_search": True, "agent_knowledge": {"enabled": True}}
    resources = _synth(tmp_path, on)["Resources"]
    types = [r["Type"] for r in resources.values()]
    kb = "AWS::Bedrock::KnowledgeBase"  # publication-gate: allow wording
    source = "AWS::Bedrock::DataSource"  # publication-gate: allow wording
    assert types.count(kb) == 2
    sources = [r for r in resources.values() if r["Type"] == source]
    assert len(sources) == 2
    assert {s["Properties"]["DataDeletionPolicy"] for s in sources} == {"RETAIN"}
    assert types.count("AWS::RDS::DBCluster") == 1
    # The Aurora VPC routes nowhere.
    assert "AWS::EC2::NatGateway" not in types
    assert "AWS::EC2::InternetGateway" not in types
    policies = {
        k: r
        for k, r in resources.items()
        if r["Type"] == "AWS::BedrockAgentCore::Policy"
    }
    assert len(policies) == 4
    chained = [set(r["DependsOn"]) for r in policies.values()]
    # One CreatePolicy at a time: every policy but the first waits on another.
    assert sum(bool(d & set(policies)) for d in chained) == 3
    for r in policies.values():
        assert "uc-business-operations/read" in json.dumps(
            r["Properties"]["Definition"]
        )
    # The main stack's exemplar builder is granted the precedent write here, and no
    # wider: the one action, on this cluster.
    (grant,) = [
        r["Properties"]["PolicyDocument"]["Statement"]
        for k, r in resources.items()
        if k.startswith("ExemplarBuilderRolePolicy")
    ]
    assert any(st["Action"] == "rds-data:ExecuteStatement" for st in grant)


class _Resuming(Exception):
    pass


class _PausedCluster:
    """A Data API client whose cluster is paused for the first `refusals` calls."""

    exceptions = SimpleNamespace(DatabaseResumingException=_Resuming)

    def __init__(self, refusals: int):
        self.refusals, self.calls = refusals, 0

    def execute_statement(self, **kw):
        self.calls += 1
        if self.calls <= self.refusals:
            raise _Resuming
        return {"sql": kw["sql"]}


def test_a_statement_waits_out_a_paused_cluster(queries, monkeypatch):
    monkeypatch.setattr(queries.time, "sleep", lambda s: None)
    # Live, the first call after a pause was refused: every caller must retry it.
    client = _PausedCluster(refusals=2)
    assert queries.execute(client, attempts=3, sql="SELECT 1") == {"sql": "SELECT 1"}
    assert client.calls == 3
    with pytest.raises(_Resuming):
        queries.execute(_PausedCluster(refusals=9), attempts=3, sql="SELECT 1")
