#!/usr/bin/env python3

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Operator commands for business-operations: `deploy.sh usecase run business-operations <verb>`.

    status    every stack of the use case, and the parameters it published
    knowledge-load <file.json>
              upsert vendors, vendor edges and lessons into agent knowledge
              (needs knowledge.agent_knowledge.enabled and a deployed stack)
    add-user  add a pool user to the use case's groups, creating them with --create:
              add-user <email> [--create] [--admin] [--author] [--limit-owner]
                       [--case-worker] [--remote-author] [--pack-admin]
                       [--groups business-operations-<g>,...] [--yes]
              (--pack-admin asks first, unless --yes)
    seed <pack> [--preset N] [<input>=<value> ...] [--list] [--yes]
              post one exception of the pack into SAP (needs test_data.enabled)
    poll [<pack>]
              run the SAP poller now, for one pack or every pack
    add-group <name>  create a group a pack bundle requires on the pool
    bundle    export | import | wait | versions | status | rollback | retire
              a pack version between environments (business_operations/bundle_ops.py)
    pack status [<pack_id>] [--json]
              each pack's state, read-only: live version and where it came from,
              what routes to it, the checkout's differences, SOP search and the
              last refused write
    trigger-mode [get] | trigger-mode set auto|manual [--pack <pack_id>] [--yes]
              read or set the deployment's trigger mode and each pack's
    break-glass [--undo] [--yes]
              stop every agent run from starting: trigger mode manual, the agent queue's
              consumer paused, the invoker at zero concurrency; --undo resumes the
              invoker and leaves trigger mode manual (docs/operating/break-glass.md)
    emergency-stop <reason> [--yes] | emergency-stop --clear <reason> [--yes]
              stop every run and let nothing new start until a clear, as
              POST /deployment/stop does; --clear ends it and leaves trigger mode manual
    kb-sync   start an ingestion job on every deployed knowledge base
    mcp-config
              print the MCP host config for the Gateway proxy and the authoring MCP
    remote-client-config [quick|copilot-studio|issuer] [--no-secret]
              print the connector fields for a gateway.people remote client and
              copy its secret to the clipboard (never printed)
    cost-report [--months N] [--json]
              Cost Explorer by Component tag and by service (needs the Project,
              Environment and Component cost-allocation tags activated)
    types [--check]
              regenerate the console's TypeScript twins of types/*.schema.json,
              or fail when a committed twin is stale (needs no AWS)
    evals-setup [--yes]
              create this deployment's SAPActionAccuracy judge (asks first)
    evals-run [--pack P] [--only a,b] [--tag T] [--seed name=value] [--results-file f]
              [--no-judges] [--allow-quality] [--publish] [--yes]
              seed each eval case of the published pack in SAP, run it through the
              deployed invoker and grade it, pipeline.max_concurrency at once; posts
              SAP documents and bills Amazon Bedrock for every case, so it refuses a stack
              whose promotion.role is not development (evals/run_regression.py)
    production-check [--offline]
              a read-only pass/fail list of what blocks production, from
              platform.yaml and (unless --offline) EventBridge; exit 1 on a FAIL
    operator-policy bundle|seed|users|trigger-mode|evals
              print the least-privilege IAM policy for that group of verbs, naming
              this deployment's own resources (business_operations/operator_policy.py)
    rerun <case_id> [--force] | rerun --dlq [--max N] [--force] [--yes]
              put one case back on the agent queue (an Amazon Bedrock-billed run), or
              re-queue each dead letter's case the same way; --force also takes
              a case parked on a person, and asks first

`deploy.sh usecase run` exports PROJECT_NAME, ENVIRONMENT, AWS_REGION and this
use case's platform.yaml block as JSON in USE_CASE_CONFIG. Exit 0 ok, 1 failure,
2 usage.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import UTC
from pathlib import Path

from business_operations import bundle_ops
from business_operations import config as uc_config
from business_operations.names import NAME

UC = Path(__file__).resolve().parent


def _deployment() -> tuple[str, str, str | None]:
    """(project, environment, region) as deploy.sh exported them."""
    return (
        os.environ.get("PROJECT_NAME", "agentcore-workshop"),
        os.environ.get("ENVIRONMENT", "dev"),
        os.environ.get("AWS_REGION"),
    )


def _config() -> uc_config.BusinessOperationsConfig:
    return uc_config.load(json.loads(os.environ.get("USE_CASE_CONFIG") or "{}"))


_confirmed = bundle_ops.confirmed


def status() -> int:
    import boto3
    import yaml

    _config()
    project, env, region = _deployment()
    cfn = boto3.client("cloudformation", region_name=region)
    manifest = yaml.safe_load((UC / "manifest.yaml").read_text())
    healthy = True
    for suffix in manifest["stacks"]:
        stack = f"{project}-{env}-{suffix}"
        try:
            state = cfn.describe_stacks(StackName=stack)["Stacks"][0]["StackStatus"]
        except Exception as e:  # noqa: BLE001 - absent stack, no creds: say so
            state = f"FAIL: {type(e).__name__}: {e}"
        healthy &= state.endswith("_COMPLETE") and "ROLLBACK" not in state
        print(f"{stack}: {state}")
    prefix = f"/{project}/{env}/use-cases/{NAME}/"
    try:
        pages = (
            boto3.client("ssm", region_name=region)
            .get_paginator("get_parameters_by_path")
            .paginate(Path=prefix)
        )
        for page in pages:
            for p in page["Parameters"]:
                print(f"  {p['Name'].removeprefix(prefix)} = {p['Value']}")
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: {prefix}: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0 if healthy else 1


def knowledge_load(path: str | None = None) -> int:
    """Load `{"vendors": [...], "vendor_edges": [...], "lessons": [...]}`.

    The only writer of those three tables today: an operator loads relationships
    from their vendor master and lessons a reviewer wrote. Runs as the caller's
    own AWS credentials, through the RDS Data API, one statement per row.
    """
    import boto3

    if not path:
        print("usage: ops.py knowledge-load <file.json>", file=sys.stderr)
        return 2
    sys.path.insert(0, str(Path(__file__).resolve().parent / "agent_knowledge"))
    import queries

    try:
        statements = queries.load_statements(json.loads(Path(path).read_text()))
    except (OSError, ValueError) as e:
        print(f"FAIL: {path}: {e}", file=sys.stderr)
        return 1
    project, env, region = _deployment()
    prefix = f"/{project}/{env}/use-cases/{NAME}/agent-knowledge"
    ssm = boto3.client("ssm", region_name=region)
    try:
        cluster, secret, database = (
            ssm.get_parameter(Name=f"{prefix}-{k}")["Parameter"]["Value"]
            for k in ("cluster-arn", "secret-arn", "database")
        )
    except ssm.exceptions.ParameterNotFound:
        print(
            "FAIL: agent knowledge is not deployed; set knowledge.agent_knowledge."
            "enabled and deploy the knowledge stack",
            file=sys.stderr,
        )
        return 1
    data = boto3.client("rds-data", region_name=region)
    for sql, params in statements:
        queries.execute(
            data,
            attempts=12,
            resourceArn=cluster,
            secretArn=secret,
            database=database,
            sql=sql,
            parameters=[
                {"name": k, "value": {"stringValue": v}} for k, v in params.items()
            ],
        )
    print(f"OK: {len(statements)} row(s) upserted into {database}")
    return 0


ADD_USER_USAGE = (
    "usage: ops.py add-user <email> [--create] [--admin] [--author] "
    f"[--limit-owner] [--case-worker] [--remote-author] [--pack-admin] [--groups {NAME}-<g>,...] [--yes]"
)


def _pack_groups() -> list[str]:
    """Every group a checkout pack's `entitlement` names: packs.checkout_groups, minus
    its CDK imports, since this verb runs on an operator's machine."""
    return sorted(
        {
            g
            for f in (UC / "packs").glob("[!_]*/pack.json")
            for named in (json.loads(f.read_text()).get("entitlement") or {}).values()
            for g in named
        }
    )


def groups_for(flags: list[str]) -> list[str] | None:
    """The groups `add-user` grants for these flags, or None on an unknown flag.

    `--groups` names a pack's own groups (an `approve_groups` entry, say). Only this
    use case's: the pool is shared, so a platform group is never this verb's to grant.
    """
    extra = {
        "--admin": f"{NAME}-admins",
        "--author": f"{NAME}-authors",
        "--limit-owner": f"{NAME}-limit-owners",
        "--case-worker": f"{NAME}-case-workers",
        "--remote-author": f"{NAME}-remote-authors",
    }
    extra["--pack-admin"] = f"{NAME}-pack-admins"  # development stacks only
    groups = [f"{NAME}-users"]
    flags = list(flags)
    while flags:
        flag = flags.pop(0)
        if flag in extra:
            groups.append(extra[flag])
            if flag == "--admin":
                # An admin group entitles to no pack, so /cases is a 403 for an admin in
                # none of the packs' groups. An admin sees every pack, so grant them.
                groups += _pack_groups()
        elif flag == "--groups" and flags:
            named = [g.strip() for g in flags.pop(0).split(",") if g.strip()]
            if not named or not all(bundle_ops._GROUP.match(g) for g in named):
                return None
            groups += named
        else:
            return None
    return list(dict.fromkeys(groups))


def add_user(args: list[str]) -> int:
    """Add every pool user with this email to the use case's groups.

    By email, not username: a federated user's username is `<IdP>_<sub>`, created at
    their first sign-in. `--create` creates a Cognito user when none has that email
    (Cognito mails a temporary password), because with `identity.idp: cognito` the
    pool starts empty and nothing else creates the first user.
    """
    create = "--create" in args
    rest = [a for a in args if a not in ("--create", "--yes")]
    groups = groups_for(rest[1:]) if rest else None
    if not rest or groups is None or "@" not in rest[0] or '"' in rest[0]:
        print(ADD_USER_USAGE, file=sys.stderr)
        return 2
    if f"{NAME}-pack-admins" in groups and not _confirmed(
        args, f"Let {rest[0]} change every pack's structure and SAP values?"
    ):
        return 1
    import boto3

    email = rest[0]
    project, env, region = _deployment()
    ssm = boto3.client("ssm", region_name=region)
    idp = boto3.client("cognito-idp", region_name=region)
    try:
        mode = ssm.get_parameter(Name=f"/{project}/{env}/auth/mode")["Parameter"][
            "Value"
        ]
        if mode == "direct":
            # No pool: the groups are app roles on the platform's Entra app, assigned
            # in Entra (docs/direct-mode.md).
            print(
                "identity.mode direct: assign the app role(s) "
                f"{', '.join(groups)} on the platform app to {email} in Entra "
                "(docs/direct-mode.md, step 6).",
                file=sys.stderr,
            )
            return 2
        pool = ssm.get_parameter(Name=f"/{project}/{env}/auth/user-pool-id")[
            "Parameter"
        ]["Value"]
        users = idp.list_users(UserPoolId=pool, Filter=f'email = "{email}"')["Users"]
        if not users and create:
            users = [
                idp.admin_create_user(
                    UserPoolId=pool,
                    Username=email,
                    UserAttributes=[
                        {"Name": "email", "Value": email},
                        {"Name": "email_verified", "Value": "true"},
                    ],
                    DesiredDeliveryMediums=["EMAIL"],
                )["User"]
            ]
            print(f"created {email}; Cognito mails a temporary password")
    except Exception as e:  # noqa: BLE001 - no creds, no pool: say so
        print(f"FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    if not users:
        print(
            f"FAIL: no user with email {email} in pool {pool}. A federated user "
            "appears after their first sign-in; a Cognito user needs --create.",
            file=sys.stderr,
        )
        return 1
    for user in users:
        for group in groups:
            try:
                idp.admin_add_user_to_group(
                    UserPoolId=pool, Username=user["Username"], GroupName=group
                )
            except idp.exceptions.ResourceNotFoundException:
                print(
                    f"FAIL: no group {group} in pool {pool}; create it with: "
                    f"add-group {group}",
                    file=sys.stderr,
                )
                return 1
        print(f"{user['Username']}: {', '.join(groups)}")
    print("Groups reach a token at its next sign-in or refresh.")
    return 0


def seed_inputs(seed: dict, words: list[str]) -> dict:
    """The seed's inputs: the declared defaults, a preset, then `name=value`s.

    Raises ValueError on a preset that does not exist, a word that is not
    `name=value`, or a value its declared input cannot take. A name the pack does not
    declare passes through as JSON (or a string), and the seeder refuses it.
    """
    declared = {i["id"]: i for i in seed.get("inputs") or []}
    values = {k: i["default"] for k, i in declared.items() if "default" in i}
    words = list(words)
    if "--preset" in words:
        at = words.index("--preset")
        presets = seed.get("presets") or []
        n = words[at + 1] if at + 1 < len(words) else ""
        if not n.isdigit() or not 1 <= int(n) <= len(presets):
            raise ValueError(f"--preset takes 1 to {len(presets)} (see --list)")
        values |= presets[int(n) - 1]["values"]
        del words[at : at + 2]
    for word in words:
        name, sep, raw = word.partition("=")
        if not sep or not name:
            raise ValueError(f"{word!r} is not <input>=<value>")
        kind = declared.get(name, {}).get("type")
        if kind == "number":
            try:
                number = float(raw)
            except ValueError:
                raise ValueError(f"{name} must be a number") from None
            values[name] = int(number) if number.is_integer() else number
        elif kind == "boolean":
            if raw.lower() not in ("true", "false", "yes", "no", "1", "0"):
                raise ValueError(f"{name} must be true or false")
            values[name] = raw.lower() in ("true", "yes", "1")
        elif kind == "select":
            options = [o["value"] for o in declared[name].get("options") or []]
            if raw not in options:
                raise ValueError(f"{name} must be one of {options}")
            values[name] = raw
        elif kind:
            values[name] = raw
        else:
            try:
                values[name] = json.loads(raw)
            except ValueError:
                values[name] = raw
    return values


def _own_function(key: str) -> tuple[str | None, str | None]:
    """(this use case's function `key`, published by the stack; region)."""
    import boto3

    project, env, region = _deployment()
    name = f"/{project}/{env}/use-cases/{NAME}/{key}"
    try:
        value = boto3.client("ssm", region_name=region).get_parameter(Name=name)
        return value["Parameter"]["Value"], region
    except Exception as e:  # noqa: BLE001 - absent stack, no creds: say so
        print(f"FAIL: {name}: {type(e).__name__}: {e}", file=sys.stderr)
        return None, region


def _invoke(fn: str, region: str | None, payload: dict, seconds: int) -> dict:
    """One synchronous invoke, never retried: a retry after a timeout would run it twice."""
    import boto3
    from botocore.config import Config

    lam = boto3.client(
        "lambda",
        region_name=region,
        config=Config(read_timeout=seconds, retries={"total_max_attempts": 1}),
    )
    out = lam.invoke(FunctionName=fn, Payload=json.dumps(payload))
    result = json.loads(out["Payload"].read() or b"{}")
    if "FunctionError" in out:
        return {"ok": False, "error": f"the function raised: {result}"}
    return result


def seed(args: list[str]) -> int:
    """Manufacture one exception of a pack in SAP, through the seeder over IAM.

    The seeder posts the pack's published `seed.steps` as the SAP system's
    service account, outside the Gateway and the run checks: real accounting
    documents, so it asks first. A synchronous invoke within the seeder's own timeout,
    never retried, so a slow SAP is never posted to twice. The poller finds the
    blocked document on its next run, or at once with `poll <pack>`.
    """
    words = [a for a in args if a not in ("--yes", "--list")]
    if not words or words[0].startswith("-"):
        print(
            "usage: ops.py seed <pack> [--preset N] [<input>=<value> ...] [--list] [--yes]",
            file=sys.stderr,
        )
        return 2
    pack_id, words = words[0], words[1:]
    if not re.fullmatch(r"[a-z][a-z0-9_]*", pack_id):
        print(f"FAIL: {pack_id!r} is not a pack id", file=sys.stderr)
        return 1
    fn, region = _own_function("test-data-function")
    if fn is None:
        print(
            f"The seeder is deployed only with use_cases.{NAME}.test_data.enabled: true.",
            file=sys.stderr,
        )
        return 1
    # The published pack's seed, which is what the seeder posts; the checkout's may be
    # another version.
    described = _invoke(fn, region, {"pack_id": pack_id, "describe": True}, 60)
    spec = described.get("seed") if described.get("ok") else None
    if not spec:
        print(f"FAIL: {described.get('error') or described}", file=sys.stderr)
        return 1
    if "--list" in args:
        print(f"{pack_id}: {spec['label']}. {spec.get('summary', '')}".rstrip())
        for i in spec.get("inputs") or []:
            hint = f" ({i['hint']})" if i.get("hint") else ""
            print(f"  {i['id']}={i.get('default', '')!s:10} {i['label']}{hint}")
        if described.get("flow", "basic") != "basic":
            print(
                f"  posting_date is required: {described['system']} signs in with "
                f"{described['flow']}, and SAP's open period is read only over Basic"
            )
        for n, preset in enumerate(spec.get("presets") or [], 1):
            print(f"  --preset {n}: {preset['label']}")
        return 0
    try:
        inputs = seed_inputs(spec, words)
    except ValueError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 2
    on = (
        f" on {described['system']} ({described.get('system_role')})"
        if "system" in described
        else ""
    )
    print(f"{pack_id} (published){on}: {spec['label']} with {json.dumps(inputs)}")
    for step in spec["steps"]:
        when = f" when {step['when']}" if "when" in step else ""
        print(f"  POST {step['service']}/{step['entity']} ({step['id']}){when}")
    if not _confirmed(args, "Post these documents into SAP?"):
        return 1
    result = _invoke(
        fn,
        region,
        {"pack_id": pack_id, "inputs": inputs, "requested_by": _caller(region)},
        150,
    )
    print(json.dumps(result, indent=2, default=str))
    # The step SAP refused comes back as `<step>_error` beside the documents that
    # landed, so a missing "ok" is not the only failure.
    failed = [k for k, v in result.items() if k.endswith("_error") and v]
    if failed:
        print(f"FAIL: {', '.join(failed)}", file=sys.stderr)
    return 0 if result.get("ok") and not failed else 1


def poll(args: list[str]) -> int:
    """Run the SAP poller now, for one pack or every pack, rather than wait for its
    schedule. It creates cases exactly as the scheduled run does, and under the `auto` trigger
    mode queues them. A named pack is polled whatever its `source.poll`; with
    none, the packs the `poller_schedule` run polls."""
    if len(args) > 1 or (args and args[0].startswith("-")):
        print("usage: ops.py poll [<pack>]", file=sys.stderr)
        return 2
    fn, region = _own_function("sap-poller-function")
    if fn is None:
        print("The poller is deployed only when a SAP system is.", file=sys.stderr)
        return 1
    result = _invoke(fn, region, {"pack_id": args[0]} if args else {}, 330)
    print(json.dumps(result, indent=2, default=str))
    packs = result.get("packs")
    ok = isinstance(packs, dict) and not any("error" in r for r in packs.values())
    return 0 if ok else 1


def _caller(region: str | None) -> str:
    """Who is running this, for the records a write leaves behind."""
    import boto3

    return boto3.client("sts", region_name=region).get_caller_identity()["Arn"]


TRIGGER_MODES = ("auto", "manual")
# The sibling parameter PUT /deployment/trigger-mode writes, and GET reads back beside
# the mode (api/trigger_mode/index.py ACTOR_SUFFIX).
ACTOR_SUFFIX = "-last-changed-by"


TRIGGER_MODE_USAGE = "usage: ops.py trigger-mode [get] | trigger-mode set auto|manual [--pack <pack_id>] [--yes]"
_PACK_ID = re.compile(r"[a-z][a-z0-9_]*")


def trigger_mode(args: list[str]) -> int:
    """Read or set the trigger mode, or one pack's mode, with the operator's own AWS
    credentials.

    Straight to the SSM parameters rather than through PUT /deployment/trigger-mode: that route
    needs an admin's ID token, which a terminal has no way to get without a browser.
    CloudTrail names the operator on this path; the version description and the
    actor record carry the same attribution the route writes.
    """
    import boto3
    from business_operations import names

    project, env, region = _deployment()
    params = names.trigger_mode_parameters(f"/{project}/{env}")
    return trigger_mode_with(
        boto3.client("ssm", region_name=region),
        params,
        args,
        lambda: _caller(region),
        sorted(
            p.parent.name
            for p in (UC / "packs").glob("*/pack.json")
            if not p.parent.name.startswith("_")
        ),
    )


def trigger_mode_with(
    ssm, params: dict, args: list[str], caller, checkout: list[str]
) -> int:
    """`trigger_mode` over a given SSM client. `checkout` is the checkout's pack ids; with
    the packs that already have a mode, they are the packs `get` lists and `--pack`
    may name."""
    from datetime import datetime

    def read(name: str) -> str | None:
        try:
            return ssm.get_parameter(Name=name)["Parameter"]["Value"]
        except ssm.exceptions.ParameterNotFound:
            return None

    def pack_modes() -> dict[str, str]:
        found: dict[str, str] = {}
        for page in ssm.get_paginator("get_parameters_by_path").paginate(
            Path=params["packs"]
        ):
            for p in page.get("Parameters", []):
                found[p["Name"].rsplit("/", 1)[-1]] = p["Value"]
        return found

    switch = params["switch"]
    words = [a for a in args if a != "--yes"]
    if words in ([], ["get"]):
        current = read(switch)
        print(f"trigger-mode = {current or 'manual (unset)'}")
        print(f"last change: {read(switch + ACTOR_SUFFIX) or '(not recorded)'}")
        modes = pack_modes()
        on = []
        for pack_id in sorted(set(checkout) | set(modes)):
            mode = modes.get(pack_id)
            effective = "auto" if current == "auto" and mode == "auto" else "manual"
            on += [pack_id] if effective == "auto" else []
            print(f"{pack_id} = {mode or 'manual (unset)'}; effective {effective}")
        if current == "auto" and not on:
            print(
                "WARN: the deployment's trigger mode is auto but no pack is: nothing runs unattended"
            )
        return 0
    pack_id = None
    if len(words) == 4 and words[2] == "--pack":
        pack_id, words = words[3], words[:2]
    if len(words) != 2 or words[0] != "set" or words[1] not in TRIGGER_MODES:
        print(TRIGGER_MODE_USAGE, file=sys.stderr)
        return 2
    mode = words[1]
    if pack_id is not None:
        if not _PACK_ID.fullmatch(pack_id):
            print(f"FAIL: {pack_id!r} is not a pack id", file=sys.stderr)
            return 2
        if pack_id not in set(checkout) | set(pack_modes()):
            print(
                f"FAIL: {pack_id} is not a pack in this checkout and has no mode here; "
                "set a pack only this stack runs with PUT /packs/<pack_id>/trigger-mode, which checks the "
                "published packs",
                file=sys.stderr,
            )
            return 2
        name, actor_name = (
            f"{params['packs']}/{pack_id}",
            f"{params['pack_actors']}/{pack_id}",
        )
        label = f"{pack_id} trigger-mode"
    else:
        name, actor_name, label = switch, switch + ACTOR_SUFFIX, "trigger-mode"
    if mode == "auto" and (stop := _standing_stop(read, params.get("stop"))):
        print(
            f"FAIL: an emergency stop stands: {stop.get('reason') or 'no reason recorded'}"
            f" ({stop.get('stopped_by', '?')}, {stop.get('stopped_at', '?')}). Nothing "
            "turns to auto until an admin clears it (emergency-stop --clear).",
            file=sys.stderr,
        )
        return 1
    current = read(name)
    if (current or "manual") == mode:
        print(f"{label} is already {mode}")
        return 0
    question = (
        f"Let every new {pack_id} exception start an Amazon Bedrock-billed run with no human, "
        "whenever the deployment's trigger mode is auto?"
        if pack_id
        else "Let every new SAP exception start an Amazon Bedrock-billed run with no human?"
    )
    if mode == "auto" and not _confirmed(args, question):
        return 1
    actor = caller()
    ssm.put_parameter(
        Name=name, Value=mode, Type="String", Overwrite=True, Description=actor[:1024]
    )
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        ssm.put_parameter(
            Name=actor_name,
            Value=f"{mode} set by {actor} at {stamp}",
            Type="String",
            Overwrite=True,
        )
    except Exception as e:  # noqa: BLE001 - the flip happened; only its record did not
        print(f"WARN: could not record who set it: {e}", file=sys.stderr)
    print(f"OK: {label} {current or 'manual (unset)'} -> {mode}")
    return 0


def _standing_stop(read, name: str | None) -> dict | None:
    """The emergency stop when it stands, as `emergency_stop.standing` reads it: an
    absent parameter is no stop; one that cannot be parsed stands."""
    if not name:
        return None
    raw = read(name)
    if raw is None:
        return None
    try:
        state = json.loads(raw)
    except ValueError:
        return {"reason": "the emergency stop could not be read"}
    if not isinstance(state, dict):
        return {"reason": "the emergency stop could not be read"}
    return state if state.get("stands") is not False else None


def break_glass(args: list[str]) -> int:
    """Stop every agent run from starting, or with --undo let them start again.

    The invoker is the only caller of the agent runtime, whoever starts the run: the
    queue (the poller, push, the sweeper, the console's enqueue and replies, rerun)
    and POST /runs, which invokes it directly. Its queue's consumer is paused first,
    so a queued case waits on the queue (seven days) rather than being throttled into
    the dead-letter queue; then zero reserved concurrency refuses every direct invoke.
    A run already under way is not stopped: it ends within the invoker's 15 minutes.
    --undo leaves trigger mode manual, so turning unattended runs back on stays a choice.
    """
    undo = "--undo" in args
    if [a for a in args if a not in ("--undo", "--yes")]:
        print("usage: ops.py break-glass [--undo] [--yes]", file=sys.stderr)
        return 2
    question = (
        "Let agent runs start again (the trigger mode stays manual)?"
        if undo
        else "Stop every agent run from starting, the ones people start included?"
    )
    if not _confirmed(args, question):
        return 1
    fn, region = _own_function("invoker-function")
    if fn is None:
        return 1
    if not undo and trigger_mode(["set", "manual"]):
        return 1
    import boto3

    lam = boto3.client("lambda", region_name=region)
    if undo:
        lam.delete_function_concurrency(FunctionName=fn)
        print(f"OK: {fn} reserved concurrency removed")
    mappings = lam.list_event_source_mappings(FunctionName=fn)["EventSourceMappings"]
    for m in mappings:
        lam.update_event_source_mapping(UUID=m["UUID"], Enabled=undo)
        state = "enabled" if undo else "disabled"
        print(f"OK: {m['EventSourceArn'].rsplit(':', 1)[-1]} -> {fn} {state}")
    if not undo:
        lam.put_function_concurrency(FunctionName=fn, ReservedConcurrentExecutions=0)
        print(f"OK: {fn} reserved concurrency 0: no run starts")
        print(
            "A run under way still finishes. To stop its SAP writes now, follow "
            "docs/operating/break-glass.md from step 3."
        )
    return 0


EMERGENCY_STOP_USAGE = (
    "usage: ops.py emergency-stop <reason> [--yes] | "
    "emergency-stop --clear <reason> [--yes]"
)


def emergency_stop(args: list[str]) -> int:
    """Pull or clear the emergency stop with the operator's own AWS credentials.

    Through the stop function, invoked over IAM, rather than straight to SSM: only that
    function's role may revoke run grants and pause the agent queue, and one function
    keeps one order of steps. The actor is this operator's STS identity; CloudTrail
    names the IAM principal that invoked.
    """
    clear = "--clear" in args
    words = [a for a in args if a not in ("--clear", "--yes")]
    reason = " ".join(words).strip()
    if not reason:
        print(EMERGENCY_STOP_USAGE, file=sys.stderr)
        return 2
    question = (
        "Clear the emergency stop so runs may start again (the trigger mode stays manual)?"
        if clear
        else "Stop every agent run, and let nothing start until someone clears it?"
    )
    if not _confirmed(args, question):
        return 1
    fn, region = _own_function("stop-function")
    if fn is None:
        return 1
    result = _invoke(
        fn,
        region,
        {
            "action": "clear" if clear else "stop",
            "reason": reason,
            "actor": _caller(region),
        },
        60,
    )
    if not result.get("ok"):
        print(f"FAIL: {result.get('error')}", file=sys.stderr)
        return 1
    stop = result["stop"]
    if clear:
        print(f"OK: cleared by {stop.get('cleared_by')} at {stop.get('cleared_at')}")
        print(
            f"agent queue resumed: {stop.get('queue_resumed')}; the trigger mode stays manual"
        )
        return 0
    print(f"OK: an emergency stop stands: {stop.get('stands')}")
    print(
        f"trigger mode manual: {stop.get('trigger_mode_set')}; agent queue paused: "
        f"{stop.get('queue_held')}; runs stopped: {stop.get('runs_total')}"
        + ("" if stop.get("complete") else " (some session stops were not asked)")
    )
    for run in stop.get("runs") or []:
        print(
            f"  {run['run_id']} {run.get('case_id') or '(no case)'}: session "
            f"{run['session_stopped']}, released {run['released']}, parked {run['parked']}"
        )
    return 0


def kb_sync(_args: list[str]) -> int:
    """Re-index every deployed knowledge base, e.g. after a failed job.

    The deploy starts a job when the checkout's corpus changes, and a SOP write starts
    one on every write; this is for anything else that changed a bucket.
    """
    import boto3

    project, env, region = _deployment()
    prefix = f"/{project}/{env}/use-cases/{NAME}"
    ssm = boto3.client("ssm", region_name=region)
    agent = boto3.client("bedrock-agent", region_name=region)
    actor = _caller(region)
    found = 0
    for corpus in ("sops", "api-docs"):
        try:
            kb, ds = (
                ssm.get_parameter(Name=f"{prefix}/{corpus}-{k}")["Parameter"]["Value"]
                for k in ("kb-id", "data-source-id")
            )
        except ssm.exceptions.ParameterNotFound:
            continue
        found += 1
        try:
            job = agent.start_ingestion_job(
                knowledgeBaseId=kb,
                dataSourceId=ds,
                description=f"ops.py kb-sync by {actor}"[:200],
            )["ingestionJob"]
            print(f"{corpus}: job {job['ingestionJobId']} {job['status']}")
        except agent.exceptions.ConflictException:
            print(f"{corpus}: a job is already running")
    if not found:
        print(
            "FAIL: no knowledge base is deployed (knowledge.* flags)", file=sys.stderr
        )
        return 1
    return 0


# The reads an MCP host can complete over sap_kit/mcp_gateway_proxy.py. get_case_state,
# load_sop, get_precedent, demo_get_ticket and search_sap_sops are reads too, but the
# run checks refuse them outside a run, so offering them would only advertise a refusal.
MCP_READ_TOOLS = (
    "search_sap_api_docs",
    "check_vendor_risk",
    "find_sap_services",
    "get_metadata",
    "get_service_hints",
    "odata_read",
    "odata_count",
)


def mcp_config(_args: list[str]) -> int:
    """The `mcpServers` block for an MCP host: the Gateway proxy and the authoring MCP.

    Read-only: resolves and prints, mints no token and writes no file.
    """
    import boto3

    project, env, region = _deployment()
    base = f"/{project}/{env}"
    ssm = boto3.client("ssm", region_name=region)
    try:
        ssm.get_parameter(
            Name=f"{base}/gateway/use-case-clients/{NAME}/reader/client-id"
        )
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: {NAME}'s reader client is not deployed: {e}", file=sys.stderr)
        return 1
    venv = UC.parents[1] / ".venv" / "bin" / "python"
    env_block = {
        "PROJECT_NAME": project,
        "ENVIRONMENT": env,
        # botocore reads AWS_DEFAULT_REGION; a host spawns with a bare environment.
        "AWS_REGION": region or "",
        "AWS_DEFAULT_REGION": region or "",
        **(
            {"AWS_PROFILE": os.environ["AWS_PROFILE"]}
            if os.environ.get("AWS_PROFILE")
            else {}
        ),
    }
    servers = {
        f"{NAME}-gateway": {
            "command": str(venv) if venv.exists() else sys.executable,
            "args": [
                str(UC.parents[1] / "sap_kit" / "mcp_gateway_proxy.py"),
                "--use-case",
                NAME,
                *(arg for tool in MCP_READ_TOOLS for arg in ("--tool", tool)),
            ],
            "env": env_block,
        }
    }
    try:
        console = ssm.get_parameter(Name=f"{base}/use-cases/{NAME}/console-url")[
            "Parameter"
        ]["Value"]
        # Stdlib only and no AWS credentials: it signs in as the analyst.
        servers[f"{NAME}-authoring"] = {
            "command": "python3",
            "args": [
                str(UC / "tools" / "mcp-authoring" / "mcp_authoring.py"),
                "--app-url",
                console,
            ],
        }
    except ssm.exceptions.ParameterNotFound:
        print("WARN: no console-url yet, so no authoring server", file=sys.stderr)
    print(json.dumps({"mcpServers": servers}, indent=2))
    print(
        f"{NAME}-gateway: this use case's read tools only, as the reader client "
        "(a service identity, not you).\n"
        f"{NAME}-authoring: packs and SOPs as you, over the API; sign in first with "
        "`python3 mcp_authoring.py login --app-url <url>` (opens a browser).",
        file=sys.stderr,
    )
    return 0


REMOTE_CLIENTS = ("quick", "copilot-studio")
#: `remote-client-config issuer`: the people Gateway's own issuer (gateway.people.issuer).
ISSUER_CLIENT = "issuer"
_CLIPBOARDS = (
    ["pbcopy"],
    ["wl-copy"],
    ["xclip", "-selection", "clipboard"],
    ["clip.exe"],
)


def remote_client_fields(
    name: str, gateway_url: str, domain_url: str, client_id: str, callbacks: list[str]
) -> list[tuple[str, str]]:
    """The connector form, field by field, for `name`'s client app."""
    auth, token = f"{domain_url}/oauth2/authorize", f"{domain_url}/oauth2/token"
    if name == "quick":
        return [
            ("MCP server endpoint", gateway_url),
            ("Connection type", "Public network"),
            ("Authentication", "User authentication (Public OAuth client unchecked)"),
            ("Client ID", client_id),
            ("Client secret", "on the clipboard"),
            ("Authorization URL", auth),
            ("Token URL", token),
            (
                "Redirect URL",
                "leave as Quick fills it; the client allows " + ", ".join(callbacks),
            ),
        ]
    return [
        ("Server URL", gateway_url),
        ("Authentication", "OAuth 2.0, Manual"),
        ("Client ID", client_id),
        ("Client secret", "on the clipboard"),
        ("Authorization URL", auth),
        ("Token URL", token),
        ("Refresh URL", token),
        ("Scopes", "openid email profile"),
        ("Tool", "User authentication, not Agent author"),
    ]


def _to_clipboard(text: str) -> bool:
    import shutil
    import subprocess  # nosec B404 - fixed argv, the secret on stdin only

    for argv in _CLIPBOARDS:
        if shutil.which(argv[0]):
            subprocess.run(argv, input=text, text=True, check=True)  # nosec B603
            return True
    return False


def issuer_fields(
    gateway_url: str, discovery: dict, clients: str, audience: str
) -> list[tuple[str, str]]:
    """The connector form for the people Gateway's own issuer: its endpoints come from
    the issuer's discovery document; the secret is the issuer's, and never here."""
    return [
        ("MCP server endpoint", gateway_url),
        ("Authentication", "User authentication (OAuth, authorization code)"),
        ("Client ID", clients.replace(",", ", ")),
        (
            "Client secret",
            "the one the identity provider issued; paste it only into the connector",
        ),
        ("Authorization URL", str(discovery.get("authorization_endpoint") or "")),
        ("Token URL", str(discovery.get("token_endpoint") or "")),
        (
            "Audience (resource)",
            gateway_url if audience == "gateway" else audience,
        ),
    ]


def issuer_config() -> int:
    """`remote-client-config issuer`: the fields for a connector that signs in through
    the organisation's own issuer. Reads SSM and the public discovery document; asks for
    no secret and prints none."""
    import boto3

    project, env, region = _deployment()
    base = f"/{project}/{env}/people-gateway"
    ssm = boto3.client("ssm", region_name=region)

    def param(key: str) -> str:
        return ssm.get_parameter(Name=f"{base}/{key}")["Parameter"]["Value"]

    try:
        discovery_url = param("issuer/discovery-url")
    except ssm.exceptions.ParameterNotFound:
        print(
            "FAIL: no issuer: set gateway.people.issuer in platform.yaml and deploy "
            "(docs/modules/gateway.md#an-organisations-own-issuer)",
            file=sys.stderr,
        )
        return 1
    if not discovery_url.startswith("https://"):
        print(f"FAIL: the discovery URL is not https: {discovery_url}", file=sys.stderr)
        return 1
    import urllib.request

    with urllib.request.urlopen(discovery_url, timeout=15) as resp:  # nosec B310 - https checked above
        discovery = json.loads(resp.read())
    fields = issuer_fields(
        param("url"), discovery, param("issuer/clients"), param("issuer/audience")
    )
    width = max(len(k) for k, _ in fields)
    for key, value in fields:
        print(f"{key:<{width}}  {value}")
    print(
        "Each person signs in through the identity provider as themselves and needs "
        f"the groups {NAME}-users and {NAME}-remote-authors, plus each pack's own, "
        "from the issuer's groups claim or their pool user.",
        file=sys.stderr,
    )
    return 0


def remote_client_config(args: list[str]) -> int:
    """The connector fields for a gateway.people remote client; its secret to the clipboard.

    Read-only: resolves names and mints nothing. The secret is never printed.
    """
    import boto3

    name = (args or ["quick"])[0]
    if name == ISSUER_CLIENT:
        return issuer_config()
    if name not in REMOTE_CLIENTS:
        print(
            "usage: ops.py remote-client-config "
            f"[{'|'.join((*REMOTE_CLIENTS, ISSUER_CLIENT))}] [--no-secret]",
            file=sys.stderr,
        )
        return 2
    project, env, region = _deployment()
    base = f"/{project}/{env}"
    ssm = boto3.client("ssm", region_name=region)

    def param(key: str) -> str:
        return ssm.get_parameter(Name=f"{base}/{key}")["Parameter"]["Value"]

    try:
        client_id = param(f"people-gateway/remote-clients/{name}/client-id")
    except ssm.exceptions.ParameterNotFound:
        print(
            f"FAIL: no {name} client: add it under gateway.people.remote_clients in "
            "platform.yaml and deploy (docs/modules/gateway.md#people-gateway)",
            file=sys.stderr,
        )
        return 1
    pool = param("auth/user-pool-id")
    client = boto3.client("cognito-idp", region_name=region).describe_user_pool_client(
        UserPoolId=pool, ClientId=client_id
    )["UserPoolClient"]
    fields = remote_client_fields(
        name,
        param("people-gateway/url"),
        param("auth/domain-url"),
        client_id,
        client.get("CallbackURLs", []),
    )
    width = max(len(k) for k, _ in fields)
    for key, value in fields:
        print(f"{key:<{width}}  {value}")
    if "--no-secret" in args:
        print("\n(secret not copied: --no-secret)")
    elif _to_clipboard(client["ClientSecret"]):
        print(
            "\nThe client secret is on the clipboard. Paste it once, then copy something else."
        )
    else:
        print(
            "\nNo clipboard tool found, so the secret is not shown. Read it yourself: "
            f"aws cognito-idp describe-user-pool-client --user-pool-id {pool} "
            f"--client-id {client_id} --query UserPoolClient.ClientSecret --output text",
        )
    print(
        "Each person signs in as themselves and needs the pool groups "
        f"{NAME}-users and {NAME}-remote-authors, plus each pack's own. After the "
        "tools change, the connector owner presses Sync.",
        file=sys.stderr,
    )
    return 0


def month_window(today, months: int) -> tuple[str, str]:
    """Cost Explorer's [start, end): the first of the month `months - 1` back, to today
    inclusive (End is exclusive, so tomorrow)."""
    from datetime import timedelta

    year, month = today.year, today.month - (months - 1)
    while month <= 0:
        month, year = month + 12, year - 1
    return f"{year}-{month:02d}-01", (today + timedelta(days=1)).isoformat()


def cost_report(args: list[str]) -> int:
    from collections import defaultdict
    from datetime import datetime

    import boto3

    try:
        months = int(args[args.index("--months") + 1]) if "--months" in args else 1
        if months < 1:
            raise ValueError
    except (IndexError, ValueError):
        print("usage: ops.py cost-report [--months N] [--json]", file=sys.stderr)
        return 2
    project, env, _ = _deployment()
    start, end = month_window(datetime.now(UTC).date(), months)
    ce = boto3.client("ce", region_name="us-east-1")  # Cost Explorer's only endpoint
    tags = {
        "And": [
            {"Tags": {"Key": "Project", "Values": [project]}},
            {"Tags": {"Key": "Environment", "Values": [env]}},
        ]
    }
    report = {}
    for label, group in (
        ("by_component", {"Type": "TAG", "Key": "Component"}),
        ("by_service", {"Type": "DIMENSION", "Key": "SERVICE"}),
    ):
        totals: dict[str, float] = defaultdict(float)
        request = {
            "TimePeriod": {"Start": start, "End": end},
            "Granularity": "MONTHLY",
            "Metrics": ["UnblendedCost"],
            "Filter": tags,
            "GroupBy": [group],
        }
        while True:
            page = ce.get_cost_and_usage(**request)
            for period in page["ResultsByTime"]:
                for g in period["Groups"]:
                    key = g["Keys"][0].removeprefix("Component$") or "(untagged)"
                    totals[key] += float(g["Metrics"]["UnblendedCost"]["Amount"])
            if not page.get("NextPageToken"):
                break
            request["NextPageToken"] = page["NextPageToken"]
        report[label] = dict(sorted(totals.items(), key=lambda kv: -kv[1]))
    if "--json" in args:
        print(
            json.dumps(
                {
                    "project": project,
                    "environment": env,
                    "start": start,
                    "end": end,
                    **report,
                },
                indent=2,
            )
        )
        return 0
    print(f"{project}-{env}, {start} to {end} (exclusive), unblended USD")
    for label, totals in report.items():
        print(f"\n{label.replace('_', ' ')}:")
        for key, cost in totals.items():
            if cost >= 0.01:
                print(f"  {key:45s} {cost:>10.2f}")
        print(f"  {'total':45s} {sum(totals.values()):>10.2f}")
    print(
        "\nEmpty means the Project, Environment and Component tags are not activated "
        "as cost-allocation tags yet (up to 24 h after activation). Amazon Bedrock inference "
        "is untagged, so model spend is not in here.",
    )
    return 0


def types(args: list[str]) -> int:
    from business_operations import data_contracts

    if args not in ([], ["--check"]):
        print("usage: types [--check]", file=sys.stderr)
        return 2
    if not args:
        for path in data_contracts.write():
            print(f"wrote {path.relative_to(UC)}")
        return 0
    stale = data_contracts.stale()
    for path in stale:
        print(f"FAIL: {path.relative_to(UC)} is stale; run: types", file=sys.stderr)
    return 1 if stale else 0


def rerun_args(args: list[str]) -> tuple[str | None, int, bool] | None:
    """(case id, or None for --dlq; how many dead letters; force), None on bad usage."""
    force = "--force" in args
    words = [a for a in args if a not in ("--force", "--yes")]
    if words[:1] == ["--dlq"]:
        rest = words[1:]
        if not rest:
            return None, 10, force
        if len(rest) == 2 and rest[0] == "--max" and rest[1].isdigit():
            return (None, int(rest[1]), force) if int(rest[1]) else None
        return None
    if len(words) == 1 and not words[0].startswith("-"):
        return words[0], 0, force
    return None


def dead_letter_case(body: str) -> tuple[str, str]:
    """(case id, what a re-run drops) for one dead letter's body; ("", why) if none."""
    try:
        msg = json.loads(body)
    except ValueError:
        return "", "body is not JSON"
    if not isinstance(msg, dict) or not isinstance(msg.get("case_id"), str):
        return "", "no case_id"
    if not msg["case_id"]:
        return "", "no case_id (an unresolved webhook message)"
    trigger, ctx = msg.get("trigger", ""), msg.get("payload") or {}
    decision = isinstance(ctx, dict) and (
        ctx.get("decision") or ctx.get("ticket_decision")
    )
    dropped = f"trigger {trigger!r}" + (f", decision {decision!r}" if decision else "")
    return msg["case_id"], dropped


def rerun(args: list[str]) -> int:
    """Re-run a case, or the agent queue's dead letters, through `OperatorRerun`.

    Not a SendMessage: the agent queue admits its producers' roles alone. The producer
    takes a case id and nothing else, so a re-run claims no human decision and the
    invoker grants it no approval. A dead letter becomes the same plain re-run of its
    case: its trigger and payload (a person's reply, a webhook's content) are dropped,
    and printed.
    """
    parsed = rerun_args(args)
    if parsed is None:
        print(
            "usage: ops.py rerun <case_id> [--force] | rerun --dlq [--max N] [--force]",
            file=sys.stderr,
        )
        return 2
    case_id, limit, force = parsed
    if force and not _confirmed(
        args, "Re-run cases parked on a person, without their answer?"
    ):
        return 1
    import boto3
    from botocore.config import Config

    project, env, region = _deployment()
    own = f"/{project}/{env}/use-cases/{NAME}"
    ssm = boto3.client("ssm", region_name=region)
    try:
        fn = ssm.get_parameter(Name=f"{own}/operator-rerun-function")["Parameter"][
            "Value"
        ]
        dlq = ssm.get_parameter(Name=f"{own}/agent-dlq-url")["Parameter"]["Value"]
    except Exception as e:  # noqa: BLE001 - no pipeline stack, no creds: say so
        print(f"FAIL: the pipeline stack is not deployed: {e}", file=sys.stderr)
        return 1
    # No retry: a retried invoke after a timeout would queue the case twice.
    lam = boto3.client(
        "lambda", region_name=region, config=Config(retries={"total_max_attempts": 1})
    )

    def queue(cid: str) -> dict:
        out = lam.invoke(
            FunctionName=fn, Payload=json.dumps({"case_id": cid, "force": force})
        )
        result = json.loads(out["Payload"].read() or b"{}")
        if "FunctionError" in out:
            return {"queued": False, "error": f"the producer raised: {result}"}
        return result

    def report(cid: str, result: dict) -> bool:
        if result.get("queued"):
            print(
                f"queued {result['case_id']} (was {result['status'] or 'no status'}) "
                f"trigger={result['trigger']} message={result['message_id']}"
            )
            return True
        print(f"FAIL: {cid}: {result.get('error')}", file=sys.stderr)
        return False

    if case_id:
        return 0 if report(case_id, queue(case_id)) else 1

    sqs = boto3.client("sqs", region_name=region)
    seen = failed = 0
    while seen < limit:
        batch = sqs.receive_message(
            QueueUrl=dlq,
            MaxNumberOfMessages=min(10, limit - seen),
            # Long enough to finish this run; a refused letter then reappears.
            VisibilityTimeout=120,
        ).get("Messages", [])
        if not batch:
            break
        for m in batch:
            seen += 1
            cid, dropped = dead_letter_case(m["Body"])
            if not cid:
                print(f"FAIL: dead letter {m['MessageId']}: {dropped}; left in the DLQ")
                failed += 1
                continue
            if report(cid, queue(cid)):
                sqs.delete_message(QueueUrl=dlq, ReceiptHandle=m["ReceiptHandle"])
                print(f"  dropped from the dead letter: {dropped}")
            else:
                failed += 1
    print(f"{seen} dead letter(s) read, {seen - failed} re-queued")
    return 1 if failed else 0


def production_check(args: list[str]) -> int:
    """Read-only: platform.yaml (PLATFORM_CONFIG, as deploy.sh resolves it) and,
    unless --offline, the EventBridge rules in this Region."""
    import yaml
    from business_operations import production_check as pc

    if [a for a in args if a != "--offline"]:
        print("usage: ops.py production-check [--offline]", file=sys.stderr)
        return 2
    path = Path(os.environ.get("PLATFORM_CONFIG") or UC.parents[1] / "platform.yaml")
    if not path.is_file():
        print(f"FAIL: no platform config at {path}", file=sys.stderr)
        return 1
    rows = pc.config_rows(yaml.safe_load(path.read_text()) or {})
    if "--offline" not in args:
        project, env, region = _deployment()
        rows.append(pc.audit_alarm_row(region, pc.trigger_mode_parameter(project, env)))
    print(f"production-check: {path}")
    return pc.report(rows)


def operator_policy(args: list[str]) -> int:
    """Print the least-privilege IAM policy for one group of verbs, for this deployment."""
    import boto3
    from business_operations import operator_policy as op

    if len(args) != 1 or args[0] not in op.GROUPS:
        print(
            f"usage: ops.py operator-policy {{{','.join(op.GROUPS)}}}", file=sys.stderr
        )
        return 2
    project, env, region = _deployment()
    try:
        session = boto3.session.Session(region_name=region)
        account = session.client("sts").get_caller_identity()["Account"]
        ssm = session.client("ssm")
        values = {}
        for key in op.parameters(args[0]):
            try:
                values[key] = ssm.get_parameter(Name=f"/{project}/{env}/{key}")[
                    "Parameter"
                ]["Value"]
            except ssm.exceptions.ParameterNotFound:
                print(f"note: /{project}/{env}/{key} is not deployed", file=sys.stderr)
    except Exception as e:  # noqa: BLE001 - no creds: say so
        print(f"FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    try:
        policy = op.render(
            args[0],
            region=session.region_name,
            account=account,
            project=project,
            env=env,
            values=values,
        )
    except op.PolicyRefused as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    print(json.dumps(policy, indent=2))
    return 0


def _evals(module: str, args: list[str]) -> int:
    import importlib

    sys.path.insert(0, str(UC / "evals"))
    return importlib.import_module(module).main(args)


VERBS = {
    "status": lambda _args: status(),
    "knowledge-load": lambda args: knowledge_load(*args[:1]),
    "add-user": add_user,
    "seed": seed,
    "poll": poll,
    "trigger-mode": trigger_mode,
    "break-glass": break_glass,
    "emergency-stop": emergency_stop,
    "kb-sync": kb_sync,
    "mcp-config": mcp_config,
    "remote-client-config": remote_client_config,
    "cost-report": cost_report,
    "types": types,
    "rerun": rerun,
    "production-check": production_check,
    "operator-policy": operator_policy,
    "bundle": bundle_ops.bundle_cmd,
    "pack": bundle_ops.pack_cmd,
    "add-group": bundle_ops.add_group,
    "evals-setup": lambda args: (
        _evals("setup_evaluations", [a for a in args if a != "--yes"])
        if _confirmed(args, "Create an evaluator on this deployment's account?")
        else 1
    ),
    "evals-run": lambda args: (
        _evals("run_regression", [a for a in args if a != "--yes"])
        if _confirmed(
            args,
            "Seed SAP documents and run the eval corpus (Amazon Bedrock-billed) on this deployment?",
        )
        else 1
    ),
}


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in VERBS:
        print(f"usage: ops.py {{{','.join(VERBS)}}}", file=sys.stderr)
        return 2
    return VERBS[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
