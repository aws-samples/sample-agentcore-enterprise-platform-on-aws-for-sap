# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The runtime role must not hand a compromised agent the whole account.

Regression guard for Checkov CKV_AWS_108 (data exfiltration) on the runtime
role's inline policy. Two statements were unscoped:

  - ECR pull on resources=["*"] — any runtime could pull every image in the
    account, and the repository ARN was already being passed into the function
    and ignored.
  - ssm:GetParameter on resources=["*"] — any parameter in the account, while
    infra_utils/agentcore_role.py (which calls itself the reference policy)
    scoped the same actions to the project path.

Parsed from source rather than synthesized: the CI test job has no aws-cdk-lib,
and this is the cheapest thing that fails if someone re-widens them.
"""

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

RUNTIME_STACK = REPO / "stacks" / "runtime_stack.py"

# Actions that read data and DO support resource-level permissions, so a "*"
# here is a real finding rather than an AWS limitation.
MUST_BE_SCOPED = (
    "ecr:BatchGetImage",
    "ecr:GetDownloadUrlForLayer",
    "ssm:GetParameter",
    "ssm:GetParameters",
)

# Actions AWS only accepts with "*". Kept explicit so the test documents which
# wildcards are deliberate instead of silently tolerating any of them.
WILDCARD_ALLOWED = {
    "ecr:GetAuthorizationToken",
    "xray:PutTraceSegments",
    "xray:PutTelemetryRecords",
    "cloudwatch:PutMetricData",
    # AgentCore data/control plane — scoping tracked separately, see the comment
    # on the AgentCoreAccess statement.
    "bedrock-agentcore:",
}


def _policy_statements() -> list[dict]:
    """Every iam.PolicyStatement(...) in runtime_stack.py as {actions, resources}."""
    tree = ast.parse(RUNTIME_STACK.read_text())
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "attr", None)
        if name != "PolicyStatement":
            continue
        entry = {"sid": None, "actions": [], "resources": [], "effect": "ALLOW"}
        for kw in node.keywords:
            if kw.arg in ("actions", "resources") and isinstance(kw.value, ast.List):
                entry[kw.arg] = [
                    el.value for el in kw.value.elts if isinstance(el, ast.Constant)
                ]
            elif kw.arg == "sid" and isinstance(kw.value, ast.Constant):
                entry["sid"] = kw.value.value
            elif kw.arg == "effect" and isinstance(kw.value, ast.Attribute):
                entry["effect"] = kw.value.attr  # iam.Effect.DENY → "DENY"
        found.append(entry)
    return found


def test_source_is_parseable_and_has_statements():
    statements = _policy_statements()
    assert len(statements) >= 6, f"expected the runtime role policies, got {statements}"


def test_readable_data_actions_are_never_on_star():
    offenders = []
    for statement in _policy_statements():
        # A Deny on "*" is the point, not a scoping miss — a deny that does not
        # cover everything is a bypass (DenyUngovernedInference).
        if statement["effect"] == "DENY":
            continue
        if "*" not in statement["resources"]:
            continue
        for action in statement["actions"]:
            if action in MUST_BE_SCOPED:
                offenders.append(f"{statement['sid']}: {action}")
    assert not offenders, (
        "these actions support resource-level permissions and must not use "
        f'resources=["*"]: {offenders}'
    )


def test_every_remaining_wildcard_is_a_known_aws_limitation():
    """A new unscoped statement should fail here rather than ship quietly."""
    unexplained = []
    for statement in _policy_statements():
        # Deny statements are exempt for the same reason as above: a Deny must
        # cover everything to enforce anything.
        if statement["effect"] == "DENY":
            continue
        if "*" not in statement["resources"]:
            continue
        for action in statement["actions"]:
            if not any(action.startswith(a) for a in WILDCARD_ALLOWED):
                unexplained.append(f"{statement['sid']}: {action}")
    assert not unexplained, (
        "wildcard resource on actions that are not documented as requiring it: "
        f"{unexplained}. Scope them, or add them to WILDCARD_ALLOWED with a reason."
    )


def test_ecr_pull_is_scoped_to_the_repository_arn():
    """The repo ARN is a variable, so assert on intent, not on parsed literals.

    _policy_statements() only collects constants, so a variable resource shows up
    as an empty list. Checking for that emptiness would pass for the wrong reason
    — it has to be paired with 'the source actually references ecr_repo_arn'.
    """
    for statement in _policy_statements():
        if "ecr:BatchGetImage" not in statement["actions"]:
            continue
        assert "*" not in statement["resources"], "ECR pull is unscoped"
        source = RUNTIME_STACK.read_text()
        assert "resources=[ecr_repo_arn]" in source, (
            "ECR pull must use the repository ARN passed into the function"
        )
        return
    raise AssertionError("no ECR pull statement found")


def test_code_interpreter_session_actions_are_complete_and_scoped():
    """The shipped clients start, invoke, and optionally stop one system tool."""
    statements = {statement["sid"]: statement for statement in _policy_statements()}
    code_interpreter = statements["AgentCoreCodeInterpreter"]
    assert code_interpreter["actions"] == [
        "bedrock-agentcore:StartCodeInterpreterSession",
        "bedrock-agentcore:InvokeCodeInterpreter",
        "bedrock-agentcore:StopCodeInterpreterSession",
    ]
    assert "*" not in code_interpreter["resources"]

    broad_actions = statements["AgentCoreAccess"]["actions"]
    assert not any("CodeInterpreter" in action for action in broad_actions)

    source = RUNTIME_STACK.read_text()
    assert 'account="aws"' in source
    assert 'resource="code-interpreter"' in source
    assert 'resource_name="aws.codeinterpreter.v1"' in source


def test_ssm_reads_are_scoped_to_the_project_path():
    source = RUNTIME_STACK.read_text()
    assert "arn:aws:ssm:*:*:parameter/{project_name}/*" in source, (
        "SSM reads must be scoped to this project's parameter path"
    )


# ── Token vault: each runtime names its own workload identity and providers ──
# The vault is shared by every runtime in the account and a provider holds its
# own exchange config, so GetResourceOauth2Token on "*" let any runtime (any
# use case's included) mint any provider's token. Synthesized, because the
# resources are built from the runtime name and the declared providers.

IDENTITY_ACTIONS = {
    "bedrock-agentcore:GetResourceOauth2Token",
    "bedrock-agentcore:CreateWorkloadIdentity",
    "bedrock-agentcore:GetWorkloadAccessToken",
    "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
    "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
}


def _role_statements(tmp_path, component: str, providers: list[str] | None, **kw):
    import json

    import aws_cdk as cdk
    from aws_cdk.assertions import Template

    from stacks.runtime_stack import RuntimeStack

    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    stack = RuntimeStack(
        cdk.App(),
        f"rt-{component}",
        project_name="proj",
        environment="dev",
        component_name=component,
        source_dir=str(tmp_path),
        credential_provider_names=providers,
        env=cdk.Environment(account="111111111111", region="us-east-1"),
        **kw,
    )
    template = Template.from_stack(stack).to_json()
    statements = [
        s
        for r in template["Resources"].values()
        if r["Type"] == "AWS::IAM::Policy"
        for s in r["Properties"]["PolicyDocument"]["Statement"]
    ]
    return statements, json.dumps(statements)


def _actions(statement) -> set[str]:
    a = statement["Action"]
    return set(a if isinstance(a, list) else [a])


def test_token_vault_actions_are_never_on_star(tmp_path):
    statements, _ = _role_statements(tmp_path, "agent-a", ["provider-a"])
    for s in statements:
        if _actions(s) & IDENTITY_ACTIONS:
            assert s["Resource"] != "*" and "*" not in s["Resource"], s
    secrets = [s for s in statements if "secretsmanager:GetSecretValue" in _actions(s)]
    assert "oauth2/*" not in str(secrets)


def test_a_runtime_names_its_own_workload_identity_and_providers(tmp_path):
    _, text = _role_statements(tmp_path, "agent-a", ["provider-a"])
    assert "workload-identity/proj_dev_agent_a-*" in text
    assert "oauth2credentialprovider/provider-a" in text
    assert "oauth2/provider-a-*" in text


def test_a_second_runtime_cannot_be_granted_another_runtimes_provider(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    _, a = _role_statements(tmp_path / "a", "agent-a", ["provider-a"])
    _, b = _role_statements(tmp_path / "b", "agent-b", ["provider-b"])
    assert "provider-b" not in a and "proj_dev_agent_b" not in a
    assert "provider-a" not in b and "proj_dev_agent_a" not in b


def test_a_runtime_that_declares_no_provider_gets_no_token(tmp_path):
    statements, _ = _role_statements(tmp_path, "agent-c", None)
    assert not any(
        "bedrock-agentcore:GetResourceOauth2Token" in _actions(s) for s in statements
    )


def test_the_platforms_runtimes_declare_what_they_use():
    source = (REPO / "app.py").read_text()
    # Orchestrator: the Gateway M2M provider plus any configured 3LO providers;
    # research-agent: the Gateway provider (web search); code-agent: none.
    assert "*identity_stack.user_provider_names," in source
    assert source.count("credential_provider_names=") == 2


def test_a_runtime_may_invoke_only_this_deployments_runtimes(tmp_path):
    import json

    # On "*", every runtime role could SigV4-invoke any runtime in the account, a tool
    # server a use case fronts with the Gateway included, and skip the Gateway's policy
    # and interceptor. A2A delegation only ever targets RuntimeStack's own names.
    statements, _ = _role_statements(tmp_path, "agent-a", None)
    invoke = [
        s for s in statements if "bedrock-agentcore:InvokeAgentRuntime" in _actions(s)
    ]
    assert len(invoke) == 1, invoke
    resource = json.dumps(invoke[0]["Resource"])
    assert resource.endswith(':111111111111:runtime/proj_dev_*"]]}'), resource


# ── Memory: one shared resource, partitioned by actor id ──

MEMORY_EVENT_ACTIONS = {
    "bedrock-agentcore:CreateEvent",
    "bedrock-agentcore:GetEvent",
    "bedrock-agentcore:ListEvents",
    "bedrock-agentcore:DeleteEvent",
    "bedrock-agentcore:ListSessions",
}


def _memory_conditions(statements) -> list:
    return [
        s.get("Condition") for s in statements if _actions(s) & MEMORY_EVENT_ACTIONS
    ]


def test_a_platform_runtime_is_refused_every_use_cases_memory_actors(tmp_path):
    statements, _ = _role_statements(tmp_path, "agent-a", None)
    # Bare subjects, which the platform agents use, still match.
    assert _memory_conditions(statements) == [
        {"StringNotLike": {"bedrock-agentcore:actorId": "uc-*"}}
    ]


def test_a_use_case_runtime_touches_only_its_own_memory_actors(tmp_path):
    statements, _ = _role_statements(
        tmp_path, "agent-a", None, memory_actor_prefix="uc-tenant-a:"
    )
    assert _memory_conditions(statements) == [
        {"StringLike": {"bedrock-agentcore:actorId": "uc-tenant-a:*"}}
    ]


def test_memory_events_are_held_to_this_deployments_memory(tmp_path):
    import json

    # The event actions take the memory resource type: on "*" a runtime's actor
    # partition would reach every other deployment's Memory in the account.
    (tmp_path / "p").mkdir()
    (tmp_path / "u").mkdir()
    platform, _ = _role_statements(tmp_path / "p", "agent-a", None)
    tenant, _ = _role_statements(
        tmp_path / "u", "agent-a", None, memory_actor_prefix="uc-tenant-a:"
    )
    for statements in (platform, tenant):
        (s,) = [x for x in statements if "bedrock-agentcore:CreateEvent" in _actions(x)]
        assert s["Resource"] != "*"
        assert json.dumps(s["Resource"]).endswith(
            ':111111111111:memory/proj_dev_memory-*"]]}'
        ), s["Resource"]


def _retrieve(statements) -> list:
    return [
        s
        for s in statements
        if "bedrock-agentcore:RetrieveMemoryRecords" in _actions(s)
    ]


def test_memory_retrieval_is_held_to_the_partition_on_the_namespace(tmp_path):
    import json

    # RetrieveMemoryRecords carries no actorId (Service Authorization Reference), so
    # the namespace keys hold it to the same partition, on this deployment's Memory.
    keys = ("bedrock-agentcore:namespace", "bedrock-agentcore:namespacePath")
    (tmp_path / "p").mkdir()
    (tmp_path / "u").mkdir()
    platform, _ = _role_statements(tmp_path / "p", "agent-a", None)
    tenant, _ = _role_statements(
        tmp_path / "u", "agent-a", None, memory_actor_prefix="uc-tenant-a:"
    )
    for statements, condition in (
        (
            platform,
            {
                # Read as a prefix, "u" or "uc" would cover every "uc-" actor.
                "StringNotLike": {
                    k: ["*/actors/uc-*", "*/actors/u", "*/actors/uc"] for k in keys
                },
                # Both keys name an actor, so no search spans every actor's records.
                "StringLikeIfExists": dict.fromkeys(keys, "/strategies/*/actors/?*"),
            },
        ),
        (
            tenant,
            {
                "StringLikeIfExists": dict.fromkeys(
                    keys, "/strategies/*/actors/uc-tenant-a:*"
                )
            },
        ),
    ):
        (s,) = _retrieve(statements)
        assert s["Condition"] == condition
        assert json.dumps(s["Resource"]).endswith(
            ':111111111111:memory/proj_dev_memory-*"]]}'
        ), s["Resource"]


def test_a_memory_actor_prefix_outside_the_reserved_shape_is_refused(tmp_path):
    import pytest

    # Without the colon "uc-a" would also match "uc-ab:..."; without "uc-" a platform
    # runtime would not be refused it.
    for bad in ("uc-tenant-a", "tenant-a:", "uc-:"):
        with pytest.raises(ValueError):
            _role_statements(tmp_path, "agent-a", None, memory_actor_prefix=bad)
