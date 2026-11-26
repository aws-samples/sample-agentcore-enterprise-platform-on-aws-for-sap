# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Interceptor check: the business-operations run checks.

The platform's Gateway interceptor runs `check` for this use case's targets only
(gateway.interceptor_extensions in platform.yaml, request_headers: true), before
Cedar. `run_checks.decide` holds the decision; this module does its reads.

Anything this module raises refuses the call (the interceptor fails closed for
this use case's targets and no one else's), so every unreadable input, a missing
parameter, a malformed token, a failed table read, is a refusal.

What it reads, all from the use case's own stack:
- `<SSM_PREFIX>/use-cases/business-operations/run-checks`, kept for SETTINGS_TTL_SECONDS:
  the table and bucket names, the apps its SAP claim names and, of those, the ones that
  run the agent (`agent_callers`, whose SAP reads need a run grant too). It is read with
  GetParameterHistory and trusted only when CloudFormation wrote its latest version
  (`trusted.py`, the SAP layer's own reader, linked here), so a hand-written version
  refuses every gated call rather than dropping `sap_callers` or naming another table.
- The run grant (`run_id`, `client_id`, `secret_sha256`, `case_id`, `packs`,
  `approved`, `approved_write`, `rehearsal`, `ask`, `expires_at`), written by the
  invoker before it starts a run.
- A ticket's `case_id`, for `demo_get_ticket`.
- The case's `pack_id`, `key` and `attributes.amount`/`attributes.currency`, the
  pack's `AUTO_WRITE_LIMIT_USD` override from the config table, and `registry.json`
  (kept for REGISTRY_TTL_SECONDS), which holds each pack's `case_document_check`, `write_amount_field` and `sap_system`.

It logs one line per call, `interceptor run {"run_id": ...}`, when the call names a
run. The interceptor's own verdict line names none; the two share the invocation's
request id, which is how an eval ties a verdict to its run (`evals/run_regression.py`).
"""

from __future__ import annotations

import json
import logging
import os
import time
from decimal import Decimal, InvalidOperation

from . import run_checks, run_grant, trusted

logger = logging.getLogger(__name__)

RUN_ID_HEADER = "x-audit-correlation-id"
#: The grant's secret, from the agent (agent/utils/mcp_topology.py GATEWAY_GRANT_HEADER).
GRANT_HEADER = "x-business-operations-run-grant"
PARAMETER = "/use-cases/business-operations/run-checks"
_REGISTRY_KEY = "registry.json"
_CONSTANT_NS_PREFIX = "constant#"

#: Only a read the writer check passed is kept; a failure raises, and the next call
#: reads again.
SETTINGS_TTL_SECONDS = 60
_settings: dict | None = None
_settings_at = 0.0
# A publish rewrites registry.json under a warm interceptor, so a read is trusted for a
# minute and then re-validated on its ETag: a 304 keeps it for another minute. Only a
# successful read is kept: a failure raises, and the next call reads again.
REGISTRY_TTL_SECONDS = 60
_registry: tuple[float, dict, str | None] | None = None
# A case's pack never changes (its id is a digest that includes the pack), so it
# is cached for the container's life. Its amount can change, so that is not.
_case_pack_cache: dict[str, str] = {}
_clients: dict = {}


def _client(service: str):
    if service not in _clients:
        import boto3

        _clients[service] = (
            boto3.resource(service) if service == "dynamodb" else boto3.client(service)
        )
    return _clients[service]


def _config() -> dict:
    global _settings, _settings_at
    now = time.monotonic()
    if _settings is None or now - _settings_at >= SETTINGS_TTL_SECONDS:
        name = os.environ["SSM_PREFIX"] + PARAMETER
        _settings = json.loads(trusted.cloudformation_wrote(_client("ssm"), name))
        _settings_at = now
    return _settings


def _client_id(headers: dict | None) -> str:
    """The caller key (`caller_key`) of the bearer."""
    return run_grant.caller_key(run_grant.bearer_claims(headers))


def _app_id(headers: dict | None) -> str:
    """The calling app, as the SAP layer keys a claim: `client_id`, else `azp`."""
    claims = run_grant.bearer_claims(headers)
    return str(claims.get("client_id") or claims["azp"])


def _table(key: str):
    global _settings
    if (
        key not in _config()
    ):  # the parameter gained this key after this container read it
        _settings = None
    return _client("dynamodb").Table(_config()[key])


def _grant(run_id: str) -> run_checks.Grant | None:
    item = run_grant.read(_table("run_grants_table"), run_id)
    if not item:
        return None
    packs = item.get("packs")
    return run_checks.Grant(
        run_id=str(item["run_id"]),
        client_id=str(item["client_id"]),
        case_id=str(item["case_id"]) if item.get("case_id") else None,
        packs=frozenset(str(p) for p in packs) if packs is not None else None,
        approved=item.get("approved") is True,
        expires_at=int(item["expires_at"]),
        approved_write=item.get("approved_write")
        if isinstance(item.get("approved_write"), dict)
        else None,
        rehearsal=item.get("rehearsal") is True,
        ask=item.get("ask") is True,
        secret_sha256=str(item.get("secret_sha256") or ""),
        approvers=frozenset(str(a) for a in item.get("approvers") or []),
    )


def _packs() -> dict:
    """registry.json's packs, at most REGISTRY_TTL_SECONDS old. Raises when it cannot be
    read: a gate that needs it refuses."""
    global _registry
    now = time.monotonic()
    if _registry is not None and now - _registry[0] < REGISTRY_TTL_SECONDS:
        return _registry[1]
    etag = _registry[2] if _registry else None
    try:
        obj = _client("s3").get_object(
            Bucket=_config()["registry_bucket"],
            Key=_REGISTRY_KEY,
            **({"IfNoneMatch": etag} if etag else {}),
        )
    except Exception as error:
        response = getattr(error, "response", None) or {}
        if (
            etag
            and (response.get("ResponseMetadata") or {}).get("HTTPStatusCode") == 304
        ):
            _registry = (now, _registry[1], etag)
            return _registry[1]
        raise
    packs = json.loads(obj["Body"].read())["packs"]
    if not isinstance(packs, dict):
        # ValueError, not TypeError: run_checks.py reads an unreadable registry as one.
        raise ValueError("registry.json has no packs object")  # noqa: TRY004
    _registry = (now, packs, obj.get("ETag"))
    return packs


def _declared(pack: str) -> dict:
    return (_packs().get(pack) or {}).get("pack") or {}


def _constants(pack: str) -> dict:
    return _declared(pack).get("constants") or {}


def _case_document_check(pack: str) -> dict | None:
    document_check = _declared(pack).get("case_document_check")
    if document_check is None:
        return None
    if not isinstance(document_check, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in document_check.items()
    ):
        raise ValueError(f"{pack}'s case_document_check is not a map of names")
    return document_check


def _limit(pack: str, symbol: str = run_checks.LIMIT_SYMBOL) -> Decimal | None:
    declared = _constants(pack)
    if symbol not in declared:
        return None
    override = (
        _table("config_table")
        .get_item(
            Key={
                "namespace": _CONSTANT_NS_PREFIX + pack,
                "config_key": symbol,
            }
        )
        .get("Item")
        or {}
    )
    # The pack's value is the ceiling: a stack's override only lowers it, so one left
    # from before a version lowered the ceiling cannot widen it again.
    values = []
    for raw in (declared[symbol], override.get("value", declared[symbol])):
        try:
            values.append(Decimal(str(raw)))
        except (InvalidOperation, TypeError) as e:
            raise ValueError(f"{symbol}={raw!r} is not a number") from e
    return min(values)


def _amount_field(pack: str) -> str | None:
    field = _declared(pack).get("write_amount_field")
    if field is not None and not isinstance(field, str):
        raise ValueError(f"{pack}'s write_amount_field is not a name")
    return field or None


def _pack_system(pack: str) -> str | None:
    """The pack's `sap_system`. A pack the registry does not hold raises: a read for it
    is refused rather than allowed on any system."""
    entry = _packs().get(pack)
    if not isinstance(entry, dict):
        # ValueError, not TypeError: run_checks.py reads an unreadable registry as one.
        raise ValueError(f"pack '{pack}' is not in the registry")  # noqa: TRY004
    system = (entry.get("pack") or {}).get("sap_system")
    return str(system) if system else None


def _any_limit_declared() -> bool:
    return any(run_checks.LIMIT_SYMBOL in _constants(p) for p in _packs())


def _case_item(case_id: str) -> dict:
    return (
        _table("cases_table")
        .get_item(
            Key={"case_id": case_id},
            ProjectionExpression="pack_id, #a, #k, #o",
            ExpressionAttributeNames={"#a": "attributes", "#k": "key", "#o": "origin"},
        )
        .get("Item")
        or {}
    )


def _case(case_id: str) -> run_checks.Case | None:
    item = _case_item(case_id)
    pack = item.get("pack_id")
    if not pack:
        return None
    attributes = item.get("attributes")
    attributes = attributes if isinstance(attributes, dict) else {}
    key = item.get("key")
    return run_checks.Case(
        pack=str(pack),
        amount=str(attributes.get("amount") or ""),
        currency=str(attributes.get("currency") or ""),
        key=key if isinstance(key, dict) else {},
        pushed=item.get("origin") == "sap-push",
    )


def _case_pack(case_id: str) -> str | None:
    if case_id not in _case_pack_cache:
        pack = _case_item(case_id).get("pack_id")
        if not pack:
            return None
        _case_pack_cache[case_id] = str(pack)
    return _case_pack_cache[case_id]


def _ticket_case(ticket_id: str) -> str | None:
    item = (
        _table("tickets_table")
        .get_item(Key={"ticket_id": ticket_id}, ProjectionExpression="case_id")
        .get("Item")
        or {}
    )
    return str(item.get("case_id") or "") or None


def _process_type_pack(process_type: str) -> str | None:
    # An incomplete pack (its source still awaits a SAP survey) routes no process type, and a
    # type two complete packs claim routes nowhere, as in the agent's registry.
    owners = [
        str(pack_id)
        for pack_id, entry in _packs().items()
        # A registry.json from before the flag routes nothing until the next publish.
        if not entry.get("incomplete", True)
        and process_type in ((entry.get("pack") or {}).get("process_type_to_sop") or {})
    ]
    return owners[0] if len(owners) == 1 else None


_LOOKUPS = run_checks.Lookups(
    grant=_grant,
    limit=_limit,
    any_limit_declared=_any_limit_declared,
    case=_case,
    case_pack=_case_pack,
    ticket_case=_ticket_case,
    process_type_pack=_process_type_pack,
    case_document_check=_case_document_check,
    dual_limit=lambda pack: _limit(pack, run_checks.DUAL_LIMIT_SYMBOL),
    amount_field=_amount_field,
    pack_system=_pack_system,
)


def check(body: dict, headers: dict | None) -> str | None:
    params = body.get("params") or {}
    run_id = run_grant.header(headers, RUN_ID_HEADER)
    if run_id:
        logger.info("interceptor run %s", json.dumps({"run_id": run_id}))
    reason = run_checks.decide(
        tool=str(params.get("name") or ""),
        arguments=params.get("arguments", {}),
        client_id=lambda: _client_id(headers),
        run_id=run_id,
        grant_secret=run_grant.header(headers, GRANT_HEADER),
        app_id=lambda: _app_id(headers),
        sap_callers=lambda: frozenset(_config()["sap_callers"]),
        agent_callers=lambda: frozenset(_config()["agent_callers"]),
        now=int(time.time()),
        lookups=_LOOKUPS,
    )
    if reason is None:
        return None
    return f"{reason}. This is an authorization decision; retrying will not change it."
