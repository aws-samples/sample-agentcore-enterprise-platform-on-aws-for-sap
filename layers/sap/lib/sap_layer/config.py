# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""`layers.sap` in platform.yaml: the SAP systems, and which use case may reach them.

```yaml
layers:
  sap:
    systems:
      s4:
        system_role: sandbox    # required: sandbox | development | quality | production
        base_url: https://sap.example.com/sap/opu/odata/sap/
        client: "100"
        secret_name: sap/s4   # {"username", "password"}
    access:                   # the operator's ceiling; a use case not named gets nothing
      business-operations: {read: "*", write: {s4: [function_import]}, bypass_gateway: [s4]}
```

Each system becomes one AWS for SAP MCP Server runtime and one Gateway target
(`names.sap_target`). `flow` is how that runtime signs in to SAP, always as one
service account at first release:

- `basic`: a SAP user and password, `{"username", "password"}` in the secret.
- `m2m-sap`: SAP's own OAuth server (SOAUTH2) issues a client-credentials token;
  `{"clientId", "clientSecret"}` in the secret, `token_url` and `scopes` set.
- `m2m-oidc`: an IdP SAP trusts issues it; same secret shape, `discovery_url`
  and `scopes` set.
- `obo` (only under the platform's direct Entra mode): SAP runs as the signed-in
  person. The Gateway exchanges the user's token for one addressed to this server's
  own Entra app (`gateway_scopes`), and the server exchanges that again for SAP's
  (`scopes`). The secret is that app's `{"clientId", "clientSecret"}`; `writes`
  default to none here.

`system_role` says what the system is, and has no default: only a `sandbox` may be seeded
with test data or run evals against (a use case's own seeder and eval runner enforce
it), and the operator's word is all it rests on.

`runtime_arn` instead attaches a runtime someone else deployed (bring your own):
nothing is built, and `writes` must say what that server has turned on.

`access.<use case>` caps what that use case may claim (`access.SapAccess.claim`):
`read` and `write` are `"*"` or the systems (and, for `write`, the operations) it
may use through the Gateway; `bypass_gateway` names the `basic`, `m2m-sap` or `m2m-oidc`
systems its own code may call outside the Gateway as that system's service account, with
no claim check (a poller, a test-data seeder).

`odata_version` is the OData version the server reads SAP's catalog and calls services in:
`v2` (the server's default), `v4` (which also needs `service_prefixes`), or `mixed`, where
SAP's catalog stays V2 and a custom `catalog.json` in `catalog_bucket` lists V2 and V4
services, each entry naming its own `ODataVersion`.

`unattended` sets how those direct calls sign in when it should not be the system's own
`flow`, `secret_name` and token settings, the server's: a `basic`, `m2m-sap` or `m2m-oidc`
block of the same fields, checked by the same rules. It is how an `obo` system, whose
server acts for a signed-in person, gets a direct caller at all.

`senders` are the IAM principals (the SAP host's role, an IAM Roles Anywhere role) that
may assume the system's sender role and put its events on the layer's `sap-events` bus.
`access.<use case>.ingest` names the systems whose events that use case may subscribe
to: each must have `senders`, and be in its `bypass_gateway`, since a nudge is answered
by reading SAP directly.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sap_layer import names

# `latest` by default, so a new deployment takes AWS's current server. What it costs:
# CloudFormation sees only the string, so a newly published image reaches a running
# runtime only when something else updates it, and two stacks deployed a week apart
# can run different servers (the version is in each runtime's startup banner). Pin
# `layers.sap.image_tag` to a version tag or a sha256 digest to hold one; the tags
# cannot be listed from outside AWS's registry account, so read it from the banner.
IMAGE_TAG = "latest"
# AWS publishes the image from a different account in each Region. Copied from the
# image URI in each Region's public template,
# https://awsforsap-mcp-server-setup-<region>.s3.<region>.amazonaws.com/cfn-launch-template/latest/AwsForSapMcpServerStack.template.json
# (read 2026-11-03): these are the Regions the server's docs list.
IMAGE_REGISTRY_ACCOUNTS = {
    "us-east-1": "754248135873",
    "us-east-2": "287871536835",
    "us-west-2": "659880308466",
    "ca-central-1": "002904791290",
    "sa-east-1": "890876258295",
    "eu-central-1": "968077439207",
    "eu-west-1": "288507842876",
    "eu-west-2": "398664673480",
    "eu-west-3": "544994275650",
    "eu-south-1": "177258422004",
    "eu-south-2": "167667035049",
    "eu-north-1": "457644036255",
    "ap-south-1": "252303747309",
    "ap-southeast-1": "846898690245",
    "ap-southeast-2": "595260646035",
    "ap-northeast-1": "581351185905",
    "ap-northeast-2": "847283080198",
}


def image_registry_account(region: str) -> str:
    try:
        return IMAGE_REGISTRY_ACCOUNTS[region]
    except KeyError:
        raise ValueError(
            f"The AWS for SAP MCP Server is not published in {region}; "
            f"it is in {', '.join(sorted(IMAGE_REGISTRY_ACCOUNTS))}"
        ) from None


Flow = Literal["basic", "m2m-sap", "m2m-oidc", "obo"]
# odata_delete is not here and cannot be: the layer never offers it to any client.
Write = Literal["create", "update", "function_import"]
SystemRole = Literal["sandbox", "development", "quality", "production"]

MCP_FLOW = {
    "basic": "BASIC",
    "m2m-sap": "M2M",
    "m2m-oidc": "M2M",
    "obo": "ON_BEHALF_OF_TOKEN_EXCHANGE",
}

# The server reads only buckets named so, and `<bucket>/<sub-path>` for a sub-path.
_CATALOG_BUCKET = re.compile(r"awsforsap-mcp-server-[a-z0-9.-]+(/[A-Za-z0-9._-]+)*")
_SYSTEM_NAME = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
_RUNTIME_ARN = re.compile(
    r"arn:aws[a-z-]*:bedrock-agentcore:[a-z0-9-]+:\d{12}:runtime/.+"
)
# What a role's trust policy can name: a role or user, never a whole account.
# What may send SAP's events: a machine's role, never a user or an account.
_SENDER_ARN = re.compile(r"arn:aws[a-z-]*:iam::\d{12}:role/[\w+=,.@/-]+")


def _https(v: str | None) -> str | None:
    if v is not None and not v.startswith("https://"):
        raise ValueError(f"{v!r} must be an https:// URL")
    return v


def _token_settings_missing(s: SapSystem | Unattended) -> list[str]:
    """The settings `s.flow` signs in with that `s` leaves out."""
    missing = []
    if s.flow != "basic" and not s.scopes:
        missing.append("scopes")
    if s.flow == "m2m-sap" and s.token_url is None:
        missing.append("token_url")
    if s.flow == "m2m-oidc" and s.discovery_url is None:
        missing.append("discovery_url")
    return missing


class Unattended(BaseModel):
    """How direct callers sign in, instead of as the system's server does. Never `obo`:
    an unattended call has no signed-in person to act for."""

    model_config = ConfigDict(extra="forbid")

    flow: Literal["basic", "m2m-sap", "m2m-oidc"] = "basic"
    secret_name: str
    scopes: list[str] = Field(default_factory=list)
    token_url: str | None = None
    discovery_url: str | None = None

    _urls = field_validator("token_url", "discovery_url")(_https)

    @model_validator(mode="after")
    def _complete(self) -> Unattended:
        if missing := _token_settings_missing(self):
            raise ValueError(f"unattended flow {self.flow} needs {', '.join(missing)}")
        return self


class SapSystem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Required, with no default (see _complete): a system nobody classified is never
    # taken for a sandbox.
    system_role: SystemRole | None = None
    base_url: str | None = None
    type: Literal["S4HANA", "ECC"] = "S4HANA"
    client: str | None = None
    flow: Flow = "basic"
    secret_name: str | None = None
    # On by default (off under obo, see _complete), the risk called out in the
    # walkthrough: under Cedar LOG_ONLY only the interceptor checks stand between a
    # claimed client and a SAP write.
    writes: list[Write] | None = None
    scopes: list[str] = Field(default_factory=list)
    # obo: what the Gateway asks Entra for, addressed to this server's own app.
    gateway_scopes: list[str] = Field(default_factory=list)
    token_url: str | None = None
    discovery_url: str | None = None
    service_prefixes: list[str] = Field(default_factory=list)
    odata_version: Literal["v2", "v4", "mixed"] = "v2"
    # The S3 bucket, and optional sub-path, of the server's custom catalog.json.
    catalog_bucket: str | None = None
    runtime_arn: str | None = None
    # How direct callers sign in; absent, as this system's server does.
    unattended: Unattended | None = None
    # Who may assume this system's sender role and put its events on the bus.
    senders: list[str] = Field(default_factory=list)

    _urls = field_validator("base_url", "token_url", "discovery_url")(_https)

    @model_validator(mode="before")
    @classmethod
    def _renamed_keys(cls, data: object) -> object:
        # A platform.yaml written before the rename names the old key; say what it became
        # instead of only "extra inputs are not permitted".
        if isinstance(data, dict) and "landscape" in data:
            raise ValueError("`landscape` is now `system_role`; rename the key")
        return data

    @field_validator("senders")
    @classmethod
    def _principals(cls, v: list[str]) -> list[str]:
        # A machine's role only: never an account, never an IAM user (its access keys
        # would sit on the SAP host for good), and never an IAM Identity Center role,
        # which is a person. What a sender puts on the bus starts cases.
        if bad := [p for p in v if not _SENDER_ARN.fullmatch(p)]:
            raise ValueError(
                f"senders {bad}: each must be an IAM role ARN, "
                "arn:aws:iam::<account>:role/<name> (not a user or an account)"
            )
        if people := [p for p in v if "/aws-reserved/" in p or "AWSReservedSSO_" in p]:
            raise ValueError(
                f"senders {people}: an IAM Identity Center role is a person, not a host"
            )
        return v

    @field_validator("catalog_bucket")
    @classmethod
    def _bucket(cls, v: str | None) -> str | None:
        if v is not None and not _CATALOG_BUCKET.fullmatch(v):
            raise ValueError(
                f"catalog_bucket {v!r}: a bucket name starting awsforsap-mcp-server-, "
                "optionally /<sub-path>, without s3://"
            )
        return v

    @model_validator(mode="after")
    def _complete(self) -> SapSystem:
        if self.system_role is None:
            raise ValueError(
                "system_role is required: sandbox, development, quality or production"
            )
        if self.writes is None:
            # Under obo no Cedar statement can grant a write yet (the scope tag
            # an Entra token carries is unmeasured), so none is listed.
            self.writes = (
                [] if self.flow == "obo" else ["create", "update", "function_import"]
            )
        if self.gateway_scopes and self.flow != "obo":
            raise ValueError("gateway_scopes is for flow obo only")
        if self.odata_version == "mixed" and self.catalog_bucket is None:
            raise ValueError(
                "odata_version mixed needs catalog_bucket: the custom catalog is "
                "where each service names its version"
            )
        # AWS's deployment guide: under V4, find_sap_services needs a prefix filter.
        if self.odata_version == "v4" and not self.service_prefixes:
            raise ValueError(
                'odata_version v4 needs service_prefixes (["*"] for every service)'
            )
        if self.runtime_arn is not None:
            if self.odata_version != "v2" or self.catalog_bucket:
                raise ValueError(
                    "odata_version and catalog_bucket set up a server this layer "
                    "builds; a runtime_arn server's owner sets its own"
                )
            if not _RUNTIME_ARN.fullmatch(self.runtime_arn):
                raise ValueError(
                    f"runtime_arn {self.runtime_arn!r} is not an AgentCore runtime ARN"
                )
            return self
        missing = [k for k in ("base_url", "secret_name") if getattr(self, k) is None]
        missing += _token_settings_missing(self)
        if self.flow == "obo" and not self.gateway_scopes:
            missing.append("gateway_scopes")
        if missing:
            raise ValueError(f"flow {self.flow} needs {', '.join(missing)}")
        return self

    @property
    def direct_secret_name(self) -> str | None:
        """The secret a direct caller signs in with: `unattended`'s, else the system's."""
        return (self.unattended or self).secret_name

    def direct_sign_in(self) -> dict:
        """What a direct caller needs beyond its secret to sign in (`sap_http.system`
        reads it), from `unattended` when set: nothing for `basic`, the token flow's
        settings otherwise."""
        s = self.unattended or self
        if s.flow == "basic":
            return {}
        return {
            "flow": s.flow,
            "token_url": s.token_url,
            "discovery_url": s.discovery_url,
            "scopes": s.scopes,
        }


Systems = Literal["*"] | list[str]
Writes = Literal["*"] | dict[str, list[Write]]
_USE_CASE = re.compile(r"[a-z][a-z0-9-]{2,40}")


class Access(BaseModel):
    """One use case's ceiling. Absent from `access`: that use case gets nothing."""

    model_config = ConfigDict(extra="forbid")

    read: Systems = Field(default_factory=list)
    write: Writes = Field(default_factory=dict)
    bypass_gateway: list[str] = Field(default_factory=list)
    ingest: list[str] = Field(default_factory=list)


class SapConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image_tag: str = IMAGE_TAG
    systems: dict[str, SapSystem] = Field(default_factory=dict)
    access: dict[str, Access] = Field(default_factory=dict)

    @field_validator("image_tag")
    @classmethod
    def _tag_or_digest(cls, v: str) -> str:
        if v.startswith("sha256:") and not re.fullmatch(r"sha256:[0-9a-f]{64}", v):
            raise ValueError(f"image_tag {v!r} is not a sha256 digest")
        return v

    @property
    def image_ref(self) -> str:
        """`:<tag>`, or `@sha256:<digest>` when `image_tag` is a digest. A tag can be
        re-pushed under every deployment; a digest cannot. AWS publishes neither the
        digest nor a way to read it from a consumer account, so the default is the tag."""
        sep = "@" if self.image_tag.startswith("sha256:") else ":"
        return f"{sep}{self.image_tag}"

    @field_validator("systems")
    @classmethod
    def _names(cls, v: dict[str, SapSystem]) -> dict[str, SapSystem]:
        for system in v:
            if not _SYSTEM_NAME.fullmatch(system):
                raise ValueError(
                    f"SAP system name {system!r}: lowercase letters and digits, "
                    "single hyphens between them"
                )
            names.sap_target(system)
        return v

    @model_validator(mode="after")
    def _access_names_what_exists(self) -> SapConfig:
        for uc, a in self.access.items():
            where = f"layers.sap.access.{uc}"
            if not _USE_CASE.fullmatch(uc):
                raise ValueError(f"{where}: {uc!r} is not a use case name")
            named = [
                *([] if a.read == "*" else a.read),
                *([] if a.write == "*" else a.write),
                *a.bypass_gateway,
                *a.ingest,
            ]
            unknown = sorted(set(named) - set(self.systems))
            if unknown:
                raise ValueError(f"{where} names unknown system(s) {unknown}")
            for system, ops in ({} if a.write == "*" else a.write).items():
                off = sorted(set(ops) - set(self.systems[system].writes))
                if off:
                    raise ValueError(
                        f"{where}.write.{system}: {off} is not on in "
                        f"layers.sap.systems.{system}.writes"
                    )
            for system in a.bypass_gateway:
                s = self.systems[system]
                # Not as obo: an unattended call has no signed-in person to act for.
                obo = s.flow == "obo" and s.unattended is None
                if obo or not (s.base_url and s.direct_secret_name):
                    raise ValueError(
                        f"{where}.bypass_gateway: {system} must be a basic, m2m-sap "
                        "or m2m-oidc system with base_url and secret_name, or have an "
                        f"unattended block (layers.sap.systems.{system}.unattended); "
                        "calls outside the Gateway sign in as a service account"
                    )
            for system in a.ingest:
                if not self.systems[system].senders:
                    raise ValueError(
                        f"{where}.ingest: {system} has no senders "
                        f"(layers.sap.systems.{system}.senders), so nothing can put "
                        "its events"
                    )
                if system not in a.bypass_gateway:
                    raise ValueError(
                        f"{where}.ingest: {system} is not in {where}.bypass_gateway; "
                        "a nudge is answered by reading SAP directly"
                    )
        return self

    def reads(self, use_case: str) -> list[str]:
        """The systems `use_case` may read through the Gateway (its ceiling)."""
        a = self.access.get(use_case)
        if a is None:
            return []
        return sorted(self.systems) if a.read == "*" else sorted(a.read)

    def writes(self, use_case: str) -> dict[str, list[str]]:
        """{system: operations} `use_case` may write through the Gateway (its ceiling)."""
        a = self.access.get(use_case)
        if a is None:
            return {}
        if a.write == "*":
            return {
                s: list(c.writes) for s, c in sorted(self.systems.items()) if c.writes
            }
        return {s: sorted(ops) for s, ops in sorted(a.write.items()) if ops}

    def direct_systems(self) -> list[str]:
        """Every system some use case may call outside the Gateway."""
        return sorted({s for a in self.access.values() for s in a.bypass_gateway})

    def ingest(self, use_case: str) -> list[str]:
        """The systems whose events `use_case` may subscribe to (its ceiling)."""
        a = self.access.get(use_case)
        return sorted(a.ingest) if a else []

    def sender_systems(self) -> list[str]:
        """The systems with senders: each gets a sender role on the bus."""
        return sorted(s for s, c in self.systems.items() if c.senders)

    def topics(self, system: str) -> list[str]:
        """The `detail-type`s `system` may put: the use cases whose `ingest` names it."""
        return sorted(uc for uc, a in self.access.items() if system in a.ingest)

    def obo_direct_systems(self) -> list[str]:
        """The obo systems some use case calls outside the Gateway, through their
        `unattended` block. On them SAP authorizes each person, but a direct caller
        reads (and a seeder may write) as the block's service account, so who sees
        that data is decided by the use case, not by SAP."""
        return [s for s in self.direct_systems() if self.systems[s].flow == "obo"]


def load(raw: dict | None) -> SapConfig:
    try:
        return SapConfig.model_validate(raw or {})
    except ValueError as e:
        raise ValueError(f"platform.yaml layers.sap is invalid:\n{e}") from None
