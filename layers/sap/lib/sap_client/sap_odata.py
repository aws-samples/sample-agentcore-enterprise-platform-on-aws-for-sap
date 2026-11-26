# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""What any direct SAP write needs over OData: the CSRF session, a POST, the open period.

For a use case's own function that `layers.sap.access.<use case>.bypass_gateway` lets write to
SAP outside the Gateway (a test-data seeder); copy this file and `sap_http.py` into
its asset, flat. Stdlib only. The origin pinning and the no-redirect rule that keep the
Authorization header on the SAP host are `sap_http`'s.
"""

from __future__ import annotations

import calendar
import html
import http.cookiejar
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone

import sap_http

ODATA_ROOT = "/sap/opu/odata/sap"
LOCK_RETRIES = 4


class _Session:
    """One cookie jar: SAP binds a CSRF token to the session that fetched it."""

    def __init__(self, sap: sap_http.SapSystem):
        self.sap = sap
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
            sap_http._NoRedirect,
        )

    def request(self, method: str, path: str, body=None, headers=None):
        """(status, headers, text). Raises RuntimeError only when SAP is unreachable."""
        url = f"{self.sap.base_url}{path}"
        if self.sap.client:
            url += ("&" if "?" in path else "?") + urllib.parse.urlencode(
                {"sap-client": self.sap.client}
            )
        if body is not None and not isinstance(body, bytes):
            body = json.dumps(body).encode()
        req = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={
                "Accept": "application/json",
                "Authorization": self.sap.header(),
                **(headers or {}),
            },
        )
        try:
            with self.opener.open(req, timeout=30) as resp:  # nosec B310 - https only (sap_http), no redirects
                return resp.status, resp.headers, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError) as e:
            raise RuntimeError(
                f"{self.sap.name} unreachable: {type(e).__name__}"
            ) from None


def _locked(text: str) -> bool:
    return "ME/006" in text or "already processing" in text


def post(sap: sap_http.SapSystem, service: str, entity: str, body: dict) -> dict:
    """POST one document to an entity set. Returns the parsed `d` object, or {}.

    Some SAP versions answer a create with 201 and a body that is not JSON: a success
    the caller must be able to tell from a failure, hence {} rather than an error.
    """
    for attempt in range(LOCK_RETRIES + 1):
        session = _Session(sap)
        status, headers, _ = session.request(
            "GET", f"{ODATA_ROOT}/{service}/", headers={"X-CSRF-Token": "Fetch"}
        )
        if status != 200:
            raise RuntimeError(f"{service} CSRF token fetch failed: HTTP {status}")
        status, _, text = session.request(
            "POST",
            f"{ODATA_ROOT}/{service}/{entity}",
            body,
            {
                "Content-Type": "application/json",
                "X-CSRF-Token": headers.get("X-CSRF-Token") or "",
            },
        )
        # A PO posted moments ago is still enqueue-locked by its own update task
        # (ME/006). SAP refused the create outright, so a retry cannot post twice.
        if status in (200, 201) or not _locked(text):
            break
        if attempt < LOCK_RETRIES:
            time.sleep(2 * (attempt + 1))
    if status not in (200, 201):
        # SAP's own message is the useful answer (an admin reads it); it carries no
        # credential.
        raise RuntimeError(f"{entity} creation failed ({status}): {text[:500]}")
    try:
        return json.loads(text)["d"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return {}


#: ADT's SQL console, the one read of a configuration table that needs no custom
#: service: the open periods are in MARV and T001B, which no OData service on a
#: standard S/4HANA publishes (SAP's API catalog has no released posting-period API).
#: The caller's Basic user needs ADT data preview; an OAuth token has no scope for ADT.
ADT_SQL = "/sap/bc/adt/datapreview/freestyle?rowNumber=10"
_COMPANY_CODE_RE = re.compile(r"^[A-Z0-9]{4}$")
# T001-OPVAR, read back from SAP and then put into the second query, so held to its
# domain (CHAR 4) like the company code rather than trusted because SAP sent it.
_VARIANT_RE = re.compile(r"^[A-Z0-9]{1,4}$")
_COLUMN_RE = re.compile(r"<dataPreview:columns>(.*?)</dataPreview:columns>", re.DOTALL)
_DATA_RE = re.compile(r"<dataPreview:data(?:/>|>([^<]*)</dataPreview:data>)")


def _rows(text: str) -> list[dict]:
    """ADT's data preview, column by column, as rows."""
    columns = {}
    for column in _COLUMN_RE.findall(text):
        name = re.search(r'dataPreview:name="([^"]+)"', column)
        if name:
            columns[name[1]] = [html.unescape(v) for v in _DATA_RE.findall(column)]
    count = min(map(len, columns.values()), default=0)
    return [{k: v[i] for k, v in columns.items()} for i in range(count)]


def _select(session: _Session, token: str, query: str) -> list[dict]:
    status, _, text = session.request(
        "POST",
        ADT_SQL,
        query.encode(),
        {
            "Accept": "application/vnd.sap.adt.datapreview.table.v1+xml",
            "Content-Type": "text/plain",
            "X-CSRF-Token": token,
        },
    )
    if status != 200:
        raise RuntimeError(f"SAP refused the period read: HTTP {status}")
    return _rows(text)


def _period(year, period) -> tuple[int, int]:
    return int(year), int(period)


def open_posting_date(
    sap: sap_http.SapSystem, company_code: str, today: date | None = None
) -> str:
    """The latest date at or before `today` that SAP lets both MM and FI post on.

    MM's period (MARV) is the current one and, when backposting is allowed, the one
    before: a goods receipt or an invoice outside them is refused M7/053 or M8/535.
    FI's (T001B, the company code's variant, every account type) must also be open.
    A period is a calendar month, so a fiscal-year variant that is not refuses.
    Raises RuntimeError naming what could not be read. A token-flow system is
    refused before any call: ADT answers a client-credentials token 403.
    """
    if sap.oauth:
        raise RuntimeError(
            f"the system signs in with {sap.oauth.get('flow')}, and the period is "
            "read through ADT, which needs Basic"
        )
    if not _COMPANY_CODE_RE.fullmatch(company_code):
        raise RuntimeError(f"{company_code!r} is not a company code")
    session = _Session(sap)
    _, headers, _ = session.request(
        "GET", "/sap/bc/adt/discovery", headers={"X-CSRF-Token": "Fetch"}
    )
    token = headers.get("X-CSRF-Token") or ""
    mm = _select(
        session,
        token,
        "SELECT m~LFGJA, m~LFMON, m~VMGJA, m~VMMON, m~XRUEM, c~OPVAR, v~XKALE "
        "FROM MARV AS m INNER JOIN T001 AS c ON c~BUKRS = m~BUKRS "
        "INNER JOIN T009 AS v ON v~PERIV = c~PERIV "
        f"WHERE m~BUKRS = '{company_code}'",
    )
    if not mm:
        raise RuntimeError(f"SAP has no MM period for company code {company_code}")
    row = mm[0]
    if not _VARIANT_RE.fullmatch(row["OPVAR"]):
        raise RuntimeError(
            f"SAP returned {row['OPVAR']!r} as {company_code}'s posting period variant"
        )
    if row["XKALE"] != "X":
        raise RuntimeError(f"{company_code}'s fiscal year is not calendar months")
    fi = _select(
        session,
        token,
        "SELECT FRYE1, FRPE1, TOYE1, TOPE1, FRYE2, FRPE2, TOYE2, TOPE2 FROM T001B "
        f"WHERE BUKRS = '{row['OPVAR']}' AND MKOAR = '+'",
    )
    periods = [_period(row["LFGJA"], row["LFMON"])]
    if row["XRUEM"] == "X":
        periods.append(_period(row["VMGJA"], row["VMMON"]))
    today = today or datetime.now(timezone.utc).date()
    best = None
    for year, month in periods:
        fi_open = any(
            _period(r[f"FRYE{n}"], r[f"FRPE{n}"])
            <= (year, month)
            <= _period(r[f"TOYE{n}"], r[f"TOPE{n}"])
            for r in fi
            for n in (1, 2)
        )
        if not fi_open or date(year, month, 1) > today:
            continue
        last = min(date(year, month, calendar.monthrange(year, month)[1]), today)
        best = last if best is None else max(best, last)
    if best is None:
        raise RuntimeError(
            f"{company_code} has no period open at or before {today} "
            f"(MM {', '.join(f'{y}/{m:02d}' for y, m in periods)})"
        )
    return best.isoformat()
