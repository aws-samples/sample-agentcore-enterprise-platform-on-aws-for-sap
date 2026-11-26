# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The platform's log retention on every Lambda log group the app creates.

Left alone, Lambda creates ``/aws/lambda/<function>`` on first invocation, with no
retention, outside any stack. ``MANAGED_LOG_GROUP_FLAG`` makes every
``lambda.Function`` (singletons and CDK's own custom-resource functions included)
create that group in its stack instead, at aws-cdk's two-year default. The Aspect
gives those groups ``observability.log_retention_days``, gives the L1 functions the
flag cannot reach (``CustomResourceProvider`` handlers, such as S3 auto-delete) a
group of their own, and gives any other group with no retention at all the same
days. A group whose author chose its retention keeps it.

On a stack deployed before the flag, the groups it now declares already exist, and the
change set fails with "already exists". docs/TROUBLESHOOTING.md gives the one-time
``cdk import`` that adopts them with their logs.
"""

from __future__ import annotations

import aws_cdk as cdk
import jsii
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs

MANAGED_LOG_GROUP_FLAG = "@aws-cdk/aws-lambda:useCdkManagedLogGroup"

#: observability.log_retention_days -> the enum LogRetention takes.
RETENTION_BY_DAYS = {
    30: logs.RetentionDays.ONE_MONTH,
    90: logs.RetentionDays.THREE_MONTHS,
    180: logs.RetentionDays.SIX_MONTHS,
    365: logs.RetentionDays.ONE_YEAR,
}


def _removal(retain: bool) -> cdk.RemovalPolicy:
    return cdk.RemovalPolicy.RETAIN if retain else cdk.RemovalPolicy.DESTROY


def log_group(scope, id: str, days: int, retain: bool) -> logs.LogGroup:
    """A log group of the stack's own, kept on delete only when `retain`."""
    group = logs.LogGroup(scope, id, removal_policy=_removal(retain))
    # The L2's default is two years, which the Aspect would leave alone.
    group.node.default_child.retention_in_days = days
    return group


def service_log_retention(
    scope, id: str, log_group_name: str, days: int, retain: bool
) -> logs.LogRetention:
    """Retention on a group a service creates itself, outside any stack.

    API Gateway's execution logs and an AgentCore runtime's DEFAULT group are named
    after an id known only at deploy and are created with no retention. LogRetention
    creates the group if the service has not yet, and sets the policy either way.
    """
    if days not in RETENTION_BY_DAYS:
        raise ValueError("log_retention_days must be one of 30, 90, 180, or 365")
    return logs.LogRetention(
        scope,
        id,
        log_group_name=log_group_name,
        retention=RETENTION_BY_DAYS[days],
        removal_policy=_removal(retain),
    )


@jsii.implements(cdk.IAspect)
class LambdaLogRetention:
    def __init__(self, days: int, retain: bool) -> None:
        self._days = days
        self._removal = (
            cdk.RemovalPolicy.RETAIN if retain else cdk.RemovalPolicy.DESTROY
        )

    def _apply(self, group: logs.CfnLogGroup) -> None:
        group.retention_in_days = self._days
        group.apply_removal_policy(self._removal)

    def visit(self, node) -> None:
        if isinstance(node, lambda_.Function):
            # The group the flag creates; one passed in as log_group is not a child.
            managed = node.node.try_find_child("LogGroup")
            if isinstance(managed, logs.LogGroup):
                self._apply(managed.node.default_child)
        elif isinstance(node, logs.CfnLogGroup):
            if node.retention_in_days is None:
                self._apply(node)
        elif (
            isinstance(node, cdk.CfnResource)
            and not isinstance(node, lambda_.CfnFunction)
            and node.cfn_resource_type == "AWS::Lambda::Function"
            and node.node.scope.node.try_find_child(f"{node.node.id}LogGroup") is None
        ):
            group = logs.CfnLogGroup(
                node.node.scope,
                f"{node.node.id}LogGroup",
                log_group_name=cdk.Fn.join("", ["/aws/lambda/", node.ref]),
            )
            self._apply(group)
