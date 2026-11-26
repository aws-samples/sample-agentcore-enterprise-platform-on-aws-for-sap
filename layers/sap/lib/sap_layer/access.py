# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""What a use case that requires `layer-sap` builds its SAP access with, at synth.

```python
sap = access.SapAccess(ctx, "<use case>")   # its row of layers.sap.access
sap.tool("s4", "odata_read")                     # the target-prefixed tool name
sap.claim(stack, callers=[...], read=["s4"], write={"s4": ["update"]}, governed_by=None)
sap.permits(stack, read=["s4"], write={"s4": ["update"]}, scope_prefix="uc-<name>")
sap.grant_direct(fn, "s4")                       # only for one in its `bypass_gateway`
sap.subscribe(stack, queue=q, dlq=dlq, systems=["s4"])  # only for one in its `ingest`
```

`claim` writes `layers/sap/claims/<use case>`, which the layer's interceptor check
reads: a SAP call from a client no claim names is refused, read or write. The caller
key is the calling app, `client_id` on a Cognito token and `azp` on an Entra one
(including a person's, whose `azp` is the console app). `claim` and `permits` refuse
at synth anything outside the operator's ceiling, `layers.sap.access.<use case>`.

A claimed write with `governed_by: None` passes the layer's check only while Cedar
enforces; `governed_by` names the use case's own interceptor check, which then
decides (run grants, limits) and must return None for a caller it did not claim.
"""

from __future__ import annotations

from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_events as events
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_ssm as ssm

from infra_utils.use_case_context import UseCaseContext
from sap_kit.cedar_policies import PolicySpec, add_policies, scoped_permit
from sap_kit.names import policy_name, tool_name
from sap_layer import names
from sap_layer.config import SapConfig, SapSystem
from sap_layer.config import load as load_config

LAYER_DIR = Path(__file__).resolve().parents[2]
CHECK_PATH = "layers/sap/sap_check"


def config(ctx: UseCaseContext) -> SapConfig:
    """`layers.sap`, validated. Raises when the layer is not enabled."""
    layers = ctx.platform.layers
    if names.NAME not in layers:
        raise ValueError(
            "the SAP layer is not enabled: add `layers: {sap: {...}}` to platform.yaml "
            "(layers/sap/walkthrough.md)"
        )
    return load_config(layers[names.NAME])


def target_tools(system: str, cfg: SapSystem) -> list[str]:
    """The target-prefixed tools a system's target offers: the reads and its writes."""
    target = names.sap_target(system)
    wanted = [*names.SAP_READ_TOOLS, *(names.WRITE_TOOLS[op] for op in cfg.writes)]
    return [tool_name(target, t) for t in sorted(wanted)]


def interceptor_role(
    stack: cdk.Stack, ctx: UseCaseContext, owner: str = names.NAME
) -> iam.IRole:
    """The platform interceptor's role, imported once per stack for `owner`.

    The inline policy CDK attaches to an imported role is named after its logical id,
    and the role is shared: each owner (the layer, each claiming use case) needs its
    own id, or two stacks' policies collide on one name.
    """
    id = "SapCheckInterceptorRole" + ("" if owner == names.NAME else f"-{owner}")
    found = stack.node.try_find_child(id)
    return found or iam.Role.from_role_arn(
        stack,
        id,
        ctx.ssm_value(stack, "gateway/interceptor-role-arn"),
    )


#: The stack parameter `ops.py rewrite` bumps so CloudFormation writes a tampered
#: claim or the check's settings again, with no template edit.
REWRITE_NONCE = "SapRewriteNonce"


def trusted_description(stack: cdk.Stack) -> str:
    """The description of a parameter the layer's check trusts only from CloudFormation.

    It carries the stack's rewrite nonce: an update that changes the nonce changes
    the description, and CloudFormation then writes the parameter again, value and all.
    `cdk deploy` keeps a parameter's previous value, so a deploy never undoes a bump.
    """
    nonce = stack.node.try_find_child(REWRITE_NONCE) or cdk.CfnParameter(
        stack,
        REWRITE_NONCE,
        type="String",
        default="0",
        description="Bumped by `deploy.sh layer run sap rewrite`; any value",
    )
    return f"Written by CloudFormation only; rewrite {nonce.value_as_string}"


def _covers(prefixes: list[str]) -> bool:
    return any(names.TARGET_PREFIX.startswith(p) for p in prefixes)


def require_check(platform, sap: SapConfig) -> None:
    """Refuse a synth with SAP systems but without the layer's check in the interceptor.

    Without it nothing refuses an unclaimed client's SAP call or `odata_delete`.
    Its prefixes must stay inside the SAP targets, or it would refuse other tenants'.
    And no SAP tool may be left out of the egress guardrail's response scan: what it
    returns is SAP data, which the production floor screens.
    """
    unscanned = [
        t
        for t in platform.security.egress_unscanned_responses
        if t.startswith((names.TARGET_PREFIX, names.LEGACY_TARGET_PREFIX))
    ]
    if unscanned:
        raise ValueError(
            f"security.egress_unscanned_responses names SAP tools {unscanned}: a SAP "
            "response is SAP data, and the egress guardrail always scans it"
        )
    if not sap.systems:
        return
    for e in platform.gateway.interceptor_extensions:
        if e.path.rstrip("/") == CHECK_PATH:
            if (
                names.TARGET_PREFIX in e.target_prefixes
                and all(
                    p.startswith(names.TARGET_PREFIX) or p == names.LEGACY_TARGET_PREFIX
                    for p in e.target_prefixes
                )
                and e.request_headers
            ):
                return
            break
    raise ValueError(
        "layers.sap needs its interceptor check, with exactly the SAP targets' prefix. "
        "Add to platform.yaml:\n"
        "gateway:\n"
        "  interceptor_extensions:\n"
        f"    - path: {CHECK_PATH}\n"
        f"      target_prefixes: [{names.TARGET_PREFIX}]\n"
        "      request_headers: true"
    )


class SapAccess:
    def __init__(self, ctx: UseCaseContext, use_case: str):
        self.ctx = ctx
        self.use_case = use_case
        self.config = config(ctx)
        if use_case not in self.config.access:
            raise ValueError(
                f"use case {use_case!r} has no row in layers.sap.access, so it may not "
                "reach SAP. Add one to platform.yaml, for example:\n"
                "layers:\n  sap:\n    access:\n"
                f'      {use_case}: {{read: "*", bypass_gateway: []}}'
            )
        self.reads = self.config.reads(use_case)
        self.writes = self.config.writes(use_case)
        self.direct = list(self.config.access[use_case].bypass_gateway)

    @property
    def systems(self) -> dict[str, SapSystem]:
        """The systems this use case may read, by name."""
        return {s: self.config.systems[s] for s in self.reads}

    def tool(self, system: str, tool: str) -> str:
        if system not in self.config.systems:
            raise ValueError(f"no SAP system {system!r} in layers.sap.systems")
        return tool_name(names.sap_target(system), tool)

    def _within(self, read: list[str], write: dict[str, list[str]]) -> None:
        where = f"layers.sap.access.{self.use_case}"
        over = sorted(set(read) - set(self.reads))
        if over:
            raise ValueError(f"{self.use_case} reads {over}, outside {where}.read")
        for system, ops in write.items():
            off = sorted(set(ops) - set(self.writes.get(system, [])))
            if off:
                raise ValueError(
                    f"{self.use_case} writes {off} on {system}, outside {where}.write"
                )

    def claim(
        self,
        stack: cdk.Stack,
        *,
        callers: list[str],
        read: list[str],
        write: dict[str, list[str]] | None = None,
        governed_by: str | None = None,
    ) -> ssm.StringParameter:
        """Publish this use case's claim, `layers/sap/claims/<use case>`.

        `callers` are app ids (`client_id` or `azp`), tokens allowed. `governed_by`
        is the directory name of this use case's own interceptor check, which must
        be listed with a prefix covering every SAP target.
        """
        write = {s: list(ops) for s, ops in (write or {}).items() if ops}
        self._within(read, write)
        if not callers:
            raise ValueError("a claim names at least one calling app")
        platform = self.ctx.platform
        require_check(platform, self.config)
        if governed_by is not None and not any(
            Path(e.path.rstrip("/")).name == governed_by and _covers(e.target_prefixes)
            for e in platform.gateway.interceptor_extensions
        ):
            raise ValueError(
                f"claim(governed_by={governed_by!r}): gateway.interceptor_extensions "
                f"lists no check {governed_by!r} with a prefix covering "
                f"{names.TARGET_PREFIX}"
            )
        parameter = ssm.StringParameter(
            stack,
            "SapClaim",
            parameter_name=f"{self.ctx.ssm_prefix}/layers/{names.NAME}/claims/{self.use_case}",
            description=trusted_description(stack),
            string_value=stack.to_json_string(
                {
                    "use_case": self.use_case,
                    "callers": callers,
                    "read": sorted(read),
                    "write": write,
                    "governed_by": governed_by,
                }
            ),
        )
        # The claim is this use case's own parameter, so the shared role may read it.
        parameter.grant_read(interceptor_role(stack, self.ctx, self.use_case))
        return parameter

    def permits(
        self,
        stack: cdk.Stack,
        *,
        read: list[str],
        write: dict[str, list[str]] | None = None,
        scope_prefix: str,
        read_scope: str = "read",
        write_scope: str = "write",
        principals: dict[str | None, list[str]] | None = None,
        after: agentcore.CfnPolicy | None = None,
    ) -> list[agentcore.CfnPolicy]:
        """Per system, a read permit and a write permit, on the platform's engine.

        Named `policy_name(..., <use case>, "sap_<system>_read")` and created one at
        a time after `after`. A write permit names only the writes the system has on:
        CreatePolicy refuses an action the target does not list. Under obo a read is
        a person's (scope None: the console app's `azp`), and SAP authorizes the person.
        """
        write = {s: list(ops) for s, ops in (write or {}).items() if ops}
        self._within(read, write)
        ctx = self.ctx
        engine_id = ctx.ssm_value(stack, "gateway/policy-engine-id")
        gateway_id = ctx.ssm_value(stack, "gateway/id")
        gateway_arn = f"arn:aws:bedrock-agentcore:{stack.region}:{stack.account}:gateway/{gateway_id}"
        # This read is what orders the stack after the layer (app.py derives
        # edges from SSM reads): CreatePolicy refuses an action on a target the
        # layer has not attached yet. A private output, so the read is referenced.
        if not stack.node.try_find_child("SapSystems"):
            cdk.CfnOutput(
                stack,
                "SapSystems",
                description="The SAP layer's systems these permits were rendered against",
                value=ctx.ssm_value(stack, f"layers/{names.NAME}/systems"),
            )
        created: list[agentcore.CfnPolicy] = []
        for system in sorted({*read, *write}, key=list(self.config.systems).index):
            cfg = self.config.systems[system]
            target = names.sap_target(system)
            key = system.replace("-", "_")
            specs = []
            if system in read:
                needs = None if cfg.flow == "obo" else read_scope
                tools = [tool_name(target, t) for t in names.LISTED_READ_TOOLS]
                specs.append((f"sap_{key}_read", tools, needs))
            ops = [op for op in cfg.writes if op in write.get(system, [])]
            if ops:
                tools = [tool_name(target, names.WRITE_TOOLS[op]) for op in ops]
                specs.append((f"sap_{key}_write", tools, write_scope))
            created += add_policies(
                stack,
                [
                    PolicySpec(
                        id=policy_id,
                        name=policy_name(
                            ctx.project, ctx.environment, self.use_case, policy_id
                        ),
                        description=f"{ctx.prefix} {self.use_case}: SAP {system} {needs or 'read'}",
                        text=scoped_permit(
                            tools,
                            needs,
                            gateway_arn,
                            scope_prefix=scope_prefix,
                            principals=principals,
                        ),
                    )
                    for policy_id, tools, needs in specs
                ],
                engine_id=engine_id,
                after=created[-1] if created else after,
            )
        return created

    def grant_direct(self, fn: _lambda.Function, system: str) -> dict:
        """Let `fn` call `system` outside the Gateway; its `{base_url, secret, client}`,
        and a token flow's settings, the spec `sap_http.system` takes. The secret and
        settings are the system's `unattended` ones when it has them.

        Refused unless `layers.sap.access.<use case>.bypass_gateway` names the system:
        such calls pass no claim check, so the operator names each one.
        """
        if system not in self.direct:
            raise ValueError(
                f"{self.use_case} calls SAP system {system!r} directly, which "
                f"layers.sap.access.{self.use_case}.bypass_gateway does not name"
            )
        cfg = self.config.systems[system]
        stack = cdk.Stack.of(fn)
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[
                    stack.format_arn(
                        service="secretsmanager",
                        resource="secret",
                        resource_name=f"{cfg.direct_secret_name}-??????",
                        arn_format=cdk.ArnFormat.COLON_RESOURCE_NAME,
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
                        resource_name=f"{self.ctx.ssm_prefix.lstrip('/')}/layers/"
                        f"{names.NAME}/systems/{system}/connection",
                    )
                ],
            )
        )
        return {
            "base_url": cfg.base_url,
            "secret": cfg.direct_secret_name,
            "client": cfg.client,
            **cfg.direct_sign_in(),
        }

    def subscribe(
        self,
        stack: cdk.Stack,
        *,
        queue: sqs.Queue,
        dlq: sqs.Queue,
        systems: list[str],
    ) -> events.CfnRule:
        """Route `systems`' events for this use case from the `sap-events` bus to `queue`.

        One rule on the bus, `{"source": ["sap.<system>", ...], "detail-type": [<use
        case>]}`: the `detail-type` is this use case's name, never the caller's choice.
        The target retries for 24 hours (185 attempts), then goes to `dlq`, a standard
        queue in this Region. Both queues admit the rule alone (`aws:SourceArn`) and
        must be defined in `stack`; a queue key defined there (`encryption_master_key`)
        is opened to EventBridge too. EventBridge cannot use an AWS owned key, nor a
        key whose policy this stack cannot change (such as the platform's).

        Refused unless `layers.sap.access.<use case>.ingest` names every system, and
        `ingest` takes only systems with `senders`.

        Publishes `layers/sap/subscriptions/<use case>` (the rule, queue and DLQ ARNs),
        which the layer's verify holds every rule on the bus to: a rule another stack
        adds on this use case's topic, or a second target on this rule, fails it.
        """
        where = f"layers.sap.access.{self.use_case}.ingest"
        if not systems:
            raise ValueError("subscribe names at least one SAP system")
        if over := sorted(set(systems) - set(self.config.ingest(self.use_case))):
            raise ValueError(
                f"{self.use_case} subscribes to {over}, outside {where} (each system "
                "there needs layers.sap.systems.<system>.senders)"
            )
        rule = events.CfnRule(
            stack,
            "SapEventsRule",
            description=f"{self.ctx.prefix} {self.use_case}: SAP events to its ingest queue",
            event_bus_name=self.ctx.ssm_value(stack, f"layers/{names.NAME}/bus-arn"),
            event_pattern={
                "source": [names.event_source(s) for s in sorted(systems)],
                "detail-type": [self.use_case],
            },
            state="ENABLED",
            targets=[
                events.CfnRule.TargetProperty(
                    id="IngestQueue",
                    arn=queue.queue_arn,
                    dead_letter_config=events.CfnRule.DeadLetterConfigProperty(
                        arn=dlq.queue_arn
                    ),
                    # EventBridge's own defaults, and its maximums, set explicitly.
                    retry_policy=events.CfnRule.RetryPolicyProperty(
                        maximum_retry_attempts=185,
                        maximum_event_age_in_seconds=24 * 3600,
                    ),
                )
            ],
        )
        events_principal = iam.ServicePrincipal("events.amazonaws.com")
        keys = {}
        for q in (queue, dlq):
            added = q.add_to_resource_policy(
                iam.PolicyStatement(
                    sid="SapEventsRule",
                    principals=[events_principal],
                    actions=["sqs:SendMessage"],
                    resources=[q.queue_arn],
                    conditions={"ArnEquals": {"aws:SourceArn": rule.attr_arn}},
                )
            )
            if not added.statement_added:
                raise ValueError(
                    "subscribe: the ingest queue and its DLQ must be defined in this "
                    "stack, so their policies can admit the rule"
                )
            if q.encryption_master_key is not None:
                keys[q.encryption_master_key.node.path] = q.encryption_master_key
        # Held to this account's rules: a condition on the rule's own ARN would make
        # key, rule and queue a cycle, and the queue policy above is what admits only
        # this rule.
        for key in keys.values():
            key.add_to_resource_policy(
                iam.PolicyStatement(
                    principals=[events_principal],
                    actions=["kms:Decrypt", "kms:GenerateDataKey"],
                    resources=["*"],
                    conditions={"StringEquals": {"aws:SourceAccount": stack.account}},
                )
            )
        # What verify compares the bus with. A stack can create a parameter only under a
        # name nobody holds, and the layer alerts on any other writer under its prefix.
        ssm.StringParameter(
            stack,
            "SapSubscription",
            parameter_name=(
                f"{self.ctx.ssm_prefix}/layers/{names.NAME}/subscriptions/{self.use_case}"
            ),
            description=trusted_description(stack),
            string_value=stack.to_json_string(
                {
                    "rule_arn": rule.attr_arn,
                    "queue_arn": queue.queue_arn,
                    "dlq_arn": dlq.queue_arn,
                }
            ),
        )
        return rule

    def egress_security_group_id(self, stack: cdk.Stack) -> str:
        """The group that reaches SAP's ports, for a direct caller in the platform VPC."""
        return self.ctx.ssm_value(
            stack, f"layers/{names.NAME}/egress-security-group-id"
        )
