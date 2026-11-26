# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The product alarms: the failures that have no other path to a person.

Two more live beside the code that
emits their metric: the notification channel (notifications.py) and the unreadable
registry (packs.py). Every alarm publishes to the platform's alarm topic, on ALARM and
on OK, and is unnamed, so CloudFormation names it after this stack.

The custom metrics are under this stack's namespace (`<prefix>/business-operations`),
emitted with a dimensionless copy, which is the one an alarm can ask for.
`treat_missing_data` is NOT_BREACHING throughout: no runs is not a failing run.

Built before the dashboard, whose alarm widget shows every alarm the stack has.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sqs as sqs

from business_operations.gateway_run_checks import CHECK_PATH
from business_operations.names import NAME
from infra_utils.use_case_context import UseCaseContext

_GT = cw.ComparisonOperator.GREATER_THAN_THRESHOLD


def topic(stack: cdk.Stack, ctx: UseCaseContext) -> sns.ITopic:
    """The platform's alarm topic, imported once per stack."""
    found = stack.node.try_find_child("AlarmTopic")
    return found or sns.Topic.from_topic_arn(
        stack,
        "AlarmTopic",
        ctx.ssm_value(stack, "ops-topic/arn"),
    )


def grant_publish(stack: cdk.Stack, ctx: UseCaseContext, fn: _lambda.Function) -> None:
    """Let `fn` publish to the alarm topic."""
    topic(stack, ctx).grant_publish(fn)
    if ctx.production:
        # The topic is encrypted with the platform CMK there.
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["kms:GenerateDataKey*", "kms:Decrypt"],
                resources=[ctx.ssm_value(stack, "security/kms-key-arn")],
                conditions={
                    "StringEquals": {
                        "kms:ViaService": f"sns.{stack.region}.amazonaws.com"
                    }
                },
            )
        )


def create(
    stack: cdk.Stack,
    ctx: UseCaseContext,
    namespace: str,
    watched: dict[str, _lambda.Function],
    cost_p90_usd: float,
    dlq: sqs.IQueue,
) -> list[cw.Alarm]:
    prefix = ctx.prefix

    def metric(name: str, stat: str, minutes: int) -> cw.Metric:
        return cw.Metric(
            namespace=namespace,
            metric_name=name,
            statistic=stat,
            period=cdk.Duration.minutes(minutes),
        )

    def across(name: str, minutes: int) -> cw.MathExpression:
        """One AWS/Lambda metric summed over the watched functions: the alarm only
        gets someone to look; the dashboard's graph says which function."""
        period = cdk.Duration.minutes(minutes)
        using = {
            f"m{i}": cw.Metric(
                namespace="AWS/Lambda",
                metric_name=name,
                dimensions_map={"FunctionName": fn.function_name},
                statistic="Sum",
                period=period,
            )
            for i, fn in enumerate(watched.values())
        }
        return cw.MathExpression(
            expression=" + ".join(f"FILL({k}, 0)" for k in using),
            using_metrics=using,
            period=period,
        )

    specs = [
        # A message here is a case whose run failed three times.
        (
            "DlqAlarm",
            "agent runs are failing into the dead-letter queue; those cases need a person.",
            dlq.metric_approximate_number_of_messages_visible(
                statistic="Maximum", period=cdk.Duration.minutes(5)
            ),
            0,
            _GT,
        ),
        (
            "AgentFailureAlarm",
            "fewer than 90% of agent runs succeeded in the last hour.",
            metric("AgentSuccess", "Average", 60),
            0.9,
            cw.ComparisonOperator.LESS_THAN_THRESHOLD,
        ),
        (
            "AgentCostAlarm",
            (
                f"p90 estimated cost per run is over {cost_p90_usd} USD "
                "(alarms.cost_p90_usd)."
            ),
            metric("AgentEstimatedCostUSD", "p90", 60),
            cost_p90_usd,
            _GT,
        ),
        # Every channel failure is swallowed so it never costs the run; the screen
        # looks healthy while it streams nothing. Five in a quarter hour is broken,
        # not unlucky: one oversized event is a lost frame the trace still holds.
        (
            "RunChannelAlarm",
            (
                "the run-events channel is dropping events: live runs render partially or "
                "not at all, though each finished trace still lands on its case."
            ),
            metric("RunChannelPublishFailure", "Sum", 15),
            4,
            _GT,
        ),
        # Over 2: a scheduled (asynchronous) invoke retries twice, so one failed
        # run is three Errors and one transient SAP timeout is not.
        (
            "LambdaErrorsAlarm",
            (
                "a pipeline function is failing; the business dashboard's function "
                "errors graph says which."
            ),
            across("Errors", 15),
            2,
            _GT,
        ),
        # None of these reserves concurrency, so a throttle is the account's pool
        # running dry, and an asynchronous invoke that exhausts its retries is gone.
        (
            "LambdaThrottlesAlarm",
            (
                "a pipeline function was throttled and its invocations dropped; check the "
                "account's Lambda concurrency."
            ),
            across("Throttles", 5),
            0,
            _GT,
        ),
    ]
    # The shared interceptor logs one line per refusal, naming the check that refused
    # (tools/egress_interceptor/handler.py); this check decides only for this use
    # case's own apps. A refusal is a run reaching past its grant, its amount limit or
    # its approved write. Five in a quarter hour is a run misbehaving, not one slip.
    check = CHECK_PATH.rsplit("/", 1)[1]
    logs.MetricFilter(
        stack,
        "RunCheckRefusalFilter",
        log_group=logs.LogGroup.from_log_group_name(
            stack,
            "InterceptorLogs",
            f"/aws/lambda/{prefix}-egress-interceptor",
        ),
        metric_namespace=namespace,
        metric_name="RunCheckRefusal",
        filter_pattern=logs.FilterPattern.all_terms("interceptor", check),
        metric_value="1",
    )
    specs.append(
        (
            "RunCheckRefusalAlarm",
            (
                f"the Gateway interceptor's {check} refused this use case's calls; its "
                f"log lines naming {check} say which tool and why."
            ),
            metric("RunCheckRefusal", "Sum", 15),
            4,
            _GT,
        )
    )
    ingest = watched.get("sap_ingest")
    if ingest is not None:
        # A refused message is dead-lettered too, so the queue alarm sees a wrong
        # sender as well as a SAP read that keeps failing; this one says which.
        ingest.add_environment("METRICS_NAMESPACE", namespace)
        specs.append(
            (
                "IngestRejectedAlarm",
                "SAP ingest is refusing messages; IngestRejected's Reason says why.",
                metric("IngestRejected", "Sum", 15),
                0,
                _GT,
            )
        )
        ingest_dlq = stack.node.find_child("IngestDlq")
        specs.append(
            (
                "IngestDlqAlarm",
                (
                    "SAP ingest messages are dead-lettered and made no case; "
                    "the poller still sweeps their exceptions up."
                ),
                ingest_dlq.metric_approximate_number_of_messages_visible(
                    statistic="Maximum", period=cdk.Duration.minutes(5)
                ),
                0,
                _GT,
            )
        )
    rule = stack.node.try_find_child("SapEventsRule")
    if rule is not None:
        # EventBridge could not deliver to the ingest queue (a key or queue policy,
        # throttling): each failure is retried for a day, then dead-lettered.
        specs.append(
            (
                "IngestRuleFailedAlarm",
                (
                    "the SAP events rule is failing to deliver to the ingest queue; "
                    "the dead-letter queue's messages carry ERROR_CODE."
                ),
                cw.Metric(
                    namespace="AWS/Events",
                    metric_name="FailedInvocations",
                    dimensions_map={"RuleName": rule.name},
                    statistic="Sum",
                    period=cdk.Duration.minutes(5),
                ),
                0,
                _GT,
            )
        )
        # The nudge normally makes the case before the poller reads it, so a case the
        # poller made for a pack that accepts pushes is a document the sender lost.
        # Over one default poller period; a document posted just before a poll can
        # race its own nudge, so one alone may be timing, a run of them is not.
        watched["sap_poller"].add_environment("METRICS_NAMESPACE", namespace)
        specs.append(
            (
                "IngestMissedAlarm",
                (
                    "the poller made cases SAP's sender should have nudged first; "
                    "the poller's IngestMissed log lines name each pack and key."
                ),
                metric("IngestMissed", "Sum", 15),
                0,
                _GT,
                # Two polls in a row: a document posted just before one poll is found
                # before its nudge lands once, where a broken sender misses every poll.
                2,
            )
        )

    action = cw_actions.SnsAction(topic(stack, ctx))
    alarms = []
    for id, what, m, threshold, op, *periods in specs:
        alarm = cw.Alarm(
            stack,
            id,
            alarm_description=f"{prefix} {NAME}: {what}",
            metric=m,
            threshold=threshold,
            comparison_operator=op,
            evaluation_periods=periods[0] if periods else 1,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        alarm.add_alarm_action(action)
        alarm.add_ok_action(action)
        alarms.append(alarm)
    return alarms
