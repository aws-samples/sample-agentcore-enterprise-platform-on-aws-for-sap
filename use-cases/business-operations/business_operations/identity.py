# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""What changes with the platform's identity mode (internal design: direct-mode.md).

**brokered** (setup A): Cognito issues every token. This use case creates its console
and delegating clients and its groups on the platform pool, and Cedar keys on the
`uc-business-operations/*` scopes of the platform-created Gateway clients.

**direct** (setup B): Entra issues every token and there is no pool. The operator
creates the app registrations (`entra` in this use case's config, and the Gateway
clients under the platform's `identity.use_case_clients`), groups become app roles on
the platform app (read from `roles`), and Cedar keys on the calling app's `azp`,
because an Entra token carries no `scope` tag (measured on the OBO spike).

A person's own token reaches the Gateway only when an `obo` SAP system needs it
(`forwards_person_token`): queued and autonomous runs have no person, so they keep the
`writer` app's token and can use only the service-account flows.
"""

from __future__ import annotations

from business_operations import names
from business_operations.config import BusinessOperationsConfig
from infra_utils.use_case_context import UseCaseContext

NAME = names.NAME
#: Where a person's app roles are in an Entra access token.
GROUP_CLAIMS_DIRECT = "roles"
#: The console token, forwarded beside the delegating bearer (runtime allowlist).
USER_TOKEN_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-User-Token"
_ENTRA_FIELDS = (
    "console_client_id",
    "api_scope",
    "delegating_client_id",
    "delegating_secret_name",
)


def direct(ctx: UseCaseContext) -> bool:
    return ctx.platform.identity.mode == "direct"


def require(ctx: UseCaseContext, cfg: BusinessOperationsConfig) -> None:
    """Refuse at synth what the mode cannot deploy."""
    set_fields = [f for f in _ENTRA_FIELDS if getattr(cfg.entra, f)]
    if not direct(ctx):
        if set_fields:
            raise ValueError(
                f"use_cases.{NAME}.entra is for identity.mode direct; brokered mode "
                f"creates these clients on the user pool (set: {', '.join(set_fields)})"
            )
        return
    missing = [f for f in _ENTRA_FIELDS if not getattr(cfg.entra, f)]
    if missing:
        raise ValueError(
            f"identity.mode direct: use_cases.{NAME}.entra needs "
            f"{', '.join(missing)} — the Entra app registrations "
            "docs/direct-mode.md has you create"
        )


def platform_app_id(ctx: UseCaseContext) -> str:
    """The app every direct-mode token for the Gateway and the API is addressed to."""
    return ctx.platform.identity.client_id


def token_url(ctx: UseCaseContext) -> str:
    return f"https://login.microsoftonline.com/{ctx.platform.identity.tenant_id}/oauth2/v2.0/token"


def forwards_person_token(ctx: UseCaseContext, cfg: BusinessOperationsConfig) -> bool:
    """A live run carries the person's token to the Gateway: only for an obo system."""
    return direct(ctx) and any(s.flow == "obo" for s in cfg.sap.systems.values())


def person_app(ctx: UseCaseContext, cfg: BusinessOperationsConfig) -> str | None:
    """The console app a person's token carries as `azp`, when that token reaches SAP."""
    return cfg.entra.console_client_id if forwards_person_token(ctx, cfg) else None


def cedar_principals(
    ctx: UseCaseContext, cfg: BusinessOperationsConfig
) -> dict[str | None, list[str]] | None:
    """scope -> the app ids (`azp`) holding it, for the Cedar condition (sap_kit.cedar_policies.principal_condition).

    None in brokered mode. The `None` key is the obo read permit's: a person's console
    token, since an app-only token fails the exchange anyway. When the person's token is
    forwarded, every tool a live run calls sees the console app, so it holds both scopes.
    """
    if not direct(ctx):
        return None
    platform = ctx.platform
    apps = platform.identity.use_case_clients.get(NAME, {})
    held = platform.use_case_gateway_clients().get(NAME, {})
    console = cfg.entra.console_client_id
    person = [console] if forwards_person_token(ctx, cfg) else []
    scopes = sorted({s for granted in held.values() for s in granted})
    out: dict[str | None, list[str]] = {
        s: [apps[c].client_id for c in sorted(held) if s in held[c]] + person
        for s in scopes
    }
    out[None] = [console]
    return out
