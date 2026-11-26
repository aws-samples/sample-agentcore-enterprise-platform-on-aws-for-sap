# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The `use_cases.business-operations:` block of platform.yaml.

app.py hands that block to build() as a plain dict; this model is what
validates it. Unknown keys are refused, so a typo fails the synth instead of
being ignored. A key joins the model when code reads it, with its default here.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    field_validator,
    model_validator,
)
from sap_layer.config import SapConfig

from infra_utils.use_case_context import UseCaseContext


class AgentKnowledgeConfig(BaseModel):
    """Precedent and vendor risk on Aurora Serverless v2. Off: it has a standing cost."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    # 0 lets the cluster pause when idle (storage cost only), at a ~20 s resume stall.
    min_acu: float = Field(0, ge=0, le=256)
    max_acu: float = Field(4, ge=1, le=256)
    seconds_until_auto_pause: int = Field(3600, ge=300, le=86400)

    @model_validator(mode="after")
    def _min_below_max(self):
        if self.min_acu > self.max_acu:
            raise ValueError("agent_knowledge.min_acu must not exceed max_acu")
        return self


class KnowledgeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Amazon Bedrock knowledge base over the SOP bucket, behind search_sap_sops.
    sop_search: bool = True
    # Amazon Bedrock knowledge base over example OpenAPI specs, behind search_sap_api_docs.
    # Off: the AWS for SAP MCP Server's get_metadata reads the live system's fields.
    api_docs_search: bool = False
    agent_knowledge: AgentKnowledgeConfig = AgentKnowledgeConfig()


class ApiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # API Gateway's CloudWatch role is one setting per account and Region, and stage
    # logging fails without it. This stack never claims it unless told to, because
    # another stack may own it already: `aws apigateway get-account` shows whether
    # one is set. Exactly one stack in the account and Region may set this true.
    set_account_cloudwatch_role: bool = False
    # The sample ticketing system on its own API. A stand-in for a real ITSM (ServiceNow,
    # Jira): replace it before production.
    sample_ticketing: bool = True
    # POST /erp-events, SAP pushing exceptions, on its own API so it can be made
    # private without touching the console's. Off: the poller needs no inbound route.
    erp_events_endpoint: bool = False


class EntraConfig(BaseModel):
    """Under identity.mode direct only: this use case's own Entra app registrations.

    Entra issues every token there and CloudFormation cannot create the apps
    (docs/direct-mode.md). The Gateway clients (`reader`, `writer`)
    are the platform's to publish (`identity.use_case_clients`); these are the ones
    only this use case uses.
    """

    model_config = ConfigDict(extra="forbid")
    # The console's single-page app: authorization code with PKCE, no secret.
    console_client_id: str = ""
    # The delegated scope on the platform app the console asks for, e.g.
    # api://<platform app id>/business-operations. Its token is what the API, the run
    # channel and (with an obo SAP system) the Gateway accept.
    api_scope: str = ""
    # Held only by POST /runs and the invoker; the runtime admits this app alone.
    delegating_client_id: str = ""
    # Secrets Manager secret NAME holding that app's client secret as a plain string.
    delegating_secret_name: str = ""


_EMAIL = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")


class NotificationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # `ses` mails a person; `tickets` files the notification in the sample ticketing
    # table (needs api.sample_ticketing). Unset, the config resolves it: `ses` with a
    # sender, else `tickets` while sample ticketing is on, since an SES sender is a
    # manual setup step and a fresh deployment would otherwise park every case.
    channel: Literal["ses", "tickets"] | None = None
    # The verified SES identity mail is sent from. Unset, every send is a channel
    # failure and raises the NotificationFailure alarm.
    ses_sender_email: str | None = None
    # Where SES may send. Empty means the sender's own domain only: recipients come
    # from case data, which is unsanitized, and this list, which the notification
    # Lambda holds in every Cedar mode, is what stands between a crafted SAP field
    # and an outbound email.
    recipient_domains: list[str] = []

    @field_validator("ses_sender_email")
    @classmethod
    def _is_email(cls, v: str | None) -> str | None:
        if v is not None and not _EMAIL.match(v):
            raise ValueError(f"ses_sender_email {v!r} is not an email address")
        return v


# `POST /erp-events` is `api.erp_events_endpoint`. The deployment's trigger mode is not in `pipeline`:
# it is the SSM parameter `PUT /deployment/trigger-mode` writes, absent (and so `manual`) until set.
# An EventBridge schedule expression. Anything else fails at deploy, not at synth.
Schedule = Annotated[str, Field(pattern=r"^(rate|cron)\(.+\)$")]


class IngestConfig(BaseModel):
    """The consumer of the use case's one ingest queue, which SAP's events (the SAP
    layer's `sap-events` bus, `layers.sap.access.business-operations.ingest`) and
    `POST /erp-events` both fill."""

    model_config = ConfigDict(extra="forbid")
    # Batches the consumer drains at once, which bounds the SAP reads a burst of
    # nudges can cause. SQS's floor is 2.
    max_concurrency: int = Field(2, ge=2, le=10)

    @model_validator(mode="before")
    @classmethod
    def _senders_moved(cls, raw):
        if isinstance(raw, dict) and "senders" in raw:
            raise ValueError(
                "pipeline.ingest.senders moved to the SAP layer: list each system's "
                "principals under layers.sap.systems.<system>.senders, and the systems "
                "under layers.sap.access.business-operations.ingest "
                "(layers/sap/walkthrough.md)"
            )
        return raw


class PipelineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # How many agent runs execute at once: the knob for Amazon Bedrock throttling and SAP
    # load. SQS's floor is 2.
    max_concurrency: int = Field(5, ge=2, le=1000)
    # In `auto`, a burst for one pack sends its first case, then the rest this many
    # seconds later, so they read the prompt cache the first one wrote. 0 disables.
    warm_delay_seconds: int = Field(15, ge=0, le=120)
    # Every poll re-reads each pack's whole open backlog from SAP (about 2,500 records on
    # the demo system), so 15 minutes, not 5. Invoking the poller Lambda polls at once.
    poller_schedule: Schedule = "rate(15 minutes)"
    # The slower run that polls only the packs with `source.poll: sweep`: those SAP
    # pushes, so the poll only catches what a sender lost.
    sweep_schedule: Schedule = "rate(1 hour)"
    # The sweeper re-queues cases left `detected`; a no-op under `manual`.
    batch_schedule: Schedule = "rate(1 hour)"
    exemplar_schedule: Schedule = "rate(1 day)"
    # Only records created on or after this date become cases, in every pack that names
    # `source.created_field`: for a lab or demo stack on a shared sandbox, whose backlog
    # (about 2,900 blocked invoices on the demo system) would otherwise all become cases.
    poll_created_since: date | None = None
    ingest: IngestConfig = Field(default_factory=IngestConfig)


class PromotionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # `development` mints pack versions and lets pack-admins change a pack's structure;
    # `quality` and `production` only import versions, and need a change reference to.
    # Unset: `production` under `deployment.mode: production`, `development` otherwise,
    # so only a QAS stack sets it.
    role: Literal["development", "quality", "production"] | None = None


class RuntimeConfig(BaseModel):
    """The agent runtime. The `sonnet` tier is the platform's `agents.model_id`."""

    model_config = ConfigDict(extra="forbid")
    # The `haiku` tier, for packs that set `model_tier: haiku`. Checked against
    # `agents.allowed_models` at synth. Unset: Claude Haiku, or the `sonnet` tier's
    # model when the allowlist leaves Haiku out.
    haiku_model_id: str | None = None
    # The `opus` tier. Unset: Claude Opus when the allowlist allows it; otherwise the
    # tier is not served, and a pack naming it fails the synth.
    opus_model_id: str | None = None
    # The model's output ceiling per call. Below it the closing answer truncates, the
    # terminal update_case_state is never written, and a finished case parks for review.
    max_tokens: int = Field(8192, ge=1024, le=128000)
    # Prompts, model responses and tool payloads, as span events, which is what the
    # evaluators read; off redacts them, and every judge scores nothing. Unset: on outside
    # `deployment.mode: production`, off in it. The risk when on: SAP document
    # contents, vendor names and operators' words land in CloudWatch in plaintext, as
    # event records in the runtime's own log group (stream `otel-rt-logs`), kept for
    # `observability.log_retention_days`. Whoever CloudWatch IAM lets read that group
    # reads them, not by pack entitlement. The spans in `aws/spans` keep only metadata.
    log_model_content: bool | None = None


class AlarmsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # The cost alarm's threshold: p90 estimated cost per run over an hour, in USD.
    # Set it a few times above your packs' own p90, read from the run metrics: the
    # default suits a haiku-tier pack, and a sonnet-tier pack needs a higher figure.
    cost_p90_usd: float = Field(0.10, gt=0)


class SeedDataConfig(BaseModel):
    """The seeder `ops.py seed <pack>` invokes over IAM: posts real SAP documents (a
    PO, a goods receipt, a blocked invoice) on S/4HANA sample data, for workshops and
    demos. Off: never against a customer's SAP by default."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool = False


class ConsoleFeaturesConfig(BaseModel):
    """Console feature flags, written into the console's `aws-exports.json` as
    `features` with every flag present (`console.exports`). The console only reads them:
    a flag shows or hides a view and grants nothing, because every route still decides
    for itself. None is defined today, so the block takes no key."""

    model_config = ConfigDict(extra="forbid")


#: One line of plain text: not blank, no surrounding space, no markup brackets and no
#: control character (a line break included). The console renders it as text either way.
_PLAIN = r"^[^\s<>\x00-\x1f\x7f](?:[^<>\x00-\x1f\x7f]*[^\s<>\x00-\x1f\x7f])?$"


class ConsoleBrandConfig(BaseModel):
    """What the console calls itself, written into `aws-exports.json` as `brand`, so a
    deployment renames it without a code change. Public, as the sign-in page is."""

    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, Field(min_length=1, max_length=40, pattern=_PLAIN)] = (
        "Business Operations"
    )
    subtitle: Annotated[str, Field(min_length=1, max_length=80, pattern=_PLAIN)] = (
        "SAP exception handling"
    )
    # The console's mark, 1 to 3 letters or digits in any script; unset, the console
    # draws its own. Written only when set.
    monogram: StrictStr | None = None

    @field_validator("monogram")
    @classmethod
    def _monogram(cls, value: str | None) -> str | None:
        if value is not None and not (
            1 <= len(value) <= 3 and all(c.isalnum() for c in value)
        ):
            raise ValueError("monogram must be 1 to 3 letters or digits")
        return value


class ConsoleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    features: ConsoleFeaturesConfig = Field(default_factory=ConsoleFeaturesConfig)
    brand: ConsoleBrandConfig = Field(default_factory=ConsoleBrandConfig)


class BusinessOperationsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    knowledge: KnowledgeConfig = KnowledgeConfig()
    api: ApiConfig = Field(default_factory=ApiConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    # Default contacts the SOPs cite as {{CONTACT_<NAME>}}; operators override them
    # through PUT /deployment/settings. Keys are lower snake-case (`ap_team`), the form every
    # check and PUT /deployment/settings compare; an upper-case key is lowered here.
    contacts: dict[str, str] = Field(default_factory=dict)
    notification: NotificationConfig = Field(default_factory=NotificationConfig)
    # Not from this block: the SAP layer's systems this use case may read, set by
    # load() from `layers.sap` (stack.py). Refused here, with where it moved.
    sap: SapConfig = Field(default_factory=SapConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    alarms: AlarmsConfig = Field(default_factory=AlarmsConfig)
    test_data: SeedDataConfig = Field(default_factory=SeedDataConfig)
    # Under identity.mode direct only.
    entra: EntraConfig = Field(default_factory=EntraConfig)
    promotion: PromotionConfig = Field(default_factory=PromotionConfig)
    console: ConsoleConfig = Field(default_factory=ConsoleConfig)
    # The tiers of tools this use case attaches to the platform's people Gateway
    # (`gateway.people`): `read` (packs, SOPs, rules, versions) and `author` (SOP and
    # pack content, development only). Empty attaches nothing (business_operations/people.py).
    people_tools: list[Literal["read", "author"]] = Field(default_factory=list)
    # A first console user, created with the stack and in every group it creates, so
    # Cognito mails a welcome with a temporary password. Set it on a new stack only: a
    # pool that already holds this email fails the create, and removing the key later
    # deletes the user. Brokered Cognito only.
    admin_email: (
        Annotated[str, Field(pattern=r"^[^@\s\"]+@[^@\s\"]+\.[^@\s\"]+$")] | None
    ) = None

    @property
    def notification_channel(self) -> Literal["ses", "tickets"]:
        """`notification.channel`, or when unset `ses` with a sender, else `tickets`
        while sample ticketing is on. With neither it stays `ses`, a channel failure
        (alarm, synth warning): there is nowhere else to send."""
        n = self.notification
        if n.channel:
            return n.channel
        no_sender = not n.ses_sender_email
        return "tickets" if no_sender and self.api.sample_ticketing else "ses"

    @field_validator("contacts")
    @classmethod
    def _lower_contact_keys(cls, contacts: dict[str, str]) -> dict[str, str]:
        lowered = {k.lower(): v for k, v in contacts.items()}
        if len(lowered) < len(contacts):
            raise ValueError(
                "contacts names one key in two cases; keep the lower-case one"
            )
        return lowered

    @field_validator("people_tools")
    @classmethod
    def _tiers_once(cls, tiers: list[str]) -> list[str]:
        if len(tiers) != len(set(tiers)):
            raise ValueError(f"people_tools names a tier twice: {tiers}")
        return tiers

    @model_validator(mode="after")
    def _tickets_need_sample_ticketing(self):
        if self.notification.channel == "tickets" and not self.api.sample_ticketing:
            raise ValueError(
                "notification.channel 'tickets' needs api.sample_ticketing: true"
            )
        return self


def load(raw: dict | None, sap: SapConfig | None = None) -> BusinessOperationsConfig:
    """The block, validated; `sap` is the SAP layer's view for this use case."""
    if "sap" in (raw or {}):
        raise ValueError(
            "platform.yaml use_cases.business-operations.sap moved to the SAP layer: "
            "put its systems under layers.sap.systems, and add "
            'layers.sap.access.business-operations: {read: "*", write: {<system>: '
            "[function_import]}, bypass_gateway: [<each poller and test-data system>]} "
            "(layers/sap/walkthrough.md)"
        )
    try:
        cfg = BusinessOperationsConfig.model_validate(raw or {})
    except ValueError as e:
        raise ValueError(
            f"platform.yaml use_cases.business-operations is invalid:\n{e}"
        ) from None
    if sap is not None:
        cfg.sap = sap
    return cfg


def promotion_role(ctx: UseCaseContext, promotion: PromotionConfig) -> str:
    """This stack's role, resolved from `deployment.mode` when unset."""
    if promotion.role:
        return promotion.role
    production = ctx.production
    return "production" if production else "development"


def promotion_contradiction(
    ctx: UseCaseContext, promotion: PromotionConfig
) -> str | None:
    """Why this role and `deployment.mode` disagree, or None."""
    if ctx.production and promotion.role == "development":
        return (
            "promotion.role is development under deployment.mode: production, so pack "
            "structure is writable over HTTP and versions are minted here. Unset it, or "
            "set quality or production."
        )
    return None
