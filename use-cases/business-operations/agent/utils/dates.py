# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Date arithmetic for the agent: the calculator evaluates numbers, never dates.

SOPs count days ("within N business days of receipt"), and the calculator cannot parse
a date, so a model left to it passes `datetime(...)` or `busday_count(...)` and gets an
error back. Stdlib only.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def days_between(start_date: str, end_date: str) -> dict:
    """Days from start_date to end_date, both YYYY-MM-DD (a SAP date's first ten characters).

    calendar_days counts every day; business_days counts Monday to Friday from
    start_date up to but not including end_date, ignoring public holidays. Both are
    negative when end_date is earlier.
    """
    try:
        start, end = (
            date.fromisoformat(start_date[:10]),
            date.fromisoformat(end_date[:10]),
        )
    except (TypeError, ValueError):
        return {"error": "start_date and end_date must be YYYY-MM-DD"}
    sign, low, high = (1, start, end) if start <= end else (-1, end, start)
    weeks, rest = divmod((high - low).days, 7)
    business = weeks * 5 + sum(
        (low + timedelta(days=i)).weekday() < 5 for i in range(rest)
    )
    return {"calendar_days": (end - start).days, "business_days": sign * business}


def current_time(timezone: str = "UTC") -> str:
    """The current date and time in ISO 8601, in an IANA timezone such as Europe/Berlin."""
    try:
        return datetime.now(ZoneInfo(timezone)).isoformat()
    except (ValueError, ZoneInfoNotFoundError):
        return f"unknown timezone {timezone!r}; use an IANA name such as UTC or Europe/Berlin"
