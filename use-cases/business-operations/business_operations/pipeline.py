# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The unattended pipeline: how exceptions become cases and cases become runs.

Four producers create or resume
cases, and every run starts at one FIFO queue the agent invoker drains:

- the SAP poller (schedule), per pack as that pack's SAP system's service account,
  for the systems the SAP layer lets this use case call directly;
- the ingest consumer, draining the one ingest queue that SAP's events (the SAP
  layer's `sap-events` bus) and `POST /erp-events` (`api.erp_events_endpoint`) fill;
- the batch sweeper (schedule), re-queueing cases left `detected`;
- the operator re-run, which `ops.py rerun` invokes to queue one existing case.

The first three queue a case only when the deployment's trigger mode
(`.../autonomy/trigger-mode`) and the case's pack (`.../autonomy/packs/<pack_id>`) both say
`auto`; each is absent until `PUT /deployment/trigger-mode` or a pack's writes it, and absent is `manual`. An operator
re-run is queued regardless. Every function runs from one asset, `pipeline/`, so the poller and
the ingest consumer map records through one copy of `polling_engine`. The exemplar builder
rides along: it reads finished cases and writes what the agent reads back.
"""

from __future__ import annotations

import json
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_lambda_event_sources as sources
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_ssm as ssm
from sap_layer import access as sap_access

from business_operations import ASSET_EXCLUDE, names
from business_operations.api import Api
from business_operations.config import BusinessOperationsConfig
from business_operations.data import Data
from business_operations.runtime import model_ids
from infra_utils.use_case_context import UseCaseContext
from sap_kit.functions import uc_function

UC = Path(__file__).resolve().parents[1]
SRC = UC / "pipeline"
NAME = names.NAME
#: What `pipeline/case_store.py` calls: `case_exists` and a conditional `put_case`.
CASE_STORE_VERBS = ("GetItem", "PutItem")


def code() -> _lambda.Code:
    """`pipeline/*.py`. The SAP layer's client and `agent_knowledge/queries.py` come
    through the API layer, which links both."""
    return _lambda.Code.from_asset(str(SRC), exclude=ASSET_EXCLUDE)


def _sap_systems(config: BusinessOperationsConfig) -> dict:
    """The systems this use case's own functions call outside the Gateway, by name:
    `{base_url, secret, client, system_role}`, and a token flow's settings. The SAP
    layer's `access.business-operations.bypass_gateway` names them; each signs in as
    its service account, by Basic or a client-credentials token (its `flow`, or its
    `unattended` block's)."""
    access = config.sap.access.get(NAME)
    return {
        name: {
            "base_url": config.sap.systems[name].base_url,
            "secret": config.sap.systems[name].direct_secret_name,
            "client": config.sap.systems[name].client,
            "system_role": config.sap.systems[name].system_role,
            **config.sap.systems[name].direct_sign_in(),
        }
        for name in (access.bypass_gateway if access else [])
    }


def _grant_sap(ctx: UseCaseContext, fn: _lambda.Function, systems: dict) -> None:
    """Each system's secret and published connection, through the SAP layer."""
    sap = sap_access.SapAccess(ctx, NAME)
    for name in systems:
        sap.grant_direct(fn, name)


def create(
    stack: cdk.Stack,
    ctx: UseCaseContext,
    config: BusinessOperationsConfig,
    data: Data,
    api: Api,
    invoker: _lambda.Function,
) -> tuple[dict[str, _lambda.Function], sqs.Queue, sqs.Queue]:
    """The pipeline's functions by key, the agent queue and its dead-letter queue."""
    cfg = config.pipeline
    ssm_prefix = ctx.ssm_prefix
    own = f"{ssm_prefix}/use-cases/{NAME}"
    asset = code()

    def param_arn(name: str) -> str:
        return stack.format_arn(
            service="ssm", resource="parameter", resource_name=name.lstrip("/")
        )

    modes = names.trigger_mode_parameters(ssm_prefix)
    trigger_mode = modes["switch"]

    # ── The agent queue ──
    dlq = sqs.Queue(
        stack,
        "AgentDlq",
        fifo=True,
        encryption=sqs.QueueEncryption.SQS_MANAGED,
        enforce_ssl=True,
        retention_period=cdk.Duration.days(14),
    )
    queue = sqs.Queue(
        stack,
        "AgentQueue",
        fifo=True,
        encryption=sqs.QueueEncryption.SQS_MANAGED,
        enforce_ssl=True,
        content_based_deduplication=True,
        # Above the invoker's 15-minute timeout, or a live run is redelivered.
        visibility_timeout=cdk.Duration.minutes(16),
        retention_period=cdk.Duration.days(7),
        dead_letter_queue=sqs.DeadLetterQueue(queue=dlq, max_receive_count=3),
    )
    data.encrypt(dlq)
    data.encrypt(queue)
    data.grant_key(invoker, ("sqs",))
    consumer = sources.SqsEventSource(
        queue,
        batch_size=1,
        max_concurrency=cfg.max_concurrency,
        report_batch_item_failures=True,
    )
    invoker.add_event_source(consumer)
    # The emergency stop pauses this consumer, so queued work waits on the queue while
    # it stands, and its clear resumes it. Nothing else on the invoker.
    stop = api.functions["stop"]
    stop.add_environment("AGENT_QUEUE_MAPPING_ID", consumer.event_source_mapping_id)
    stop.add_to_role_policy(
        iam.PolicyStatement(
            actions=["lambda:UpdateEventSourceMapping"],
            resources=[
                stack.format_arn(
                    service="lambda",
                    resource="event-source-mapping",
                    resource_name=consumer.event_source_mapping_id,
                    arn_format=cdk.ArnFormat.COLON_RESOURCE_NAME,
                )
            ],
        )
    )

    # Who may start a run. A queued message is the invoker's input, so the queue's own
    # policy refuses every other sender, whatever IAM grants elsewhere in the account.
    senders: list[str] = []

    # The console's enqueue and reply routes and the ticket decisions queue too.
    for key in ("cases", "tickets"):
        fn = api.functions.get(key)
        if fn:
            fn.add_environment("AGENT_QUEUE_URL", queue.queue_url)
            queue.grant_send_messages(fn)
            data.grant_key(fn, ("sqs",))
            senders.append(fn.role.role_arn)
    api.functions["trigger_mode"].add_environment("AUTONOMOUS_CAPABLE", "true")

    common = {
        "CASES_TABLE": data.cases.table_name,
        "AGENT_QUEUE_URL": queue.queue_url,
        "TRIGGER_MODE_PARAMETER": trigger_mode,
        "PACK_MODE_PREFIX": modes["packs"],
        # No case is queued while an emergency stop stands (`case_store.Gate`).
        "EMERGENCY_STOP_PARAMETER": modes["stop"],
        "REGISTRY_BUCKET": data.registry.bucket_name,
        "SAP_MCP_ENABLED": "true" if config.sap.systems else "false",
        "WARM_DELAY_SECONDS": str(cfg.warm_delay_seconds),
        **(
            {"POLL_CREATED_SINCE": cfg.poll_created_since.isoformat()}
            if cfg.poll_created_since
            else {}
        ),
    }

    def producer(fn: _lambda.Function, *, reads_mode: bool = True) -> None:
        queue.grant_send_messages(fn)
        data.grant_key(fn, ("sqs",))
        senders.append(fn.role.role_arn)
        # Every producer refuses while an emergency stop stands, the re-run included.
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter"], resources=[param_arn(modes["stop"])]
            )
        )
        if reads_mode:
            fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["ssm:GetParameter"],
                    # The switch, and one mode per pack (pipeline/case_store.Gate).
                    resources=[
                        param_arn(trigger_mode),
                        param_arn(f"{modes['packs']}/*"),
                    ],
                )
            )

    def function(id: str, handler: str, timeout: int, **kw) -> _lambda.Function:
        return uc_function(
            stack,
            id,
            ctx,
            asset,
            handler=handler,
            layers=[api.layer],
            timeout=timeout,
            environment=common,
            **kw,
        )

    def schedule(
        id: str, expression: str, fn: _lambda.Function, what: str, event=None
    ) -> None:
        events.Rule(
            stack,
            id,
            schedule=events.Schedule.expression(expression),
            description=f"{ctx.prefix} {NAME}: {what}",
        ).add_target(
            targets.LambdaFunction(
                fn, event=event and events.RuleTargetInput.from_object(event)
            )
        )

    functions: dict[str, _lambda.Function] = {}

    # ── SAP push (POST /erp-events): validates and enqueues on the ingest queue ──
    push = api.functions.get("erp_events")
    if push is not None:
        # The token SAP presents. Generated, never a placeholder: the handler compares
        # against it. No punctuation, because SM59 keeps it in a password field.
        token = secretsmanager.Secret(
            stack,
            "ErpEventsToken",
            description=f"{ctx.prefix} {NAME}: the token SAP presents on POST /erp-events",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template="{}",
                generate_string_key="token",
                password_length=48,
                exclude_punctuation=True,
            ),
        )
        push.add_environment("INGEST_SECRET_ARN", token.secret_arn)
        for k in ("REGISTRY_BUCKET", "SAP_MCP_ENABLED"):
            push.add_environment(k, common[k])
        token.grant_read(push)
        data.grant(push, data.registry)
        functions["erp_events"] = push
        _out(stack, own, "erp-events-token-secret-arn", token.secret_arn)
        cdk.CfnOutput(stack, "ErpEventsTokenSecret", value=token.secret_name)

    # ── SAP poller ──
    systems = _sap_systems(config)
    if systems:
        poller = function(
            "SapPoller",
            "sap_poller.handler",
            300,
            memory_size=512,
        )
        poller.add_environment("SAP_SYSTEMS", json.dumps(systems))
        _grant_sap(ctx, poller, systems)
        data.grant_cases(poller, *CASE_STORE_VERBS)
        data.grant(poller, data.registry)
        producer(poller)
        _vpc(stack, ctx, poller)
        schedule(
            "SapPollerSchedule", cfg.poller_schedule, poller, "polls SAP for exceptions"
        )
        # Always, not only when a checkout pack sweeps: an imported pack can, with no
        # deploy. A run that finds no `sweep` pack returns before reading SAP.
        schedule(
            "SapSweepSchedule",
            cfg.sweep_schedule,
            poller,
            "sweeps SAP for packs that push",
            {"sweep": True},
        )
        functions["sap_poller"] = poller
    unpolled = sorted(set(config.sap.reads(NAME)) - set(systems))
    if unpolled:
        # A system left out of `bypass_gateway` (and so any obo one without an
        # `unattended` block, or bring-your-own one, which it cannot name) has no
        # unattended producer.
        cdk.Annotations.of(stack).add_warning_v2(
            f"{NAME}:unpolled-sap",
            f"SAP system(s) {unpolled} are not polled: the poller needs a basic, m2m-sap "
            f"or m2m-oidc system named in layers.sap.access.{NAME}.bypass_gateway; an "
            "obo system needs a layers.sap.systems.<system>.unattended block for it to "
            "sign in with. Their exceptions become cases only by ingest or by hand.",
        )

    # ── SAP ingest ──
    subscribed = config.sap.ingest(NAME)
    if subscribed or push is not None:
        functions["sap_ingest"] = _ingest(
            stack,
            ctx,
            subscribed,
            systems,
            data,
            function,
            producer,
            own,
            push,
            cfg.ingest.max_concurrency,
        )
    if subscribed:
        # The layer's config puts every `ingest` system in `bypass_gateway`, which
        # the poller reads.
        functions["sap_poller"].add_environment(
            "INGEST_SYSTEMS", json.dumps(subscribed)
        )

    # ── Batch sweeper ──
    batch = function("BatchRunner", "batch_runner.handler", 300)
    data.grant(batch, data.cases)
    producer(batch)
    schedule(
        "BatchRunnerSchedule", cfg.batch_schedule, batch, "re-queues detected cases"
    )
    functions["batch_runner"] = batch

    # ── Operator re-run (`ops.py rerun`): a person, so ungated by the switch ──
    rerun = function("OperatorRerun", "operator_rerun.handler", 30)
    data.grant(rerun, data.cases)
    producer(rerun, reads_mode=False)
    functions["operator_rerun"] = rerun

    # ── Exemplar builder ──
    model = _exemplar_model(ctx, config)
    exemplar = function(
        "ExemplarBuilder", "exemplar_builder.handler", 300, memory_size=512
    )
    exemplar.add_environment("EXEMPLAR_BUCKET", data.exemplars.bucket_name)
    exemplar.add_environment("MODEL_ID", model)
    exemplar.add_environment("AGENT_KNOWLEDGE_PARAMETER_PREFIX", f"{own}/")
    data.grant(exemplar, data.cases, data.registry)
    data.exemplars.grant_write(exemplar)
    data.grant_key(exemplar)
    foundation = model.split(".", 1)[1] if model.split(".", 1)[0] in _GEO else model
    exemplar.add_to_role_policy(
        iam.PolicyStatement(
            actions=["bedrock:InvokeModel"],
            resources=[
                # A cross-Region profile routes to the model in whichever Region it picks.
                f"arn:{stack.partition}:bedrock:*::foundation-model/{foundation}",
                f"arn:{stack.partition}:bedrock:*:{stack.account}:inference-profile/{model}",
            ],
        )
    )
    exemplar.add_to_role_policy(
        iam.PolicyStatement(
            actions=["ssm:GetParameter"],
            resources=[param_arn(f"{own}/agent-knowledge-*")],
        )
    )
    schedule(
        "ExemplarBuilderSchedule", cfg.exemplar_schedule, exemplar, "builds exemplars"
    )
    functions["exemplar_builder"] = exemplar
    # The knowledge stack grants it the precedent write when agent knowledge is on.
    _out(stack, own, "exemplar-builder-role-arn", exemplar.role.role_arn)

    queue.add_to_resource_policy(
        iam.PolicyStatement(
            sid="OnlyTheProducersSend",
            effect=iam.Effect.DENY,
            principals=[iam.AnyPrincipal()],
            actions=["sqs:SendMessage"],
            resources=[queue.queue_arn],
            conditions={"ArnNotEquals": {"aws:PrincipalArn": senders}},
        )
    )
    _out(stack, own, "agent-queue-url", queue.queue_url)
    _out(stack, own, "agent-dlq-url", dlq.queue_url)
    for key, fn in functions.items():
        _out(stack, own, f"{key.replace('_', '-')}-function", fn.function_name)
    return functions, queue, dlq


_GEO = {"us", "eu", "apac", "global", "jp", "au", "ca", "us-gov"}


def _exemplar_model(ctx: UseCaseContext, config: BusinessOperationsConfig) -> str:
    """The cheaper tier: condensing a trace into steps needs no more."""
    return model_ids(ctx, config)["haiku"]


def _out(stack: cdk.Stack, own: str, key: str, value: str) -> None:
    ssm.StringParameter(
        stack, f"Out-{key}", parameter_name=f"{own}/{key}", string_value=value
    )


def _ingest(
    stack,
    ctx,
    subscribed: list[str],
    systems: dict,
    data: Data,
    function,
    producer,
    own: str,
    push: _lambda.Function | None,
    max_concurrency: int,
):
    """The use case's one ingest queue, its DLQ and their key, drained by `sap_ingest`.

    Two writers only: the rule `subscribe` puts on the SAP layer's bus for the
    `subscribed` systems, and `push` (`POST /erp-events`). Both write the same
    EventBridge event, whose `source` names the system. The consumer serves the
    subscribed systems and, with `push`, every system it calls directly, since the
    route sets the source from the pack's own system. A nudge is answered by reading SAP, so
    it gets the poller's direct access to those systems.
    """
    served = sorted(set(subscribed) | (set(systems) if push is not None else set()))
    specs = {name: systems[name] for name in served}
    fn = function("SapIngest", "sap_ingest.handler", 180, memory_size=512)
    fn.add_environment("SAP_SYSTEMS", json.dumps(specs))
    _grant_sap(ctx, fn, specs)
    data.grant_cases(fn, *CASE_STORE_VERBS)
    data.grant(fn, data.registry)
    producer(fn)
    _vpc(stack, ctx, fn)

    # In every mode: EventBridge cannot deliver to a queue on an AWS owned key, nor on
    # the platform's, whose policy this stack cannot change; `subscribe` opens this one.
    key = kms.Key(
        stack,
        "IngestKey",
        alias=names.ingest_key_alias(ctx.prefix),
        description=f"{ctx.prefix} {NAME}: the SAP ingest queue and its DLQ",
        enable_key_rotation=True,
        # Deleted with its queues, in production too: they hold in-flight messages
        # only, which a destroy removes with them. A retained key would outlive its
        # alias and protect nothing.
        removal_policy=cdk.RemovalPolicy.DESTROY,
    )
    dlq = sqs.Queue(
        stack,
        "IngestDlq",
        encryption=sqs.QueueEncryption.KMS,
        encryption_master_key=key,
        enforce_ssl=True,
        retention_period=cdk.Duration.days(4),
    )
    queue = sqs.Queue(
        stack,
        "IngestQueue",
        encryption=sqs.QueueEncryption.KMS,
        encryption_master_key=key,
        enforce_ssl=True,
        # Above the consumer's timeout, or a batch still being read is redelivered.
        visibility_timeout=cdk.Duration.minutes(6),
        retention_period=cdk.Duration.days(4),
        dead_letter_queue=sqs.DeadLetterQueue(queue=dlq, max_receive_count=3),
    )
    fn.add_event_source(
        sources.SqsEventSource(
            queue,
            batch_size=100,
            # A burst of nudges coalesces into one read per pack.
            max_batching_window=cdk.Duration.seconds(20),
            max_concurrency=max_concurrency,
            report_batch_item_failures=True,
        )
    )

    # Who may write: the rule and the push route, whatever IAM grants elsewhere in the account.
    # EventBridge carries no principal ARN, so the rule passes on its `aws:SourceArn`.
    allowed: dict[str, object] = {}
    if subscribed:
        rule = sap_access.SapAccess(ctx, NAME).subscribe(
            stack, queue=queue, dlq=dlq, systems=subscribed
        )
        rule.name = names.sap_events_rule_name(ctx.prefix)
        allowed["aws:SourceArn"] = rule.attr_arn
        _out(stack, own, "ingest-rule-name", rule.name)
    if push is not None:
        queue.grant_send_messages(push)
        push.add_environment("INGEST_QUEUE_URL", queue.queue_url)
        push.add_environment("SAP_SYSTEMS", json.dumps(specs))
        allowed["aws:PrincipalArn"] = push.role.role_arn
    queue.add_to_resource_policy(
        iam.PolicyStatement(
            sid="OnlyTheRuleAndPushSend",
            effect=iam.Effect.DENY,
            principals=[iam.AnyPrincipal()],
            actions=["sqs:SendMessage"],
            resources=[queue.queue_arn],
            conditions={"ArnNotEquals": allowed},
        )
    )
    _out(stack, own, "ingest-queue-url", queue.queue_url)
    _out(stack, own, "ingest-dlq-url", dlq.queue_url)
    cdk.CfnOutput(stack, "IngestQueueUrl", value=queue.queue_url)
    return fn


def _vpc(stack: cdk.Stack, ctx: UseCaseContext, fn: _lambda.Function) -> None:
    """The platform's private subnets when `security.networking` is on, as the SAP MCP
    server's runtimes use. The subnet list is an SSM token, so it is set on the L1."""
    if not ctx.platform.security.networking:
        return

    # The SAP layer's group, which reaches SAP's ports, for every function that
    # calls SAP directly (the poller, the test-data seeder).
    sg = sap_access.SapAccess(ctx, NAME).egress_security_group_id(stack)
    fn.node.default_child.add_property_override(
        "VpcConfig",
        {
            "SubnetIds": cdk.Fn.split(
                ",", ctx.ssm_value(stack, "networking/private-subnet-ids")
            ),
            "SecurityGroupIds": [sg],
        },
    )
    fn.role.add_managed_policy(
        iam.ManagedPolicy.from_aws_managed_policy_name(
            "service-role/AWSLambdaVPCAccessExecutionRole"
        )
    )
