# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
from utils.dates import days_between


def test_days_between_counts_calendar_and_weekdays():
    # 2026-09-13 is a Sunday; the failing call was busday_count(9-13, 11-4).
    assert days_between("2026-09-13", "2026-11-04") == {
        "calendar_days": 52,
        "business_days": 37,
    }
    assert days_between("2026-11-06", "2026-11-09") == {  # Friday to Monday
        "calendar_days": 3,
        "business_days": 1,
    }
    assert days_between("2026-11-09", "2026-11-06")["business_days"] == -1
    assert days_between("2026-08-31T00:00:00", "2026-08-31")["calendar_days"] == 0
    assert "error" in days_between("31/08/2026", "2026-08-31")


def test_current_time_takes_an_iana_zone():
    from utils.dates import current_time

    assert current_time().endswith("+00:00")
    assert "unknown timezone" in current_time("Mars/Olympus")
