# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The people target (`api/people/index.py`): gates 3 and 4 for a remote MCP client.

The composer and the console's handlers run in-process against a dict-backed S3, as
in `test_business_operations_packs.py`. Every tool is compared with the console's own
answer for the same person, and every refusal has a test that passes the call
through, and so fails, if its check is removed.
"""

from __future__ import annotations

import importlib.util
import json
import time
from types import SimpleNamespace

import boto3
import pytest

# isort: split
# Puts the use case and its API layer on the path, as the packs tests run them.
from test_business_operations_packs import (  # noqa: F401 - `tree` is a fixture
    AUTHORS,
    CONSOLE,
    SECOND,
    UC,
    USERS,
    _authoring,
    _sops_route,
    tree,
)

# isort: split
from business_operations import config as uc_config
from business_operations import people as uc_people

from infra_utils.platform_config import PlatformConfig
from infra_utils.use_case_context import UseCaseContext

REMOTE_AUTHORS = "business-operations-remote-authors"
CLERK = "business-operations-ap-clerk"
QUICK = "quickclientid"
TARGET = "business-operations-people"
PERSON = (USERS, AUTHORS, REMOTE_AUTHORS, CLERK)


class _Table:
    def scan(self, **_):
        return {"Items": []}


@pytest.fixture
def stack(monkeypatch, tree):  # noqa: F811
    """A seeded development stack, the console's routes and the people target."""
    aws = _authoring(monkeypatch, tree)
    _sops_route(monkeypatch, aws, None)
    for k, v in {
        "API_ALLOWED_CLIENTS": f"{CONSOLE},{QUICK}",
        "REMOTE_AUTHORS_GROUP": REMOTE_AUTHORS,
        "PEOPLE_TIERS": "read,author",
        "PEOPLE_TARGET": TARGET,
        "CONFIG_TABLE": "cfg",
        "CONTACTS_JSON": "{}",
        "SAP_SYSTEM_ROLES": "{}",
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(
        boto3, "resource", lambda *a, **k: SimpleNamespace(Table=lambda _n: _Table())
    )
    spec = importlib.util.spec_from_file_location(
        "bo_people", UC / "api" / "people" / "index.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return SimpleNamespace(aws=aws, people=mod)


def _context(tool: str, target: str = TARGET):
    return SimpleNamespace(
        client_context=SimpleNamespace(
            custom={"bedrockAgentCoreToolName": f"{target}___{tool}"}
        )
    )


def _caller(groups=PERSON, **over):
    caller = {
        "sub": "6f1c-sub",
        "username": "pat",
        "client": "quick",
        "client_id": QUICK,
        "groups": list(groups),
    }
    caller.update(over)
    return caller


def _call(s, tool, caller=None, **arguments):
    event = {**arguments, "caller": _caller() if caller is None else caller}
    return s.people.handler(event, _context(tool))


def _console(s, name, method, resource, params=None, body=None, query=None):
    """The console's own answer, for the same person through the console client."""
    module = s.people._route(name)
    event = {
        "httpMethod": method,
        "resource": resource,
        "path": resource,
        "headers": {},
        "pathParameters": params,
        "queryStringParameters": query,
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {
            "authorizer": {
                "claims": {
                    "aud": CONSOLE,
                    "sub": "6f1c-sub",
                    "username": "pat",
                    "cognito:groups": "[" + " ".join(PERSON) + "]",
                }
            }
        },
    }
    resp = module.handler(event, None)
    return {"status": resp["statusCode"], "body": json.loads(resp["body"])}


def _stable(node):
    """The answer without the fake S3's read times, which differ per read."""
    if isinstance(node, dict):
        return {k: _stable(v) for k, v in node.items() if k != "last_modified"}
    if isinstance(node, list):
        return [_stable(v) for v in node]
    return node


# ── every read tool answers what the console answers ──


READS = [
    ("list_packs", {}, "config", "GET", "/registry", None),
    ("get_pack", {"pack_id": SECOND}, "packs", "GET", "/packs/{pack_id}", None),
    ("list_sops", {"pack_id": SECOND}, "sops", "GET", "/packs/{pack_id}/sops", None),
    (
        "get_sop",
        {"pack_id": SECOND, "process_type": "po_accrual"},
        "sops",
        "GET",
        "/packs/{pack_id}/sops/{process_type}",
        None,
    ),
    (
        "get_base_prompt",
        {"pack_id": SECOND},
        "packs",
        "GET",
        "/packs/{pack_id}/base_prompt",
        None,
    ),
    (
        "get_rules",
        {"pack_id": "finance_ops"},
        "config",
        "GET",
        "/packs/{pack_id}/rules",
        None,
    ),
    (
        "list_versions",
        {"pack_id": SECOND},
        "packs",
        "GET",
        "/packs/{pack_id}/versions",
        None,
    ),
    (
        "get_version",
        {"pack_id": SECOND, "version": 1},
        "packs",
        "GET",
        "/packs/{pack_id}/versions/{version}",
        None,
    ),
]


@pytest.mark.parametrize(("tool", "args", "group", "method", "resource", "body"), READS)
def test_a_read_answers_what_the_console_answers(
    stack, tool, args, group, method, resource, body
):
    got = _call(stack, tool, **args)
    params = {k: str(v) for k, v in args.items()} or None
    want = _console(stack, group, method, resource, params, body)
    assert _stable(got) == _stable(want), tool
    assert got["status"] == 200, got


def test_check_sop_answers_what_the_console_answers(stack):
    args = {"pack_id": SECOND, "process_type": "po_accrual", "content": "Version 2\nx"}
    got = _call(stack, "check_sop", **args)
    want = _console(
        stack,
        "sops",
        "POST",
        "/packs/{pack_id}/sops/{process_type}/check",
        {"pack_id": SECOND, "process_type": "po_accrual"},
        {"content": "Version 2\nx"},
    )
    assert got == want and got["status"] == 200


def test_a_pack_the_person_may_not_see_is_refused_as_in_the_console(stack):
    groups = (USERS, AUTHORS, REMOTE_AUTHORS)  # not an AP clerk
    got = _call(stack, "get_pack", caller=_caller(groups), pack_id="finance_ops")
    assert got["status"] == 403


# ── writes ──


def _version(s, pack_id):
    return _call(s, "get_pack", pack_id=pack_id)["body"]["version"]


def _clean_sop() -> str:
    text = (UC / "packs" / "finance_ops" / "sops" / "price_variance.txt").read_text()
    return text.replace("MUST", "MUST", 1) + "\n"


def test_put_sop_writes_a_version_with_its_door_and_a_diff(stack):
    base = _version(stack, "finance_ops")
    content = _clean_sop()
    got = _call(
        stack,
        "put_sop",
        pack_id="finance_ops",
        process_type="price_variance",
        content=content,
        based_on=base,
    )
    assert got["status"] == 200, got
    assert got["body"]["version"] == base + 1
    assert got["body"]["diff"].startswith(f"--- v{base}")
    assert (
        stack.aws.objects[("sops", "finance_ops/price_variance.txt")]
        == content.encode()
    )
    invoke = stack.aws.invokes[-1]
    assert invoke["door"] == {
        "via": "people-gateway",
        "client": "quick",
        "by": "6f1c-sub",
        "display": "pat",
    }
    assert invoke["actor"] == "pat"
    # The pack-admin role is never in a people call's roles.
    assert "pack_admin" not in invoke["roles"]
    versions = _call(stack, "list_versions", pack_id="finance_ops")["body"]["versions"]
    assert versions[0]["door"] == invoke["door"]
    assert versions[0]["origin"]["door"] == invoke["door"]
    # A console edit carries no door.
    assert all(v["door"] is None for v in versions[1:])


def test_put_sop_with_findings_writes_nothing(stack):
    base = _version(stack, SECOND)
    before = stack.aws.objects[("sops", f"{SECOND}/po_accrual.txt")]
    got = _call(
        stack,
        "put_sop",
        pack_id=SECOND,
        process_type="po_accrual",
        content="Version 2\nThe agent MUST stop.",
        based_on=base,
    )
    assert got["status"] == 422 and got["body"]["findings"], got
    assert stack.aws.objects[("sops", f"{SECOND}/po_accrual.txt")] == before
    assert _version(stack, SECOND) == base


def test_a_stale_base_is_the_composers_409(stack):
    base = _version(stack, "finance_ops")
    args = {"pack_id": "finance_ops", "process_type": "price_variance"}
    first = _call(stack, "put_sop", content=_clean_sop(), based_on=base, **args)
    assert first["status"] == 200, first
    stale = _call(stack, "put_sop", content=_clean_sop() + "x\n", based_on=base, **args)
    assert stale["status"] == 409, stale
    assert f"at v{base + 1}" in json.dumps(stale["body"])


def test_put_base_prompt_and_set_rule_values_are_content_edits(stack):
    base = _version(stack, SECOND)
    got = _call(
        stack,
        "put_base_prompt",
        pack_id=SECOND,
        based_on=base,
        display_name="Second, renamed",
        base_prompt="Work it.\n{SOP_CONTENT}\n{PLATFORM_MECHANICS}\n",
    )
    assert got["status"] == 200, got
    assert stack.aws.invokes[-1]["door"]["via"] == "people-gateway"
    base = _version(stack, "finance_ops")
    rule = _call(
        stack,
        "set_rule_values",
        pack_id="finance_ops",
        based_on=base,
        values={"PRICE_VARIANCE_PCT": 3},
    )
    assert rule["status"] == 200, rule
    assert stack.aws.invokes[-1]["pack"] == {"constants": {"PRICE_VARIANCE_PCT": 3}}


def test_create_pack_carries_its_door(stack):
    got = _call(
        stack,
        "create_pack",
        pack_id="remote_draft",
        display_name="Remote draft",
        description="A pack made from an external AI.",
        short_id="rd",
        kind="filter",
        definition="An invoice blocked for a reason nobody handles yet.",
        process_types=["triage"],
    )
    assert got["status"] == 201, got
    assert stack.aws.invokes[-1]["door"]["client"] == "quick"


def test_the_composer_drops_a_malformed_door(stack):
    base = _version(stack, SECOND)
    answer = stack.aws.composer.handler(
        {
            "pack_id": SECOND,
            "roles": ["author"],
            "actor": "x",
            "pack": {"display_name": "Forged"},
            "expected_version": base,
            "door": {"via": "console", "client": "x", "by": "y", "display": "z"},
        },
        None,
    )
    assert "error" not in answer, answer
    versions = _call(stack, "list_versions", pack_id=SECOND)["body"]["versions"]
    assert versions[0]["door"] is None


# ── gate 3: refusals ──


def _refused(answer, status=None) -> bool:
    return "error" in answer and (status is None or answer["status"] == status)


def test_a_call_without_a_tool_name_is_refused(stack):
    assert _refused(stack.people.handler({"caller": _caller()}, None), 400)
    assert _refused(stack.people.handler({"caller": _caller()}, SimpleNamespace()), 400)


@pytest.mark.parametrize(
    "full",
    [
        "get_pack",  # no target
        "business-operations-case-management___get_pack",  # another target
        f"{TARGET}___update_case_state",  # not in the set
        f"{TARGET}___put_controls",
        f"{TARGET}___stop",
    ],
)
def test_a_tool_outside_the_set_is_refused(stack, full):
    context = SimpleNamespace(
        client_context=SimpleNamespace(custom={"bedrockAgentCoreToolName": full})
    )
    answer = stack.people.handler({"caller": _caller(), "pack_id": SECOND}, context)
    assert _refused(answer, 404), answer


def test_a_tier_not_attached_is_refused(stack, monkeypatch):
    monkeypatch.setenv("PEOPLE_TIERS", "read")
    answer = _call(
        stack, "put_base_prompt", pack_id=SECOND, based_on=1, description="x"
    )
    assert _refused(answer, 404)
    assert not _refused(_call(stack, "get_pack", pack_id=SECOND))


@pytest.mark.parametrize(
    "caller",
    [
        "absent",
        "not a dict",
        {"username": "pat", "client_id": QUICK, "groups": list(PERSON)},
        {"sub": "", "client_id": QUICK, "groups": list(PERSON)},
        {"sub": "s", "groups": list(PERSON)},
        {"sub": "s", "client_id": QUICK},
        {"sub": "s", "client_id": QUICK, "groups": "business-operations-users"},
    ],
)
def test_a_call_without_a_verified_caller_is_refused(stack, caller):
    event = {"pack_id": SECOND}
    if caller != "absent":
        event["caller"] = caller
    assert _refused(stack.people.handler(event, _context("get_pack")), 401)


def test_an_argument_the_tool_does_not_take_is_refused(stack):
    answer = _call(stack, "get_pack", pack_id=SECOND, entitlement={"groups": []})
    assert _refused(answer, 400) and "entitlement" in answer["error"]


def test_a_missing_argument_is_refused(stack):
    assert _refused(_call(stack, "get_sop", pack_id=SECOND), 400)


@pytest.mark.parametrize("based_on", [None, "3", True, 1.5])
def test_a_write_needs_an_integer_based_on(stack, based_on):
    args = {"pack_id": SECOND, "process_type": "po_accrual", "content": "x"}
    if based_on is not None:
        args["based_on"] = based_on
    assert _refused(_call(stack, "put_sop", **args), 400)


@pytest.mark.parametrize("limit", ["AUTO_WRITE_LIMIT_USD", "DUAL_APPROVAL_LIMIT_USD"])
def test_a_write_limit_is_never_a_rule_value_here(stack, limit):
    base = _version(stack, "finance_ops")
    answer = _call(
        stack,
        "set_rule_values",
        pack_id="finance_ops",
        based_on=base,
        values={limit: 1},
    )
    assert _refused(answer, 400) and limit in answer["error"]
    assert _version(stack, "finance_ops") == base


def test_put_base_prompt_needs_something_to_put(stack):
    assert _refused(_call(stack, "put_base_prompt", pack_id=SECOND, based_on=1), 400)


def test_a_person_is_held_to_thirty_writes_a_minute(stack):
    people = stack.people
    for i in range(people.WRITES_PER_MINUTE):
        people._rate("6f1c-sub", now=100.0 + i)
    with pytest.raises(people.Refused) as refused:
        people._rate("6f1c-sub", now=130.0)
    assert refused.value.answer["status"] == 429
    people._rate("someone-else", now=130.0)  # per person
    people._rate("6f1c-sub", now=161.0)  # the window moves on
    # And through the handler: the refused write reaches no route.
    people._writes["6f1c-sub"].clear()
    people._writes["6f1c-sub"].extend([time.monotonic()] * people.WRITES_PER_MINUTE)
    invokes = len(stack.aws.invokes)
    answer = _call(
        stack, "put_base_prompt", pack_id=SECOND, based_on=1, description="x"
    )
    assert _refused(answer, 429) and len(stack.aws.invokes) == invokes


# ── gate 4: the console's code, with the remote authors group ──


def test_a_person_outside_the_remote_authors_group_is_refused_every_tool(stack):
    caller = _caller((USERS, AUTHORS, CLERK))
    assert _refused(_call(stack, "list_packs", caller=caller), 403)
    answer = _call(
        stack,
        "put_base_prompt",
        caller=caller,
        pack_id=SECOND,
        based_on=1,
        description="x",
    )
    assert _refused(answer, 403)


def test_an_unset_remote_authors_group_admits_no_one(stack, monkeypatch):
    monkeypatch.setenv("REMOTE_AUTHORS_GROUP", "")
    assert _refused(_call(stack, "list_packs"), 403)


def test_a_person_outside_the_users_group_is_refused_by_admit(stack):
    answer = _call(stack, "list_packs", caller=_caller((AUTHORS, REMOTE_AUTHORS)))
    assert answer["status"] == 403, answer


def test_a_client_admit_does_not_know_is_refused(stack):
    answer = _call(stack, "list_packs", caller=_caller(client_id="other-client"))
    assert answer["status"] == 403, answer


def test_a_remote_author_without_the_authors_floor_writes_nothing(stack):
    base = _version(stack, SECOND)
    caller = _caller((USERS, REMOTE_AUTHORS))
    answer = _call(
        stack,
        "put_base_prompt",
        caller=caller,
        pack_id=SECOND,
        based_on=base,
        description="x",
    )
    assert answer["status"] == 403, answer
    assert _version(stack, SECOND) == base


def test_structure_groups_are_removed_from_a_people_call(stack):
    admin = (*PERSON, "business-operations-pack-admins", "business-operations-admins")
    event = stack.people._event(
        stack.people.BY_NAME["get_pack"], {"pack_id": SECOND}, _caller(admin), []
    )
    assert event["requestContext"]["authorizer"]["claims"]["cognito:groups"] == []
    groups = stack.people._groups(_caller(admin))
    assert "business-operations-pack-admins" not in groups
    assert "business-operations-admins" not in groups
    # So a pack-admin through the people Gateway cannot change a pack's mapping.
    base = _version(stack, SECOND)
    answer = stack.people.handler(
        {
            "caller": _caller(admin),
            "pack_id": SECOND,
            "based_on": base,
            "description": "x",
        },
        _context("put_base_prompt"),
    )
    assert answer["status"] == 200
    assert "pack_admin" not in stack.aws.invokes[-1]["roles"]


def test_a_write_outside_development_is_refused(stack, monkeypatch):
    monkeypatch.setenv("PROMOTION_ROLE", "quality")
    stack.people._routes.clear()  # the routes read their role at import
    answer = _call(
        stack, "put_base_prompt", pack_id=SECOND, based_on=1, description="x"
    )
    assert answer["status"] == 403, answer


# ── the fixed set ──


def test_the_set_is_exactly_the_designs():
    mod = uc_people.handler_module()
    assert {(t.name, t.tier, t.method, t.resource) for t in mod.TOOLS} == {
        ("list_packs", "read", "GET", "/registry"),
        ("get_pack", "read", "GET", "/packs/{pack_id}"),
        ("list_sops", "read", "GET", "/packs/{pack_id}/sops"),
        ("get_sop", "read", "GET", "/packs/{pack_id}/sops/{process_type}"),
        ("check_sop", "read", "POST", "/packs/{pack_id}/sops/{process_type}/check"),
        ("get_base_prompt", "read", "GET", "/packs/{pack_id}/base_prompt"),
        ("get_rules", "read", "GET", "/packs/{pack_id}/rules"),
        ("list_versions", "read", "GET", "/packs/{pack_id}/versions"),
        ("get_version", "read", "GET", "/packs/{pack_id}/versions/{version}"),
        ("put_sop", "author", "PUT", "/packs/{pack_id}/sops/{process_type}"),
        ("put_base_prompt", "author", "PATCH", "/packs/{pack_id}"),
        ("set_rule_values", "author", "PATCH", "/packs/{pack_id}"),
        ("create_pack", "author", "POST", "/packs"),
    }
    assert {t.handler for t in mod.TOOLS} == {"packs", "sops", "config"}


def test_every_tool_schema_declares_caller_and_requires_what_gate_3_does():
    for t in uc_people.handler_module().TOOLS:
        schema = t.schema()["InputSchema"]
        assert "caller" in schema["Properties"] and "caller" not in schema["Required"]
        assert set(schema["Required"]) == set(t.required)
        assert set(schema["Properties"]) == set(t.required) | set(t.optional) | {
            "caller"
        }


def test_every_tool_schema_is_in_the_gateways_key_case_all_the_way_down():
    """CloudFormation's GatewayTarget refuses a lowercase schema key at any depth."""

    def keys(node, under_properties=False):
        if isinstance(node, dict):
            for k, v in node.items():
                if not under_properties:
                    yield k
                yield from keys(v, under_properties=(k == "Properties"))

    for t in uc_people.handler_module().TOOLS:
        lower = [k for k in keys(t.schema()["InputSchema"]) if k[:1].islower()]
        assert not lower, (t.name, lower)


def test_the_tiers_choose_the_listed_tools():
    read = uc_config.load({"people_tools": ["read"]})
    names = {t["Name"] for t in uc_people.tools(read)}
    assert "get_pack" in names and "put_sop" not in names
    both = uc_config.load({"people_tools": ["read", "author"]})
    assert len(uc_people.tools(both)) == len(uc_people.handler_module().TOOLS)


# ── configuration ──


def _ctx(people=True, production=False):
    gateway = (
        {
            "people": {
                "enabled": True,
                "remote_clients": [{"name": "quick", "regions": ["us-east-1"]}],
            }
        }
        if people
        else {}
    )
    platform = PlatformConfig.model_validate({"gateway": gateway})
    return UseCaseContext(
        project="p",
        environment="e",
        region="us-east-1",
        platform=platform,
        production=production,
    )


def test_people_tools_are_off_by_default():
    assert uc_config.load({}).people_tools == []
    uc_people.require(_ctx(people=False), uc_config.load({}))


def test_people_tools_need_the_people_gateway():
    with pytest.raises(ValueError, match="gateway.people.enabled"):
        uc_people.require(
            _ctx(people=False), uc_config.load({"people_tools": ["read"]})
        )


@pytest.mark.parametrize(
    ("production", "role"), [(True, None), (False, "quality"), (False, "production")]
)
def test_author_is_development_only(production, role):
    raw = {"people_tools": ["read", "author"]}
    if role:
        raw["promotion"] = {"role": role}
    with pytest.raises(ValueError, match="development only"):
        uc_people.require(_ctx(production=production), uc_config.load(raw))
    read = {k: v for k, v in raw.items() if k != "people_tools"}
    uc_people.require(
        _ctx(production=production), uc_config.load({**read, "people_tools": ["read"]})
    )


def test_a_tier_twice_or_unknown_is_refused():
    with pytest.raises(ValueError):
        uc_config.load({"people_tools": ["read", "read"]})
    with pytest.raises(ValueError):
        uc_config.load({"people_tools": ["decide"]})


# ── the synthesized target ──

FIXTURE = UC / "fixtures" / "contract" / "business-operations.yaml"
MAIN = "agentcore-workshop-dev-uc-business-operations"


_REMOTE = "    remote_clients:\n      - name: quick\n        regions: [us-east-1]\n"
#: A placeholder issuer: what an organisation's own identity provider would be.
_ISSUER = (
    "    issuer:\n"
    "      discovery_url: https://idp.example.com/.well-known/openid-configuration\n"
    "      clients: [example-client, second-client]\n"
    "      audience: gateway\n"
    "      groups: {from: claim, claim: groups}\n"
)


def _synth(people_tools: str | None, issuer: bool = False) -> dict:
    from app_synth import synth_app

    text = FIXTURE.read_text().replace(
        "gateway:\n",
        "gateway:\n  people:\n    enabled: true\n" + (_ISSUER if issuer else _REMOTE),
    )
    if people_tools is not None:
        text = text.replace(
            "business-operations: {}",
            f"business-operations: {{people_tools: {people_tools}}}",
        )
    result, out = synth_app(text)
    assert result.returncode == 0, result.stderr[-3000:]
    return json.loads((out / f"{MAIN}.template.json").read_text())


def _resources(template, kind):
    return {
        k: r["Properties"]
        for k, r in template["Resources"].items()
        if r["Type"] == kind
    }


def _ssm(template, node) -> str:
    """The parameter name a `{"Ref": ...}` to an SSM parameter value resolves."""
    return template["Parameters"][node["Ref"]]["Default"]


def _target_fn(template) -> tuple[str, dict]:
    ((key, fn),) = [
        (k, p)
        for k, p in _resources(template, "AWS::Lambda::Function").items()
        if k.startswith("PeopleTarget")
    ]
    return key, fn


def test_no_people_tools_attaches_nothing():
    template = _synth(None)
    text = json.dumps(template)
    assert "PeopleTarget" not in text and "remote-authors" not in text
    assert "people-gateway" not in text


def test_the_target_is_on_the_people_gateway_with_its_tools():
    template = _synth("[read, author]")
    (target,) = [
        p
        for p in _resources(template, "AWS::BedrockAgentCore::GatewayTarget").values()
        if p["Name"] == TARGET
    ]
    assert _ssm(template, target["GatewayIdentifier"]).endswith("/people-gateway/id")
    tools = target["TargetConfiguration"]["Mcp"]["Lambda"]["ToolSchema"][
        "InlinePayload"
    ]
    assert {t["Name"] for t in tools} == {
        t.name for t in uc_people.handler_module().TOOLS
    }
    assert all("caller" in t["InputSchema"]["Properties"] for t in tools)


def test_read_alone_lists_no_write():
    template = _synth("[read]")
    targets = [
        p
        for p in _resources(template, "AWS::BedrockAgentCore::GatewayTarget").values()
        if p["Name"] == TARGET
    ]
    tools = targets[0]["TargetConfiguration"]["Mcp"]["Lambda"]["ToolSchema"][
        "InlinePayload"
    ]
    assert not {t["Name"] for t in tools} & {
        "put_sop",
        "put_base_prompt",
        "set_rule_values",
        "create_pack",
    }
    _, fn = _target_fn(template)
    assert fn["Environment"]["Variables"]["PEOPLE_TIERS"] == "read"


def test_the_target_holds_only_its_routes_grants_and_one_invoker():
    template = _synth("[read, author]")
    key, fn = _target_fn(template)
    env = fn["Environment"]["Variables"]
    assert fn["ReservedConcurrentExecutions"] == 2
    assert fn["Handler"] == "people/index.handler"
    assert env["REMOTE_AUTHORS_GROUP"] == REMOTE_AUTHORS
    node = env["API_ALLOWED_CLIENTS"]
    (ref,) = node["Fn::Join"][1] if "Fn::Join" in node else [node]
    assert _ssm(template, ref).endswith(
        "/people-gateway/remote-clients/quick/client-id"
    )
    role = fn["Role"]["Fn::GetAtt"][0]
    statements = [
        s
        for p in _resources(template, "AWS::IAM::Policy").values()
        if {"Ref": role} in p["Roles"]
        for s in p["PolicyDocument"]["Statement"]
    ]
    actions = {
        a
        for s in statements
        for a in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])
    }
    text = json.dumps(statements)
    # No write to any table, no SSM write, no stop, no run.
    assert not {
        a
        for a in actions
        if a.startswith("dynamodb:")
        and a
        not in {
            "dynamodb:GetItem",
            "dynamodb:Query",
            "dynamodb:Scan",
            "dynamodb:BatchGetItem",
            "dynamodb:ConditionCheckItem",
            "dynamodb:DescribeTable",
            "dynamodb:GetRecords",
            "dynamodb:GetShardIterator",
        }
    }, actions
    assert (
        "ssm:PutParameter" not in actions and "bedrock:StartIngestionJob" not in actions
    )
    assert "bedrock-agentcore:InvokeAgentRuntime" not in actions
    assert "Cases" not in text  # no grant on the cases table
    (permission,) = [
        p
        for p in _resources(template, "AWS::Lambda::Permission").values()
        if p["FunctionName"] == {"Fn::GetAtt": [key, "Arn"]}
    ]
    assert _ssm(template, permission["Principal"]).endswith("/people-gateway/role-arn")


def test_the_remote_authors_group_and_the_refusal_alarms_exist():
    template = _synth("[read]")
    groups = [
        p["GroupName"]
        for p in _resources(template, "AWS::Cognito::UserPoolGroup").values()
    ]
    assert REMOTE_AUTHORS in groups
    alarms = _resources(template, "AWS::CloudWatch::Alarm")
    names = {p.get("MetricName") for p in alarms.values()}
    assert {"Refusals", "PeopleRefusals"} <= names
    (mf,) = [
        p
        for k, p in _resources(template, "AWS::Logs::MetricFilter").items()
        if k.startswith("PeopleTargetRefusals")
    ]
    assert mf["MetricTransformations"][0]["MetricName"] == "PeopleRefusals"


# ── verify.py: denial on the people Gateway ──

ROLE = "arn:aws:iam::111111111111:role/p-dev-people-gateway-role"


class _NotFound(Exception):
    pass


class _Ssm:
    exceptions = SimpleNamespace(ParameterNotFound=_NotFound)

    def __init__(self, params):
        self.params = params

    def get_parameter(self, Name):
        if Name not in self.params:
            raise _NotFound(Name)
        return {"Parameter": {"Value": self.params[Name]}}

    def get_parameters_by_path(self, Path, Recursive):
        return {
            "Parameters": [
                {"Name": k, "Value": v}
                for k, v in self.params.items()
                if k.startswith(Path)
            ]
        }


class _Lambda:
    def __init__(self, principals, answers):
        self.principals = principals
        self.answers = answers
        self.calls: list[dict] = []

    def get_policy(self, FunctionName):
        statements = [{"Principal": p} for p in self.principals]
        return {"Policy": json.dumps({"Statement": statements})}

    def invoke(self, FunctionName, ClientContext, Payload):
        event = json.loads(Payload)
        self.calls.append(event)
        answer = self.answers["caller" in event]
        return {"Payload": SimpleNamespace(read=lambda: json.dumps(answer).encode())}


PARAMS = {
    "/p/dev/people-gateway/url": "https://people.example/mcp",
    "/p/dev/people-gateway/role-arn": ROLE,
    "/p/dev/use-cases/business-operations/people-target-function": "fn",
}


@pytest.fixture
def people_check(monkeypatch):
    spec = importlib.util.spec_from_file_location("bo_verify_people", UC / "verify.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def run(
        params=PARAMS,
        anonymous=(401, {"WWW-Authenticate": "Bearer"}),
        reader=401,
        principals=({"AWS": ROLE},),
        answers=None,
    ):
        import requests

        def post(url, json=None, headers=None, timeout=None):
            if "Authorization" in (headers or {}):
                return SimpleNamespace(status_code=reader, headers={})
            return SimpleNamespace(status_code=anonymous[0], headers=anonymous[1])

        monkeypatch.setattr(requests, "post", post)
        lam = _Lambda(
            list(principals), answers or {False: {"status": 401}, True: {"status": 403}}
        )
        monkeypatch.setattr(boto3, "client", lambda *_a, **_k: lam)
        return mod.people_check("reader-token", _Ssm(params), "p", "dev", "us-east-1")

    return run


def test_verify_passes_when_every_denial_holds(people_check):
    out = people_check()
    assert "refused an anonymous call and the reader's token" in out
    assert "admits only that Gateway's role" in out


def test_verify_skips_without_a_people_gateway(people_check):
    assert people_check(params={}) == ""


@pytest.mark.parametrize(
    "kw",
    [
        {"anonymous": (200, {})},
        {"anonymous": (401, {})},
        {"reader": 200},
        {"principals": ({"AWS": ROLE}, {"Service": "apigateway.amazonaws.com"})},
        {"principals": ()},
        {"answers": {False: {"status": 200}, True: {"status": 403}}},
        {"answers": {False: {"status": 401}, True: {"status": 200}}},
    ],
)
def test_verify_fails_when_a_denial_does_not_hold(people_check, kw):
    with pytest.raises(SystemExit) as failed:
        people_check(**kw)
    assert str(failed.value).startswith("FAIL: ")


# ── with the people Gateway's own issuer ──


def test_with_an_issuer_the_target_admits_the_issuers_clients():
    template = _synth("[read, author]", issuer=True)
    _, fn = _target_fn(template)
    assert fn["Environment"]["Variables"]["API_ALLOWED_CLIENTS"] == (
        "example-client,second-client"
    )
    assert "remote-clients" not in json.dumps(template)


def test_a_door_through_the_issuer_names_it(stack):
    base = _version(stack, SECOND)
    caller = _caller(client="issuer", client_id=QUICK)
    answer = _call(
        stack,
        "put_base_prompt",
        caller=caller,
        pack_id=SECOND,
        based_on=base,
        description="From the issuer",
    )
    assert answer["status"] == 200, answer
    assert stack.aws.invokes[-1]["door"]["client"] == "issuer"


ISSUER_PARAMS = {
    **PARAMS,
    "/p/dev/people-gateway/issuer/discovery-url": "https://idp.example.com/.well-known/openid-configuration",
}


def test_verify_with_an_issuer_checks_no_remote_client_is_left(people_check):
    assert "trusts its issuer alone" in people_check(params=ISSUER_PARAMS)
    left = {
        **ISSUER_PARAMS,
        "/p/dev/people-gateway/remote-clients/quick/client-id": "x",
    }
    with pytest.raises(SystemExit) as failed:
        people_check(params=left)
    assert "still publishes remote clients" in str(failed.value)


def test_verify_without_an_issuer_does_not_look_for_one(people_check):
    assert "issuer" not in people_check()
