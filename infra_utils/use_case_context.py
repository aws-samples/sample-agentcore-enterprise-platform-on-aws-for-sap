# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""What app.py hands every use case's and layer's ``build(app, ctx, config)``.

A use case reaches the platform through SSM parameters and tokens
(docs/PLATFORM_INTERFACE.md), never through core stack objects. ``ctx`` carries only
what it needs to name and place its own resources so they match the platform's:

- the deployment's names: ``project``, ``environment``, ``prefix`` (stack names),
  ``ssm_prefix`` (where the platform publishes), ``region`` and ``cdk_env``;
- ``platform``, the validated platform.yaml, for settings a use case follows
  (identity mode, Cedar, the gateway's interceptor extensions);
- the settings the platform's own stacks were built with, already resolved from
  platform.yaml, the environment and CDK context. Read these here rather than from
  ``platform``, so a use case agrees with the platform when an override is set:
  ``production`` (keep data, harden; ``deployment.mode: production``),
  ``log_retention_days``, ``model_id`` and the runtime settings ``runtime_kwargs()``
  returns;
- ``ssm_value`` and ``ssm_ref``, the two ways to read a parameter the platform, a
  layer or the use case itself publishes.

It is frozen: a use case cannot change what the next one is given. Fields are only
ever added, so a use case written against an older platform keeps building. A use
case written for the dict ``ctx`` of interface 2 also keeps building: ``ctx["prefix"]``
reads the same value as ``ctx.prefix``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import aws_cdk as cdk


@dataclass(frozen=True)
class UseCaseContext:
    project: str
    environment: str
    platform: Any  # infra_utils.platform_config.PlatformConfig
    region: str = "us-east-1"
    cdk_env: cdk.Environment | None = None
    production: bool = False
    log_retention_days: int = 30
    model_id: str = ""  # agents.model_id; empty means each agent's own default
    allowed_models: tuple[str, ...] = ()
    require_guardrails: bool = False
    cognito_issuer_url: str = ""
    # network_mode, subnet_ids and security_group_ids when the runtimes run in the
    # platform's VPC; empty otherwise.
    network: dict[str, Any] = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        """Read-only mapping access, for use cases written for interface 2's dict."""
        if key in ("prefix", "ssm_prefix") or key in {f.name for f in fields(self)}:
            return getattr(self, key)
        raise KeyError(key)

    @property
    def prefix(self) -> str:
        """``<project>-<environment>``, the start of every stack name."""
        return f"{self.project}-{self.environment}"

    @property
    def ssm_prefix(self) -> str:
        """``/<project>/<environment>``, under which the platform publishes."""
        return f"/{self.project}/{self.environment}"

    def ssm_value(self, scope, name: str) -> str:
        """The parameter ``<ssm_prefix>/<name>``, resolved at deploy.

        A template parameter, so the stack synthesizes without the platform deployed.
        Asking twice in one stack returns the same token.
        """
        from aws_cdk import aws_ssm as ssm

        return ssm.StringParameter.value_for_string_parameter(
            scope, f"{self.ssm_prefix}/{name}"
        )

    def ssm_ref(self, name: str) -> str:
        """``<ssm_prefix>/<name>`` as a ``{{resolve:ssm:...}}`` dynamic reference.

        For a property CloudFormation resolves in place, such as a runtime's
        environment, where a template parameter would be one more stack input.
        """
        return f"{{{{resolve:ssm:{self.ssm_prefix}/{name}}}}}"

    def runtime_kwargs(self) -> dict[str, Any]:
        """RuntimeStack keyword arguments that build a runtime as the platform's are."""
        return {
            "allowed_models": list(self.allowed_models),
            "require_guardrails": self.require_guardrails,
            "guardrail_floor": self.production,
            "retain_data": self.production,
            "log_retention_days": self.log_retention_days,
            "cognito_issuer_url": self.cognito_issuer_url,
            **self.network,
        }
