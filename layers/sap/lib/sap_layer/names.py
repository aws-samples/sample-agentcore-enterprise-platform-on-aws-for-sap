# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The names the SAP layer gives AWS resources, checked at synth against service limits.

Target and tool names are pure functions of the system name, so a use case computes
them at synth from platform.yaml; it needs SSM only at run time.

Some names here were first created by the business-operations use case, before the
layer existed, and AWS fixes them at create: the runtime name, the execution role's
name and the providers'. They keep that use case's hash seed (`HASH_SEED`) and their
`business-operations` text forever; a new seed would replace every runtime and role.
"""

from __future__ import annotations

import hashlib

from sap_kit.names import (
    RUNTIME_NAME,
    TOOL_NAME_MAX,
    TOOL_SEPARATOR,
    deployment_hash,
    tool_name,
)

NAME = "sap"
#: Historical: the owner the create-only names were first hashed for. Never change it.
HASH_SEED = "business-operations"
#: Historical: the text in the descriptions of the resources the layer took over.
#: A stack refactor moves a resource only with its properties unchanged.
LEGACY_LABEL = "business-operations"
#: Historical: app.py's cost tag on those resources, their first stack's suffix.
LEGACY_COMPONENT = "uc-business-operations-sap"

# The AWS for SAP MCP Server tools behind each SAP target. The longest one sets how
# long a SAP system name can be.
SAP_TOOLS = (
    "find_sap_services",
    "get_metadata",
    "get_service_hints",
    "odata_count",
    "odata_create",
    "odata_function_import",
    "odata_read",
    "odata_update",
)
#: The ones that change a document in SAP. `odata_delete` is in neither list: the
#: layer's check refuses it for every caller, and no permit may name it.
SAP_WRITE_TOOLS = ("odata_create", "odata_function_import", "odata_update")
SAP_READ_TOOLS = tuple(t for t in SAP_TOOLS if t not in SAP_WRITE_TOOLS)
#: The read tools in the order the server lists them (1.1.0-958, tools/list over
#: SigV4) and the read permits name them: a reorder rewrites every deployed permit.
LISTED_READ_TOOLS = (
    "find_sap_services",
    "get_metadata",
    "get_service_hints",
    "odata_read",
    "odata_count",
)
#: Each write operation, as `writes` and a claim name it, and its tool.
WRITE_TOOLS = {
    "create": "odata_create",
    "update": "odata_update",
    "function_import": "odata_function_import",
}
TARGET_PREFIX = "sap-"
#: The targets' first name. The layer's check may list it beside TARGET_PREFIX while
#: a deployment renames them, so no SAP call is unchecked in between.
LEGACY_TARGET_PREFIX = "business-operations-sap-"
#: Set when the targets were `business-operations-sap-<system>`, where Converse's
#: 64-character tool names left 16. Kept, so a system name valid before the rename
#: is valid after it and no other limit is ever the one that bites.
SAP_SYSTEM_MAX = 16


def sap_target(system: str) -> str:
    """The Gateway target name for one SAP system, `<TARGET_PREFIX><system>`.

    Every SAP tool on it must still fit Converse's limit, which the
    SAP_SYSTEM_MAX characters a system name may have always leave room for.
    """
    if len(system) > SAP_SYSTEM_MAX:
        raise ValueError(
            f"SAP system name {system!r} is {len(system)} characters; at most "
            f"{SAP_SYSTEM_MAX} fit (the model sees "
            f"'{TARGET_PREFIX}<system>{TOOL_SEPARATOR}<tool>', which Amazon Bedrock "
            f"Converse caps at {TOOL_NAME_MAX})."
        )
    target = f"{TARGET_PREFIX}{system}"
    for tool in SAP_TOOLS:
        tool_name(target, tool)
    return target


def event_source(system: str) -> str:
    """The `source` of a SAP system's events on the bus, `sap.<system>`: at most 20
    characters. Only that system's sender role may put it."""
    sap_target(system)
    return f"{NAME}.{system}"


def event_bus_name(prefix: str) -> str:
    """The deployment's one `sap-events` bus, `<project>-<env>-sap-events`. Fixed, so a
    layer redeploy never replaces the bus out from under the use cases' rules."""
    return f"{prefix}-{NAME}-events"


def _hash(project: str, environment: str) -> str:
    return deployment_hash(project, environment, HASH_SEED)


def sap_runtime_name(project: str, environment: str, system: str) -> str:
    """The AWS for SAP MCP Server runtime for one system, `bo_sap_<system>_<hash>`.

    Runtime names are unique per account and Region, so the name carries the
    deployment hash; at most 32 characters with a 16-character system name.
    """
    name = f"bo_sap_{system.replace('-', '_')}_{_hash(project, environment)}"
    if not RUNTIME_NAME.fullmatch(name):
        raise ValueError(
            f"SAP runtime name {name!r} does not match {RUNTIME_NAME.pattern}"
        )
    return name


def sap_provider_name(project: str, environment: str, system: str) -> str:
    """The token-vault OAuth2 provider of one system's runtime (m2m and obo flows).

    The account has one vault, so the name carries the deployment hash too.
    """
    return f"{LEGACY_LABEL}-sap-{system}-{_hash(project, environment)}"


def sap_gateway_provider_name(project: str, environment: str, system: str) -> str:
    """The provider the Gateway exchanges a user's token through (flow obo)."""
    return f"{sap_provider_name(project, environment, system)}-gw"


# AWS's template names its execution role this, with a `^[a-z0-9]{1,8}$` id, and
# the image's repository policy admits a pull only by a role so named.
SAP_ROLE_PREFIX = "AWSForSAP-MCP-Server-ExecutionRole-"


def sap_role_name(project: str, environment: str, system: str) -> str:
    """`AWSForSAP-MCP-Server-ExecutionRole-<id>`, id an 8-hex hash of the deployment and system.

    IAM role names are unique per account, and the id must never change: a new
    name replaces the role, and with it the runtime.
    """
    role_id = hashlib.sha256(
        f"{project}_{environment}/{HASH_SEED}/{system}".encode()
    ).hexdigest()[:8]
    return SAP_ROLE_PREFIX + role_id
