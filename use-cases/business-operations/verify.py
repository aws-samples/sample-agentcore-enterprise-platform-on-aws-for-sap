#!/usr/bin/env python3

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Verify business-operations with real calls through the shared Gateway.

Mints a token for this use case's own `reader` client (published by the
platform under gateway/use-case-clients/) and proves, cheapest first:

- the client is admitted and the case-management target is listed;
- under Cedar ENFORCE, the reader is not even offered update_case_state;
- the run checks refuse get_case_state for a call that names no run;
- another use case's tool (the platform's sample tool) passes the run checks untouched;
- with a short-lived run grant for this client, get_case_state answers, and
  under ENFORCE Cedar still refuses the reader's update_case_state;
- a rehearsal's grant is refused update_case_state, and a SAP write on a grant
  naming no case is refused before it reaches SAP.

For each SAP system this use case claims on the SAP layer, it also reads SAP through
that system's target with the reader, a claimed client (a `success: false` answer, a
SAP 401 for one, is a failure), and under ENFORCE checks the reader is offered no SAP
write.

The pipeline, without creating a case: the agent queue feeds the invoker the eval
runner finds by its published name, and when
push ingest is deployed, `POST /erp-events` refuses a call without the token. The SAP
ingest queue feeds the ingest consumer; when this use case subscribes on the SAP
layer's bus, its rule is there and targets that queue with its dead-letter queue; the
queue's policy lets only that rule and `POST /erp-events` send; the dead-letter queue is
empty; and a nudge for a document SAP does not have, handed to the consumer in the event
`POST /erp-events` enqueues, is answered with no failure (so no case). No AWS principal
but SAP's sender role can put a `sap.` event on the bus, so the nudge cannot go through
it, nor through the queue, whose policy refuses verify's own credentials.

With that grant, `load_sop` answers a published process type with the SOP headed by
its S3 version, `search_sap_sops` returns that pack's SOPs with their constants filled,
and a second grant naming no real pack is refused `search_sap_sops`.
Each knowledge tool the `knowledge:` flags turned on answers too: `search_sap_api_docs`
with a hit, `check_vendor_risk` and `get_precedent` from Aurora.

Then the published packs: `registry.json` speaks `pack-registry/1`, holds at least one
pack, gives each process type to one pack, and every SOP a published pack maps is in
the SOP bucket (the seed and a bundle import both write SOPs before the registry, so
a miss means that invariant broke) and cites only constants its pack declares.

The console API: `/cases` refuses a call without a token, as does `/tickets` when
sample ticketing is on; the case rating route refuses the reader's token. The seeder,
when test data is on, has no API Gateway grant: `ops.py seed` invokes it over IAM.
Every product alarm exists and publishes to the platform's alarm topic on ALARM and OK.
The run-event channel's subscribe goes through its checking handler, the Gateway role's
trust names the Gateway (`aws:SourceArn`), and the agent runtime's Memory event grant is
held to its own actor prefix. The log group AgentCore creates for the agent runtime
carries a retention. The SAP runtimes themselves are the SAP layer's (layers/sap/verify.py).

When the platform's people Gateway is deployed, denial first: an anonymous call is a 401
with `WWW-Authenticate`, and the reader's token is refused, since the Gateway admits its
remote clients alone, or with an issuer that issuer's tokens alone (and then no remote
client is left). When this use case attaches `people_tools`, its people target's
resource policy names the people Gateway's role and nothing else, and the target refuses a
call with no caller and one from someone outside `business-operations-remote-authors`.

Then the agent runtime, without a model call: it is READY, the registry it reads
at cold start exists, it has a model for every tier a published pack names, and its
authorizer refuses the reader's token, because only
the delegating client may call it (and so name the acting person).

The grants come from the invoker (`{"verify_grant": ...}`), the only writer the
run-grants table admits, and expire two minutes later. Exits 0 with `OK: ...`, 1 with
`FAIL: ...`.

Usage: python use-cases/business-operations/verify.py
"""

import base64
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
UC = Path(__file__).resolve().parent
sys.path[:0] = [str(REPO), str(UC), str(UC / "api" / "layer" / "python")]

NAME = "business-operations"
TARGET = f"{NAME}-case-management"
# A case id in the derived shape (case_key.DERIVED_CASE_ID_PATTERN) that names no case:
# the case tool refuses anything else before it reads the table.
PROBE_CASE = "verify-" + "probe" * 5 + "p"


def client_token(project: str, env: str, client: str, region: str) -> str:
    """client_credentials for one of this use case's clients, all its scopes."""
    import boto3

    ssm = boto3.client("ssm", region_name=region)
    base = f"/{project}/{env}/gateway/use-case-clients/{NAME}/{client}"
    client_id = ssm.get_parameter(Name=f"{base}/client-id")["Parameter"]["Value"]
    secret_name = ssm.get_parameter(Name=f"{base}/client-secret-name")["Parameter"][
        "Value"
    ]
    secret = boto3.client("secretsmanager", region_name=region).get_secret_value(
        SecretId=secret_name
    )["SecretString"]
    auth = f"/{project}/{env}/auth"
    form = {"grant_type": "client_credentials"}
    if ssm.get_parameter(Name=f"{auth}/mode")["Parameter"]["Value"] == "direct":
        # An Entra app registration: the platform app's `.default` is the Gateway's
        # audience, and the tenant's v2 issuer names the token endpoint.
        issuer = ssm.get_parameter(Name=f"{auth}/issuer-url")["Parameter"]["Value"]
        url = issuer.removesuffix("/v2.0") + "/oauth2/v2.0/token"
        form["scope"] = ssm.get_parameter(Name=f"{auth}/m2m-scope")["Parameter"][
            "Value"
        ]
    else:
        account = boto3.client("sts", region_name=region).get_caller_identity()[
            "Account"
        ]
        url = f"https://{project}-{env}-{account}.auth.{region}.amazoncognito.com/oauth2/token"
    pair = f"{urllib.parse.quote_plus(client_id)}:{urllib.parse.quote_plus(secret)}"
    basic = base64.b64encode(pair.encode()).decode()
    req = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(form).encode(),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {basic}",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:  # nosec B310 - https URL above
        return json.loads(resp.read())["access_token"]


def mcp(url: str, token: str, method: str, params=None, headers=None) -> dict:
    """One JSON-RPC request to the Gateway; a non-200 is a failure, not an answer.

    Not scripts/test_gateway.mcp_request, which sends no extra headers.
    """
    import requests

    resp = requests.post(
        url,
        json={"jsonrpc": "2.0", "id": method, "method": method, "params": params or {}},
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            **(headers or {}),
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"{method}: HTTP {resp.status_code} {resp.text[:200]}")
    return resp.json()


def _text(result: dict) -> str:
    body = result.get("result") or {}
    return " ".join(c.get("text", "") for c in body.get("content") or [])


def _claims(token: str) -> dict:
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def probe_grant(ssm, project: str, env: str, **spec) -> dict:
    """The headers that present a two-minute grant for the reader, minted by the
    invoker: its run id and its secret."""
    import boto3

    name = ssm.get_parameter(
        Name=f"/{project}/{env}/use-cases/{NAME}/invoker-function"
    )["Parameter"]["Value"]
    resp = boto3.client("lambda", region_name=ssm.meta.region_name).invoke(
        FunctionName=name, Payload=json.dumps({"verify_grant": spec}).encode()
    )
    body = json.loads(resp["Payload"].read() or b"null")
    if (
        resp.get("FunctionError")
        or not isinstance(body, dict)
        or not {"run_id", "grant"} <= set(body)
    ):
        sys.exit(f"FAIL: the invoker did not mint a probe grant: {str(body)[:300]}")
    return {
        "x-audit-correlation-id": body["run_id"],
        "x-business-operations-run-grant": body["grant"],
    }


def main() -> int:
    # Imported here so a missing dependency is reported as FAIL:, not a traceback.
    import boto3

    from infra_utils.platform_config import resolve_region

    project = os.environ.get("PROJECT_NAME", "agentcore-workshop")
    env = os.environ.get("ENVIRONMENT", "dev")
    region = resolve_region()
    ssm = boto3.client("ssm", region_name=region)
    gateway_url = ssm.get_parameter(Name=f"/{project}/{env}/gateway/url")["Parameter"][
        "Value"
    ]
    run_checks = json.loads(
        ssm.get_parameter(Name=f"/{project}/{env}/use-cases/{NAME}/run-checks")[
            "Parameter"
        ]["Value"]
    )
    enforced = run_checks["cedar_enforced"]

    token = client_token(project, env, "reader", region)
    tools = mcp(gateway_url, token, "tools/list").get("result", {}).get("tools")
    names = sorted(t["name"] for t in tools or [])
    read, write = f"{TARGET}___get_case_state", f"{TARGET}___update_case_state"
    if read not in names:
        sys.exit(f"FAIL: tools/list for the reader client lacks {read}: {names}")
    if enforced and write in names:
        sys.exit(f"FAIL: Cedar ENFORCE offers the reader client {write}")

    probe = {"name": read, "arguments": {"case_id": PROBE_CASE}}
    text = _text(mcp(gateway_url, token, "tools/call", probe))
    if "belongs to no run" not in text:
        sys.exit(f"FAIL: the run checks let {read} through with no run: {text[:300]}")

    other = next((n for n in names if not n.startswith((f"{NAME}-", "sap-"))), None)
    if other:
        result = mcp(
            gateway_url,
            token,
            "tools/call",
            {"name": other, "arguments": {"text": "verify probe"}},
        )
        if "Refused by the Gateway" in _text(result):
            sys.exit(f"FAIL: the run checks refused another use case's {other}")

    header = probe_grant(ssm, project, env)
    # The run id alone, which the browser holds, must not be enough.
    run_id_only = {"x-audit-correlation-id": header["x-audit-correlation-id"]}
    text = _text(mcp(gateway_url, token, "tools/call", probe, run_id_only))
    if "belongs to no run" not in text:
        sys.exit(
            f"FAIL: the run checks let {read} through on a run id alone: {text[:300]}"
        )
    text = _text(mcp(gateway_url, token, "tools/call", probe, header))
    if "verify-probe" not in text or "Refused" in text:
        sys.exit(f"FAIL: {read} with a run grant did not answer: {text[:300]}")
    if enforced:
        update = {
            "name": write,
            "arguments": {
                "case_id": PROBE_CASE,
                "updates": "{}",
                "action": "x",
            },
        }
        result = mcp(gateway_url, token, "tools/call", update, header)
        if "error" not in result:
            sys.exit(f"FAIL: Cedar let the reader client call {write}: {result}")
    sops = sop_check(gateway_url, token, header, names, ssm, project, env)
    sops += knowledge_check(gateway_url, token, header, names, ssm, project, env)
    sops += write_refusal_check(gateway_url, token, ssm, project, env, enforced)

    sap = sap_check(gateway_url, token, names, ssm, project, env, enforced)
    sap += pipeline_check(ssm, project, env, region)
    sap += ingest_check(ssm, project, env, region)
    sap += api_check(token, ssm, project, env)
    sap += alarms_check(ssm, project, env, region)
    sap += isolation_check(ssm, project, env, region)
    sap += retention_check(ssm, project, env, region)
    sap += people_check(token, ssm, project, env, region)
    packs = packs_check(ssm, project, env, region)
    runtime_check(token, ssm, project, env, region)
    print(
        f"OK: reader lists {len(names)} tool(s); the run checks refused {read} with no "
        f"run and with a run id but not its secret, and passed it with both; {other or 'no other use case'}'s tool passed "
        "untouched"
        + ("; Cedar ENFORCE refused the reader's write" if enforced else "")
        + sops
        + sap
        + packs
        + "; the runtime is READY and refused the reader's token"
    )
    return 0


def write_refusal_check(gateway_url, token, ssm, project, env, enforced) -> str:
    """The run checks refuse what a grant does not cover, before any target runs: a
    rehearsal's case update, and a SAP write on a run that names no case. Under ENFORCE
    Cedar refuses the reader both as well, so its refusal also counts there."""

    def refused(call: dict, header: dict, run_checks_reason: str) -> bool:
        result = mcp(gateway_url, token, "tools/call", call, header)
        return run_checks_reason in _text(result) or (enforced and "error" in result)

    rehearsal = probe_grant(ssm, project, env, rehearsal=True)
    update = {
        "name": f"{TARGET}___update_case_state",
        "arguments": {"case_id": "verify-probe", "updates": "{}", "action": "x"},
    }
    if not refused(update, rehearsal, "is a rehearsal"):
        sys.exit("FAIL: the run checks let a rehearsal update a case")
    targets = json.loads(
        ssm.get_parameter(Name=f"/{project}/{env}/use-cases/{NAME}/sap-targets")[
            "Parameter"
        ]["Value"]
    )
    if not targets:
        return "; the run checks refused a rehearsal's case update"
    write = {
        "name": f"{next(iter(targets.values()))}___odata_function_import",
        "arguments": {
            "service_name": "API_SUPPLIERINVOICE_PROCESS_SRV",
            "function_name": "Release",
            "parameters": {"SupplierInvoice": "'verify-probe'", "FiscalYear": "'0000'"},
        },
    }
    header = probe_grant(ssm, project, env)
    if not refused(write, header, "cannot be tied to a case's document"):
        sys.exit("FAIL: the run checks let a SAP write tied to no case through")
    return "; the run checks refused a rehearsal's case update and a SAP write tied to no case"


def sop_check(gateway_url, token, header, names, ssm, project, env) -> str:
    """load_sop answers a published process type with its versioned SOP, and a run
    whose grant names other packs is refused another pack's SOP search."""
    tool = f"{NAME}-sops___load_sop"
    if tool not in names:
        sys.exit(f"FAIL: tools/list for the reader client lacks {tool}")
    import boto3

    bucket = ssm.get_parameter(
        Name=f"/{project}/{env}/use-cases/{NAME}/registry-bucket"
    )["Parameter"]["Value"]
    published = json.loads(
        boto3.client("s3", region_name=ssm.meta.region_name)
        .get_object(Bucket=bucket, Key="registry.json")["Body"]
        .read()
    )["packs"]
    first = next(
        (
            (p, pt)
            for p, e in sorted(published.items())
            if not e.get("incomplete")
            for pt in sorted(e["pack"].get("process_type_to_sop") or {})
        ),
        None,
    )
    if first is None:
        sys.exit(
            "FAIL: no pack is published here; a quality or production stack takes "
            "packs only by import (ops bundle import <bundle> --change-ref <ref> --apply)"
        )
    pack, process_type = first
    call = {"name": tool, "arguments": {"process_type": process_type}}
    text = _text(mcp(gateway_url, token, "tools/call", call, header))
    if not text.startswith(f"[sop {pack}/") or " version " not in text.split("\n")[0]:
        sys.exit(f"FAIL: {tool} {process_type} did not answer its SOP: {text[:300]}")
    search = f"{NAME}-knowledge___search_sap_sops"
    if search not in names:
        return f"; load_sop answered {process_type} with its versioned SOP"
    probe = {"name": search, "arguments": {"query": "tolerance", "pack_id": pack}}
    # Constants are always declared, so one left raw means the hit skipped the
    # substitution load_sop applies. An unset contact stays raw by design.
    hit = _text(mcp(gateway_url, token, "tools/call", probe, header))
    found = re.findall(r"\{\{([A-Z][A-Z0-9_]*)\}\}", hit)
    raw = sorted({s for s in found if not s.startswith("CONTACT_")})
    if raw:
        sys.exit(f"FAIL: {search} returned {pack}'s SOP with {raw} unfilled")
    scoped = probe_grant(ssm, project, env, packs=["verify-no-such-pack"])
    text = _text(
        mcp(
            gateway_url,
            token,
            "tools/call",
            probe,
            scoped,
        )
    )
    if "not a use case this run may touch" not in text:
        sys.exit(
            f"FAIL: the run checks let a run search another pack's SOPs: {text[:300]}"
        )
    return (
        f"; load_sop answered {process_type} with its versioned SOP, a SOP search "
        "came back with its constants filled, and the run checks refused a SOP search "
        "outside the run's packs"
    )


def knowledge_check(gateway_url, token, header, names, ssm, project, env) -> str:
    """Each knowledge tool the deployment turned on answers through the Gateway: the API
    docs search returns a hit (so ingestion ran), and both agent-knowledge tools reach
    Aurora, waiting out a paused cluster."""
    features = json.loads(
        ssm.get_parameter(Name=f"/{project}/{env}/use-cases/{NAME}/knowledge-features")[
            "Parameter"
        ]["Value"]
    )
    calls = []
    if features.get("api_docs_search"):
        calls.append(
            (
                f"{NAME}-knowledge___search_sap_api_docs",
                {"query": "supplier invoice"},
                "[",
            )
        )
    if features.get("agent_knowledge"):
        target = f"{NAME}-agent-knowledge"
        calls += [
            (
                f"{target}___check_vendor_risk",
                {"supplier_number": "verify-probe"},
                '{"checked": "verify-probe"',
            ),
            (
                f"{target}___get_precedent",
                {"process_type": "verify-probe", "amount": 0},
                '{"precedents": ',
            ),
        ]
    for tool, arguments, starts in calls:
        if tool not in names:
            sys.exit(f"FAIL: tools/list for the reader client lacks {tool}")
        call = {"name": tool, "arguments": arguments}
        text = _text(mcp(gateway_url, token, "tools/call", call, header))
        if not text.startswith(starts):
            sys.exit(f"FAIL: {tool} did not answer: {text[:300]}")
    return "".join(f"; {tool.split('___')[1]} answered" for tool, _, _ in calls)


def pipeline_check(ssm, project, env, region) -> str:
    import boto3
    import requests

    own = f"/{project}/{env}/use-cases/{NAME}"

    def param(key: str) -> str | None:
        try:
            return ssm.get_parameter(Name=f"{own}/{key}")["Parameter"]["Value"]
        except ssm.exceptions.ParameterNotFound:
            return None

    queue_url = param("agent-queue-url")
    if not queue_url:
        sys.exit("FAIL: no agent queue is published")
    queue_arn = boto3.client("sqs", region_name=region).get_queue_attributes(
        QueueUrl=queue_url, AttributeNames=["QueueArn"]
    )["Attributes"]["QueueArn"]
    mappings = boto3.client("lambda", region_name=region).list_event_source_mappings(
        EventSourceArn=queue_arn
    )["EventSourceMappings"]
    if not any(m["State"] == "Enabled" for m in mappings):
        sys.exit("FAIL: nothing consumes the agent queue")
    # The eval runner invokes the published name; it must be what the queue feeds.
    invoker = param("invoker-function")
    if not invoker or not any(
        m["FunctionArn"].endswith(f":function:{invoker}") for m in mappings
    ):
        sys.exit(
            f"FAIL: invoker-function {invoker!r} is not what the agent queue feeds"
        )
    # `ops.py rerun`'s producer: deployed, reads the cases table, refuses an unknown case.
    rerun = param("operator-rerun-function")
    if rerun:
        out = boto3.client("lambda", region_name=region).invoke(
            FunctionName=rerun,
            Payload=json.dumps({"case_id": "verify-" + "a" * 26}),
        )
        answer = json.loads(out["Payload"].read() or b"{}")
        if "FunctionError" in out or answer.get("error") != "no such case":
            sys.exit(f"FAIL: operator-rerun on an unknown case answered {answer}")
    url = param("erp-events-url")
    if url:
        status = requests.post(url, json={}, timeout=30).status_code
        if status != 401:
            sys.exit(f"FAIL: POST /erp-events without a token answered HTTP {status}")
    return (
        "; the agent queue feeds the invoker"
        + (", operator-rerun refused an unknown case" if rerun else "")
        + (" and /erp-events refused a call without the token" if url else "")
    )


def _as_list(value) -> list:
    return value if isinstance(value, list) else [value]


def queue_policy_problem(policy: dict, rule_arn, push_role) -> str | None:
    """Why the ingest queue's policy lets anyone but the rule and the push route send, or None.

    Its Allows are the rule's alone, and its deny refuses every sender whose
    `aws:SourceArn` is not the rule and whose `aws:PrincipalArn` is not the push route's role.
    """
    allowed = {
        k: v
        for k, v in (("aws:SourceArn", rule_arn), ("aws:PrincipalArn", push_role))
        if v
    }
    deny = None
    for st in policy.get("Statement", []):
        actions = {a.lower() for a in _as_list(st.get("Action", []))}
        if st.get("Effect") == "Allow":
            source = (st.get("Condition") or {}).get("ArnEquals", {})
            if st.get("Principal") != {"Service": "events.amazonaws.com"} or (
                not rule_arn or _as_list(source.get("aws:SourceArn")) != [rule_arn]
            ):
                return f"it allows {st.get('Principal')} {sorted(actions)}"
        elif st.get("Sid") == "OnlyTheRuleAndPushSend":
            deny = st
    if deny is None:
        return "it has no deny for every other sender"
    condition = deny.get("Condition") or {}
    got = {k: _as_list(v) for k, v in condition.get("ArnNotEquals", {}).items()}
    if (
        deny.get("Principal") not in ("*", {"AWS": "*"})
        or _as_list(deny.get("Action")) != ["sqs:SendMessage"]
        or set(condition) != {"ArnNotEquals"}
        or got != {k: [v] for k, v in allowed.items()}
    ):
        return f"its deny exempts {got}, not exactly {allowed}"
    return None


def ingest_check(ssm, project, env, region) -> str:
    import boto3

    own = f"/{project}/{env}/use-cases/{NAME}"
    params = {
        p["Name"].removeprefix(f"{own}/"): p["Value"]
        for page in ssm.get_paginator("get_parameters_by_path").paginate(Path=own)
        for p in page["Parameters"]
    }
    if "ingest-queue-url" not in params:
        return ""
    consumer = params.get("sap-ingest-function")
    if not consumer:
        sys.exit("FAIL: an ingest queue is published but no sap-ingest-function")
    sqs = boto3.client("sqs", region_name=region)
    lam = boto3.client("lambda", region_name=region)
    attrs = sqs.get_queue_attributes(
        QueueUrl=params["ingest-queue-url"], AttributeNames=["QueueArn", "Policy"]
    )["Attributes"]
    arn = attrs["QueueArn"]
    dlq = sqs.get_queue_attributes(
        QueueUrl=params["ingest-dlq-url"],
        AttributeNames=["QueueArn", "ApproximateNumberOfMessages"],
    )["Attributes"]
    mappings = lam.list_event_source_mappings(EventSourceArn=arn)["EventSourceMappings"]
    if not any(
        m["State"] == "Enabled" and m["FunctionArn"].endswith(f":function:{consumer}")
        for m in mappings
    ):
        sys.exit("FAIL: nothing consumes the ingest queue")

    checked = ["the ingest queue feeds the consumer"]
    rule_arn = None
    rule_name = params.get("ingest-rule-name")
    if rule_name:
        events = boto3.client("events", region_name=region)
        bus = ssm.get_parameter(Name=f"/{project}/{env}/layers/sap/bus-arn")[
            "Parameter"
        ]["Value"]
        rule = events.describe_rule(Name=rule_name, EventBusName=bus)
        if rule.get("State") != "ENABLED":
            sys.exit(f"FAIL: the SAP events rule {rule_name} is {rule.get('State')}")
        rule_arn = rule["Arn"]
        targets = [
            (t["Arn"], (t.get("DeadLetterConfig") or {}).get("Arn"))
            for t in events.list_targets_by_rule(Rule=rule_name, EventBusName=bus)[
                "Targets"
            ]
        ]
        if targets != [(arn, dlq["QueueArn"])]:
            sys.exit(
                f"FAIL: the SAP events rule {rule_name} targets {targets}, not the "
                "ingest queue with its dead-letter queue"
            )
        checked.append(f"rule {rule_name} targets it with its dead-letter queue")
    push = params.get("erp-events-function")
    role = lam.get_function_configuration(FunctionName=push)["Role"] if push else None
    problem = queue_policy_problem(
        json.loads(attrs.get("Policy") or "{}"), rule_arn, role
    )
    if problem:
        sys.exit(f"FAIL: the ingest queue's policy is open: {problem}")
    checked.append(
        "only "
        + " and ".join(
            w for w, on in (("the rule", rule_arn), ("/erp-events", role)) if on
        )
        + " may send to it"
    )
    if int(dlq["ApproximateNumberOfMessages"]):
        sys.exit(
            f"FAIL: {dlq['ApproximateNumberOfMessages']} ingest message(s) dead-lettered"
        )

    # A nudge for document 0 of a pack on a served system: SAP holds none, so the read
    # answers nothing, and the consumer must answer it without a failure.
    served = list(
        json.loads(
            lam.get_function_configuration(FunctionName=consumer)["Environment"][
                "Variables"
            ].get("SAP_SYSTEMS", "{}")
        )
    )
    registry = json.loads(
        boto3.client("s3", region_name=region)
        .get_object(Bucket=params["registry-bucket"], Key="registry.json")["Body"]
        .read()
    )
    probed = None
    for pack_id, entry in sorted(registry.get("packs", {}).items()):
        source = entry.get("source") or {}
        named = source.get("sap_system") or entry["pack"].get("sap_system")
        system = named or (served[0] if len(served) == 1 else None)
        nested = bool((source.get("iterate") or {}).get("path"))
        fields = [
            k["field"]
            for k in source.get("key") or []
            if k.get("source", "self") == "parent"
            or (not nested and k.get("source", "self") == "self")
        ]
        if (
            not fields
            or source.get("kind", "filter") not in ("filter", "absence", "aging")
            or source.get("push", "nudge") == "off"
            or entry.get("incomplete")
            or system not in served
        ):
            continue
        body = {
            "source": f"sap.{system}",
            "detail-type": NAME,
            "detail": {"pack_id": pack_id, "keys": [{f: "0" for f in fields}]},
        }
        record = {
            "messageId": "verify",
            "eventSourceARN": arn,
            "body": json.dumps(body),
        }
        out = lam.invoke(
            FunctionName=consumer, Payload=json.dumps({"Records": [record]})
        )
        answer = json.loads(out["Payload"].read() or b"{}")
        if "FunctionError" in out or answer.get("batchItemFailures") != []:
            sys.exit(f"FAIL: a nudge for {pack_id} document 0 answered {answer}")
        probed = pack_id
        break
    checked.append("its dead-letter queue is empty")
    if probed:
        checked.append(
            f"the consumer, handed /erp-events's event for a {probed} nudge on "
            "document 0, answered with no case"
        )
    return "; " + ", ".join(checked)


def api_check(token, ssm, project, env) -> str:
    import requests

    own = f"/{project}/{env}/use-cases/{NAME}"
    api = ssm.get_parameter(Name=f"{own}/api-url")["Parameter"]["Value"].rstrip("/")
    status = requests.get(f"{api}/cases", timeout=30).status_code
    if status != 401:
        sys.exit(f"FAIL: /cases without a token answered HTTP {status}")
    # A rating steers precedent, so a machine client must not be able to cast one.
    status = requests.put(
        f"{api}/cases/ap-{'a' * 26}/rating",
        json={"rating": "positive"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    ).status_code
    if status not in (401, 403):
        sys.exit(f"FAIL: a rating with the reader's token answered HTTP {status}")
    checked = (
        "; /cases refused a call without a token and the rating route "
        "refused the reader's token"
    )
    status = requests.get(f"{api}/tickets", timeout=30).status_code
    # 401: mounted behind the authorizer. 403: not mounted (sample ticketing off),
    # API Gateway's answer to a path it does not have.
    if status not in (401, 403):
        sys.exit(f"FAIL: /tickets without a token answered HTTP {status}")
    if status == 401:
        checked += "; /tickets refused a call without a token"
    try:
        seeder = ssm.get_parameter(Name=f"{own}/test-data-function")["Parameter"][
            "Value"
        ]
    except ssm.exceptions.ParameterNotFound:
        return checked
    import boto3

    lam = boto3.client("lambda", region_name=ssm.meta.region_name)
    try:
        policy = lam.get_policy(FunctionName=seeder)["Policy"]
    except lam.exceptions.ResourceNotFoundException:
        policy = ""
    if "apigateway.amazonaws.com" in policy:
        sys.exit("FAIL: the test-data seeder is reachable from API Gateway")
    # A "not a pack" id: the seeder reads the registry and posts nothing.
    out = json.loads(
        lam.invoke(FunctionName=seeder, Payload=b'{"pack_id": "_verify"}')[
            "Payload"
        ].read()
    )
    if out.get("ok") or "seed.steps" not in str(out.get("error")):
        sys.exit(f"FAIL: the seeder answered a pack it has no steps for with {out}")
    checked += "; the seeder has no route and reads the registry"
    return checked


PEOPLE_TARGET = f"{NAME}-people"
_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": "verify",
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "verify", "version": "1"},
    },
}


def people_check(token, ssm, project, env, region) -> str:
    """Denial on the people Gateway, then the people target's own refusals.

    Nothing here signs a person in, so what a person may do is the live test's; this
    proves who may not.
    """
    import base64

    import boto3
    import requests

    base = f"/{project}/{env}"
    try:
        url = ssm.get_parameter(Name=f"{base}/people-gateway/url")["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        return ""
    accept = {"Accept": "application/json, text/event-stream"}
    resp = requests.post(url, json=_INITIALIZE, headers=accept, timeout=30)
    challenged = any(k.lower() == "www-authenticate" for k in resp.headers)
    if resp.status_code != 401 or not challenged:
        sys.exit(
            f"FAIL: the people Gateway answered an anonymous call with HTTP "
            f"{resp.status_code}{'' if challenged else ' and no WWW-Authenticate'}"
        )
    bearer = {**accept, "Authorization": f"Bearer {token}"}
    status = requests.post(
        url, json=_INITIALIZE, headers=bearer, timeout=30
    ).status_code
    if status not in (401, 403):
        sys.exit(
            f"FAIL: the people Gateway admitted the reader's token (HTTP {status})"
        )
    checked = "; the people Gateway refused an anonymous call and the reader's token"
    try:
        ssm.get_parameter(Name=f"{base}/people-gateway/issuer/discovery-url")
    except ssm.exceptions.ParameterNotFound:
        pass
    else:
        # With an issuer, the pool's tokens are not this Gateway's: no remote client
        # may be left to mint one (the reader's token, a pool token, is refused above).
        left = ssm.get_parameters_by_path(
            Path=f"{base}/people-gateway/remote-clients/", Recursive=True
        ).get("Parameters")
        if left:
            sys.exit(
                "FAIL: the people Gateway has an issuer and still publishes remote "
                f"clients: {[p['Name'] for p in left]}"
            )
        checked += ", and it trusts its issuer alone"

    try:
        fn = ssm.get_parameter(Name=f"{base}/use-cases/{NAME}/people-target-function")[
            "Parameter"
        ]["Value"]
    except ssm.exceptions.ParameterNotFound:
        return checked
    role = ssm.get_parameter(Name=f"{base}/people-gateway/role-arn")["Parameter"][
        "Value"
    ]
    lam = boto3.client("lambda", region_name=region)
    statements = json.loads(lam.get_policy(FunctionName=fn)["Policy"])["Statement"]
    others = [
        s.get("Principal") for s in statements if s.get("Principal") != {"AWS": role}
    ]
    if others or not statements:
        sys.exit(f"FAIL: the people target may be invoked by {others or 'no one'}")
    context = base64.b64encode(
        json.dumps(
            {"custom": {"bedrockAgentCoreToolName": f"{PEOPLE_TARGET}___list_packs"}}
        ).encode()
    ).decode()

    def call(arguments: dict) -> dict:
        resp = lam.invoke(
            FunctionName=fn,
            ClientContext=context,
            Payload=json.dumps(arguments).encode(),
        )
        out = json.loads(resp["Payload"].read() or b"null")
        return out if isinstance(out, dict) else {}

    if call({}).get("status") != 401:
        sys.exit("FAIL: the people target answered a call with no caller")
    outsider = {
        "sub": "verify-probe",
        "username": "verify",
        "client": "verify",
        "client_id": "verify",
        "groups": [f"{NAME}-users", f"{NAME}-authors"],
    }
    if call({"caller": outsider}).get("status") != 403:
        sys.exit("FAIL: the people target answered someone outside the remote authors")
    return (
        checked + "; the people target admits only that Gateway's role, and refused a "
        "call with no caller and one outside the remote authors"
    )


#: The product alarms every deployment has; the stack's id prefix on each name.
ALARMS = (
    "DlqAlarm",
    "AgentFailureAlarm",
    "AgentCostAlarm",
    "RunChannelAlarm",
    "LambdaErrorsAlarm",
    "LambdaThrottlesAlarm",
    "NotificationChannelAlarm",
    "RegistryUnavailableAlarm",
)


def alarms_check(ssm, project, env, region) -> str:
    """Every product alarm exists and publishes to the platform topic on ALARM and OK."""
    import boto3

    topic = ssm.get_parameter(Name=f"/{project}/{env}/ops-topic/arn")["Parameter"][
        "Value"
    ]
    stack = f"{project}-{env}-uc-{NAME}"
    alarms = [
        a
        for page in boto3.client("cloudwatch", region_name=region)
        .get_paginator("describe_alarms")
        .paginate(AlarmNamePrefix=f"{stack}-")
        for a in page["MetricAlarms"]
    ]
    for name in ALARMS:
        found = [a for a in alarms if a["AlarmName"].startswith(f"{stack}-{name}")]
        if not found:
            sys.exit(f"FAIL: no {name} on {stack}")
    unwired = [
        a["AlarmName"]
        for a in alarms
        if topic not in a["AlarmActions"] or topic not in a["OKActions"]
    ]
    if unwired:
        sys.exit(f"FAIL: alarms that do not publish to the platform topic: {unwired}")
    # The audit-change alerts (business_operations/audit_alerts.py).
    param = f"/{project}/{env}/use-cases/{NAME}/autonomy/trigger-mode"
    rules = [
        r
        for page in boto3.client("events", region_name=region)
        .get_paginator("list_rules")
        # CloudFormation truncates the stack name in a rule's name; match the pattern.
        .paginate()
        for r in page["Rules"]
    ]
    packs = param.replace("/trigger-mode", "/packs/")
    if not any(
        f'"{param}"' in (r.get("EventPattern") or "")
        and packs in (r.get("EventPattern") or "")
        for r in rules
    ):
        sys.exit(f"FAIL: no EventBridge rule watches {param} and {packs}*")
    s3 = boto3.client("s3", region_name=region)
    for key in ("registry-bucket", "sops-bucket"):
        bucket = ssm.get_parameter(Name=f"/{project}/{env}/use-cases/{NAME}/{key}")[
            "Parameter"
        ]["Value"]
        conf = s3.get_bucket_notification_configuration(Bucket=bucket)
        if not conf.get("LambdaFunctionConfigurations"):
            sys.exit(f"FAIL: the {key} notifies no audit alert")
    return (
        f"; {len(alarms)} alarms publish to the platform topic on ALARM and OK; "
        "the deployment's trigger mode, each pack's mode, the registry and the SOPs alert it on a "
        "write"
    )


def isolation_check(ssm, project, env, region) -> str:
    """The run channel checks subscribes; the Gateway role and Memory stay this deployment's.

    That each SAP runtime admits only the Gateway's role is the SAP layer's verify.
    """
    import boto3

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from business_operations import names as bo_names

    appsync = boto3.client("appsync", region_name=region)
    api_name = bo_names.run_events_api_name(f"{project}-{env}")
    (api,) = [a for a in appsync.list_apis()["apis"] if a["name"] == api_name]
    handlers = (
        appsync.get_channel_namespace(apiId=api["apiId"], name="runs")[
            "channelNamespace"
        ].get("handlerConfigs")
        or {}
    )
    if (handlers.get("onSubscribe") or {}).get("behavior") != "DIRECT":
        sys.exit("FAIL: the run-event channel admits a subscribe it never checks")
    iam = boto3.client("iam")
    trust = iam.get_role(RoleName=f"{project}-{env}-gateway-role")["Role"]
    if "aws:SourceArn" not in json.dumps(trust["AssumeRolePolicyDocument"]):
        sys.exit("FAIL: the Gateway role trusts AgentCore on behalf of any resource")
    role = f"{project}-{env}-{NAME}-runtime-role"
    policy = [
        st
        for name in iam.list_role_policies(RoleName=role)["PolicyNames"]
        for st in iam.get_role_policy(RoleName=role, PolicyName=name)["PolicyDocument"][
            "Statement"
        ]
    ]
    statements = [
        st
        for st in policy
        if "bedrock-agentcore:ListEvents" in json.dumps(st.get("Action"))
    ]
    if not statements or any(
        st.get("Condition")
        != {"StringLike": {"bedrock-agentcore:actorId": f"uc-{NAME}:*"}}
        for st in statements
    ):
        sys.exit("FAIL: the agent runtime may touch Memory actors outside its own")
    # Retrieval carries the namespace, not the actor id, so it is held there.
    if any(
        "bedrock-agentcore:RetrieveMemoryRecords" in json.dumps(st.get("Action"))
        and f"/actors/uc-{NAME}:*" not in json.dumps(st.get("Condition"))
        for st in policy
    ):
        sys.exit("FAIL: the agent runtime may retrieve Memory records outside its own")
    return (
        "; every run-channel subscribe is checked"
        "; the Gateway role trusts only this Gateway"
        "; the agent's Memory events and retrievals are held to its own actors"
    )


def retention_check(ssm, project, env, region) -> str:
    """The log group AgentCore creates for the agent runtime, with no retention, has one.

    The SAP runtimes' are the SAP layer's verify.
    """
    import boto3

    arn = ssm.get_parameter(Name=f"/{project}/{env}/runtimes/{NAME}/arn")["Parameter"][
        "Value"
    ]
    logs = boto3.client("logs", region_name=region)
    name = f"/aws/bedrock-agentcore/runtimes/{arn.rsplit('/', 1)[1]}-DEFAULT"
    groups = logs.describe_log_groups(logGroupNamePrefix=name)["logGroups"]
    days = next(
        (g.get("retentionInDays") for g in groups if g["logGroupName"] == name), None
    )
    if not days:
        sys.exit(f"FAIL: {name} keeps model content and has no retention")
    return "; the agent runtime's log group has retention"


def runtime_check(token, ssm, project, env, region) -> None:
    import boto3
    import requests

    arn = ssm.get_parameter(Name=f"/{project}/{env}/runtimes/{NAME}/arn")["Parameter"][
        "Value"
    ]
    described = boto3.client(
        "bedrock-agentcore-control", region_name=region
    ).get_agent_runtime(agentRuntimeId=arn.rsplit("/", 1)[1])
    if described["status"] != "READY":
        sys.exit(f"FAIL: the agent runtime is {described['status']}, not READY")
    bucket = ssm.get_parameter(
        Name=f"/{project}/{env}/use-cases/{NAME}/registry-bucket"
    )["Parameter"]["Value"]
    published = json.loads(
        boto3.client("s3", region_name=region)
        .get_object(Bucket=bucket, Key="registry.json")["Body"]
        .read()
    )
    # A tier the runtime has no model for is refused at run time, so every case of
    # that pack would park for review.
    from business_operations.runtime import TIER_ENV
    from business_operations.validate_packs import named_tiers

    served = described.get("environmentVariables") or {}
    for pack_id, entry in published.get("packs", {}).items():
        for where, tier in named_tiers(entry.get("pack") or {}):
            if not served.get(TIER_ENV.get(tier, "")):
                sys.exit(
                    f"FAIL: pack {pack_id} sets {where} {tier!r}, which the agent "
                    "runtime has no model for"
                )
    escaped = urllib.parse.quote(arn, safe="")
    resp = requests.post(
        f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{escaped}"
        "/invocations?qualifier=DEFAULT",
        json={},
        headers={
            "Authorization": f"Bearer {token}",
            "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": f"verify-{'0' * 33}",
        },
        timeout=30,
    )
    if resp.status_code not in (401, 403):
        sys.exit(
            f"FAIL: the agent runtime answered the reader's token with HTTP "
            f"{resp.status_code}; only the delegating client may call it"
        )


def packs_check(ssm, project, env, region) -> str:
    import boto3

    def param(key: str) -> str:
        name = f"/{project}/{env}/use-cases/{NAME}/{key}"
        return ssm.get_parameter(Name=name)["Parameter"]["Value"]

    s3 = boto3.client("s3", region_name=region)
    published = json.loads(
        s3.get_object(Bucket=param("registry-bucket"), Key="registry.json")[
            "Body"
        ].read()
    )
    if published.get("schema") != "pack-registry/1" or not published.get("packs"):
        sys.exit(
            f"FAIL: registry.json is unpublished or stale: schema {published.get('schema')!r}, "
            f"{len(published.get('packs') or {})} pack(s)"
        )
    unflagged = sorted(
        p for p, e in published["packs"].items() if "incomplete" not in e
    )
    if unflagged:
        sys.exit(
            f"FAIL: registry.json predates the published `incomplete` flag ({unflagged}): "
            "republish it (`ops.py bundle import`) so every reader reads one answer"
        )
    # A process type two packs claim routes to neither, and the composer refuses to
    # publish one, so a hit here is a corpus written around the composer.
    claims: dict[str, list[str]] = {}
    for pack_id, entry in sorted(published["packs"].items()):
        for process_type in entry["pack"].get("process_type_to_sop") or {}:
            claims.setdefault(process_type, []).append(pack_id)
    contested = {t: owners for t, owners in claims.items() if len(owners) > 1}
    if contested:
        sys.exit(f"FAIL: process types claimed by more than one pack: {contested}")
    sops = param("sops-bucket")
    # An incomplete pack (created in the console, awaiting a SAP survey) routes nothing, so
    # its SOPs may not exist yet.
    keys = {
        key
        for entry in published["packs"].values()
        if not entry.get("incomplete")
        for key in (entry["pack"].get("process_type_to_sop") or {}).values()
    }
    from pack_tiers import undeclared_symbols

    constants = {
        pack_id: entry["pack"].get("constants")
        for pack_id, entry in published["packs"].items()
    }
    for key in sorted(keys):
        stem = key.rsplit(".", 1)[0]
        found = [stem + e for e in (".txt", ".md") if _exists(s3, sops, stem + e)]
        if not found:
            sys.exit(
                f"FAIL: a published pack maps {key}, which is not in the SOP bucket"
            )
        # An edit made before the SOP routes checked symbols is still in the bucket.
        text = s3.get_object(Bucket=sops, Key=found[0])["Body"].read().decode()
        pack_id = key.split("/")[0]
        if error := undeclared_symbols(found[0], text, constants.get(pack_id)):
            sys.exit(f"FAIL: {error}")
    # The pack import at deploy adopts every live pack, so a pack with no version was never imported here.
    versions = {
        pack_id: (entry.get("bundle") or {}).get("version")
        for pack_id, entry in published["packs"].items()
    }
    if unversioned := sorted(p for p, v in versions.items() if v is None):
        sys.exit(f"FAIL: published pack(s) {', '.join(unversioned)} have no version")
    return (
        f"; {len(published['packs'])} pack(s) published with all {len(keys)} SOP(s), "
        + ", ".join(f"{p}@{v}" for p, v in sorted(versions.items()))
    )


def _exists(s3, bucket: str, key: str) -> bool:
    from botocore.exceptions import ClientError

    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError:
        return False


def sap_check(gateway_url, token, names, ssm, project, env, enforced) -> str:
    """One real SAP read per system: the first page of its service catalog."""
    targets = json.loads(
        ssm.get_parameter(Name=f"/{project}/{env}/use-cases/{NAME}/sap-targets")[
            "Parameter"
        ]["Value"]
    )
    for system, target in targets.items():
        tool = f"{target}___find_sap_services"
        if tool not in names:
            sys.exit(f"FAIL: tools/list for the reader client lacks {tool}")
        writes = [
            n
            for n in names
            if n.startswith(f"{target}___odata_")
            and not n.endswith(("_read", "_count"))
        ]
        if enforced and writes:
            sys.exit(f"FAIL: under ENFORCE the reader is offered SAP writes: {writes}")
        result = mcp(
            gateway_url, token, "tools/call", {"name": tool, "arguments": {"top": 1}}
        )
        body = result.get("result") or {}
        try:
            answer = json.loads(_text(result))
        except ValueError:
            answer = None
        # The AWS for SAP MCP Server reports SAP's own refusal (a 401, say) as a successful call.
        refused = isinstance(answer, dict) and answer.get("success") is False
        if (
            "error" in result
            or body.get("isError")
            or refused
            or not _text(result).strip()
        ):
            sys.exit(
                f"FAIL: SAP system {system}: {tool} did not answer: "
                f"{json.dumps(result)[:300]}"
            )
    return f"; read SAP through {len(targets)} system(s)" if targets else ""


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 - no creds, no network, no stack: FAIL
        sys.exit(f"FAIL: {type(e).__name__}: {e}")
