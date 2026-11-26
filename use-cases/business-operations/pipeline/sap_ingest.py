# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The ingest queue's consumer: SAP says an exception happened, and this makes the case.

The use case has one ingest queue, and only two writers reach it: the rule the SAP
layer's `subscribe` put on its `sap-events` bus, and `POST /erp-events`. Each message
is an EventBridge event: `source` is `sap.<system>`, `detail-type` this use case's
name, and `detail` the message. Only that system's sender role may put its `source` on
the bus, and `POST /erp-events` sets it from the pack's own system, so the source names the system
the message speaks for. It is still checked against the systems this consumer serves
(`unknown-source`), and a message naming a `system` must name the same one.

Two shapes arrive in `detail` (`types/sap-nudge.schema.json`,
`types/sap-events.schema.json`):

- **A nudge**, `{"version": 1, "pack_id", "system"?, "keys"?}`. The pack's own OData
  query is re-read, narrowed to those keys, or in full without them. Nothing in the
  message becomes case data, so a key that is not an exception in SAP makes nothing,
  and a nudge can only make the poller's answer arrive sooner.
- **Typed records**, the `/erp-events` envelope plus an optional `version` and
  `system`, mapped through the pack's `source` block as the poller does. Only for a
  pack whose `source.push` is `records`: the case is only as true as the sender.

A refused message is counted (`IngestRejected`, by reason) and fails, so it reaches
the dead-letter queue after its retries. So does one whose SAP read or case write
failed. Never log a message's values or a record's: the reasons are fixed strings, and
a pack id is logged only once it names a published pack. `erp_events` runs `parse` and
`accept` too, so the route answers 400 for what this would refuse.
"""

import json
import os
import re
import time

import registry
from case_store import Gate, case_exists, enqueue, get_cases_table, put_case
from content_filter import sanitize_external_content
from polling_engine import (
    _fetch_pages,
    compose_filter,
    load_pack_sources,
    process_entities,
    push_mode,
    query,
)
from sap_http import system
from sap_poller import SAP_SYSTEMS, system_of

#: The `detail-type` every message carries: this use case's name.
TOPIC = "business-operations"
SOURCE_PREFIX = "sap."
VERSION = 1
# 100 invoices with their PO items run 200-400 KB.
MAX_BODY_BYTES = 1_000_000
#: SQS's own limit, which bounds the whole event around a typed body.
MAX_EVENT_BYTES = 1_048_576
MAX_RECORDS = 100
MAX_NUDGE_BYTES = 16 * 1024
MAX_KEYS = 50
#: Keys per GET: twenty `(A eq 'x' and B eq 'y')` terms keep the URL well under the
#: 8 KB request line SAP's ICM accepts by default.
KEYS_PER_GET = 20
_ENVELOPE_KEYS = {"pack_id", "timestamp", "records"}
NUDGE_KEYS = frozenset({"version", "pack_id", "system", "keys"})
TYPED_EXTRA_KEYS = frozenset({"version", "system"})
#: A key value lands inside a `$filter` literal, so it may hold no quote or space.
KEY_VALUE = re.compile(r"[A-Za-z0-9._/-]{1,40}")
METRICS_NAMESPACE = os.environ.get("METRICS_NAMESPACE", "")


class Rejected(Exception):
    """A message this consumer refuses. `reason` is a fixed string, safe to log."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def envelope_error(payload) -> str | None:
    """Why `payload` is not a typed envelope, or None. A gate: the caller is not ours."""
    if not isinstance(payload, dict) or set(payload) != _ENVELOPE_KEYS:
        return f"expected exactly the keys {sorted(_ENVELOPE_KEYS)}"
    if not isinstance(payload["pack_id"], str) or not isinstance(
        payload["timestamp"], str
    ):
        return "pack_id and timestamp must be strings"
    records = payload["records"]
    if not isinstance(records, list) or not 1 <= len(records) <= MAX_RECORDS:
        return f"records must be a list of 1 to {MAX_RECORDS} objects"
    if not all(isinstance(r, dict) for r in records):
        return "every record must be an object"
    return None


def sanitize_deep(value):
    """Every string in a record, at any depth: which fields reach the prompt is config."""
    if isinstance(value, str):
        return sanitize_external_content(value, source="sap-push")
    if isinstance(value, dict):
        return {k: sanitize_deep(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_deep(v) for v in value]
    return value


def as_event(system_name: str, detail: dict) -> str:
    """The message `POST /erp-events` enqueues: the event the SAP layer's bus delivers."""
    return json.dumps(
        {
            "source": SOURCE_PREFIX + system_name,
            "detail-type": TOPIC,
            "detail": detail,
        },
        ensure_ascii=False,
    )


def parse(body: str) -> tuple[str, str, dict]:
    """`(system, "nudge" | "records", message)` from one event, or Rejected."""
    try:
        if len(body.encode("utf-8")) > MAX_EVENT_BYTES:
            raise Rejected("too-large")
        msg = json.loads(body)
    except (json.JSONDecodeError, UnicodeEncodeError):
        raise Rejected("malformed") from None
    if not isinstance(msg, dict):
        raise Rejected("shape")
    source = msg.get("source")
    door = (
        source.removeprefix(SOURCE_PREFIX)
        if isinstance(source, str) and source.startswith(SOURCE_PREFIX)
        else None
    )
    if door not in SAP_SYSTEMS:
        raise Rejected("unknown-source")
    if msg.get("detail-type") != TOPIC:
        raise Rejected("topic")
    msg = msg.get("detail")
    if not isinstance(msg, dict):
        raise Rejected("shape")
    size = len(
        json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8", "surrogatepass"
        )
    )
    if size > MAX_BODY_BYTES:
        raise Rejected("too-large")
    version = msg.get("version", VERSION)
    if type(version) is not int or version != VERSION:  # `True == 1` in Python
        raise Rejected("version")
    if msg.get("system", door) != door:
        raise Rejected("system")

    if "records" in msg:
        envelope = {k: v for k, v in msg.items() if k not in TYPED_EXTRA_KEYS}
        if envelope_error(envelope):
            raise Rejected("shape")
        return door, "records", envelope

    if size > MAX_NUDGE_BYTES:
        raise Rejected("too-large")
    if not set(msg) <= NUDGE_KEYS or not isinstance(msg.get("pack_id"), str):
        raise Rejected("shape")
    keys = msg.get("keys")
    if keys is not None:
        if not isinstance(keys, list) or not 1 <= len(keys) <= MAX_KEYS:
            raise Rejected("keys")
        for key in keys:
            if not isinstance(key, dict) or not all(
                isinstance(v, str) and KEY_VALUE.fullmatch(v) for v in key.values()
            ):
                raise Rejected("key-value")
    return door, "nudge", msg


def accept(door: str, shape: str, msg: dict, config: dict) -> None:
    """Refuse what `config`, the pack `msg` names, does not take from `door`."""
    if system_of(config) != door:
        raise Rejected("system")
    mode = push_mode(config)
    if shape == "records":
        if mode != "records":
            raise Rejected("records-not-accepted")
        return
    if mode not in ("nudge", "records") or config.get("kind") == "event":
        raise Rejected("nudge-not-accepted")
    fields = key_fields(config)
    if "keys" in msg and (
        not fields or any(set(k) != set(fields) for k in msg["keys"])
    ):
        raise Rejected("key-fields")


def key_fields(config: dict) -> list[str]:
    """The key fields a nudge names: those on the entity the pack queries, so they
    can narrow its `$filter`. A child's field is not filterable from the parent."""
    nested = bool((config.get("iterate") or {}).get("path"))
    return [
        spec["field"]
        for spec in config["key"]
        if spec.get("source", "self") == "parent"
        or (not nested and spec.get("source", "self") == "self")
    ]


def key_filter(fields: list[str], keys: list[dict]) -> str:
    """`(F1 eq 'v1' and F2 eq 'v2') or ...`. Values are checked by `KEY_VALUE`."""
    return " or ".join(
        "(" + " and ".join(f"{f} eq '{key[f]}'" for f in fields) + ")" for key in keys
    )


def reread(config: dict, sap, keys: list[dict] | None) -> list[dict] | None:
    """The pack's own query, narrowed to `keys` (all of it when None). None if a
    read failed, so the nudges behind it are retried."""
    url, base = query(config, sap)
    label = config.get("label", config["pack_id"])
    fields = key_fields(config)
    entities: list[dict] = []
    batches = (
        [None]
        if keys is None
        else [keys[i : i + KEYS_PER_GET] for i in range(0, len(keys), KEYS_PER_GET)]
    )
    for batch in batches:
        predicates = [
            p
            for p in (compose_filter(config), batch and key_filter(fields, batch))
            if p
        ]
        params = dict(base)
        if len(predicates) == 1:
            params["$filter"] = predicates[0]
        elif predicates:
            params["$filter"] = " and ".join(f"({p})" for p in predicates)
        got = _fetch_pages(sap, url, params, label)
        if got is None:
            return None
        entities.extend(got)
    return entities


def _count_rejected(reason: str) -> None:
    print(f"Ingest refused a message: {reason}")
    if METRICS_NAMESPACE:
        print(
            json.dumps(
                {
                    "_aws": {
                        "Timestamp": int(time.time() * 1000),
                        "CloudWatchMetrics": [
                            {
                                "Namespace": METRICS_NAMESPACE,
                                "Dimensions": [[], ["Reason"]],
                                "Metrics": [
                                    {"Name": "IngestRejected", "Unit": "Count"}
                                ],
                            }
                        ],
                    },
                    "Reason": reason,
                    "IngestRejected": 1,
                }
            )
        )


def _make_cases(
    config: dict, records: list[dict], trigger: str
) -> tuple[int, int, int]:
    # Same trigger-mode gate as the poller, read once per message; only the recorded
    # trigger differs.
    gate = Gate()
    return process_entities(
        config,
        records,
        get_cases_table(),
        case_exists,
        put_case,
        lambda case_id, pack_id, _pt: enqueue(case_id, pack_id, trigger, gate),
        # A nudge's records are SAP's own answer; only typed records are the sender's.
        origin="sap-push" if trigger == "sap-push" else None,
    )


def handler(event: dict, context) -> dict:
    """An SQS batch. Answers the messages to retry (`ReportBatchItemFailures`)."""
    records = event.get("Records") or []
    registry.refresh_for_request()
    try:
        packs = {c["pack_id"]: c for c in load_pack_sources()}
    except registry.RegistryUnavailable as e:
        print(f"Registry unavailable, retrying the batch: {e}")
        return {
            "batchItemFailures": [{"itemIdentifier": r["messageId"]} for r in records]
        }

    failed: list[str] = []
    # pack_id -> message ids, and the keys to read (None: the whole query).
    nudges: dict[str, dict] = {}
    for rec in records:
        mid = rec["messageId"]
        try:
            door, shape, msg = parse(rec.get("body") or "")
            config = packs.get(msg["pack_id"])
            if config is None:
                raise Rejected("unknown-pack")
            accept(door, shape, msg, config)
            if shape == "records":
                try:
                    created, skipped, rejected = _make_cases(
                        config, [sanitize_deep(r) for r in msg["records"]], "sap-push"
                    )
                except Exception as e:  # noqa: BLE001 - retried, then dead-lettered
                    print(f"Records for {config['pack_id']} failed: {type(e).__name__}")
                    failed.append(mid)
                    continue
                print(
                    f"Records for {config['pack_id']}: {created} created, "
                    f"{skipped} skipped, {rejected} rejected"
                )
                continue

            fields = key_fields(config)
            entry = nudges.setdefault(config["pack_id"], {"ids": [], "keys": set()})
            entry["ids"].append(mid)
            if "keys" not in msg:
                entry["keys"] = None  # one full read covers every key in the batch
            elif entry["keys"] is not None:
                entry["keys"].update(tuple(k[f] for f in fields) for k in msg["keys"])
        except Rejected as r:
            _count_rejected(r.reason)
            failed.append(mid)

    for pack_id, entry in nudges.items():
        config = packs[pack_id]
        fields = key_fields(config)
        keys = (
            None
            if entry["keys"] is None
            else [dict(zip(fields, values)) for values in sorted(entry["keys"])]
        )
        try:
            name = system_of(config)
            entities = reread(config, system(name, SAP_SYSTEMS[name]), keys)
            if entities is None:
                raise RuntimeError("SAP read failed")
            created, skipped, rejected = _make_cases(config, entities, "sap-nudge")
        except Exception as e:  # noqa: BLE001 - retried, then dead-lettered
            print(f"Nudge for {pack_id} failed: {type(e).__name__}")
            failed.extend(entry["ids"])
            continue
        print(
            f"Nudge for {pack_id}: {len(entry['ids'])} messages, "
            f"{'all' if keys is None else len(keys)} keys, "
            f"{created} created, {skipped} skipped, {rejected} rejected"
        )

    return {"batchItemFailures": [{"itemIdentifier": mid} for mid in failed]}
