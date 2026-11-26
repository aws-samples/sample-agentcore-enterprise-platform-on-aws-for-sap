# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
r"""Names a use case or layer gives AWS resources, checked at synth against service limits.

A name that is too long fails CreateAgentRuntime or CreatePolicy at deploy, and a
tool name that is too long fails the model call at run time, long after the
deploy went green. Each helper here checks one kind of name and raises a
ValueError saying which setting to shorten.

The limits come from botocore's service models (botocore 1.43.123):

- bedrock-agentcore-control, CreateAgentRuntime ``agentRuntimeName``
  (shape AgentRuntimeName): pattern ``[a-zA-Z][a-zA-Z0-9_]{0,47}``
- bedrock-agentcore-control, CreatePolicy ``name`` (shape PolicyName):
  min 1, max 48, pattern ``[A-Za-z][A-Za-z0-9_]*``. Unique per account and Region
- bedrock-agentcore-control, CreateGatewayTarget ``name`` (shape TargetName):
  pattern ``([0-9a-zA-Z][-]?){1,100}``
- bedrock-runtime, Converse ``ToolSpecification.name`` (shape ToolName):
  min 1, max 64, pattern ``[a-zA-Z0-9_-]+``
- bedrock-agent, CreateKnowledgeBase and CreateDataSource ``name`` (shape Name):
  pattern ``([0-9a-zA-Z][_-]?){1,100}``
- bedrock, CreateGuardrail ``name``: min 1, max 50, pattern ``[0-9a-zA-Z-_]+``
- bedrock-agentcore-control, CreateOauth2CredentialProvider ``name`` (shape
  CredentialProviderName): pattern ``[a-zA-Z0-9\-_]+``, max 128
- appsync, CreateApi ``name`` (shape ApiName): pattern ``[A-Za-z0-9_\-\ ]+``, max 50

use-cases/business-operations/tests/test_business_operations.py checks that these constants still match the
models.
"""

from __future__ import annotations

import hashlib
import re

RUNTIME_NAME_MAX = 48
POLICY_NAME_MAX = 48
TOOL_NAME_MAX = 64
GUARDRAIL_NAME_MAX = 50
CREDENTIAL_PROVIDER_NAME_MAX = 128
EVENT_API_NAME_MAX = 50
RUNTIME_NAME = re.compile(r"[a-zA-Z][a-zA-Z0-9_]{0,47}")
POLICY_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
TARGET_NAME = re.compile(r"([0-9a-zA-Z][-]?){1,100}")
TOOL_NAME = re.compile(r"[a-zA-Z0-9_-]+")
KB_NAME = re.compile(r"([0-9a-zA-Z][_-]?){1,100}")
CREDENTIAL_PROVIDER_NAME = re.compile(r"[a-zA-Z0-9\-_]+")
EVENT_API_NAME = re.compile(r"[A-Za-z0-9_\-\ ]+")
GUARDRAIL_NAME = re.compile(r"[0-9a-zA-Z-_]+")

# The Gateway shows the model each tool as `{target}___{tool}`.
TOOL_SEPARATOR = "___"
HASH_LEN = 8


def tool_name(target: str, tool: str) -> str:
    """The tool name the model sees for `tool` on Gateway target `target`."""
    if not TARGET_NAME.fullmatch(target):
        raise ValueError(
            f"Gateway target name {target!r} does not match {TARGET_NAME.pattern}: "
            "letters and digits, single hyphens between them, at most 100 characters."
        )
    full = f"{target}{TOOL_SEPARATOR}{tool}"
    if len(full) > TOOL_NAME_MAX or not TOOL_NAME.fullmatch(full):
        raise ValueError(
            f"Tool name {full!r} is {len(full)} characters; Amazon Bedrock Converse allows "
            f"{TOOL_NAME_MAX} matching {TOOL_NAME.pattern}. Shorten the target "
            f"name ({len(target)}) or the tool name ({len(tool)})."
        )
    return full


def deployment_hash(project: str, environment: str, owner: str) -> str:
    """8 hex characters naming one deployment of `owner` (a use case or layer).

    For names unique per account and Region. The input must never change: a new
    hash renames, and so replaces, every resource named with it.
    """
    return hashlib.sha256(f"{project}_{environment}/{owner}".encode()).hexdigest()[
        :HASH_LEN
    ]


def policy_name(project: str, environment: str, owner: str, policy: str) -> str:
    """A Cedar policy name unique to this deployment and `owner`: `<policy>_<hash>`.

    CreatePolicy names are unique per account and Region, across every policy
    engine, so the name carries the deployment hash rather than a readable prefix,
    which alone would use most of the 48 characters.
    """
    name = f"{policy}_{deployment_hash(project, environment, owner)}"
    if len(name) > POLICY_NAME_MAX or not POLICY_NAME.fullmatch(name):
        raise ValueError(
            f"Cedar policy name {name!r} is {len(name)} characters; CreatePolicy "
            f"allows {POLICY_NAME_MAX} matching {POLICY_NAME.pattern}. Policy "
            f"{policy!r} must start with a letter, use letters, digits and "
            f"underscores, and be at most {POLICY_NAME_MAX - HASH_LEN - 1} characters."
        )
    return name


def checked(name: str, pattern: re.Pattern, limit: int, what: str) -> str:
    """`name` when it fits `pattern` and `limit`; otherwise a ValueError naming the fix."""
    if len(name) > limit or not pattern.fullmatch(name):
        raise ValueError(
            f"{what} name {name!r} is {len(name)} characters; the limit is {limit} "
            f"matching {pattern.pattern}. Shorten `project` or `environment` in "
            "platform.yaml."
        )
    return name
