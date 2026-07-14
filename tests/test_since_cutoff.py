from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta

import pytest

from imp.cli.main import _SINCE_ERROR, _since_cutoff


@pytest.fixture
def utc_tz():
    """Pin the process timezone to UTC so anchors are deterministic."""
    prev = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = prev
        time.tzset()


# 2026-07-15 is a Wednesday (weekday()==2); its ISO-week Monday is 2026-07-13.
_NOW = datetime(2026, 7, 15, 15, 30, tzinfo=UTC)


def test_anchor_today_is_midnight(utc_tz) -> None:
    assert _since_cutoff("today", _NOW) == datetime(2026, 7, 15, tzinfo=UTC)


def test_anchor_yesterday_is_prior_midnight(utc_tz) -> None:
    assert _since_cutoff("yesterday", _NOW) == datetime(2026, 7, 14, tzinfo=UTC)


def test_anchor_this_week_is_monday_midnight(utc_tz) -> None:
    assert _since_cutoff("this-week", _NOW) == datetime(2026, 7, 13, tzinfo=UTC)


def test_anchor_this_month_is_first_of_month(utc_tz) -> None:
    assert _since_cutoff("this-month", _NOW) == datetime(2026, 7, 1, tzinfo=UTC)


def test_rolling_hours_days_weeks() -> None:
    assert _since_cutoff("24h", _NOW) == _NOW - timedelta(hours=24)
    assert _since_cutoff("2d", _NOW) == _NOW - timedelta(days=2)
    assert _since_cutoff("1w", _NOW) == _NOW - timedelta(weeks=1)


def test_rolling_months_and_years_are_approximate() -> None:
    assert _since_cutoff("3m", _NOW) == _NOW - timedelta(days=90)
    assert _since_cutoff("1y", _NOW) == _NOW - timedelta(days=365)


def test_parsing_is_case_insensitive(utc_tz) -> None:
    assert _since_cutoff("TODAY", _NOW) == datetime(2026, 7, 15, tzinfo=UTC)
    assert _since_cutoff("2D", _NOW) == _NOW - timedelta(days=2)


def test_unrecognized_spec_raises_value_error() -> None:
    for bad in ("banana", "5x", "", "2 d", "d2"):
        with pytest.raises(ValueError):
            _since_cutoff(bad, _NOW)


def test_since_error_message() -> None:
    assert _SINCE_ERROR == (
        "imp: --since expects 'today' or a number with a unit — "
        "h/d/w/m/y (e.g. 2d, 1w, 3m).")
