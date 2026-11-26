# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Config-driven OData polling engine.

Reads each pack's `source` block from the published registry and runs the common
fetch → iterate → skip → map → dedupe → create → enqueue pipeline.

`sap_poller` drives the whole pipeline on a schedule; `erp_events` enters at
`process_entities` with records SAP pushed. Both map through this one copy, or the
two transports disagree about case identity.
"""

import os
import re
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import registry
from case_key import CaseKeyError, derive_case_id
from sap_http import SapError

_INITIAL_STATUS = "detected"  # Must be in CaseStatus enum (types/cases.schema.json)

# What makes a record an exception — and therefore which transport can produce it.
# These three the poller can serve by querying; `event` arrives by push, because
# there is no query that finds "SAP told us this happened". The build gate
# (business_operations/validate_packs.py) knows the full set and rejects the kinds
# nothing executes yet, so anything outside this set reaching the poller is a config
# that skipped the gate.
POLLED_KINDS = {"filter", "absence", "aging"}


def push_mode(config: dict) -> str:
    """`off`, `nudge` or `records`: what this pack accepts from a sender.

    A polled kind defaults to `nudge`, which can only re-read what the poller would
    find. Typed records are opt-in, and an `event` pack has no query to re-read.
    """
    default = "nudge" if config.get("kind", "filter") in POLLED_KINDS else "off"
    return config.get("push") or default


def poll_mode(config: dict) -> str:
    """`schedule`, `sweep` or `off`: which poller run reads this pack.

    `schedule` (the default for a polled kind) is every `poller_schedule` run, `sweep`
    only the slower `sweep_schedule` run, `off` none. An `event` pack has no query.
    """
    default = "schedule" if config.get("kind", "filter") in POLLED_KINDS else "off"
    return config.get("poll") or default


_DEFAULT_KIND = "filter"
# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def load_pack_sources() -> list[dict]:
    """Every published pack's `source` block, from the registry.

    A pack without one declares no source, which is legitimate: nothing polls or
    pushes for it. Publishing is enough for the poller to see a new pack.
    """
    configs = list(registry.sources())
    for cfg in configs:
        kind = cfg.get("kind")
        if kind not in POLLED_KINDS | {"event"}:
            # The build gate rejects kinds nothing executes, so this config skipped it.
            print(
                f"WARNING: pack '{cfg.get('pack_id')}' declares kind {kind!r}, which no "
                f"executor serves; treating it as '{_DEFAULT_KIND}'"
            )
    return configs


# ---------------------------------------------------------------------------
# SAP value helpers
# ---------------------------------------------------------------------------


def parse_sap_date(val) -> str | None:
    """Normalise however SAP spelled a date → one ISO string.

    OData sends `/Date(ms)/`; a SAP push sends what ABAP serializes a DATS as,
    a bare `2026-08-14`. Both have to land the same stored value or the two
    transports produce cases that differ on a field neither of them disagrees
    about.
    """
    if not val or val == "null":
        return None
    s = str(val)
    if "/Date(" in s:
        try:
            ms = int(s.split("(")[1].split(")")[0].split("+")[0])
            # UTC, then drop the offset. SAP puts a DATS at midnight UTC, so reading
            # the epoch in the *local* zone moves the date a day west of Greenwich —
            # right in a UTC Lambda, wrong on a developer's machine, which is the
            # difference this function exists to remove.
            return (
                datetime.fromtimestamp(ms / 1000, timezone.utc)
                .replace(tzinfo=None)
                .isoformat()
            )
        except (ValueError, IndexError, OverflowError, OSError):
            return s
    try:
        return (
            datetime.fromisoformat(s.replace("Z", "+00:00"))
            .replace(tzinfo=None)
            .isoformat()
        )
    except ValueError:
        # Not a date we recognise — pass it through rather than lose it.
        return s


def _cast(val, cast_type: str):
    """Apply a cast to a raw SAP value."""
    if cast_type == "sap_date":
        return parse_sap_date(val)
    if cast_type == "float":
        return float(val or 0)
    if cast_type == "decimal2":
        return Decimal(str(round(float(val or 0), 2)))
    if cast_type == "abs_decimal":
        return Decimal(str(abs(float(val or 0))))
    return val


# ---------------------------------------------------------------------------
# Path resolution — navigate dotted paths like "to_AccountAssignment.results[0].GLAccount"
# ---------------------------------------------------------------------------

_IDX_RE = re.compile(r"^(.+)\[(\d+)]$")


def _ci_get(obj, key: str):
    """dict.get() falling back to a case-insensitive key match.

    OData returns the CDS view's casing (`SupplierInvoice`); a SAP push carries
    ABAP structure component names, which are uppercase (`SUPPLIERINVOICE`).
    Matching loosely here is one lookup instead of a name map per pack.
    """
    if not isinstance(obj, dict):
        return None
    if key in obj:
        return obj[key]
    lowered = key.lower()
    for k, v in obj.items():
        if k.lower() == lowered:
            return v
    return None


def _resolve_path(obj: dict, dotted: str):
    """Walk a dotted path with optional [N] indexing. Returns None on miss."""
    cur = obj
    for seg in dotted.split("."):
        if cur is None:
            return None
        m = _IDX_RE.match(seg)
        if m:
            cur = _ci_get(cur, m.group(1)) or {}
            if isinstance(cur, list):
                idx = int(m.group(2))
                cur = cur[idx] if idx < len(cur) else None
            else:
                cur = None
        else:
            if isinstance(cur, dict):
                cur = _ci_get(cur, seg)
            else:
                return None
    return cur


def _resolve_scoped(parent: dict | None, child: dict | None, path: str):
    """Resolve a path prefixed with parent./child./self. against the right dict."""
    if path.startswith("parent."):
        return _resolve_path(parent or {}, path[7:])
    if path.startswith("child."):
        return _resolve_path(child or {}, path[6:])
    if path.startswith("self."):
        return _resolve_path(child or parent or {}, path[5:])
    # No prefix — try child first, then parent
    return _resolve_path(child or {}, path) or _resolve_path(parent or {}, path)


# ---------------------------------------------------------------------------
# Skip-condition evaluator
# ---------------------------------------------------------------------------


def _eval_skip(condition: dict, parent: dict | None, child: dict | None) -> bool:
    """Return True if this record should be skipped."""
    op = condition["op"]

    if op == "and":
        return all(_eval_skip(c, parent, child) for c in condition["conditions"])

    # Determine which dict to read from
    scope = condition.get("scope")
    if scope == "parent":
        target = parent or {}
    elif scope == "child":
        target = child or {}
    else:
        target = child or parent or {}

    raw = _resolve_path(target, condition["field"])

    if op == "blank":
        return not str(raw or "").strip()
    if op == "empty":
        return not raw or (isinstance(raw, list) and len(raw) == 0)
    if op == "present":
        return bool(str(raw or "").strip())
    if op == "lte":
        try:
            return float(raw or 0) <= condition["value"]
        except (ValueError, TypeError):
            return True

    return False


# ---------------------------------------------------------------------------
# Process-type resolver
# ---------------------------------------------------------------------------


def _resolve_process_type(cfg: dict, parent: dict | None, child: dict | None) -> str:
    """Evaluate process_type rules, return first match or default."""
    pt = cfg.get("process_type", {})
    for rule in pt.get("rules", []):
        when = rule["when"]
        # Reuse skip evaluator — "present" means the condition is met
        if _eval_skip(when, parent, child):
            return rule["then"]
    return pt.get("default", "unknown")


# ---------------------------------------------------------------------------
# Title interpolation
# ---------------------------------------------------------------------------


_TITLE_RE = re.compile(r"\{([^}]+)}")


def _render_title(
    template: str, parent: dict | None, child: dict | None, extra: dict
) -> str:
    """Interpolate {parent.Field}, {child.Field}, {self.Field}, {abs_amount} etc."""

    def _replace(m):
        key = m.group(1)
        # Check extra computed values first
        if key in extra:
            return str(extra[key])
        # Handle pipe-separated fallbacks: {parent.A|parent.B}
        for alt in key.split("|"):
            val = _resolve_scoped(parent, child, alt.strip())
            if val and str(val).strip():
                return str(val).strip()
        return ""

    return _TITLE_RE.sub(_replace, template)


# ---------------------------------------------------------------------------
# Field mapping
# ---------------------------------------------------------------------------


def _map_fields(field_map: dict, parent: dict | None, child: dict | None) -> dict:
    """Apply field_map config to produce case fields."""
    result = {}
    for case_field, spec in field_map.items():
        val = None

        if "path" in spec:
            val = _resolve_scoped(parent, child, spec["path"])
            if val is None and "fallback" in spec:
                val = _resolve_scoped(parent, child, spec["fallback"])

        # Coerce, don't just strip: a SAP push serializes NUMC columns as JSON
        # numbers where OData sends strings (PurchaseOrderItem → 10 vs "10"), and
        # `strip` marks exactly the text-identity fields where that difference
        # would otherwise give the two transports differently-typed case fields.
        # `_resolve_key` already does this for document_number/item_id (its
        # `str(val or "")` return); this is the same normalisation for field_map.
        if spec.get("strip") and val is not None:
            val = str(val).strip()

        if "cast" in spec and val is not None:
            val = _cast(val, spec["cast"])

        if spec.get("omit_blank") and not str(val or "").strip():
            continue

        result[case_field] = val

    return result


# ---------------------------------------------------------------------------
# Natural key resolution
# ---------------------------------------------------------------------------


def _resolve_key(
    spec: dict, parent: dict | None, child: dict | None, child_idx: int
) -> str:
    """Resolve one field of the case's natural key from config."""
    source = spec.get("source", "self")
    if source == "parent":
        obj = parent or {}
    elif source == "child":
        obj = child or {}
    else:
        obj = child or parent or {}

    val = _ci_get(obj, spec["field"])

    if spec.get("strip") and isinstance(val, str):
        val = val.strip()

    if not val and spec.get("fallback"):
        val = spec["fallback"]

    if not val and spec.get("fallback_index"):
        val = str(child_idx + 1).zfill(4)

    return str(val or "")


# ---------------------------------------------------------------------------
# Core polling pipeline
# ---------------------------------------------------------------------------


def compose_filter(
    config: dict, now: datetime | None = None, since: str | None = None
) -> str | None:
    """The `$filter` this pack polls with — its own predicate, the age cutoff an
    `aging` pack gets from its kind, and, when the stack sets
    `pipeline.poll_created_since` (`since`, default `$POLL_CREATED_SINCE`) and the pack
    names `source.created_field`, only records created on or after that date.

    The cutoff is composed per poll rather than written into the config because a
    date literal in a config file is correct for one day. OData V2 wants a naive
    `datetime'...'`, and SAP returns a DATS field at midnight, so the boundary is a
    date rather than the current instant. V4 types a DATS as `Edm.Date`, whose
    literal is the bare date.
    """
    predicates = [p for p in [config.get("filter")] if p]

    aging = config.get("aging")
    if config.get("kind") == "aging" and aging:
        cutoff = (now or datetime.now(timezone.utc)).date() - timedelta(
            days=int(aging["older_than_days"])
        )
        literal = (
            str(cutoff)
            if config.get("odata_version") == "v4"
            else f"datetime'{cutoff}T00:00:00'"
        )
        predicates.append(f"{aging['field']} lt {literal}")

    since = since if since is not None else os.environ.get("POLL_CREATED_SINCE", "")
    if since and config.get("created_field"):
        day = date.fromisoformat(since)
        literal = (
            str(day)
            if config.get("odata_version") == "v4"
            else f"datetime'{day}T00:00:00'"
        )
        predicates.append(f"{config['created_field']} ge {literal}")

    if len(predicates) > 1:
        return " and ".join(f"({p})" for p in predicates)
    return predicates[0] if predicates else None


#: How many OData pages one poll will follow. SAP decides the page size, so this is a
#: stop on a remote-controlled loop, not a tuning knob — a `__next` that never clears
#: (or points at itself) would otherwise spin until the Lambda times out. Hitting it
#: prints, because a cap that truncates silently is the bug this paging fixes.
_MAX_POLL_PAGES = 20


#: A pack's OData names, as they may appear in the read's URL. A V2 service is one
#: segment; a V4 service is its path, segments of the same characters. No `.`, `?`,
#: `#` or `%`, so a pack cannot step out of its service or append a query of its own:
#: the poller reads as the system's service account, outside the Gateway's checks.
SERVICE_RE = re.compile(r"[A-Za-z0-9_]+(?:/[A-Za-z0-9_]+)*")
ENTITY_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


def query(config: dict, sap) -> tuple[str, dict]:
    """The URL and query options of this pack's read, before its `$filter`.

    V2 services sit under `/sap/opu/odata/sap/<service>`; a V4 `service` is its whole
    path under `/sap/opu/odata4/` (`sap/<group>/srvd_a2x/sap/<service>/0001`), and V4
    answers JSON without `$format`. Raises ValueError for a service or entity that is
    not an OData name.
    """
    for key, pattern in (("service", SERVICE_RE), ("entity", ENTITY_RE)):
        if not isinstance(config.get(key), str) or not pattern.fullmatch(config[key]):
            raise ValueError(f"source.{key} {config.get(key)!r} is not an OData name")
    if config.get("odata_version") == "v4":
        url, params = f"{sap.base_url}/sap/opu/odata4/{config['service']}", {}
    else:
        url = f"{sap.base_url}/sap/opu/odata/sap/{config['service']}"
        params = {"$format": "json"}
    if config.get("expand"):
        params["$expand"] = config["expand"]
    if config.get("select"):
        params["$select"] = config["select"]
    return f"{url}/{config['entity']}", params


def _fetch_pages(sap, url: str, params: dict, label: str) -> list[dict] | None:
    """Every record across every OData page, or None if a request failed.

    SAP Gateway caps a collection at its own page size and reports the rest with V2's
    `d.__next` or V4's `@odata.nextLink`, a URL carrying `$skiptoken` and the original
    query options; V4's may be relative to the request. Reading only the first page is
    permanently lossy: the next poll re-reads the same first page, so page 2's records
    are never ingested. `sap.get_json` refuses a next link on another host, since it
    would carry the Authorization header there.
    """
    records: list[dict] = []
    next_url: str | None = url
    for page in range(_MAX_POLL_PAGES):
        try:
            # A next link embeds its own query string; re-adding ours would duplicate it.
            body = sap.get_json(next_url, params if page == 0 else None)
        except SapError as e:
            print(f"{label} poll error: {e}")
            return None
        if "value" in body:
            records.extend(body["value"])
            link = body.get("@odata.nextLink")
        else:
            body = body.get("d", {})
            records.extend(body.get("results", []))
            link = body.get("__next")
        if not link:
            return records
        next_url = urllib.parse.urljoin(next_url, link)

    print(
        f"{label}: stopped after {_MAX_POLL_PAGES} pages, {len(records)} records read"
    )
    return records


def poll_pack(config: dict, sap, table, case_exists_fn, put_case_fn, enqueue_fn):
    """Run the polling pipeline for one pack. Returns (created, skipped, rejected).

    Never raises: one pack whose config or case writes blow up must not take the rest
    of the sweep with it. Deliberately not on `process_entities`, which the push route
    calls directly and where a raise is the 500 that makes SAP redeliver.
    """
    label = config.get("label", config["pack_id"])
    try:
        return _poll_pack(
            config, sap, table, case_exists_fn, put_case_fn, enqueue_fn, label
        )
    except Exception as e:  # noqa: BLE001 - one pack's failure, not the sweep's
        print(f"{label} poll failed: {type(e).__name__}: {e}")
        return 0, 0, 0


def _poll_pack(
    config: dict, sap, table, case_exists_fn, put_case_fn, enqueue_fn, label: str
) -> tuple[int, int, int]:
    if config.get("kind") == "event":
        # An `event` pack has no queryable predicate: SAP says something happened.
        print(f"Skipping {label}: kind 'event' arrives by push, not by polling")
        return 0, 0, 0

    url, params = query(config, sap)
    odata_filter = compose_filter(config)
    if odata_filter:
        params["$filter"] = odata_filter

    print(f"Polling SAP {sap.name} for {label}...")
    entities = _fetch_pages(sap, url, params, label)
    if entities is None:
        return 0, 0, 0
    return process_entities(
        config, entities, table, case_exists_fn, put_case_fn, enqueue_fn
    )


def process_entities(
    config: dict,
    entities: list[dict],
    table,
    case_exists_fn,
    put_case_fn,
    enqueue_fn,
    origin: str | None = None,
) -> tuple[int, int, int]:
    """
    Map entity records to cases, then create and enqueue the new ones.

    `origin="sap-push"` marks cases made from a sender's typed records rather than
    read from SAP, which the run checks read: a pushed amount never lifts the approval limit.

    Shared by both transports: `sap_poller` GETs the records,
    `erp_events` receives them from SAP. Same mapping, same dedupe,
    same case shape.

    Returns (created, skipped, rejected). A record the pack's mapping cannot read (an
    amount of "n/a") is `rejected` and the rest still run: raising would stop every
    record after it in SAP's order, on every poll, for as long as it stays in the
    result. Only the case table and the enqueue raise.
    """
    pack_id = config["pack_id"]
    label = config.get("label", pack_id)
    # `pack_id` is what the digest covers, and `short_id` the prefix a human
    # reads off the id. `iterate` is optional: a pack whose exceptions are one flat
    # entity has no child collection to walk.
    short_id = config["short_id"]
    key_specs = config["key"]
    kind = config.get("kind", _DEFAULT_KIND)
    iterate_cfg = config.get("iterate", {})
    field_map = config.get("field_map", {})
    skip_conditions = config.get("skip_when", [])

    created = skipped = rejected = 0
    nav_path = iterate_cfg.get("path")

    for entity in entities:
        if nav_path:
            nested = _ci_get(entity, nav_path)
            # OData V2 wraps children in {"results": [...]}; V4 and a SAP push send
            # the child rows as a plain array.
            children = (
                nested.get("results", []) if isinstance(nested, dict) else nested or []
            )
            if kind == "absence" and children:
                # For `absence` the empty collection *is* the exception, so a record
                # that has one is a record that is fine.
                skipped += 1
                continue
            if not children:
                # One pass with no child. What the record's key needs decides whether
                # that is a case: a key reading a child field resolves to nothing and
                # is skipped loudly below, so this cannot mint an id from an empty key.
                children = [{}]
            parent = entity
        else:
            children = [entity]
            parent = None

        for child_idx, child in enumerate(children):
            p = parent
            c = child if nav_path else None
            record = c or entity

            try:
                if any(_eval_skip(cond, p, c or child) for cond in skip_conditions):
                    skipped += 1
                    continue
                # The natural key, in declared order: this is what the case IS, and
                # what its id is a digest of. A field may also be stored as a
                # descriptive attribute (`as`), which is how an AP case keeps its
                # document_number.
                key = {}
                key_attributes = {}
                for spec in key_specs:
                    value = _resolve_key(spec, p, record, child_idx)
                    key[spec.get("name", spec["field"])] = value
                    if spec.get("as"):
                        key_attributes[spec["as"]] = value
                case_id = derive_case_id(pack_id, short_id, key)
            except CaseKeyError as e:
                # A key with a missing field identifies nothing, so there is no id to
                # mint: two different exceptions would derive the same one. Skip loudly
                # rather than collapsing them into one case.
                print(f"Skipping {label} case with unusable key: {e}")
                skipped += 1
                continue
            except Exception as e:  # noqa: BLE001 - one record, not the pack
                rejected += 1
                _log_rejected(label, None, e)
                continue

            if case_exists_fn(table, case_id):
                skipped += 1
                continue

            try:
                process_type = _resolve_process_type(config, p, record)
                mapped = _map_fields(field_map, p, record)
                abs_amount_val = mapped.get("amount")
                title_extra = {}
                if abs_amount_val is not None:
                    title_extra["abs_amount"] = f"{float(abs_amount_val):,.2f}"
                title = _render_title(config.get("title", ""), p, record, title_extra)
            except Exception as e:  # noqa: BLE001 - one record, not the pack
                rejected += 1
                _log_rejected(label, case_id, e)
                continue

            now = datetime.now(timezone.utc).isoformat()
            case_item = {
                "case_id": case_id,
                "key": key,
                "pack_id": pack_id,
                "process_type": process_type,
                "title": title,
                "status": _INITIAL_STATUS,
                "agent_traces": [],
                "created_at": now,
                "updated_at": now,
                **key_attributes,
                # Nested, not spread: what a pack pulls out of SAP is the pack's,
                # and the platform's own columns are not a namespace a config file can
                # write into. It is also what lets this poller create a case for a
                # pack whose fields the schema has never heard of.
                "attributes": mapped,
            }
            if origin:
                case_item["origin"] = origin

            # A failed write raises: on push that is the 500 that makes SAP resend,
            # and the conditional put makes the resend idempotent. A case put but not
            # enqueued stays `detected`, which the batch sweeper picks up.
            if put_case_fn(table, case_item):
                created += 1
                print(f"Created {label} case: {_describe(case_id, key)}")
                enqueue_fn(case_id, pack_id, process_type)
            else:
                skipped += 1

    return created, skipped, rejected


def _log_rejected(label: str, case_id: str | None, e: Exception) -> None:
    """The pack, the id if one was derived, and the error's type. Never its message:
    `float("n/a")` puts the record's own value in it."""
    print(
        f"Rejected one {label} record{f' ({case_id})' if case_id else ''}: {type(e).__name__}"
    )


def _describe(case_id: str, key: dict) -> str:
    """The id, then the natural key that produced it: a minted id is unreadable alone."""
    return f"{case_id} ({', '.join(f'{k}={v}' for k, v in key.items())})"
