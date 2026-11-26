#!/usr/bin/env python3

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Operator commands for the SAP layer: `deploy.sh layer run sap <verb>`.

    secrets <system> [unattended]
                       store the credential layers.sap.systems.<system> names (or
                       its unattended block names), read from the terminal, never
                       from argv
    rewrite [stack ...]
                       have CloudFormation write the layer's check settings and every
                       claim again (after a write outside it): bumps each owning
                       stack's SapRewriteNonce and updates it on its previous template.
                       Name a stack whose claim was deleted, since nothing tags it then

`deploy.sh layer run` exports PROJECT_NAME, ENVIRONMENT, AWS_REGION and the
`layers.sap` block of platform.yaml as JSON in LAYER_CONFIG. Exit 0 ok, 1 failure,
2 usage.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
sys.path.insert(0, str(HERE.parents[1]))

from sap_layer import config as sap_config


def store(
    name: str, id_key: str, secret_key: str, region: str | None, owner: str
) -> int:
    """Create secret `name`, or put a new version merged over the old one. An exit code.

    The value is read with getpass from a terminal only: never argv, never a pipe, so
    it is not in `ps`, shell history or a CI log.
    """
    from getpass import getpass

    import boto3

    if not sys.stdin.isatty():
        print("FAIL: secrets reads the credential from a terminal", file=sys.stderr)
        return 1
    sm = boto3.client("secretsmanager", region_name=region)
    try:
        current = json.loads(sm.get_secret_value(SecretId=name)["SecretString"])
    except sm.exceptions.ResourceNotFoundException:
        current = None
    except ValueError:
        current = "not JSON"
    if current is not None and not isinstance(current, dict):
        print(f"FAIL: {name} is not a JSON object; fix it by hand", file=sys.stderr)
        return 1
    old_id = str((current or {}).get(id_key) or "")
    if current is None:
        print(f"{name}: does not exist yet; it will be created")
    else:
        stored = "set" if current.get(secret_key) else "unset"
        print(f"{name}: {id_key}={old_id or '(unset)'}, {secret_key} {stored}")
    new_id = input(f"{id_key}{f' [{old_id}]' if old_id else ''}: ").strip() or old_id
    value = getpass(f"{secret_key}: ")
    if not new_id or not value:
        print(f"FAIL: both {id_key} and {secret_key} are needed", file=sys.stderr)
        return 1
    body = json.dumps({**(current or {}), id_key: new_id, secret_key: value})
    if current is None:
        sm.create_secret(
            Name=name, SecretString=body, Description=f"{owner}: written by ops.py"
        )
    else:
        sm.put_secret_value(SecretId=name, SecretString=body)
    print(f"OK: {name}: {id_key}={new_id}, {secret_key} stored")
    return 0


def secret_spec(
    cfg: sap_config.SapConfig, args: list[str]
) -> tuple[str, tuple[str, str]]:
    """(secret name, (id key, secret key)) for `secrets <system> [unattended]`.

    Raises ValueError saying why when the arguments or the config name no secret.
    """
    if len(args) not in (1, 2) or args[1:] not in ([], ["unattended"]):
        raise ValueError("usage: ops.py secrets <system> [unattended]")
    system = cfg.systems.get(args[0])
    if system is None:
        raise ValueError(
            f"no SAP system {args[0]!r} in layers.sap.systems "
            f"(configured: {', '.join(cfg.systems) or 'none'})"
        )
    if args[1:]:
        if system.unattended is None:
            raise ValueError(f"layers.sap.systems.{args[0]} has no unattended block")
        system = system.unattended
    elif system.runtime_arn:
        raise ValueError(
            f"layers.sap.systems.{args[0]} attaches a runtime someone else deployed; "
            "its credentials are that owner's"
        )
    keys = (
        ("username", "password")
        if system.flow == "basic"
        else ("clientId", "clientSecret")
    )
    return system.secret_name, keys


def secrets(args: list[str]) -> int:
    try:
        cfg = sap_config.load(json.loads(os.environ.get("LAYER_CONFIG") or "{}"))
        name, (id_key, secret_key) = secret_spec(cfg, args)
    except ValueError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 2
    code = store(name, id_key, secret_key, os.environ.get("AWS_REGION"), "sap layer")
    if code == 0:
        print(
            "A direct caller (a poller) re-reads it when SAP refuses the old one, and "
            "within 15 minutes regardless; the AWS for SAP MCP Server reads a Basic secret on "
            "every request, so it needs no restart. An m2m clientId is resolved into "
            "the token-vault provider at deploy: redeploy the layer-sap stack after "
            "changing it."
        )
    return code


def rewrite(args: list[str]) -> int:
    import time

    import boto3

    region = os.environ.get("AWS_REGION")
    root = f"/{os.environ.get('PROJECT_NAME', 'agentcore-workshop')}/{os.environ.get('ENVIRONMENT', 'dev')}/layers/sap"
    ssm = boto3.client("ssm", region_name=region)
    cfn = boto3.client("cloudformation", region_name=region)
    parameters = [f"{root}/check"] + [
        p["Name"]
        for page in ssm.get_paginator("get_parameters_by_path").paginate(
            Path=f"{root}/claims"
        )
        for p in page["Parameters"]
    ]
    stacks = set(args)
    for name in parameters:
        try:
            tags = ssm.list_tags_for_resource(ResourceType="Parameter", ResourceId=name)
        except ssm.exceptions.InvalidResourceId:
            print(f"{name}: absent; name its stack to restore it", file=sys.stderr)
            continue
        stacks |= {
            t["Value"]
            for t in tags["TagList"]
            if t["Key"] == "aws:cloudformation:stack-name"
        }
    nonce = str(int(time.time()))
    for stack in sorted(stacks):
        (described,) = cfn.describe_stacks(StackName=stack)["Stacks"]
        keys = [p["ParameterKey"] for p in described.get("Parameters", [])]
        if NONCE not in keys:
            print(
                f"FAIL: {stack} has no {NONCE}; deploy it once first", file=sys.stderr
            )
            return 1
        print(f"{stack}: {NONCE}={nonce}")
        cfn.update_stack(
            StackName=stack,
            UsePreviousTemplate=True,
            # SSM-typed parameters are resolved again; every other keeps its value.
            Parameters=[
                {"ParameterKey": k, "ParameterValue": nonce}
                if k == NONCE
                else {"ParameterKey": k, "UsePreviousValue": True}
                for k in keys
            ],
            Capabilities=[
                "CAPABILITY_IAM",
                "CAPABILITY_NAMED_IAM",
                "CAPABILITY_AUTO_EXPAND",
            ],
            # The execution role, so the check trusts what CloudFormation writes.
            **({"RoleARN": described["RoleARN"]} if described.get("RoleARN") else {}),
        )
        cfn.get_waiter("stack_update_complete").wait(StackName=stack)
    print(f"OK: CloudFormation wrote {', '.join(sorted(stacks))} again")
    return 0


NONCE = "SapRewriteNonce"
VERBS = {"secrets": secrets, "rewrite": rewrite}


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in VERBS:
        print(f"usage: ops.py {{{','.join(VERBS)}}}", file=sys.stderr)
        return 2
    return VERBS[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
