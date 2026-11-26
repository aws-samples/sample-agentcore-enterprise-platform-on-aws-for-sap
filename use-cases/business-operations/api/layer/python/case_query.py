# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The case list's query: its parameters, what a case matches, its order and its cursor.

`GET /cases` and `GET /cases/counts` read the cases table's `pack-status-updated-index`
(partition key `pack_id` and `status`, sort key `updated_at`), one partition per pack and
status. Everything here is pure, so the handler holds only the reads.

The packs a query reads always come from the caller's entitlement and `pack_id`, never
from a `next_token`: a token carries only where the last page ended and a digest of the
request it belongs to, so a forged one can move the cursor and nothing else.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from case_status import VIEWS
from contracts import CASE_STATUSES

INDEX = "pack-status-updated-index"
#: `sort=amount`'s index: the same partitions, ordered by :data:`AMOUNT_SORT`.
AMOUNT_INDEX = "pack-status-amount-index"
#: The top-level number every case is written with (:func:`amount_sort`).
AMOUNT_SORT = "amount_sort"
#: What a case with no readable amount sorts as: below every amount, so last when
#: descending and first when ascending. An index has one place for a value, not two.
NO_AMOUNT = Decimal("-1E+100")
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
#: Index entries one search reads per pack and status before it hands back a cursor.
READ_BUDGET = 1000
MAX_Q = 64
MAX_EXCEPTION_TYPES = 20
MAX_PACKS = 50
SORTS = ("updated_at", "amount")
ORDERS = ("asc", "desc")

_LIST_PARAMS = frozenset(
    {"view", "q", "exception_type", "pack_id", "sort", "order", "limit", "next_token"}
)
_COUNT_PARAMS = frozenset({"q", "exception_type", "pack_id", "by"})
#: What `GET /cases/counts?by=` may break the views down by.
COUNT_BY = ("exception_type",)
_REPEATABLE = frozenset({"exception_type", "pack_id"})
_TYPE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_PACK = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class QueryError(ValueError):
    """A parameter this query refuses, with the parameter's name."""

    def __init__(self, param: str, message: str):
        super().__init__(message)
        self.param = param


def view_statuses(view: str) -> tuple[str, ...]:
    """The statuses a view reads, in the schema's order."""
    statuses = VIEWS[view][0]
    return tuple(s for s in CASE_STATUSES if statuses is None or s in statuses)


@dataclass(frozen=True)
class Query:
    view: str = "all"
    q: str = ""
    exception_types: tuple[str, ...] = ()
    pack_ids: tuple[str, ...] = ()
    sort: str = "updated_at"
    order: str = "desc"
    limit: int = DEFAULT_LIMIT
    after: tuple | None = field(default=None)
    #: `GET /cases/counts?by=exception_type`: also count each view by exception type.
    by_exception: bool = False

    @property
    def asc(self) -> bool:
        return self.order == "asc"

    @property
    def filtered(self) -> bool:
        return bool(self.q or self.exception_types)

    def digest(self) -> str:
        """What a `next_token` is bound to: every parameter but the cursor itself."""
        doc = [
            self.view,
            self.q,
            sorted(self.exception_types),
            sorted(self.pack_ids),
            self.sort,
            self.order,
            self.limit,
        ]
        return hashlib.sha256(json.dumps(doc).encode()).hexdigest()[:16]


def params(event: dict) -> dict[str, list[str]]:
    """Every query parameter as a list of its values, as API Gateway passes them."""
    multi = event.get("multiValueQueryStringParameters") or {}
    single = event.get("queryStringParameters") or {}
    out = {k: list(v) for k, v in multi.items() if v is not None}
    for k, v in single.items():
        out.setdefault(k, [v])
    return out


def _one(raw: dict[str, list[str]], name: str) -> str | None:
    values = raw.get(name)
    if not values:
        return None
    if len(values) > 1:
        raise QueryError(name, f"{name} takes one value")
    return values[0]


def _filters(raw: dict[str, list[str]]) -> dict:
    q = _one(raw, "q")
    if q is not None:
        q = q.strip()
        if not 1 <= len(q) <= MAX_Q or _CONTROL.search(q):
            raise QueryError(
                "q", f"q must be 1-{MAX_Q} characters, with no control characters"
            )
    types = raw.get("exception_type") or []
    if len(types) > MAX_EXCEPTION_TYPES or not all(_TYPE.match(t) for t in types):
        raise QueryError(
            "exception_type",
            f"exception_type takes at most {MAX_EXCEPTION_TYPES} snake_case process types",
        )
    packs = raw.get("pack_id") or []
    if len(packs) > MAX_PACKS or not all(_PACK.match(p) for p in packs):
        raise QueryError("pack_id", f"pack_id takes at most {MAX_PACKS} pack ids")
    return {
        "q": (q or "").lower(),
        "exception_types": tuple(dict.fromkeys(types)),
        "pack_ids": tuple(dict.fromkeys(packs)),
    }


def _unknown(raw: dict[str, list[str]], allowed: frozenset[str]) -> None:
    for name, values in raw.items():
        if name not in allowed:
            raise QueryError(name, f"Unknown parameter {name}")
        if name not in _REPEATABLE and len(values) > 1:
            raise QueryError(name, f"{name} takes one value")


def parse_counts(raw: dict[str, list[str]]) -> Query:
    """`GET /cases/counts`: the filters only."""
    _unknown(raw, _COUNT_PARAMS)
    by = _one(raw, "by")
    if by is not None and by not in COUNT_BY:
        raise QueryError("by", f"by must be one of {', '.join(COUNT_BY)}")
    return Query(**_filters(raw), by_exception=by == "exception_type")


def parse_list(raw: dict[str, list[str]]) -> Query:
    """`GET /cases`: every parameter, checked, with the view's default order."""
    _unknown(raw, _LIST_PARAMS)
    filters = _filters(raw)
    view = _one(raw, "view") or "all"
    if view not in VIEWS:
        raise QueryError("view", f"view must be one of {', '.join(VIEWS)}")
    sort = _one(raw, "sort") or "updated_at"
    if sort not in SORTS:
        raise QueryError("sort", f"sort must be one of {', '.join(SORTS)}")
    order = _one(raw, "order") or VIEWS[view][1]
    if order not in ORDERS:
        raise QueryError("order", "order must be asc or desc")
    raw_limit = _one(raw, "limit")
    try:
        limit = int(raw_limit) if raw_limit is not None else DEFAULT_LIMIT
    except ValueError:
        limit = 0
    if not 1 <= limit <= MAX_LIMIT:
        raise QueryError("limit", f"limit must be an integer 1-{MAX_LIMIT}")
    query = Query(view=view, sort=sort, order=order, limit=limit, **filters)
    token = _one(raw, "next_token")
    if token:
        return Query(**{**query.__dict__, "after": decode_token(token, query)})
    return query


# ── what a case matches, and where it sorts ──


def list_fields(fields: dict | None) -> tuple[str | None, str | None]:
    """`(amount, party)`: the attributes the Cases table shows beside the fixed columns.

    The pack's first `amount` field, and its first plain `string` field that is not a
    reason, not hidden from the panel and not a currency field: the console's own rule
    (`tableFields` in `frontend/src/lib/caseViews.ts`), so the search matches the column
    a person reads.
    """
    fields = fields if isinstance(fields, dict) else {}
    currencies = {
        f.get("currency_field") for f in fields.values() if isinstance(f, dict)
    }
    amount = party = None
    for name, spec in fields.items():
        if not isinstance(spec, dict):
            continue
        if amount is None and spec.get("type") == "amount":
            amount = name
        if (
            party is None
            and spec.get("type") == "string"
            and spec.get("role") != "reason"
            and spec.get("panel") is not False
            and name not in currencies
        ):
            party = name
    return amount, party


def exception_of(entry: dict) -> str | None:
    """The type the filter and the Exception column use: `exception_type`, or the
    process type while the agent has identified none."""
    return entry.get("exception_type") or entry.get("process_type")


def matches(entry: dict, query: Query, party: str | None) -> bool:
    """Does this index entry pass the query's search and exception filter?"""
    if query.exception_types and exception_of(entry) not in query.exception_types:
        return False
    if not query.q:
        return True
    attributes = (
        entry.get("attributes") if isinstance(entry.get("attributes"), dict) else {}
    )
    candidates = (
        entry.get("document_number"),
        attributes.get(party) if party else None,
        entry.get("case_id"),
    )
    return any(str(c).lower().startswith(query.q) for c in candidates if c is not None)


def amount_of(entry: dict, amount: str | None) -> Decimal | None:
    attributes = (
        entry.get("attributes") if isinstance(entry.get("attributes"), dict) else {}
    )
    value = attributes.get(amount) if amount else None
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def amount_sort(attributes: object, fields: dict | None) -> Decimal:
    """What a case is written with as :data:`AMOUNT_SORT`: the pack's first `amount`
    field (the Cases table's column, :func:`list_fields`), or :data:`NO_AMOUNT`.

    Every writer of a new case calls this, so the index holds every case.
    """
    value = amount_of({"attributes": attributes}, list_fields(fields)[0])
    return NO_AMOUNT if value is None else value


def index_for(query: Query) -> tuple[str, str]:
    """`(index, its sort key attribute)` for this query's order."""
    if query.sort == "amount":
        return AMOUNT_INDEX, AMOUNT_SORT
    return INDEX, "updated_at"


def sort_key(entry: dict, query: Query) -> tuple:
    """A total order: the sort value, then `case_id`. JSON-safe, as a cursor carries it."""
    case_id = str(entry.get("case_id") or "")
    if query.sort == "amount":
        value = entry.get(AMOUNT_SORT)
        return (str(Decimal(str(value)) if value is not None else NO_AMOUNT), case_id)
    return (str(entry.get("updated_at") or ""), case_id)


def _compare(key: tuple, query: Query) -> tuple:
    """A sort key as Python compares it: an amount compares as a number."""
    if query.sort == "amount":
        return (Decimal(key[0]), key[1])
    return key


def ordered(
    entries: list[tuple[tuple, dict]], query: Query
) -> list[tuple[tuple, dict]]:
    """Entries in the query's order."""
    return sorted(entries, key=lambda e: _compare(e[0], query), reverse=not query.asc)


def beyond(key: tuple, cursor: tuple | None, query: Query) -> bool:
    """Does `key` come after `cursor` in the query's order?"""
    if cursor is None:
        return True
    a, b = _compare(key, query), _compare(cursor, query)
    return a > b if query.asc else a < b


# ── the cursor ──


def encode_token(query: Query, after: tuple) -> str:
    state = {"v": 1, "d": query.digest(), "after": list(after)}
    return base64.urlsafe_b64encode(json.dumps(state).encode()).decode()


def decode_token(token: str, query: Query) -> tuple:
    """The cursor a `next_token` carries, checked against this request's parameters."""
    bad = QueryError("next_token", "Invalid next_token")
    if len(token) > 2048:
        raise bad
    try:
        state = json.loads(base64.urlsafe_b64decode(token.encode()))
    except (binascii.Error, ValueError):
        raise bad from None
    if (
        not isinstance(state, dict)
        or set(state) != {"v", "d", "after"}
        or state["v"] != 1
        or state["d"] != query.digest()
        or not isinstance(state["after"], list)
    ):
        raise bad
    after = state["after"]
    ok = len(after) == 2 and all(isinstance(v, str) for v in after)
    if ok and query.sort == "amount":
        try:
            ok = Decimal(after[0]).is_finite()
        except InvalidOperation:
            ok = False
    if not ok or any(len(str(v)) > 256 for v in after):
        raise bad
    return tuple(after)


# ── one page ──


@dataclass
class Partition:
    """One pack and status as a search read it."""

    entries: list[tuple[tuple, dict]]
    #: Where the read stopped when the partition was not read to its end, else None.
    frontier: tuple | None
    #: Index entries read, matched or not.
    read: int = 0


def page(partitions: list[Partition], query: Query) -> tuple[list[dict], tuple | None]:
    """`(entries, cursor)`: the page across every partition, and where the next starts.

    An entry beyond a partition's frontier is held back: the partition has unread
    entries that may sort before it. The cursor is then the earliest frontier, so the
    next page resumes where the least-read partition stopped; a page can be short, even
    empty, and still have a cursor. `frontier` is always an entry the partition read, so
    the cursor never passes an entry no partition has returned.
    """
    candidates = ordered(
        [e for p in partitions for e in p.entries if beyond(e[0], query.after, query)],
        query,
    )
    frontiers = [p.frontier for p in partitions if p.frontier is not None]
    stop = None
    if frontiers:
        stop = ordered([(f, {}) for f in frontiers], query)[0][0]
        candidates = [e for e in candidates if not beyond(e[0], stop, query)]
    taken = candidates[: query.limit]
    if len(candidates) > query.limit:
        return [e[1] for e in taken], taken[-1][0]
    # Every candidate up to the earliest frontier was taken, so the next page starts there.
    return [e[1] for e in taken], stop


# ── the done summary ──

SUMMARY_DAYS = 30
_SUMMARY_PARAMS = frozenset({"since", "pack_id"})


@dataclass(frozen=True)
class Summary:
    since: datetime
    pack_ids: tuple[str, ...] = ()


def parse_summary(raw: dict[str, list[str]], now: datetime) -> Summary:
    """`GET /cases/summary`: `since`, at most 30 days back, and `pack_id`."""
    _unknown(raw, _SUMMARY_PARAMS)
    packs = _filters({"pack_id": raw.get("pack_id") or []})["pack_ids"]
    value = _one(raw, "since")
    if value is None:
        return Summary(now - timedelta(days=1), packs)
    message = (
        f"since must be an ISO 8601 time with a zone, at most {SUMMARY_DAYS} days ago"
    )
    try:
        since = datetime.fromisoformat(value) if len(value) <= 40 else None
    except ValueError:
        since = None
    if since is None:
        raise QueryError("since", message)
    if since.tzinfo is None or not (now - timedelta(days=SUMMARY_DAYS) <= since <= now):
        raise QueryError("since", message)
    return Summary(since.astimezone(UTC), packs)


def _bucket() -> dict:
    return {"closed": 0, "by_outcome": {}, "value": {}, "agent_cost_usd": Decimal(0)}


def _add(
    bucket: dict, outcome: str, currency: str, amount: Decimal | None, cost
) -> None:
    bucket["closed"] += 1
    row = bucket["by_outcome"].setdefault(outcome, {"cases": 0, "value": {}})
    row["cases"] += 1
    if amount is not None:
        for values in (bucket["value"], row["value"]):
            values[currency] = values.get(currency, Decimal(0)) + amount
    try:
        bucket["agent_cost_usd"] += Decimal(str(cost)) if cost is not None else 0
    except InvalidOperation:
        pass


def summarize(
    entries: list[tuple[dict, dict | None]], since: datetime, now: datetime
) -> dict:
    """Totals and one row per UTC day from `since` to `now`, over closed cases'
    index entries, each with its pack's `fields`."""
    totals = _bucket()
    days: dict[str, dict] = {}
    day = since.date()
    while day <= now.date():
        days[day.isoformat()] = _bucket()
        day += timedelta(days=1)
    for entry, fields in entries:
        amount_field, _ = list_fields(fields)
        currency_field = (
            ((fields or {}).get(amount_field) or {}).get("currency_field")
            if amount_field
            else None
        )
        attributes = (
            entry.get("attributes") if isinstance(entry.get("attributes"), dict) else {}
        )
        currency = str(attributes.get(currency_field) or "") if currency_field else ""
        amount = amount_of(entry, amount_field)
        outcome = str(entry.get("disposition") or "none")
        cost = (entry.get("cost_summary") or {}).get("total_cost_usd")
        closed_on = str(entry.get("updated_at") or "")[:10]
        for bucket in (totals, days.get(closed_on)):
            if bucket is not None:
                _add(bucket, outcome, currency or "unknown", amount, cost)
    return {
        "totals": totals,
        "days": [{"date": d, **b} for d, b in days.items()],
    }
