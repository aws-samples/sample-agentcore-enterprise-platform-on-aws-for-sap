# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Observability Stack — CloudWatch vended logs and traces for AgentCore resources.

Implements Requirement 12: Observability Integration
- CloudWatch vended log delivery per AgentCore resource (APPLICATION_LOGS)
- Log groups with 1-month retention
- CloudWatch Transaction Search, without which X-Ray rejects every span
- Optional (observability.alarms): per-resource CloudWatch alarms and the
  platform dashboard, sending to the ops topic
- OpsTopicStack, beside it: the SNS ops topic, published to SSM as
  ops-topic/arn. Its own stack, so a use case that sends alarms to it does not
  wait for the runtimes this stack depends on. It deploys with
  observability.alarms or when a use case's manifest asks for it (alarm_topic)
- Optional (observability.span_retention_days): retention on the span groups
"""

import json

import aws_cdk as cdk
import jsii
from aws_cdk import (
    aws_cloudwatch as cloudwatch,
)
from aws_cdk import (
    aws_cloudwatch_actions as cw_actions,
)
from aws_cdk import (
    aws_events as events,
)
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    aws_kms as kms,
)
from aws_cdk import (
    aws_logs as logs,
)
from aws_cdk import (
    aws_sns as sns,
)
from aws_cdk import (
    aws_sns_subscriptions as subscriptions,
)
from aws_cdk import (
    aws_ssm as ssm,
)
from aws_cdk import (
    custom_resources as cr,
)
from constructs import Construct

from infra_utils.transaction_search import (
    SPAN_LOG_GROUPS,
    TRACE_DESTINATION,
    transaction_search_caller_statements,
    xray_logs_resource_policy,
)


def ops_topic_parameter(project: str, environment: str) -> str:
    return f"/{project}/{environment}/ops-topic/arn"


class OpsTopicStack(cdk.Stack):
    """The platform's SNS ops topic: every platform alarm, and any use case's that
    asks for it. Moving it here from ObservabilityStack replaced the topic, so an
    existing email subscription had to be confirmed again, once."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        project_name: str,
        environment: str,
        key: kms.IKey | None = None,
        alarm_email: str = "",
        **kwargs,
    ):
        super().__init__(scope, id, **kwargs)
        # Never alias/aws/sns: its key policy cannot grant CloudWatch, so every
        # alarm action would fail to deliver. A customer managed key works once
        # its policy lets CloudWatch use it.
        self.topic = sns.Topic(
            self,
            "PlatformAlarms",
            display_name="Platform CloudWatch alarms",
            master_key=key,
        )
        if key:
            key.add_to_resource_policy(
                iam.PolicyStatement(
                    sid="CloudWatchAlarmsPublishToEncryptedTopic",
                    principals=[iam.ServicePrincipal("cloudwatch.amazonaws.com")],
                    actions=["kms:Decrypt", "kms:GenerateDataKey*"],
                    resources=["*"],
                    # Only this account's alarms (confused-deputy prevention).
                    conditions={"StringEquals": {"aws:SourceAccount": self.account}},
                )
            )
        if alarm_email:
            # SNS mails a confirmation link; nothing flows until clicked.
            self.topic.add_subscription(subscriptions.EmailSubscription(alarm_email))
        ssm.StringParameter(
            self,
            "SSMOpsTopicArn",
            parameter_name=ops_topic_parameter(project_name, environment),
            string_value=self.topic.topic_arn,
        )


@jsii.implements(events.IRuleTarget)
class _AccountScopedTopic:
    """targets.SnsTopic, but EventBridge's grant on the topic and its key holds only
    for this account's rules (aws:SourceAccount); the stock target grants it bare."""

    def __init__(self, topic: sns.ITopic):
        self._topic = topic

    def bind(
        self, rule: events.IRule, id: str | None = None
    ) -> events.RuleTargetConfig:
        self._topic.grant_publish(
            iam.ServicePrincipal(
                "events.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": cdk.Stack.of(rule).account}
                },
            )
        )
        return events.RuleTargetConfig(arn=self._topic.topic_arn)


class ObservabilityStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        project_name: str,
        environment: str,
        monitored_resources: dict[str, str] | None = None,
        orchestrator_runtime_generation: int = 1,
        enable_traceability: bool = False,
        enable_transaction_search: bool = True,
        enable_alarms: bool = False,
        log_retention_days: int = 30,
        span_retention_days: int | None = None,
        retain_data: bool = False,
        alerts_key: kms.IKey | None = None,
        **kwargs,
    ):
        super().__init__(scope, id, **kwargs)

        prefix = f"{project_name}-{environment}"
        retention_by_days = {
            30: logs.RetentionDays.ONE_MONTH,
            90: logs.RetentionDays.THREE_MONTHS,
            180: logs.RetentionDays.SIX_MONTHS,
            365: logs.RetentionDays.ONE_YEAR,
        }
        if log_retention_days not in retention_by_days:
            raise ValueError("log_retention_days must be one of 30, 90, 180, or 365")

        # ── CloudWatch Transaction Search (prerequisite for any tracing) ──
        # The runtimes emit OTLP spans whether or not this is configured; with
        # the account still pointed at the XRay destination, X-Ray answers every
        # batch with HTTP 400 and the traces this module promises never exist.
        #
        # CAVEAT: both resources below are account- and region-scoped, not
        # per-stack. In an account where a platform team owns tracing centrally,
        # deploy with -c enable_transaction_search=false. Deleting this stack
        # deliberately does NOT revert the destination: other workloads in the
        # account may depend on it by then.
        if enable_transaction_search:
            logs.CfnResourcePolicy(
                self,
                "XRaySpansResourcePolicy",
                policy_name=f"{prefix}-transaction-search-xray",
                policy_document=json.dumps(
                    xray_logs_resource_policy(self.account, self.region)
                ),
            )

            self.trace_destination = cr.AwsCustomResource(
                self,
                "TraceSegmentDestination",
                # No onDelete: see the caveat above.
                on_update=cr.AwsSdkCall(
                    service="XRay",
                    action="updateTraceSegmentDestination",
                    parameters={"Destination": TRACE_DESTINATION},
                    physical_resource_id=cr.PhysicalResourceId.of(
                        f"{prefix}-trace-destination"
                    ),
                    # The API refuses a no-op with InvalidRequestException
                    # ("The destination is already set to CloudWatchLogs") —
                    # hit live deploying into an account that already had
                    # Transaction Search on. Ignoring the CODE is broader than
                    # the message (the SDK matcher only sees codes), so a
                    # genuinely failed set could slip through here —
                    # check_observability.py check 1 (GetTraceSegmentDestination
                    # is CloudWatchLogs and ACTIVE) is the backstop, and it runs
                    # in MODULE_VERIFY[9].
                    ignore_error_codes_matching="InvalidRequestException",
                ),
                # UpdateTraceSegmentDestination also provisions the span log
                # groups and starts Application Signals discovery, so the caller
                # needs more than the two xray actions. Full set in infra_utils.
                policy=cr.AwsCustomResourcePolicy.from_statements(
                    [
                        iam.PolicyStatement.from_json(statement)
                        for statement in transaction_search_caller_statements(
                            self.partition
                        )
                    ]
                ),
                install_latest_aws_sdk=False,
            )

            # The span groups are account-wide and created by the call above,
            # never by this stack, so retention is set rather than owned: no
            # onDelete, and unset leaves the account's choice alone.
            for group in SPAN_LOG_GROUPS if span_retention_days else ():
                safe = group.strip("/").replace("/", "-").replace("aws-", "")
                retention = cr.AwsCustomResource(
                    self,
                    f"SpanRetention-{safe}",
                    on_update=cr.AwsSdkCall(
                        service="CloudWatchLogs",
                        action="putRetentionPolicy",
                        parameters={
                            "logGroupName": group,
                            "retentionInDays": span_retention_days,
                        },
                        physical_resource_id=cr.PhysicalResourceId.of(
                            f"{prefix}-span-retention-{safe}"
                        ),
                    ),
                    policy=cr.AwsCustomResourcePolicy.from_statements(
                        [
                            iam.PolicyStatement.from_json(statement)
                            for statement in transaction_search_caller_statements(
                                self.partition
                            )
                            if statement["Sid"]
                            == "TransactionSearchLogGroupPermissions"
                        ]
                    ),
                    install_latest_aws_sdk=False,
                )
                retention.node.add_dependency(self.trace_destination)

        for resource_name, resource_arn in (monitored_resources or {}).items():
            safe_name = resource_name.replace("-", "").replace("_", "").title()
            # Logs DeliverySource rejects changing ResourceArn in place even
            # though the generic CloudFormation diff does not mark the field
            # immutable. Keep the normal IDs byte-identical for generation 1;
            # a controlled orchestrator replacement gets a new source name and
            # source/delivery logical IDs so CloudFormation creates the new
            # binding instead of attempting the unsupported update.
            delivery_generation = (
                orchestrator_runtime_generation
                if resource_name == "runtime-orchestrator"
                else 1
            )
            delivery_id_suffix = (
                f"G{delivery_generation}" if delivery_generation > 1 else ""
            )
            delivery_name_suffix = (
                f"-g{delivery_generation}" if delivery_generation > 1 else ""
            )

            log_group = logs.LogGroup(
                self,
                f"Logs{safe_name}",
                log_group_name=f"/aws/bedrock-agentcore/{prefix}/{resource_name}",
                retention=retention_by_days[log_retention_days],
                removal_policy=(
                    cdk.RemovalPolicy.RETAIN
                    if retain_data
                    else cdk.RemovalPolicy.DESTROY
                ),
            )

            # Vended log delivery source
            source = cdk.CfnResource(
                self,
                f"Source{safe_name}{delivery_id_suffix}",
                type="AWS::Logs::DeliverySource",
                properties={
                    "Name": (
                        f"{prefix}-{resource_name}-app-logs{delivery_name_suffix}"
                    ),
                    "ResourceArn": resource_arn,
                    "LogType": "APPLICATION_LOGS",
                },
            )

            # Vended log delivery destination
            dest = cdk.CfnResource(
                self,
                f"Dest{safe_name}",
                type="AWS::Logs::DeliveryDestination",
                properties={
                    "Name": f"{prefix}-{resource_name}-cw-dest",
                    "DestinationResourceArn": log_group.log_group_arn,
                },
            )

            # Connect source → destination
            cdk.CfnResource(
                self,
                f"Delivery{safe_name}{delivery_id_suffix}",
                type="AWS::Logs::Delivery",
                properties={
                    "DeliverySourceName": source.ref,
                    "DeliveryDestinationArn": dest.get_att("Arn").to_string(),
                },
            )

        # ── Alarms + platform dashboard (observability.alarms) ──
        # Every metric name and dimension set below was read off a live
        # deployment — the AWS/Bedrock-AgentCore namespace is not documented
        # well enough to guess, and a wrong dimension is an alarm that can
        # never fire. tests/test_alarms.py pins the load-bearing strings.
        if enable_alarms:
            topic = sns.Topic.from_topic_arn(
                self,
                "OpsTopic",
                ssm.StringParameter.value_for_string_parameter(
                    self, ops_topic_parameter(project_name, environment)
                ),
            )
        if enable_alarms:
            ns = "AWS/Bedrock-AgentCore"
            five_min = cdk.Duration.minutes(5)

            def metric(
                name: str,
                dims: dict[str, str],
                stat: str = "Sum",
                label: str | None = None,
            ) -> cloudwatch.Metric:
                return cloudwatch.Metric(
                    namespace=ns,
                    metric_name=name,
                    dimensions_map=dims,
                    statistic=stat,
                    period=five_min,
                    label=label,
                )

            def alarm(
                name: str,
                alarm_metric: cloudwatch.IMetric,
                description: str,
                *,
                threshold: float = 0,
                evaluation_periods: int = 1,
                datapoints_to_alarm: int | None = None,
                comparison: cloudwatch.ComparisonOperator = (
                    cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD
                ),
            ) -> None:
                a = cloudwatch.Alarm(
                    self,
                    f"Alarm-{name}",
                    alarm_name=f"{prefix}-{name}",
                    metric=alarm_metric,
                    threshold=threshold,
                    evaluation_periods=evaluation_periods,
                    datapoints_to_alarm=datapoints_to_alarm,
                    comparison_operator=comparison,
                    # Error metrics only emit when nonzero: missing data is
                    # good news — and it keeps the alarm quiet if AWS ever
                    # renames a series out from under us.
                    treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
                    alarm_description=description,
                )
                a.add_alarm_action(cw_actions.SnsAction(topic))
                a.add_ok_action(cw_actions.SnsAction(topic))

            # A content-filter intervention is a blocked prompt attack or harmful
            # content on any guardrail in the account; PII masking (its own policy
            # type) is the guardrail working as configured, so it does not alarm.
            alarm(
                "guardrail-content-interventions",
                cloudwatch.Metric(
                    namespace="AWS/Bedrock/Guardrails",
                    metric_name="InvocationsIntervened",
                    dimensions_map={
                        "Operation": "ApplyGuardrail",
                        "GuardrailPolicyType": "ContentPolicy",
                    },
                    statistic="Sum",
                    period=five_min,
                ),
                "A Bedrock guardrail's content filter intervened (a prompt attack or "
                "harmful content). Check first: the egress interceptor's log lines "
                "with verdict blocked, and the runtimes' guardrail traces.",
            )

            widgets: list[list[cloudwatch.IWidget]] = [
                [cloudwatch.TextWidget(markdown=f"# {prefix} platform", width=24)]
            ]
            resources = monitored_resources or {}

            # Runtime components. The Name dimension is the runtime's
            # AgentCore name + endpoint — this mirrors RuntimeStack's rt_name
            # derivation (AgentCore names use underscores) and is pinned
            # against it by tests/test_alarms.py: rename there, fail here.
            for key, arn in resources.items():
                if not key.startswith("runtime-"):
                    continue
                component = key.removeprefix("runtime-")
                rt_name = f"{project_name}_{environment}_{component}".replace("-", "_")
                if component == "orchestrator" and orchestrator_runtime_generation > 1:
                    rt_name = f"{rt_name}_g{orchestrator_runtime_generation}"
                dims = {
                    "Resource": arn,
                    "Operation": "InvokeAgentRuntime",
                    "Name": f"{rt_name}::DEFAULT",
                }
                alarm(
                    f"{key}-system-errors",
                    metric("SystemErrors", dims),
                    f"AgentCore-side failures in {key} across two consecutive "
                    f"periods. Check first: /aws/bedrock-agentcore/{prefix}/{key}.",
                    evaluation_periods=2,
                )
                alarm(
                    f"{key}-throttles",
                    metric("Throttles", dims),
                    f"InvokeAgentRuntime on {key} is being throttled. Check "
                    "first: concurrent sessions vs the AgentCore runtime quota.",
                )
                alarm(
                    f"{key}-latency-p99",
                    metric("Latency", dims, "p99"),
                    f"{key} p99 latency above 30s for 15 minutes — pathology, "
                    "not normal agent slowness. Check first: the slow trace in "
                    "CloudWatch Transaction Search.",
                    threshold=30_000,
                    evaluation_periods=3,
                    datapoints_to_alarm=3,
                )
                widgets.append(
                    [
                        cloudwatch.GraphWidget(
                            title=f"{key} traffic",
                            left=[
                                metric("Invocations", dims),
                                metric("Sessions", dims),
                            ],
                            width=8,
                        ),
                        cloudwatch.GraphWidget(
                            title=f"{key} errors",
                            left=[
                                metric("SystemErrors", dims),
                                metric("UserErrors", dims),
                                metric("Throttles", dims),
                            ],
                            stacked=True,
                            width=8,
                        ),
                        cloudwatch.GraphWidget(
                            title=f"{key} latency",
                            left=[
                                metric("Latency", dims, "p99", "p99"),
                                metric("Latency", dims, "p50", "p50"),
                            ],
                            width=8,
                        ),
                    ]
                )

            if "gateway" in resources:
                dims = {
                    "Resource": resources["gateway"],
                    "Operation": "InvokeGateway",
                    "Protocol": "MCP",
                }
                alarm(
                    "gateway-system-errors",
                    metric("SystemErrors", dims),
                    "AgentCore-side gateway failures across two consecutive "
                    f"periods. Check first: /aws/bedrock-agentcore/{prefix}/gateway.",
                    evaluation_periods=2,
                )
                alarm(
                    "gateway-throttles",
                    metric("Throttles", dims),
                    "InvokeGateway is being throttled. Check first: which agent "
                    "is hammering tools (runtime Invocations on the dashboard).",
                )
                alarm(
                    "gateway-latency-p99",
                    metric("Latency", dims, "p99"),
                    "Gateway p99 latency above 5s for 15 minutes. Check first: "
                    "the Lambda tool targets' duration and errors.",
                    threshold=5_000,
                    evaluation_periods=3,
                    datapoints_to_alarm=3,
                )
                widgets.append(
                    [
                        cloudwatch.GraphWidget(
                            title="gateway invocations",
                            left=[metric("Invocations", dims)],
                            width=8,
                        ),
                        cloudwatch.GraphWidget(
                            title="gateway errors",
                            left=[
                                metric("SystemErrors", dims),
                                metric("UserErrors", dims),
                                metric("Throttles", dims),
                            ],
                            stacked=True,
                            width=8,
                        ),
                        cloudwatch.GraphWidget(
                            title="gateway latency",
                            left=[
                                metric("Latency", dims, "p99", "Latency p99"),
                                metric("Duration", dims, "p99", "Duration p99"),
                            ],
                            width=8,
                        ),
                    ]
                )

            if "memory" in resources:
                mem = resources["memory"]
                # Memory metrics are per-Operation. No SEARCH fan-out for the
                # alarms — CreateEvent and ListEvents are the two operations
                # every agent turn depends on; the rest ride the dashboard.
                for op in ("CreateEvent", "ListEvents"):
                    dims = {"Resource": mem, "Operation": op}
                    alarm(
                        f"memory-{op.lower()}-system-errors",
                        metric("SystemErrors", dims),
                        f"Memory {op} failing across two consecutive periods — "
                        "agent turns are losing conversation state. Check "
                        f"first: /aws/bedrock-agentcore/{prefix}/memory.",
                        evaluation_periods=2,
                    )
                    alarm(
                        f"memory-{op.lower()}-throttles",
                        metric("Throttles", dims),
                        f"Memory {op} is being throttled. Check first: which "
                        "runtime's Invocations spiked on the dashboard.",
                    )
                mem_ops = ("CreateEvent", "GetEvent", "ListEvents")
                widgets.append(
                    [
                        cloudwatch.GraphWidget(
                            title="memory invocations",
                            left=[
                                metric(
                                    "Invocations",
                                    {"Resource": mem, "Operation": op},
                                    label=op,
                                )
                                for op in mem_ops
                            ],
                            width=8,
                        ),
                        cloudwatch.GraphWidget(
                            title="memory errors",
                            left=[
                                metric(
                                    name,
                                    {"Resource": mem, "Operation": op},
                                    label=f"{op} {name}",
                                )
                                for op in ("CreateEvent", "ListEvents")
                                for name in ("SystemErrors", "Throttles")
                            ],
                            stacked=True,
                            width=8,
                        ),
                        cloudwatch.GraphWidget(
                            title="memory latency",
                            left=[
                                metric(
                                    "Latency",
                                    {"Resource": mem, "Operation": op},
                                    "p99",
                                    label=op,
                                )
                                for op in mem_ops
                            ],
                            width=8,
                        ),
                    ]
                )

            # Model inference is account-level in AWS/Bedrock: the aggregate
            # series carry NO dimensions. InvocationThrottles may hold no data
            # until the first throttle — NOT_BREACHING keeps that quiet.
            def bedrock(name: str, stat: str = "Sum") -> cloudwatch.Metric:
                return cloudwatch.Metric(
                    namespace="AWS/Bedrock",
                    metric_name=name,
                    statistic=stat,
                    period=five_min,
                )

            alarm(
                "bedrock-invocation-throttles",
                bedrock("InvocationThrottles"),
                "5+ Bedrock throttles in 5 minutes — a couple of retried "
                "throttles are normal, this is sustained. Check first: "
                "EstimatedTPMQuotaUsage on the dashboard, then Service Quotas.",
                threshold=5,
                comparison=(
                    cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD
                ),
            )
            alarm(
                "bedrock-invocation-server-errors",
                bedrock("InvocationServerErrors"),
                "Bedrock 5xx across two consecutive periods. Check first: the "
                "AWS Health Dashboard for the model's region.",
                evaluation_periods=2,
            )
            widgets.append(
                [
                    cloudwatch.GraphWidget(
                        title="Bedrock invocations",
                        left=[bedrock("Invocations")],
                        width=6,
                    ),
                    cloudwatch.GraphWidget(
                        title="Bedrock latency",
                        left=[bedrock("InvocationLatency", "Average")],
                        width=6,
                    ),
                    cloudwatch.GraphWidget(
                        title="Bedrock tokens",
                        left=[
                            bedrock("InputTokenCount"),
                            bedrock("OutputTokenCount"),
                        ],
                        width=6,
                    ),
                    cloudwatch.GraphWidget(
                        title="Bedrock TPM quota usage",
                        # Per-ModelId only — no aggregate series exists, and
                        # the model ids in play aren't known at synth. SEARCH
                        # picks up whatever models the agents actually use.
                        left=[
                            cloudwatch.MathExpression(
                                expression=(
                                    "SEARCH('{AWS/Bedrock,ModelId} "
                                    'MetricName="EstimatedTPMQuotaUsage"\', '
                                    "'Average')"
                                ),
                                using_metrics={},
                                label="",
                                period=five_min,
                            )
                        ],
                        width=6,
                    ),
                ]
            )
            widgets.append(
                [
                    cloudwatch.GraphWidget(
                        title="Active sessions (account)",
                        left=[
                            cloudwatch.Metric(
                                namespace=ns,
                                metric_name="ActiveSessionCount",
                                dimensions_map={"Service": "AgentCore.Runtime"},
                                statistic="Average",
                                period=five_min,
                            )
                        ],
                        width=24,
                    )
                ]
            )

            cloudwatch.Dashboard(
                self,
                "PlatformDashboard",
                dashboard_name=f"{prefix}-platform",
                widgets=widgets,
            )

        # ── Detective controls: alert on sensitive AgentCore API calls (item 7) ──
        # Requires CloudTrail management events (provided by the security stack). Routes
        # sensitive config-change events to an SNS topic for SOC/IR — end-to-end traceability.
        self.alerts_topic = None
        if enable_traceability:
            self.alerts_topic = sns.Topic(
                self,
                "SecurityAlerts",
                topic_name=f"{prefix}-agentcore-security-alerts",
                display_name="AgentCore security alerts",
                # The target's grant_publish gives EventBridge the key too.
                master_key=alerts_key,
            )
            rule = events.Rule(
                self,
                "SensitiveAgentCoreEvents",
                rule_name=f"{prefix}-agentcore-sensitive-events",
                description="Alert on sensitive AgentCore config-change API calls.",
                event_pattern=events.EventPattern(
                    source=["aws.bedrock-agentcore"],
                    detail_type=["AWS API Call via CloudTrail"],
                    detail={
                        "eventName": [
                            "CreateGateway",
                            "UpdateGateway",
                            "DeleteGateway",
                            "DeleteMemory",
                            "PutResourcePolicy",
                            "DeleteResourcePolicy",
                            "CreatePolicy",
                            "UpdatePolicy",
                            "DeletePolicy",
                            "UpdatePolicyEngine",
                            # A target or runtime change reroutes tool calls or swaps the
                            # agent's code as surely as a Gateway change does.
                            "CreateGatewayTarget",
                            "UpdateGatewayTarget",
                            "DeleteGatewayTarget",
                            "UpdateAgentRuntime",
                            "DeleteAgentRuntime",
                        ],
                    },
                ),
            )
            rule.add_target(_AccountScopedTopic(self.alerts_topic))
            # The egress interceptor runs every tenant's checks, and its environment
            # names the role whose writes those checks trust: a code or configuration
            # change to it rewrites every check as surely as a Cedar change does.
            # functionName may be the name, an ARN or a qualified ARN.
            interceptor = f"{prefix}-egress-interceptor"
            events.Rule(
                self,
                "InterceptorChanges",
                rule_name=f"{prefix}-interceptor-changes",
                description="Alert on code, configuration or permission changes to the egress interceptor.",
                event_pattern=events.EventPattern(
                    source=["aws.lambda"],
                    detail_type=["AWS API Call via CloudTrail"],
                    detail={
                        "eventName": [
                            "UpdateFunctionCode20150331v2",
                            "UpdateFunctionConfiguration20150331v2",
                            "AddPermission20150331v2",
                            "RemovePermission20150331v2",
                            "DeleteFunction20150331",
                            "UpdateAlias20150331",
                            "PublishVersion20150331",
                        ],
                        "requestParameters": {
                            "functionName": [
                                interceptor,
                                {"wildcard": f"*:function:{interceptor}"},
                                {"wildcard": f"*:function:{interceptor}:*"},
                            ]
                        },
                    },
                ),
            ).add_target(_AccountScopedTopic(self.alerts_topic))
            # Disabling or scheduling the deletion of a key makes everything it
            # encrypts unreadable, the platform CMK's tables and trail included.
            events.Rule(
                self,
                "KmsKeyDisableOrDelete",
                rule_name=f"{prefix}-kms-key-disable-or-delete",
                description="Alert when any KMS key is disabled or scheduled for deletion.",
                event_pattern=events.EventPattern(
                    source=["aws.kms"],
                    detail_type=["AWS API Call via CloudTrail"],
                    detail={
                        "eventSource": ["kms.amazonaws.com"],
                        "eventName": ["DisableKey", "ScheduleKeyDeletion"],
                    },
                ),
            ).add_target(_AccountScopedTopic(self.alerts_topic))
            cdk.CfnOutput(
                self, "SecurityAlertsTopicArn", value=self.alerts_topic.topic_arn
            )

        cdk.CfnOutput(
            self,
            "MonitoredResources",
            value=",".join(monitored_resources.keys())
            if monitored_resources
            else "none",
        )
