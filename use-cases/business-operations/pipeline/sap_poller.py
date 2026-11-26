# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The SAP poller: on a schedule, read each published pack's exceptions out of SAP.

Two rules invoke it: `poller_schedule` polls the packs whose `source.poll` is
`schedule`, and `sweep_schedule` (input `{"sweep": true}`) the ones on `sweep`. A
`pack_id` polls that pack whatever its `poll`, `off` included.

Each pack declares where its exceptions come from in its `source` block, and which
SAP system in `sap_system` (the only system when there is one). The pipeline is
`polling_engine`'s; this is the scheduled entry point that signs in to SAP with that
system's service account (Basic or a client-credentials token, by its `flow`).

Unless the deployment's trigger mode and the pack's own mode both say `auto` (absent is
`manual`), new cases are created in `detected` and not queued: the poller has no first-run baseline, so `auto` would queue every open
exception it can see.

On a system this use case subscribes to on the SAP layer's bus
(`layers.sap.access.business-operations.ingest`), a pack that declares `source.push`
(`nudge` or `records`) says SAP sends it, so its case normally comes from the nudge first and the
poller skips it. A case the poller creates for such a pack is a document the sender lost
(outbox stuck, drain job stopped, send refused, BAdI not hit): it is counted
(`IngestMissed`, by pack) and logged with its key. The default `push` does not count: a
pack nothing sends would otherwise count every case it gets.
"""

import json
import os
import time

import case_store
import registry
from case_store import case_exists, enqueue, get_cases_table, put_case
from polling_engine import load_pack_sources, poll_mode, poll_pack
from sap_http import system
from warm_stagger import Stagger, remaining_seconds

# {name: {base_url, secret, client}}, one entry per configured SAP system.
SAP_SYSTEMS = json.loads(os.environ.get("SAP_SYSTEMS", "{}"))
WARM_DELAY_SECONDS = int(os.environ.get("WARM_DELAY_SECONDS", "0"))
#: The systems subscribed to on the SAP layer's bus, and the namespace to count misses
#: in: both set only when `layers.sap.access.business-operations.ingest` names one.
INGEST_SYSTEMS = frozenset(json.loads(os.environ.get("INGEST_SYSTEMS", "[]")))
METRICS_NAMESPACE = os.environ.get("METRICS_NAMESPACE", "")


def _missed_line(pack_id: str, count: int, **fields) -> None:
    """One EMF line adding `count` to `IngestMissed` for `pack_id`."""
    print(
        json.dumps(
            {
                "_aws": {
                    "Timestamp": int(time.time() * 1000),
                    "CloudWatchMetrics": [
                        {
                            "Namespace": METRICS_NAMESPACE,
                            "Dimensions": [[], ["Pack"]],
                            "Metrics": [{"Name": "IngestMissed", "Unit": "Count"}],
                        }
                    ],
                },
                "Pack": pack_id,
                "IngestMissed": count,
                **fields,
            },
            default=str,
        )
    )


def put_missed(table, item) -> bool:
    """`put_case`, and one metric line naming the case's pack and key if it was new."""
    if not put_case(table, item):
        return False
    _missed_line(item["pack_id"], 1, case_id=item["case_id"], key=item["key"])
    return True


def system_of(cfg: dict) -> str | None:
    """Which configured SAP system a pack reads from, or None if it cannot say."""
    entry = registry.packs().get(cfg["pack_id"]) or {}
    named = cfg.get("sap_system") or (entry.get("pack") or {}).get("sap_system")
    if named:
        return named if named in SAP_SYSTEMS else None
    return next(iter(SAP_SYSTEMS)) if len(SAP_SYSTEMS) == 1 else None


def handler(event, context):
    registry.refresh_for_request()
    configs = load_pack_sources()
    # `ops.py poll <pack>` invokes this with one pack; the schedules pass none.
    only = event.get("pack_id") if isinstance(event, dict) else None
    if only:
        configs = [c for c in configs if c["pack_id"] == only]
        if not configs:
            print(f"{only} is not a published pack with a source: nothing to poll")
            return {"packs": {only: {"error": "not-polled"}}, "cases_created": 0}
    else:
        run = "sweep" if isinstance(event, dict) and event.get("sweep") else "schedule"
        configs = [c for c in configs if poll_mode(c) == run]
    if not configs:
        print("No published pack is polled on this run: nothing to poll")
        return {"packs": {}, "cases_created": 0, "cases_skipped": 0}

    table = get_cases_table()
    # Once per poll, not per case: a poll can create hundreds, and Parameter Store's
    # throughput is the account's. The switch is read now and each pack's mode once;
    # setting either `manual` stops the next poll.
    gate = case_store.Gate()
    # Each pack's first new case goes at once; the rest wait one warm delay.
    stagger = Stagger(
        pack_of=lambda msg: msg[1],
        send=lambda msg: enqueue(msg[0], msg[1], "poller", gate),
        warm_delay=WARM_DELAY_SECONDS,
    )
    results = {}
    for cfg in configs:
        pack_id = cfg["pack_id"]
        name = system_of(cfg)
        if name is None:
            print(
                f"Skipping {pack_id}: its sap_system is not one of "
                f"{sorted(SAP_SYSTEMS) or 'the configured systems (none)'}"
            )
            results[pack_id] = {"error": "no-sap-system"}
            continue
        try:
            sap = system(name, SAP_SYSTEMS[name])
        except Exception as e:  # noqa: BLE001 - one system's secret, not the sweep
            print(f"Skipping {pack_id}: cannot sign in to {name}: {type(e).__name__}")
            results[pack_id] = {"system": name, "error": type(e).__name__}
            continue
        pushed = (
            METRICS_NAMESPACE
            and name in INGEST_SYSTEMS
            and cfg.get("push") in ("nudge", "records")
        )
        created, skipped, rejected = poll_pack(
            cfg,
            sap,
            table,
            case_exists,
            put_missed if pushed else put_case,
            lambda *msg: stagger.offer(msg),
        )
        if pushed:
            # A real zero each poll: with only the 1s a miss writes, the alarm's
            # evaluation would keep finding the last misses and stay in ALARM for hours
            # after the sender is fixed.
            _missed_line(pack_id, 0)
        results[pack_id] = {
            "system": name,
            "created": created,
            "skipped": skipped,
            "rejected": rejected,
        }
    stagger.flush(remaining_seconds(context))

    created = sum(r.get("created", 0) for r in results.values())
    skipped = sum(r.get("skipped", 0) for r in results.values())
    rejected = sum(r.get("rejected", 0) for r in results.values())
    print(
        f"Polling complete: {created} created, {skipped} skipped, {rejected} rejected"
    )
    return {
        "packs": results,
        "cases_created": created,
        "cases_skipped": skipped,
        "cases_rejected": rejected,
    }
