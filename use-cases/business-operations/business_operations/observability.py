# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The use case's business dashboard.

The console links it from the rail for admins,
together with AgentCore's own agent observability; the in-app Observability and
Analytics pages and their `/observability/*` routes were cut in favour of both.

Built after the pipeline, because it graphs the functions and queues it creates. One list
of watched functions feeds the error graph and the alarm set, so they cannot disagree
about what the stack runs. The platform keeps its own infrastructure dashboard; nothing
here edits it.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_ssm as ssm

from business_operations import names
from infra_utils.use_case_context import UseCaseContext


def create(
    stack: cdk.Stack,
    ctx: UseCaseContext,
    namespace: str,
    watched: dict[str, _lambda.Function],
    queue: sqs.IQueue,
    dlq: sqs.IQueue,
) -> None:
    url = _dashboard(stack, namespace, watched, queue, dlq)
    # Read by the runtime stack, which writes the console's aws-exports.json.
    ssm.StringParameter(
        stack,
        "Out-dashboard-url",
        parameter_name=f"{ctx.ssm_prefix}/use-cases/{names.NAME}/dashboard-url",
        string_value=url,
    )


def _dashboard(
    stack: cdk.Stack,
    namespace: str,
    watched: dict[str, _lambda.Function],
    queue: sqs.Queue,
    dlq: sqs.Queue,
) -> str:
    def metric(name: str, stat: str, period: cdk.Duration, label: str | None = None):
        return cw.Metric(
            namespace=namespace,
            metric_name=name,
            statistic=stat,
            period=period,
            label=label,
        )

    day, hour = cdk.Duration.days(1), cdk.Duration.hours(1)
    # Unnamed: CloudFormation picks a unique name, so two stacks never collide.
    board = cw.Dashboard(
        stack, "BusinessDashboard", default_interval=cdk.Duration.hours(6)
    )
    board.add_widgets(
        *(
            cw.SingleValueWidget(
                title=title, metrics=[metric(m, s, day)], width=6, height=4
            )
            for title, m, s in (
                ("Cases processed (24h)", "AgentSuccess", "SampleCount"),
                ("Success rate (24h)", "AgentSuccess", "Average"),
                ("Average cost per run, USD (24h)", "AgentEstimatedCostUSD", "Average"),
                ("Average turns per run (24h)", "AgentTurns", "Average"),
            )
        )
    )
    board.add_widgets(
        cw.GraphWidget(
            title="Tokens",
            left=[
                metric("AgentInputTokens", "Sum", hour, "Input"),
                metric("AgentOutputTokens", "Sum", hour, "Output"),
                metric("AgentCacheReadTokens", "Sum", hour, "Cache read"),
                metric("AgentCacheWriteTokens", "Sum", hour, "Cache write"),
            ],
            width=12,
            height=6,
        ),
        cw.GraphWidget(
            title="Cost and latency",
            left=[
                metric("AgentEstimatedCostUSD", "Average", hour, "Average cost, USD")
            ],
            right=[
                metric("AgentLatencyMs", "p50", hour, "p50 latency"),
                metric("AgentLatencyMs", "p90", hour, "p90 latency"),
            ],
            width=12,
            height=6,
        ),
    )
    alarms = [a for a in stack.node.find_all() if isinstance(a, cw.Alarm)]
    row = [
        cw.GraphWidget(
            title="Function errors",
            left=[
                f.metric_errors(period=cdk.Duration.minutes(5), label=label)
                for label, f in watched.items()
            ],
            width=12,
            height=6,
        ),
        cw.GraphWidget(
            title="Agent queue depth",
            left=[
                q.metric_approximate_number_of_messages_visible(
                    statistic="Maximum", period=cdk.Duration.minutes(1), label=label
                )
                for q, label in ((queue, "Queue"), (dlq, "Dead letters"))
            ],
            width=6 if alarms else 12,
            height=6,
        ),
    ]
    # An AlarmStatusWidget with no alarm is an invalid dashboard body.
    if alarms:
        row.append(
            cw.AlarmStatusWidget(title="Alarms", alarms=alarms, width=6, height=6)
        )
    board.add_widgets(*row)
    url = (
        f"https://{stack.region}.console.aws.amazon.com/cloudwatch/home"
        f"?region={stack.region}#dashboards/dashboard/{board.dashboard_name}"
    )
    cdk.CfnOutput(stack, "BusinessDashboardUrl", value=url)
    return url
