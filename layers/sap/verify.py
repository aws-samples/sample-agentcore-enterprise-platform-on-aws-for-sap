#!/usr/bin/env python3

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Verify the SAP layer with real calls, and with no client of its own.

From `layers/sap/systems`, cheapest first:

- each runtime this layer builds is READY, admits only the Gateway's role (its
  resource policy's DenyEveryoneElse), and its log group has a retention;
- the newest start banner each runtime logged since its last update says
  `Session Verify: True`, so the server verifies SAP's certificate;
- each SAP target on the Gateway is READY;
- when the check's settings say Cedar enforces, the live Gateway's policy engine is
  in ENFORCE: the check lets an ungoverned claimed write through on that word alone;
- through the first target, the platform's own M2M client, which no use case
  claims, is refused a SAP read by the layer's check, and refused `odata_delete`.
- when some system has `senders`: the `sap-events` bus's policy denies `PutEvents` to
  every principal but the sender roles, each role trusts exactly its `senders` and may
  put only its own `source` with its ceilings' `detail-type`s, a probe with this
  verifier's own credentials is refused, and every rule on the bus routes one use
  case's name from systems inside its `ingest` ceiling, with a dead-letter queue.

The positive proof, a SAP read that answers, belongs to each use case's own verify:
only a claimed client gets one. Exits 0 with `OK: ...`, 1 with `FAIL: ...`.

Usage: python layers/sap/verify.py
"""

import json
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "layers" / "sap" / "lib"))
sys.path.insert(0, str(REPO))


def mcp(url: str, token: str, method: str, params=None) -> dict:
    """One JSON-RPC request to the Gateway; a non-200 is a failure, not an answer."""
    import requests

    resp = requests.post(
        url,
        json={"jsonrpc": "2.0", "id": method, "method": method, "params": params or {}},
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"{method}: HTTP {resp.status_code} {resp.text[:200]}")
    return resp.json()


def _text(result: dict) -> str:
    body = result.get("result") or {}
    return " ".join(c.get("text", "") for c in body.get("content") or [])


def session_verify(messages: list[str]) -> bool | None:
    """What the newest start banner says about verifying SAP's certificate, or None
    when no session started (messages oldest first)."""
    said = [re.search(r"Session Verify:\s*(\w+)", m) for m in messages]
    said = [m.group(1) for m in said if m]
    return said[-1] == "True" if said else None


def unread_banner_failure(unstarted: list[str], mode: str | None) -> str | None:
    """Why verify fails on runtimes whose certificate banner it could not read, or None.

    Unread is not verified. Outside production verify says so and passes; production
    proves the certificate check or fails.
    """
    if not unstarted or mode != "production":
        return None
    return (
        "no session since its last update has logged the certificate banner of "
        f"{', '.join(unstarted)}, so verify cannot say it checks SAP's certificate: "
        "start a session (any SAP read through the Gateway) and run verify again"
    )


def cedar_mismatch(settings: dict, gateway: dict) -> str | None:
    """Why the check's word on Cedar is wrong for the live Gateway, or None.

    The check allows a write no use-case check governs only while `cedar_enforced`,
    which the layer's stack writes from platform.yaml; an out-of-band switch to
    LOG_ONLY would leave that write ungoverned."""
    if settings.get("cedar_enforced") is not True:
        return None
    mode = (gateway.get("policyEngineConfiguration") or {}).get("mode")
    if mode != "ENFORCE":
        return (
            "the SAP check's settings say Cedar enforces, but the live Gateway's "
            f"policy engine mode is {mode or 'unset'}"
        )
    return None


def _list(v) -> list:
    return v if isinstance(v, list) else [v]


def _refused(error) -> bool:
    return error.response.get("Error", {}).get("Code", "").startswith("AccessDenied")


def check_ingest(
    ingest: dict,
    bus_arn: str,
    roles: dict[str, str],
    events,
    iam,
    subscriptions: dict[str, dict],
) -> tuple[str | None, list[str]]:
    """Why the `sap-events` door is open wider than `ingest` says, or None; and the
    ceilings no rule subscribes (reported, not failed: their use case may be off).

    `ingest` is the layer's `ingest` parameter, `roles` each sender system's role ARN,
    `subscriptions` each use case's published subscription (`subscription_of`).
    """
    from botocore.exceptions import ClientError

    want = sorted(roles.values())
    policy = json.loads(events.describe_event_bus(Name=bus_arn).get("Policy") or "{}")
    deny = [
        st
        for st in policy.get("Statement", [])
        if st.get("Effect") == "Deny"
        and st.get("Principal") in ("*", {"AWS": "*"})
        and "events:PutEvents" in _list(st.get("Action"))
    ]
    held = [
        sorted(
            _list(
                st.get("Condition", {})
                .get("ArnNotEquals", {})
                .get("aws:PrincipalArn", [])
            )
        )
        for st in deny
    ]
    if want not in held:
        return (
            (
                f"the sap-events bus's policy does not deny PutEvents to every principal "
                f"but the sender roles {want}"
            ),
            [],
        )
    # In the bus's own account an allow in its policy grants PutEvents by itself and
    # bypasses the source and topic conditions on the sender roles' own policies.
    if allows := [
        st for st in policy.get("Statement", []) if st.get("Effect") == "Allow"
    ]:
        return (
            (
                f"the sap-events bus's policy allows {[st.get('Sid') for st in allows]}: "
                "in its own account that grants PutEvents past the sender roles' "
                "source and topic conditions"
            ),
            [],
        )
    for system, arn in sorted(roles.items()):
        topics = sorted(ingest["systems"].get(system, {}).get("topics", []))
        bound = {
            key: sorted(_list(vals))
            for st in policy.get("Statement", [])
            if st.get("Effect") == "Deny"
            and arn
            in _list(
                st.get("Condition", {}).get("ArnEquals", {}).get("aws:PrincipalArn")
            )
            for key, vals in st["Condition"]
            .get("ForAnyValue:StringNotEquals", {})
            .items()
        }
        if topics and (
            bound.get("events:source") != [f"sap.{system}"]
            or bound.get("events:detail-type") != topics
        ):
            return (
                (
                    f"the sap-events bus's policy does not hold {arn} to source "
                    f"sap.{system} and topics {topics}"
                ),
                [],
            )
    for system, spec in sorted(ingest["systems"].items()):
        role = roles[system].rsplit("/", 1)[-1]
        trust = iam.get_role(RoleName=role)["Role"]["AssumeRolePolicyDocument"]
        trusted = sorted(
            p
            for st in trust.get("Statement", [])
            if st.get("Effect") == "Allow"
            for p in _list((st.get("Principal") or {}).get("AWS", []))
        )
        if trusted != sorted(spec["senders"]):
            return f"sender role {role} trusts {trusted}, not {spec['senders']}", []
        if iam.list_attached_role_policies(RoleName=role)["AttachedPolicies"]:
            return f"sender role {role} has a managed policy attached", []
        statements = [
            st
            for name in iam.list_role_policies(RoleName=role)["PolicyNames"]
            for st in iam.get_role_policy(RoleName=role, PolicyName=name)[
                "PolicyDocument"
            ]["Statement"]
        ]
        expected = {
            "ForAllValues:StringEquals": {
                "events:source": [f"sap.{system}"],
                "events:detail-type": sorted(spec["topics"]),
            },
            "Null": {"events:source": "false", "events:detail-type": "false"},
        }
        if spec["topics"]:
            ok = len(statements) == 1 and statements[0].get("Effect") == "Allow"
            st = statements[0] if ok else {}
            cond = st.get("Condition", {})
            got = {
                "ForAllValues:StringEquals": {
                    k: sorted(_list(v))
                    for k, v in cond.get("ForAllValues:StringEquals", {}).items()
                },
                "Null": {k: str(v).lower() for k, v in cond.get("Null", {}).items()},
            }
            ok = (
                ok
                and _list(st.get("Action")) == ["events:PutEvents"]
                and _list(st.get("Resource")) == [bus_arn]
                and set(cond) == set(expected)
                and got == expected
            )
        else:
            ok = not statements
        if not ok:
            return (
                (
                    f"sender role {role} may do more than PutEvents on the bus with source "
                    f"sap.{system} and detail-type in {spec['topics']}"
                ),
                [],
            )

    # Our own credentials must be refused: only a sender role may put `sap.` events.
    first = min(ingest["systems"])
    try:
        sent = events.put_events(
            Entries=[
                {
                    "EventBusName": bus_arn,
                    "Source": f"sap.{first}",
                    "DetailType": "verify-probe",
                    "Detail": "{}",
                }
            ]
        )
    except ClientError as e:
        if not _refused(e):
            raise
    else:
        if not sent.get("FailedEntryCount"):
            return (
                (
                    f"the sap-events bus accepted a sap.{first} event from the verifier's "
                    "credentials, which are no sender role's"
                ),
                [],
            )

    ceilings = ingest["ceilings"]
    subscribed = set()
    for page in events.get_paginator("list_rules").paginate(EventBusName=bus_arn):
        for rule in page["Rules"]:
            pattern = json.loads(rule.get("EventPattern") or "{}")
            topic = _list(pattern.get("detail-type", []))
            sources = _list(pattern.get("source", []))
            uc = topic[0] if len(topic) == 1 else None
            inside = {f"sap.{s}" for s in ceilings.get(uc, [])}
            if uc not in ceilings or not sources or not set(sources) <= inside:
                return (
                    (
                        f"rule {rule['Name']} on the sap-events bus routes "
                        f"detail-type {topic} from {sources}, outside every "
                        "layers.sap.access.<use case>.ingest"
                    ),
                    [],
                )
            targets = events.list_targets_by_rule(
                Rule=rule["Name"], EventBusName=bus_arn
            )["Targets"]
            if not targets or not all(
                (t.get("DeadLetterConfig") or {}).get("Arn") for t in targets
            ):
                return f"rule {rule['Name']} has a target with no dead-letter queue", []
            if why := subscription_mismatch(rule, targets, uc, subscriptions.get(uc)):
                return why, []
            subscribed.add(uc)
    return None, sorted(set(ceilings) - subscribed)


#: CloudFormation writing as a CDK app's bootstrap execution role in this account.
_CFN_WRITER = r"arn:aws[a-z-]*:sts::{account}:assumed-role/cdk-[a-z0-9]+-cfn-exec-role-{account}-[a-z0-9-]+/AWSCloudFormation"


def subscription_of(ssm, name: str, account: str) -> dict | None:
    """A use case's published subscription, or None when it has none.

    Raises when its latest version was not written by a stack deploy: a hand-written
    subscription could vouch for a rule another stack added. Read from the parameter's
    history, as the layer's check reads a claim.
    """
    entries, token = [], None
    try:
        while True:
            page = ssm.get_parameter_history(
                Name=name, MaxResults=50, **({"NextToken": token} if token else {})
            )
            entries += page["Parameters"]
            token = page.get("NextToken")
            if not token:
                break
    except ssm.exceptions.ParameterNotFound:
        return None
    latest = max(entries, key=lambda e: e["Version"])
    writer = latest.get("LastModifiedUser") or ""
    if not re.fullmatch(_CFN_WRITER.format(account=account), writer):
        raise ValueError(
            f"{name} was last written by {writer or 'nobody known'}, not a stack deploy"
        )
    return json.loads(latest["Value"])


#: What a subscribed rule's one target may carry: an input transform, a role or HTTP
#: parameters would send something other than the event, or somewhere else.
_TARGET_KEYS = {"Id", "Arn", "DeadLetterConfig", "RetryPolicy"}


def subscription_mismatch(
    rule: dict, targets: list[dict], uc: str, subscription: dict | None
) -> str | None:
    """Why a rule on `uc`'s topic is not the one `uc` subscribed, or None.

    A rule names its use case only by its `detail-type`, which any stack may put in a
    rule of its own: the rule's ARN, its one target and that target's DLQ must be the
    ones `uc`'s `subscribe` published (`layers/sap/subscriptions/<uc>`).
    """
    name = rule["Name"]
    if subscription is None:
        return (
            f"rule {name} routes {uc}'s topic on the sap-events bus, but {uc} "
            "publishes no subscription (layers/sap/subscriptions/<use case>), so no "
            "subscribe made it"
        )
    if rule.get("Arn") != subscription.get("rule_arn"):
        return (
            f"rule {name} routes {uc}'s topic on the sap-events bus, but {uc}'s "
            f"subscription is rule {subscription.get('rule_arn')}: another stack "
            "added it"
        )
    if len(targets) != 1:
        return f"rule {name} has {len(targets)} targets; {uc} subscribed one, its queue"
    (target,) = targets
    if target.get("Arn") != subscription.get("queue_arn"):
        return (
            f"rule {name} sends {uc}'s events to {target.get('Arn')}, not its "
            f"subscribed queue {subscription.get('queue_arn')}"
        )
    if (target.get("DeadLetterConfig") or {}).get("Arn") != subscription.get("dlq_arn"):
        return f"rule {name}'s dead-letter queue is not {uc}'s subscribed one"
    if extra := sorted(set(target) - _TARGET_KEYS):
        return f"rule {name}'s target also sets {', '.join(extra)}"
    return None


def main() -> int:
    # Imported here so a missing dependency is reported as FAIL:, not a traceback.
    import boto3
    from sap_layer import names
    from utils import get_m2m_token

    from infra_utils.platform_config import resolve_region

    project = os.environ.get("PROJECT_NAME", "agentcore-workshop")
    env = os.environ.get("ENVIRONMENT", "dev")
    region = resolve_region()
    ssm = boto3.client("ssm", region_name=region)

    def param(name: str) -> str:
        return ssm.get_parameter(Name=f"/{project}/{env}/{name}")["Parameter"]["Value"]

    systems = json.loads(param("layers/sap/systems"))
    if not systems:
        print("OK: the SAP layer serves no system yet")
        return 0

    control = boto3.client("bedrock-agentcore-control", region_name=region)
    ours = {
        names.sap_runtime_name(project, env, s): s
        for s, spec in systems.items()
        if not spec.get("runtime_arn")
    }
    runtimes = [
        r
        for page in control.get_paginator("list_agent_runtimes").paginate()
        for r in page["agentRuntimes"]
        if r["agentRuntimeName"] in ours
    ]
    if len(runtimes) != len(ours):
        found = {r["agentRuntimeName"] for r in runtimes}
        sys.exit(f"FAIL: SAP runtime(s) missing: {sorted(set(ours) - found)}")
    logs = boto3.client("logs", region_name=region)
    unstarted: list[str] = []
    for r in runtimes:
        if r["status"] != "READY":
            sys.exit(f"FAIL: {r['agentRuntimeName']} is {r['status']}, not READY")
        if systems[ours[r["agentRuntimeName"]]]["flow"] != "obo":
            policy = control.get_resource_policy(resourceArn=r["agentRuntimeArn"])
            if "DenyEveryoneElse" not in policy.get("policy", ""):
                sys.exit(f"FAIL: {r['agentRuntimeName']} admits more than the Gateway")
        group = f"/aws/bedrock-agentcore/runtimes/{r['agentRuntimeId']}-DEFAULT"
        found = logs.describe_log_groups(logGroupNamePrefix=group)["logGroups"]
        if not any(
            g["logGroupName"] == group and g.get("retentionInDays") for g in found
        ):
            sys.exit(f"FAIL: {group} keeps model content and has no retention")
        banners = [
            e["message"]
            for page in logs.get_paginator("filter_log_events").paginate(
                logGroupName=group,
                startTime=int(r["lastUpdatedAt"].timestamp() * 1000),
                filterPattern='"Session Verify"',
            )
            for e in page["events"]
        ]
        verifying = session_verify(banners)
        if verifying is False:
            sys.exit(
                f"FAIL: {r['agentRuntimeName']} logs `Session Verify: False`, so it "
                "is not verifying SAP's certificate: deploy the SAP layer, and if "
                "the runtime already sets MCP_SERVER_SESSION_VERIFY=true, check the "
                "image's configuration for certificate verification"
            )
        if verifying is None:
            unstarted.append(r["agentRuntimeName"])

    gateway_id = param("gateway/id")
    mismatch = cedar_mismatch(
        json.loads(param("layers/sap/check")),
        control.get_gateway(gatewayIdentifier=gateway_id),
    )
    if mismatch:
        sys.exit(f"FAIL: {mismatch}")
    targets = {
        t["name"]: t
        for page in control.get_paginator("list_gateway_targets").paginate(
            gatewayIdentifier=gateway_id
        )
        for t in page["items"]
    }
    for spec in systems.values():
        t = targets.get(spec["target"])
        if t is None or t["status"] != "READY":
            state = t["status"] if t else "absent"
            sys.exit(f"FAIL: SAP target {spec['target']} is {state}, not READY")

    spec = next(iter(systems.values()))
    url, token = param("gateway/url"), get_m2m_token(project, env, region)
    read = {"name": f"{spec['target']}___find_sap_services", "arguments": {"top": 1}}
    text = _text(mcp(url, token, "tools/call", read))
    if "no use case claims SAP access" not in text:
        sys.exit(
            "FAIL: the platform's own M2M client, which no use case claims, was not "
            f"refused a SAP read by the layer's check: {text[:300]}"
        )
    delete = {
        "name": f"{spec['target']}___odata_delete",
        "arguments": {"service_name": "x", "entity_set": "x", "key": "x"},
    }
    text = _text(mcp(url, token, "tools/call", delete))
    if "odata_delete is not available" not in text:
        sys.exit(f"FAIL: the layer's check did not refuse odata_delete: {text[:300]}")
    door = ""
    try:
        ingest = json.loads(param("layers/sap/ingest"))
    except ssm.exceptions.ParameterNotFound:
        ingest = None  # no system has senders: no bus
    if ingest:
        roles = {
            s: param(f"layers/sap/systems/{s}/sender-role-arn")
            for s in ingest["systems"]
        }
        account = boto3.client("sts", region_name=region).get_caller_identity()[
            "Account"
        ]
        subscriptions = {
            uc: sub
            for uc in ingest["ceilings"]
            if (
                sub := subscription_of(
                    ssm, f"/{project}/{env}/layers/sap/subscriptions/{uc}", account
                )
            )
        }
        why, idle = check_ingest(
            ingest,
            param("layers/sap/bus-arn"),
            roles,
            boto3.client("events", region_name=region),
            boto3.client("iam", region_name=region),
            subscriptions,
        )
        if why:
            sys.exit(f"FAIL: {why}")
        door = (
            "; the sap-events bus refused the verifier's probe and admits only its "
            "sender roles, each limited to its own source and its ceilings' topics, "
            "and every rule on it is inside a ceiling with a dead-letter queue"
            + (f" (no rule yet for {', '.join(idle)})" if idle else "")
        )
    if why := unread_banner_failure(unstarted, os.environ.get("DEPLOYMENT_MODE")):
        sys.exit(f"FAIL: {why}")
    banner = (
        f"; UNVERIFIED certificate check, no session since its update to read the "
        f"banner of: {', '.join(unstarted)} (run verify again)"
        if unstarted
        else ""
    )
    print(
        f"OK: {len(runtimes)} SAP runtime(s) READY, admitting only the Gateway's role, "
        f"with log retention, verifying SAP's certificate; {len(systems)} SAP "
        "target(s) READY; the live Cedar mode matches the check's settings; the "
        f"layer's check refused an unclaimed client's SAP read and odata_delete{door}"
        f"{banner}"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 - no creds, no network, no stack: FAIL
        sys.exit(f"FAIL: {type(e).__name__}: {e}")
