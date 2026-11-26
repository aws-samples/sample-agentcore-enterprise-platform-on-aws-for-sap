# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""business-operations MCP clients and operator verbs: the reads it starts the Gateway
proxy with, the constants the authoring MCP copies, and the pure parts of ops.py.

The authoring MCP is stdlib-only so it can travel to an analyst's laptop alone, which
is why it copies three values instead of importing them; each is pinned here.
"""

from __future__ import annotations

import io
import json
import re
import sys
from datetime import date
from pathlib import Path

import pytest

UC = Path(__file__).resolve().parents[3] / "use-cases" / "business-operations"
sys.path.insert(0, str(UC))
sys.path.insert(0, str(UC / "api" / "layer" / "python"))
sys.path.insert(0, str(UC / "tools" / "mcp-authoring"))

import mcp_authoring
import ops
import pack_compose
from business_operations import api
from business_operations import config as uc_config
from business_operations.names import NAME

from sap_kit import mcp_gateway_proxy as proxy

# ── What the authoring MCP copies ──


def test_the_authoring_redirect_is_the_one_the_console_client_registers():
    # Cognito matches redirect_uri exactly; a drift fails every sign-in.
    assert mcp_authoring.REDIRECT_URI == api.AUTHORING_REDIRECT_URI


def test_create_pack_enums_match_the_composer():
    assert mcp_authoring._CREATE_KINDS == pack_compose.CREATE_KINDS
    assert mcp_authoring._ATTRIBUTE_TYPES == pack_compose.ATTRIBUTE_TYPES


@pytest.mark.parametrize(
    "path",
    [
        "api/trigger_mode/index.py",
        "containment/index.py",
        "business_operations/containment.py",
    ],
)
def test_trigger_mode_attribution_suffix_matches_the_route(path):
    source = (UC / path).read_text()
    assert re.search(
        rf'^ACTOR_SUFFIX = "{re.escape(ops.ACTOR_SUFFIX)}"$', source, re.MULTILINE
    )


# ── The Gateway proxy, as this use case starts it ──


@pytest.mark.parametrize(
    "name, offered",
    [
        ("sap-s4___odata_read", True),
        # Reads too, but pack-scoped, so the run checks refuse them outside a run.
        ("business-operations-knowledge___search_sap_sops", False),
        ("sap-s4___odata_update", False),
        ("business-operations-case-management___update_case_state", False),
        # A read, but the run checks refuse it outside a run.
        ("business-operations-case-management___get_case_state", False),
        ("business-operations-tickets___demo_get_ticket", False),
        # Another tenant's same-named tool on the shared Gateway.
        ("other-use-case___odata_read", False),
        ("odata_read", False),
    ],
)
def test_the_proxy_offers_only_this_use_cases_reads(name, offered):
    assert proxy.allowlist(NAME, ops.MCP_READ_TOOLS)(name) is offered


def test_the_proxy_offers_no_tool_the_run_checks_refuse_outside_a_run():
    """The proxy never has a run, so a tool the run checks gate would only be refused."""
    from business_operations_check import run_checks

    gated = [t for t in ops.MCP_READ_TOOLS if run_checks.gated(f"{NAME}-target___{t}")]
    assert gated == []


# ── ops.py ──


def _cfg(**block):
    return uc_config.load(block)


@pytest.mark.parametrize(
    "today, months, window",
    [
        (date(2026, 11, 2), 1, ("2026-11-01", "2026-11-03")),
        (date(2026, 11, 30), 3, ("2026-09-01", "2026-12-01")),
        (date(2026, 2, 15), 3, ("2025-12-01", "2026-02-16")),
        (date(2026, 12, 31), 1, ("2026-12-01", "2027-01-01")),
    ],
)
def test_month_window(today, months, window):
    assert ops.month_window(today, months) == window


def test_trigger_mode_set_auto_refuses_without_confirmation(monkeypatch, capsys):
    monkeypatch.delenv("NON_INTERACTIVE", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert not ops._confirmed([], "Go?")
    assert ops._confirmed(["--yes"], "Go?")
    monkeypatch.setenv("NON_INTERACTIVE", "1")
    assert ops._confirmed([], "Go?")


# ── Irreversible verbs ask first ──


@pytest.mark.parametrize(
    "argv",
    [
        ["add-user", "a@example.com", "--pack-admin"],
        ["rerun", "case-1", "--force"],
        ["rerun", "--dlq", "--force"],
        ["bundle", "import", "packs/finance_ops", "--apply", "--force"],
        ["evals-setup"],
        ["break-glass"],
        ["break-glass", "--undo"],
    ],
)
def test_an_irreversible_verb_refuses_without_confirmation(argv, monkeypatch, capsys):
    # No terminal and no --yes: the verb stops before any AWS call.
    monkeypatch.delenv("NON_INTERACTIVE", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setitem(sys.modules, "boto3", None)  # an AWS call would raise
    assert ops.main(argv) == 1
    assert "needs a terminal, or --yes" in capsys.readouterr().err


def test_operator_policies_name_only_this_deployments_resources():
    from business_operations import operator_policy as op

    own = "use-cases/business-operations"
    values = {
        f"{own}/pack-composer-function": "p-dev-uc-bo-PackComposer",
        f"{own}/test-data-function": "p-dev-uc-bo-TestData",
        f"{own}/operator-rerun-function": "p-dev-uc-bo-Rerun",
        f"{own}/stop-function": "p-dev-uc-bo-Stop",
        f"{own}/agent-dlq-url": "https://sqs.us-west-2.amazonaws.com/1/p-dev-dlq",
        "auth/user-pool-id": "us-west-2_abc",
    }
    # evals is pinned on its own below: built-in judges and models carry no account.
    for group in set(op.GROUPS) - {"evals"}:
        assert set(op.parameters(group)) <= set(values) | {f"{own}/sap-poller-function"}
        doc = op.render(
            group,
            region="us-west-2",
            account="1",
            project="p",
            env="dev",
            values=values,
        )
        for st in doc["Statement"]:
            assert st["Effect"] == "Allow"
            assert all(
                ":1:" in r and "*" not in r.split(":")[-1][:-1] for r in st["Resource"]
            )
    # The trigger-mode policy invokes the stop function, for `emergency-stop`, and no other.
    policy = op.render(
        "trigger-mode",
        region="us-west-2",
        account="1",
        project="p",
        env="dev",
        values=values,
    )
    (stop_invoke,) = [
        s for s in policy["Statement"] if s["Action"] == ["lambda:InvokeFunction"]
    ]
    assert stop_invoke["Resource"] == [
        "arn:aws:lambda:us-west-2:1:function:p-dev-uc-bo-Stop"
    ]
    seed = op.render(
        "seed", region="us-west-2", account="1", project="p", env="dev", values=values
    )
    invoke = next(
        s for s in seed["Statement"] if s["Action"] == ["lambda:InvokeFunction"]
    )
    # No SAP system, no poller: the statement leaves it out rather than guess.
    assert invoke["Resource"] == [
        "arn:aws:lambda:us-west-2:1:function:p-dev-uc-bo-TestData",
        "arn:aws:lambda:us-west-2:1:function:p-dev-uc-bo-Rerun",
    ]
    assert "arn:aws:sqs:us-west-2:1:p-dev-dlq" in json.dumps(seed)
    assert ops.main(["operator-policy", "nope"]) == 2


def test_the_evals_policy_is_what_evals_run_touches_and_nothing_else():
    from business_operations import operator_policy as op

    sys.path.insert(0, str(UC / "evals"))
    import setup_evaluations

    assert op.JUDGE_MODEL == setup_evaluations.JUDGE_MODEL
    own = "use-cases/business-operations"
    values = {
        f"{own}/test-data-function": "p-dev-TestData",
        f"{own}/invoker-function": "p-dev-Invoker",
        f"{own}/pack-composer-function": "p-dev-Composer",
        f"{own}/cases-table": "p-dev-cases",
        f"{own}/tickets-table": "p-dev-tickets",
        "runtimes/business-operations/arn": "arn:aws:bedrock-agentcore:r:1:runtime/rt-1",
        "evals/results-bucket": "p-evals-results",
        "evals/lease-table": "p-leases",
        f"{own}/promotion-role": "development",
    }
    assert set(op.parameters("evals")) == set(values)
    doc = op.render(
        "evals", region="r", account="1", project="p", env="dev", values=values
    )
    by_action: dict[str, set] = {}
    denies = [st for st in doc["Statement"] if st["Effect"] == "Deny"]
    for st in doc["Statement"]:
        if st["Effect"] == "Deny":
            continue
        for a in st["Action"]:
            by_action.setdefault(a, set()).update(st["Resource"])
    # The cases table is writable, but never a case's approvals.
    assert denies == [
        {
            "Sid": "NeverWriteACasesApprovals",
            "Effect": "Deny",
            "Action": ["dynamodb:PutItem"],
            "Resource": ["arn:aws:dynamodb:r:1:table/p-dev-cases"],
            "Condition": {
                "ForAnyValue:StringEquals": {"dynamodb:Attributes": ["action_log"]}
            },
        }
    ]
    lease = next(st for st in doc["Statement"] if st["Sid"] == "TakeAndReturnTheLease")
    assert lease["Condition"] == {
        "ForAllValues:StringEquals": {"dynamodb:LeadingKeys": ["dev"]}
    }
    assert by_action["lambda:InvokeFunction"] == {
        f"arn:aws:lambda:r:1:function:p-dev-{f}"
        for f in ("TestData", "Invoker", "Composer")
    }
    assert by_action["dynamodb:PutItem"] == {
        "arn:aws:dynamodb:r:1:table/p-dev-cases",
        "arn:aws:dynamodb:r:1:table/p-leases",
    }
    assert by_action["dynamodb:DeleteItem"] == {"arn:aws:dynamodb:r:1:table/p-leases"}
    assert (
        "arn:aws:logs:r:1:log-group:/aws/bedrock-agentcore/runtimes/rt-1-DEFAULT"
        in (by_action["logs:StartQuery"])
    )
    assert "arn:aws:s3:::p-evals-results/*" in by_action["s3:PutObject"]
    # A bare "*" only where the action has no resource type.
    assert {a for a, rs in by_action.items() if "*" in rs} == {
        "bedrock-agentcore:ListEvaluators",
        "logs:GetQueryResults",
    }
    # Nothing that writes SAP outside the seeder, deploys, or touches IAM.
    assert not any(
        a.split(":")[0] in {"iam", "cloudformation", "secretsmanager"}
        for a in by_action
    )


@pytest.mark.parametrize("role", ["quality", "production", None])
def test_the_evals_policy_is_refused_for_any_stack_but_development(role, capsys):
    from business_operations import operator_policy as op

    values = {} if role is None else {op.ROLE_KEY: role}
    with pytest.raises(op.PolicyRefused, match="only for a development stack"):
        op.render("evals", region="r", account="1", project="p", env="e", values=values)
    # The other groups take no role: their verbs hold the composer's own rules.
    op.render("bundle", region="r", account="1", project="p", env="e", values=values)


def test_operator_policy_evals_fails_on_a_quality_stack(monkeypatch, capsys):
    from business_operations import operator_policy as op

    class Ssm:
        class exceptions:
            ParameterNotFound = KeyError

        def get_parameter(self, Name):
            if Name.endswith(op.ROLE_KEY):
                return {"Parameter": {"Value": "quality"}}
            raise KeyError(Name)

    class Session:
        region_name = "r"

        def __init__(self, **_):
            pass

        def client(self, name):
            if name == "sts":
                return type(
                    "S", (), {"get_caller_identity": lambda _s: {"Account": "1"}}
                )()
            return Ssm()

    boto3 = type(sys)("boto3")
    boto3.session = type(sys)("boto3.session")
    boto3.session.Session = Session
    monkeypatch.setitem(sys.modules, "boto3", boto3)
    monkeypatch.setattr(ops, "_deployment", lambda: ("p", "e", "r"))
    assert ops.main(["operator-policy", "evals"]) == 1
    err = capsys.readouterr().err
    assert "FAIL: this stack's promotion role is quality" in err


# ── remote-client-config: the connector form, and the secret never printed ──


def test_remote_client_fields_quick_and_copilot():
    quick = dict(
        ops.remote_client_fields(
            "quick", "https://gw/mcp", "https://d", "CID", ["https://cb"]
        )
    )
    assert quick["MCP server endpoint"] == "https://gw/mcp"
    assert quick["Authorization URL"] == "https://d/oauth2/authorize"
    assert quick["Token URL"] == "https://d/oauth2/token"
    assert quick["Client ID"] == "CID" and "https://cb" in quick["Redirect URL"]
    copilot = dict(
        ops.remote_client_fields(
            "copilot-studio", "https://gw/mcp", "https://d", "C", []
        )
    )
    assert copilot["Refresh URL"] == copilot["Token URL"]
    assert copilot["Scopes"] == "openid email profile"


def test_remote_client_config_never_prints_the_secret(monkeypatch, capsys):
    from types import SimpleNamespace

    params = {
        "/p/e/people-gateway/remote-clients/quick/client-id": "CID",
        "/p/e/auth/user-pool-id": "pool",
        "/p/e/people-gateway/url": "https://gw/mcp",
        "/p/e/auth/domain-url": "https://d",
    }
    fake_ssm = SimpleNamespace(
        exceptions=SimpleNamespace(ParameterNotFound=KeyError),
        get_parameter=lambda Name: {"Parameter": {"Value": params[Name]}},
    )
    fake_idp = SimpleNamespace(
        describe_user_pool_client=lambda **_: {
            "UserPoolClient": {"ClientSecret": "s3cr3t", "CallbackURLs": ["https://cb"]}
        }
    )
    import boto3

    monkeypatch.setattr(
        boto3, "client", lambda svc, **_: fake_ssm if svc == "ssm" else fake_idp
    )
    monkeypatch.setattr(ops, "_deployment", lambda: ("p", "e", "us-east-1"))
    copied = []
    monkeypatch.setattr(ops, "_to_clipboard", lambda text: copied.append(text) or True)
    assert ops.main(["remote-client-config", "quick"]) == 0
    out = capsys.readouterr()
    assert copied == ["s3cr3t"]
    assert "s3cr3t" not in out.out + out.err and "CID" in out.out

    monkeypatch.setattr(ops, "_to_clipboard", lambda text: False)
    assert ops.main(["remote-client-config", "quick"]) == 0
    out = capsys.readouterr()
    assert "s3cr3t" not in out.out + out.err and "describe-user-pool-client" in out.out
    assert ops.main(["remote-client-config", "other"]) == 2


# ── remote-client-config issuer: the organisation's own issuer, no secret ──


def test_issuer_fields_come_from_the_discovery_document():
    fields = dict(
        ops.issuer_fields(
            "https://gw/mcp",
            {
                "authorization_endpoint": "https://idp.example.com/authorize",
                "token_endpoint": "https://idp.example.com/token",
            },
            "a,b",
            "gateway",
        )
    )
    assert fields["MCP server endpoint"] == "https://gw/mcp"
    assert fields["Authorization URL"] == "https://idp.example.com/authorize"
    assert fields["Token URL"] == "https://idp.example.com/token"
    assert fields["Client ID"] == "a, b"
    assert fields["Audience (resource)"] == "https://gw/mcp"
    literal = dict(ops.issuer_fields("https://gw/mcp", {}, "a", "api://people"))
    assert literal["Audience (resource)"] == "api://people"


def test_issuer_config_prints_the_fields_and_asks_for_no_secret(monkeypatch, capsys):
    import urllib.request
    from types import SimpleNamespace

    params = {
        "/p/e/people-gateway/issuer/discovery-url": "https://idp.example.com/.well-known/openid-configuration",
        "/p/e/people-gateway/issuer/clients": "example-client",
        "/p/e/people-gateway/issuer/audience": "gateway",
        "/p/e/people-gateway/url": "https://gw/mcp",
    }
    fake_ssm = SimpleNamespace(
        exceptions=SimpleNamespace(ParameterNotFound=KeyError),
        get_parameter=lambda Name: {"Parameter": {"Value": params[Name]}},
    )
    import boto3

    calls = []
    monkeypatch.setattr(boto3, "client", lambda svc, **_: calls.append(svc) or fake_ssm)
    monkeypatch.setattr(ops, "_deployment", lambda: ("p", "e", "us-east-1"))
    doc = json.dumps({"token_endpoint": "https://idp.example.com/token"}).encode()

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp(doc))
    assert ops.main(["remote-client-config", "issuer"]) == 0
    out = capsys.readouterr().out
    assert "https://idp.example.com/token" in out and "example-client" in out
    # No secret is read anywhere: only SSM.
    assert calls == ["ssm"]

    params.pop("/p/e/people-gateway/issuer/discovery-url")
    assert ops.main(["remote-client-config", "issuer"]) == 1


def test_issuer_config_refuses_a_plain_http_discovery_url(monkeypatch):
    from types import SimpleNamespace

    import boto3

    fake_ssm = SimpleNamespace(
        exceptions=SimpleNamespace(ParameterNotFound=KeyError),
        get_parameter=lambda Name: {"Parameter": {"Value": "http://idp.example.com/x"}},
    )
    monkeypatch.setattr(boto3, "client", lambda *_a, **_k: fake_ssm)
    monkeypatch.setattr(ops, "_deployment", lambda: ("p", "e", "us-east-1"))
    assert ops.main(["remote-client-config", "issuer"]) == 1
