# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Packs: the publish path, the authoring routes, and who may write the corpus.

**Packs move as bundles; in development the deploy imports the checkout.** `packs/`
ships as an asset; a custom resource keyed on its hash invokes `pack_composer` with a pack import
request. In a `promotion.role: development` stack it imports each checkout pack that has
no version yet or that changed since the last deploy import took it, and fails the deploy for a
pack that console edits changed too; every stack adopts its live packs as version 0 and
republishes the platform prompt. Outside development everything is `ops.py bundle
import` (`pack_composer`'s verbs), which writes SOPs first and the registry last, so a
published mapping always has its SOP.

**One writer, enforced by the bucket.** An explicit deny refuses `PutObject` and
`DeleteObject` on the registry bucket and on the SOP bucket to every principal but the
composer; the pack and SOP routes invoke it. The SOP bucket also refuses any key that is not `.txt` or `.md`, or
the `.txt.metadata.json`/`.md.metadata.json` sidecar naming a SOP's pack, to everyone. Outside production the auto-delete provider is exempt so teardown works.
An IAM grant cannot override an explicit deny, so a function holding `s3:PutObject` on
the account cannot rewrite the entitlement map or the agent's instructions.

**Routes** on the console API: `POST /packs`, `/packs/status`, `/packs/{pack_id}` (GET,
PATCH), its `base_prompt`, its `evaluations`, its `sops` by process type, and
`/sops/sync`. Each handler admits first
(this API's client and the base group), then the authoring gates.

**Alarm.** An unreadable `registry.json` turns every console route into a 503; the
handlers count it as `RegistryUnavailable`, and an alarm on that count publishes to the
platform's ops topic.

**Groups.** Every group a checkout pack's `entitlement` names is created on the shared
pool; validate_packs.py refuses one without the use case's prefix.
"""

from __future__ import annotations

import json
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_assets as s3_assets
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_ssm as ssm
from aws_cdk import custom_resources as cr
from sap_layer import names as sap_names

from business_operations import (
    ASSET_EXCLUDE,
    agent_knowledge,
    knowledge,
    names,
    notifications,
    sop_tools,
    validate_packs,
)
from business_operations import alarms as uc_alarms
from business_operations import api as uc_api
from business_operations.config import (
    BusinessOperationsConfig,
    promotion_contradiction,
    promotion_role,
)
from business_operations.data import _AUTO_DELETE_PROVIDER, Data
from business_operations.runtime import model_ids
from infra_utils.use_case_context import UseCaseContext
from sap_kit import names as core_names
from sap_kit.functions import uc_function

# isort: split
# validate_packs put the layer on the path.
from pack_tiers import (
    CONTACT_PREFIX,
    DEVELOPMENT,
    METADATA_SUFFIX,
    PACK_ADMINS_GROUP,
    SYMBOL_RE,
    TEXT_EXTENSIONS,
)

UC = Path(__file__).resolve().parents[1]
PACKS = validate_packs.PACKS
NAME = names.NAME


def known_tools(cfg: BusinessOperationsConfig, case_tools: list[dict]) -> set[str]:
    """Every full tool name a pack may name: this use case's targets, whether or not a
    flag turns one off here (the agent is told which are unserved), and the SAP tools
    of each configured system and of each system a pack names. A named system that is
    not configured is a synth warning (`unconfigured_systems`), not an error: the
    shipped packs name `s4`, and a deployment may call its system something else."""
    defined = {
        names.CASE_TARGET: case_tools,
        names.NOTIFICATION_TARGET: notifications.NOTIFICATION_TOOLS,
        names.TICKETS_TARGET: notifications.TICKET_TOOLS,
        names.KNOWLEDGE_TARGET: list(knowledge.SEARCH_TOOLS.values()),
        names.SOPS_TARGET: sop_tools.TOOLS,
        names.AGENT_KNOWLEDGE_TARGET: agent_knowledge.TOOLS,
    }
    tools = {
        names.tool_name(target, t["Name"]) for target, ts in defined.items() for t in ts
    }
    systems = {*cfg.sap.systems, *_named_systems()}
    return tools | {
        names.tool_name(sap_names.sap_target(s), t)
        for s in systems
        for t in sap_names.SAP_TOOLS
    }


def unconfigured_systems(cfg: BusinessOperationsConfig) -> list[str]:
    """SAP systems a pack names that this deployment does not configure. With none
    configured the registry keeps every SAP pack inert, so only a mismatch counts."""
    if not cfg.sap.systems:
        return []
    return sorted(set(_named_systems()) - set(cfg.sap.systems))


def _named_systems(root: Path = PACKS) -> list[str]:
    """The SAP systems the checkout's packs name in `gateway_tools`."""
    systems = set()
    for pack_file in root.glob("[!_]*/pack.json"):
        for tool in json.loads(pack_file.read_text()).get("gateway_tools") or []:
            target = tool.split(core_names.TOOL_SEPARATOR)[0]
            if target.startswith(sap_names.TARGET_PREFIX):
                systems.add(target.removeprefix(sap_names.TARGET_PREFIX))
    return sorted(systems)


def unconfigured_contacts(
    cfg: BusinessOperationsConfig, root: Path = PACKS
) -> list[str]:
    """`contacts` keys the checkout's SOPs cite as `{{CONTACT_<KEY>}}` that this
    deployment leaves unset. The agent reads those placeholders verbatim, and
    `PUT /deployment/settings` can only override a key the deployment declares."""
    cited = {
        symbol.removeprefix(CONTACT_PREFIX).lower()
        for sop in root.glob("[!_]*/sops/*")
        if sop.name.endswith(TEXT_EXTENSIONS)
        for symbol in SYMBOL_RE.findall(sop.read_text(encoding="utf-8"))
        if symbol.startswith(CONTACT_PREFIX)
    }
    return sorted(k for k in cited if not (cfg.contacts.get(k) or "").strip())


def unpolled_packs(root: Path = PACKS) -> list[str]:
    """Checkout packs with `source.poll: off`: push only, so nothing notices a
    sender that stops."""
    return sorted(
        pack_file.parent.name
        for pack_file in root.glob("[!_]*/pack.json")
        if (json.loads(pack_file.read_text()).get("source") or {}).get("poll") == "off"
    )


def checkout_groups(root: Path = PACKS) -> list[str]:
    groups: set[str] = set()
    for pack_file in root.glob("[!_]*/pack.json"):
        for named in (
            json.loads(pack_file.read_text()).get("entitlement") or {}
        ).values():
            groups.update(named)
    return sorted(groups)


def create(
    stack: cdk.Stack,
    ctx: UseCaseContext,
    cfg: BusinessOperationsConfig,
    data: Data,
    api: uc_api.Api,
    *,
    case_tools: list[dict],
    pool_id: str | None,
) -> None:
    """`pool_id` None (direct mode): the groups are app roles the operator creates."""
    validate_packs.require_valid(
        known_tools(cfg, case_tools), served_tiers=set(model_ids(ctx, cfg))
    )
    if missing := unconfigured_systems(cfg):
        cdk.Annotations.of(stack).add_warning_v2(
            f"{NAME}:pack-sap-system",
            f"A pack names SAP tools on system(s) {', '.join(missing)}, which "
            f"use_cases.{NAME}.sap.systems does not configure: the Gateway never lists "
            "those tools, so the pack's SAP reads and writes fail. Rename the system, "
            "or edit the pack's gateway_tools.",
        )
    if missing := unconfigured_contacts(cfg):
        cdk.Annotations.of(stack).add_warning_v2(
            f"{NAME}:sop-contacts",
            f"The SOPs cite contact(s) {', '.join(missing)}, which "
            f"use_cases.{NAME}.contacts does not set: the agent reads those "
            "placeholders verbatim and cannot notify them. Set them there; PUT /deployment/settings "
            "can then override them.",
        )
    if push_only := unpolled_packs():
        cdk.Annotations.of(stack).add_warning_v2(
            f"{NAME}:pack-poll-off",
            f"Pack(s) {', '.join(push_only)} set source.poll 'off': only SAP's sender "
            "makes their cases, so a sender that stops loses exceptions with no alarm. "
            "Poll 'sweep' catches what a sender lost.",
        )
    role = promotion_role(ctx, cfg.promotion)
    if why := promotion_contradiction(ctx, cfg.promotion):
        cdk.Annotations.of(stack).add_warning_v2(f"{NAME}:promotion-role", why)
    ssm_prefix = ctx.ssm_prefix
    retain = ctx.production
    own = f"{ssm_prefix}/use-cases/{NAME}"

    # A created pack's SAP reads name a system; with several there is no right one.
    systems = list(cfg.sap.systems)
    created_tools = names.created_pack_tools(systems[0] if len(systems) == 1 else None)

    def function(id: str, source: Path, env: dict[str, str], timeout: int = 15):
        env = {"PROMOTION_ROLE": role, **env}
        f = uc_function(
            stack,
            id,
            ctx,
            source,
            layers=[api.layer],
            timeout=timeout,
            memory_size=512,
            environment={**api.env, **env},
        )
        data.grant(f, data.registry)
        return f

    # ── The composer: the only writer of the registry ──
    composer = function(
        "PackComposer",
        UC / "pack_composer",
        {
            "SOPS_BUCKET": data.sops.bucket_name,
            "CREATED_PACK_TOOLS": json.dumps(list(created_tools)),
            # What an import checks a bundle's `requires` against (check 6).
            # ponytail: in the 4 KB Lambda environment (about 1 KB with one SAP
            # system); move it to an SSM parameter if many systems push it past that.
            "PACK_ENVIRONMENT": json.dumps(
                {
                    "tools": sorted(known_tools(cfg, case_tools)),
                    "model_tiers": sorted(model_ids(ctx, cfg)),
                    "contacts": sorted(k for k, v in cfg.contacts.items() if v.strip()),
                },
                separators=(",", ":"),
            ),
            "USER_POOL_ID": pool_id or "",
            "STACK_NAME": stack.stack_name,
            # `status` shows each value a SOP cites; the deploy import refuses rule overrides.
            "CONFIG_TABLE": data.config.table_name,
            "CONTACTS_JSON": json.dumps(cfg.contacts),
            **_kb_env(own),
        },
        timeout=120,
    )
    # One publish at a time: overlapping ones would each mirror the old corpus, and the
    # later registry write would drop the earlier pack. The route answers 409.
    composer.node.default_child.reserved_concurrent_executions = 1
    data.grant(composer, data.registry, write=True)
    data.sops.grant_put(composer)
    data.sops.grant_delete(composer)  # a retired pack, or a SOP a version drops
    # To list a pack's SOPs, for its live state and for drift.
    composer.add_to_role_policy(
        iam.PolicyStatement(actions=["s3:ListBucket"], resources=[data.sops.bucket_arn])
    )
    data.sops.grant_read(composer)
    data.grant(composer, data.config)  # read only: it never writes an override
    _grant_reindex(stack, composer, own)
    if pool_id:
        # Import check 6: every group a bundle names exists. It never creates one.
        composer.add_to_role_policy(
            iam.PolicyStatement(
                actions=["cognito-idp:GetGroup"],
                resources=[
                    stack.format_arn(
                        service="cognito-idp",
                        resource="userpool",
                        resource_name=pool_id,
                    )
                ],
            )
        )

    asset = s3_assets.Asset(
        stack, "PacksAsset", path=str(PACKS), exclude=["_seed", *ASSET_EXCLUDE]
    )
    asset.grant_read(composer)
    # The pack import at deploy publishes the platform prompt and adopts live packs in every role,
    # so the composer runs one only with this proof, which the import invoker alone may
    # read: the operator policies grant the composer's invoke, never this secret.
    # The logical ids keep their names from before D86 (PackSeed*), so a redeploy
    # replaces none of these resources.
    import_proof = secretsmanager.Secret(
        stack,
        "PackSeedProof",
        description=f"{ctx.prefix} {NAME}: proof that a pack import comes from the deploy",
        generate_secret_string=secretsmanager.SecretStringGenerator(
            exclude_punctuation=True, password_length=48
        ),
    )
    import_proof.grant_read(composer)
    composer.add_environment("DEPLOY_IMPORT_PROOF_ARN", import_proof.secret_arn)
    # The import reaches the composer through a retry: an author's save that overlaps a
    # deploy would otherwise throttle it and fail the deploy.
    import_invoker = uc_function(
        stack,
        "PackSeedInvoker",
        ctx,
        UC / "pack_composer",
        handler="deploy_import_invoker.handler",
        layers=[api.layer],
        timeout=300,
        environment={
            "PACK_COMPOSER_FUNCTION": composer.function_name,
            "DEPLOY_IMPORT_PROOF_ARN": import_proof.secret_arn,
        },
    )
    composer.grant_invoke(import_invoker)
    import_proof.grant_read(import_invoker)
    provider = cr.Provider(stack, "PackSeedProvider", on_event_handler=import_invoker)
    deploy_import = cdk.CustomResource(
        stack,
        "PackSeed",
        service_token=provider.service_token,
        properties={
            "AssetBucket": asset.s3_bucket_name,
            "AssetKey": asset.s3_object_key,
            # Import again when the composer, the API layer it builds the registry with,
            # or the role changes too, so it adopts the live packs and republishes
            # under the new rules.
            "Composer": knowledge.tree_hash(
                UC / "pack_composer", UC / "api" / "layer" / "python", pattern="*.py"
            )[:16],
            "Role": role,
            # Outside development the import fails while a pack holds an edit made in
            # place. A rollback restores properties without this, so it never fails.
            "InPlaceEdits": "refuse",
            # In development a changed checkout pack is imported. Gated the same way, so
            # a rollback to a release before this neither imports nor refuses one.
            "CheckoutChanges": "import",
            # Every role: the import fails while the Config table overrides a rule
            # constant. Absent from a rollback's properties, so a rollback never fails.
            "RuleOverrides": "refuse",
        },
    )
    # `ops.py bundle` invokes the composer over IAM.
    ssm.StringParameter(
        stack,
        "Out-pack-composer-function",
        parameter_name=f"{own}/pack-composer-function",
        string_value=composer.function_name,
    )
    # `ops.py operator-policy evals` renders only for a development stack.
    ssm.StringParameter(
        stack,
        "Out-promotion-role",
        parameter_name=f"{own}/promotion-role",
        string_value=role,
    )

    # ── The routes ──
    packs_fn = function(
        "PacksFunction",
        UC / "api" / "packs",
        {"PACK_COMPOSER_FUNCTION": composer.function_name},
    )
    composer.grant_invoke(packs_fn)
    sops_fn = function(
        "SopsFunction",
        UC / "api" / "sops",
        {
            "SOPS_BUCKET": data.sops.bucket_name,
            "PACK_COMPOSER_FUNCTION": composer.function_name,
            **_kb_env(own),
        },
        timeout=30,
    )
    # Reads only: its writes are the composer's.
    data.grant(sops_fn, data.sops)
    composer.grant_invoke(sops_fn)
    # `POST /sops/sync`, and the search state the listing shows.
    _grant_reindex(stack, sops_fn, own)

    authed = {
        "authorizer": api.authorizer,
        "authorization_type": api.authorization_type,
    }
    root = api.rest_api.root
    sops = apigw.LambdaIntegration(sops_fn)
    # Reindexing is every pack's, so it stays at the root.
    root.add_resource("sops").add_resource("sync").add_method("POST", sops, **authed)
    packs_i = apigw.LambdaIntegration(packs_fn)
    pack_r = uc_api.pack_resource(api.rest_api)
    packs_r = pack_r.parent_resource
    packs_r.add_method("POST", packs_i, **authed)
    packs_r.add_resource("status").add_method("GET", packs_i, **authed)
    for method in ("GET", "PATCH"):
        pack_r.add_method(method, packs_i, **authed)
    pack_r.add_resource("base_prompt").add_method("GET", packs_i, **authed)
    # The pack's stored versions, read-only, for the pack page's Versions tab.
    versions_r = pack_r.add_resource("versions")
    versions_r.add_method("GET", packs_i, **authed)
    versions_r.add_resource("{version}").add_method("GET", packs_i, **authed)
    # A pack's SOPs, by the process types that map them.
    sops_r = pack_r.add_resource("sops")
    for method in ("GET", "POST"):
        sops_r.add_method(method, sops, **authed)
    sop_r = sops_r.add_resource("{process_type}")
    for method in ("GET", "PUT", "DELETE"):
        sop_r.add_method(method, sops, **authed)
    sop_r.add_resource("check").add_method("POST", sops, **authed)
    # The Evaluations part: example cases (through the composer) and the last run.
    evaluations_r = pack_r.add_resource("evaluations")
    evaluations_r.add_method("GET", packs_i, **authed)
    example_r = evaluations_r.add_resource("{test_id}")
    for method in ("PUT", "DELETE"):
        example_r.add_method(method, packs_i, **authed)
    api.functions.update(
        {"packs": packs_fn, "sops": sops_fn, "pack_composer": composer}
    )
    # GET /registry reports each pack's `access` under this stack's promotion role, as
    # the pack and SOP routes decide under it (api/layer/python/pack_access.py).
    api.functions["config"].add_environment("PROMOTION_ROLE", role)

    # ── Who may write each bucket ──
    exempt = [] if retain else [_auto_delete_role(stack)]
    _only_writers(data.registry, [composer.role.role_arn, *exempt])
    _only_writers(data.sops, [composer.role.role_arn, *exempt])
    data.sops.add_to_resource_policy(
        iam.PolicyStatement(
            sid="DenyNonTextSops",
            effect=iam.Effect.DENY,
            principals=[iam.AnyPrincipal()],
            actions=["s3:PutObject"],
            # A SOP is text; its knowledge-base sidecar is the one other key, and
            # only the writers above can put either.
            not_resources=[
                data.sops.arn_for_objects(f"*{ext}{suffix}")
                for suffix in ("", METADATA_SUFFIX)
                for ext in TEXT_EXTENSIONS
            ],
        )
    )

    _registry_alarm(stack, ctx, api)

    for group in checkout_groups() if pool_id else ():
        if group in (
            uc_api.USERS_GROUP,
            uc_api.ADMINS_GROUP,
            uc_api.AUTHORS_GROUP,
            uc_api.LIMIT_OWNERS_GROUP,
            uc_api.CASE_WORKERS_GROUP,
            names.REMOTE_AUTHORS_GROUP,
        ):
            continue
        # Before the deploy import, which checks every group a pack names exists.
        deploy_import.node.add_dependency(
            cognito.CfnUserPoolGroup(
                stack,
                f"PackGroup-{group}",
                user_pool_id=pool_id,
                group_name=group,
                description=f"{NAME}: named by a pack's entitlement",
            )
        )
    if pool_id and role == DEVELOPMENT:
        # Structure over HTTP, in development only; refused everywhere else.
        cognito.CfnUserPoolGroup(
            stack,
            "PackAdminsGroup",
            user_pool_id=pool_id,
            group_name=PACK_ADMINS_GROUP,
            description=f"{NAME}: changes a pack's structure in development",
        )


def _kb_env(own: str) -> dict[str, str]:
    """Where the knowledge stack publishes the SOP knowledge base, read per call."""
    return {
        "SOPS_KB_ID_PARAM": f"{own}/sops-kb-id",
        "SOPS_DATA_SOURCE_ID_PARAM": f"{own}/sops-data-source-id",
        "SOPS_REINDEX_FLAG_PARAM": f"{own}/{knowledge.REINDEX_FLAG_PARAM}",
    }


def _grant_reindex(stack: cdk.Stack, fn: _lambda.Function, own: str) -> None:
    """Start an ingestion job on the SOP knowledge base after a SOP write, or set the
    flag the knowledge stack's follow-up reads when one is already running."""
    fn.add_to_role_policy(
        iam.PolicyStatement(
            actions=["ssm:PutParameter"],
            resources=[
                stack.format_arn(
                    service="ssm",
                    resource="parameter",
                    resource_name=f"{own.lstrip('/')}/{knowledge.REINDEX_FLAG_PARAM}",
                )
            ],
        )
    )
    fn.add_to_role_policy(
        iam.PolicyStatement(
            actions=["ssm:GetParameter"],
            resources=[
                stack.format_arn(
                    service="ssm",
                    resource="parameter",
                    resource_name=f"{own.lstrip('/')}/sops-{p}",
                )
                for p in ("kb-id", "data-source-id")
            ],
        )
    )
    # The knowledge base is created later, by the knowledge stack, so its id is not
    # known here. Re-indexing is the whole power, in this account and Region only.
    fn.add_to_role_policy(
        iam.PolicyStatement(
            actions=["bedrock:StartIngestionJob", "bedrock:ListIngestionJobs"],
            resources=[
                stack.format_arn(
                    service="bedrock", resource="knowledge-base", resource_name="*"
                )
            ],
        )
    )


def _registry_alarm(stack: cdk.Stack, ctx: UseCaseContext, api: uc_api.Api) -> None:
    alarm = cloudwatch.Alarm(
        stack,
        "RegistryUnavailableAlarm",
        alarm_description=(
            f"{ctx.prefix} {NAME}: registry.json could not be read, so every console "
            "route answers 503. Check the registry bucket and the pack composer's log."
        ),
        # The dimension the handlers emit (api_http.route).
        metric=cloudwatch.Metric(
            namespace=api.env["METRICS_NAMESPACE"],
            metric_name="RegistryUnavailable",
            dimensions_map={"Stack": stack.stack_name},
            statistic="Sum",
            period=cdk.Duration.minutes(5),
        ),
        threshold=0,
        comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        evaluation_periods=1,
        treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
    )
    topic = uc_alarms.topic(stack, ctx)
    alarm.add_alarm_action(cw_actions.SnsAction(topic))
    alarm.add_ok_action(cw_actions.SnsAction(topic))


def _auto_delete_role(stack: cdk.Stack) -> str:
    provider = stack.node.try_find_child(_AUTO_DELETE_PROVIDER)
    if provider is None:
        raise RuntimeError(
            f"{_AUTO_DELETE_PROVIDER} not found: teardown would be denied"
        )
    return provider.role_arn


def _only_writers(bucket: s3.Bucket, writers: list[str]) -> None:
    bucket.add_to_resource_policy(
        iam.PolicyStatement(
            sid="DenyWritesButTheWriters",
            effect=iam.Effect.DENY,
            principals=[iam.AnyPrincipal()],
            actions=["s3:PutObject", "s3:DeleteObject"],
            resources=[bucket.arn_for_objects("*")],
            conditions={"ArnNotEquals": {"aws:PrincipalArn": writers}},
        )
    )
