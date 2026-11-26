# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Test data: the seeder, its record of what it posted, and the console's routes to it.

Built only when `test_data.enabled`, which `deployment.mode: production` refuses.

**Who may seed.** `ops.py seed <pack>` invokes the seeder over IAM, so whoever holds
`lambda:InvokeFunction` on it may seed; CloudTrail names them only on a trail that logs
Lambda data events, which the platform's does not. The console's routes
(`/packs/{pack_id}/test-data`, `api/test_data/index.py`) are the other caller: an
operator who may see the pack, for a pack whose system is a sandbox. The route invokes
the seeder asynchronously, because a chain outlasts API Gateway's 29 s, and the seeder's
asynchronous invocations are never retried, because a retried chain posts a second set
of documents.

**What it posted.** The seeder is the only writer of the test data table: one record
per seed, keyed by pack and post, with a `case_id` index. The case producers (the
poller, SAP's events) read it to mark the case they open as test data, and the seeder
copies the same mark onto a case already open when it settles; that `UpdateItem` is its
only grant on the cases table.

**How it writes to SAP.** Straight to the pack's SAP system over OData, with that
system's service account (the secret the poller and the system's MCP server use, sent
as Basic or exchanged for a token by the system's `flow`),
not through the Gateway. The run checks and their amount gate stand between the *agent* and
SAP: they tie a write to a run the invoker started and hold a model-chosen amount to
what a human approved. Here there is no model and no run. An operator runs the verb,
and the seeder posts the document chain the pack declares (a PO, a goods receipt,
a blocked invoice) at the amounts that operator gave, the same act as
posting them in SAP by hand. Off by default, because those are real accounting documents.

What it posts is the pack's `seed.steps`, read from the published registry when it is
invoked, so a pack imported later seeds with no deploy. Its SAP system is the poller's
rule at run time: the pack's `sap_system`, or the only system. A checkout pack that
declares a `seed` block and resolves to no system fails the synth rather than shipping
a verb that fails.

**Only a sandbox.** The seeder holds IAM on the secret of a system whose
`layers.sap.systems.<s>.system_role` is `sandbox`, and on no other. A checkout pack that
seeds and resolves to any other system role fails the synth, and so does a seeder with no
sandbox to seed. Every direct system stays in its `SAP_SYSTEMS`, so it resolves a pack's
system by the poller's rule, and it refuses a pack imported later that resolves to one
that is not a sandbox (`test_data/index.py`).
"""

from __future__ import annotations

import json
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_ssm as ssm

from business_operations import names
from business_operations.api import API_SRC, Api, pack_resource
from business_operations.config import BusinessOperationsConfig
from business_operations.data import Data
from business_operations.pipeline import _grant_sap, _sap_systems, _vpc
from infra_utils.use_case_context import UseCaseContext
from sap_kit.functions import uc_function

UC = Path(__file__).resolve().parents[1]
SRC = UC / "test_data"
PACKS = UC / "packs"
NAME = names.NAME


def declared_seeds() -> dict[str, dict]:
    """pack_id -> its pack.json, for every pack that declares a `seed` block."""
    out = {}
    for path in sorted(PACKS.glob("*/pack.json")):
        pack = json.loads(path.read_text())
        if pack.get("seed"):
            out[path.parent.name] = pack
    return out


def check_seed_systems(config: BusinessOperationsConfig) -> dict[str, dict]:
    """The direct systems by name, every one of them, for the seeder's resolution.
    Raises for a checkout pack that seeds and resolves to none of them or to one that
    is not a sandbox, and when none is a sandbox."""
    systems = _sap_systems(config)
    for pack_id, pack in declared_seeds().items():
        name = pack.get("sap_system") or (
            next(iter(systems)) if len(systems) == 1 else None
        )
        if name not in systems:
            raise ValueError(
                f"test_data.enabled: pack {pack_id} seeds SAP, but "
                + (
                    f"its sap_system {name!r} is not in layers.sap.access.{NAME}.bypass_gateway"
                    if name
                    else f"layers.sap.access.{NAME}.bypass_gateway names no single system "
                    "for it; set its sap_system"
                )
            )
        if systems[name]["system_role"] != "sandbox":
            raise ValueError(
                f"test_data.enabled: pack {pack_id} seeds SAP system {name}, whose "
                f"system role is {systems[name]['system_role']}; the seeder seeds only a "
                f"system with layers.sap.systems.{name}.system_role: sandbox"
            )
        if config.sap.systems[name].flow == "obo":
            # On an obo system SAP authorizes each person; the seeder would post as
            # the unattended block's service account instead, outside that posture.
            raise ValueError(
                f"test_data.enabled: pack {pack_id} seeds SAP system {name}, whose "
                "server signs people in on behalf of themselves (flow: obo); the "
                "seeder posts as a service account, so it seeds only a system whose "
                "own flow is basic, m2m-sap or m2m-oidc"
            )
    if not sandboxes(systems):
        raise ValueError(
            "test_data.enabled, but no system in "
            f"layers.sap.access.{NAME}.bypass_gateway has system_role: sandbox, and the seeder "
            "seeds only a sandbox"
        )
    return systems


def sandboxes(systems: dict[str, dict]) -> list[str]:
    """The systems the seeder may seed, and so holds IAM on."""
    return [n for n, s in systems.items() if s["system_role"] == "sandbox"]


#: `api/layer/python/test_data_records.CASE_INDEX`, which the producers query.
CASE_INDEX = "by-case"


def _table(stack: cdk.Stack) -> dynamodb.Table:
    """One record per seed. Unnamed, like every table here; never production."""
    table = dynamodb.Table(
        stack,
        "TestDataTable",
        partition_key=dynamodb.Attribute(
            name="pack_id", type=dynamodb.AttributeType.STRING
        ),
        sort_key=dynamodb.Attribute(name="post_id", type=dynamodb.AttributeType.STRING),
        billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
        point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
            point_in_time_recovery_enabled=True
        ),
        encryption=dynamodb.TableEncryption.AWS_MANAGED,
        removal_policy=cdk.RemovalPolicy.DESTROY,
    )
    table.add_global_secondary_index(
        index_name=CASE_INDEX,
        partition_key=dynamodb.Attribute(
            name="case_id", type=dynamodb.AttributeType.STRING
        ),
        sort_key=dynamodb.Attribute(name="post_id", type=dynamodb.AttributeType.STRING),
    )
    return table


def _grant_query(table: dynamodb.Table, fn: _lambda.Function) -> None:
    """Query on the table and its index, and nothing that writes."""
    table.grant(fn, "dynamodb:Query")
    fn.add_to_role_policy(
        iam.PolicyStatement(
            actions=["dynamodb:Query"],
            resources=[f"{table.table_arn}/index/{CASE_INDEX}"],
        )
    )


def create(
    stack: cdk.Stack,
    ctx: UseCaseContext,
    config: BusinessOperationsConfig,
    data: Data,
    api: Api,
    case_makers: list[_lambda.Function] = (),
) -> _lambda.Function | None:
    if not config.test_data.enabled:
        return None
    if ctx.production:
        # The seeder posts real documents with the system's service account, outside
        # the Gateway, Cedar and the run checks.
        raise ValueError(
            "use_cases.business-operations.test_data.enabled is refused under "
            "deployment.mode: production: the seeder posts real SAP documents"
        )
    systems = check_seed_systems(config)
    records = _table(stack)
    fn = uc_function(
        stack,
        "TestDataFunction",
        ctx,
        # `seed_steps`, the registry and the SAP layer's two clients come from the API
        # layer, which links the clients rather than copying them.
        SRC,
        layers=[api.layer],
        # A chain waits between documents for SAP's own update task; ops.py seed
        # waits a little longer than this.
        timeout=120,
        environment={
            "SAP_SYSTEMS": json.dumps(systems),
            "REGISTRY_BUCKET": data.registry.bucket_name,
            "TEST_DATA_TABLE": records.table_name,
            "CASES_TABLE": data.cases.table_name,
            # The limit overrides the expectation it records is worked out under.
            "CONFIG_TABLE": data.config.table_name,
        },
    )
    _grant_sap(ctx, fn, sandboxes(systems))
    data.grant(fn, data.registry, data.config)
    # The table's only writer, and PutItem is all it calls.
    records.grant(fn, "dynamodb:PutItem")
    # The mark on a case already open; its one call on the cases table.
    data.grant_cases(fn, "UpdateItem")
    _vpc(stack, ctx, fn)
    # A retried chain is a second set of real documents; the route says "posting" and
    # the record says whether it landed.
    fn.configure_async_invoke(retry_attempts=0, max_event_age=cdk.Duration.minutes(15))

    for maker in case_makers:
        maker.add_environment("TEST_DATA_TABLE", records.table_name)
        _grant_query(records, maker)

    _routes(stack, ctx, data, api, records, fn, systems)

    # Where ops.py seed and verify.py find it, rather than a pinned name.
    ssm.StringParameter(
        stack,
        "Out-test-data-function",
        parameter_name=f"{ctx.ssm_prefix}/use-cases/{NAME}/test-data-function",
        string_value=fn.function_name,
    )
    return fn


def _routes(
    stack: cdk.Stack,
    ctx: UseCaseContext,
    data: Data,
    api: Api,
    records: dynamodb.Table,
    seeder: _lambda.Function,
    systems: dict[str, dict],
) -> None:
    """`/packs/{pack_id}/test-data` on the console API: the only function besides
    `ops.py seed` that may invoke the seeder."""
    fn = uc_function(
        stack,
        "TestDataApiFunction",
        ctx,
        API_SRC / "test_data",
        layers=[api.layer],
        timeout=15,
        environment={
            **api.env,
            "SEEDER_FUNCTION_NAME": seeder.function_name,
            "TEST_DATA_TABLE": records.table_name,
            "CASES_TABLE": data.cases.table_name,
            "CONFIG_TABLE": data.config.table_name,
            "SEED_SYSTEMS": json.dumps(
                {name: system["system_role"] for name, system in systems.items()}
            ),
        },
    )
    data.grant(fn, data.registry, data.config)
    records.grant(fn, "dynamodb:Query")
    data.grant_cases(fn, "BatchGetItem")
    seeder.grant_invoke(fn)
    authed = {
        "authorizer": api.authorizer,
        "authorization_type": api.authorization_type,
    }
    integration = apigw.LambdaIntegration(fn)
    pack = pack_resource(api.rest_api)
    test_data = pack.add_resource("test-data")
    for method in ("GET", "POST"):
        test_data.add_method(method, integration, **authed)
    test_data.add_resource("expectation").add_method("POST", integration, **authed)
    api.functions["test_data"] = fn
