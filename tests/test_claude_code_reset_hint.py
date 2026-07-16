"""_reset_hint_seconds: parse 'resets <time> (<zone>)' into seconds-to-wait."""

from __future__ import annotations

import zoneinfo
from datetime import datetime

import imp.providers.claude_code as cc
from imp.providers import errors as perr
from imp.providers.claude_code import _reset_hint_seconds
from tests.cc_fakes import result

_LA = zoneinfo.ZoneInfo("America/Los_Angeles")


def _epoch(y: int, mo: int, d: int, h: int, mi: int) -> float:
    return datetime(y, mo, d, h, mi, tzinfo=_LA).timestamp()


def test_4am_same_day_plus_margin() -> None:
    # now 01:00, resets 04:00 same day -> 3h + 120s
    now = _epoch(2026, 1, 15, 1, 0)
    assert _reset_hint_seconds("resets 4am (America/Los_Angeles)", now) == 10920.0


def test_430pm_same_day() -> None:
    # now 14:00, resets 16:30 -> 2h30m + 120s
    now = _epoch(2026, 1, 15, 14, 0)
    assert _reset_hint_seconds("resets 4:30pm (America/Los_Angeles)", now) == 9120.0


def test_24_hour_form() -> None:
    # now 14:00, resets 16:00 (no am/pm) -> 2h + 120s
    now = _epoch(2026, 1, 15, 14, 0)
    assert _reset_hint_seconds("resets 16:00 (America/Los_Angeles)", now) == 7320.0


def test_rolls_over_to_tomorrow() -> None:
    # now 23:00, resets 02:00 already passed today -> tomorrow 02:00 -> 3h + 120s
    now = _epoch(2026, 1, 15, 23, 0)
    assert _reset_hint_seconds("resets 2am (America/Los_Angeles)", now) == 10920.0


def test_over_six_hours_returns_none() -> None:
    # now 05:00, resets 04:00 already passed -> tomorrow 04:00 (~23h) > 6h -> None
    now = _epoch(2026, 1, 15, 5, 0)
    assert _reset_hint_seconds("resets 4am (America/Los_Angeles)", now) is None


def test_same_day_over_six_hours_returns_none() -> None:
    # now 01:00, resets 23:00 same day (~22h) > 6h -> None
    now = _epoch(2026, 1, 15, 1, 0)
    assert _reset_hint_seconds("resets 11pm (America/Los_Angeles)", now) is None


def test_unknown_zone_returns_none() -> None:
    now = _epoch(2026, 1, 15, 1, 0)
    assert _reset_hint_seconds("resets 4am (Mars/Phobos)", now) is None


def test_no_match_returns_none() -> None:
    now = _epoch(2026, 1, 15, 1, 0)
    assert _reset_hint_seconds("I edited the file and ran the tests.", now) is None


def test_dst_transition_day_is_zone_aware() -> None:
    # now 00:30 PST on spring-forward day, resets 04:00 PDT.
    # Real elapsed accounts for the lost 02:00-03:00 hour: 2h30m + 120s.
    now = _epoch(2026, 3, 8, 0, 30)
    assert _reset_hint_seconds("resets 4am (America/Los_Angeles)", now) == 9120.0


def test_case_insensitive_and_uppercase_ampm() -> None:
    now = _epoch(2026, 1, 15, 14, 0)
    assert _reset_hint_seconds("RESETS 4:30PM (America/Los_Angeles)", now) == 9120.0


def test_12am_normalizes_to_midnight() -> None:
    # now 22:00, resets 12am -> next midnight 00:00 tomorrow -> 2h + 120s
    now = _epoch(2026, 1, 15, 22, 0)
    assert _reset_hint_seconds("resets 12am (America/Los_Angeles)", now) == 7320.0


def test_12pm_normalizes_to_noon() -> None:
    # now 10:00, resets 12pm -> noon 12:00 same day -> 2h + 120s
    now = _epoch(2026, 1, 15, 10, 0)
    assert _reset_hint_seconds("resets 12pm (America/Los_Angeles)", now) == 7320.0


def test_hour_zero_with_ampm_returns_none() -> None:
    now = _epoch(2026, 1, 15, 1, 0)
    assert _reset_hint_seconds("resets 0am (America/Los_Angeles)", now) is None


def test_hour_thirteen_with_ampm_returns_none() -> None:
    now = _epoch(2026, 1, 15, 1, 0)
    assert _reset_hint_seconds("resets 13am (America/Los_Angeles)", now) is None


def test_minute_out_of_range_returns_none() -> None:
    now = _epoch(2026, 1, 15, 1, 0)
    assert _reset_hint_seconds("resets 4:60 (America/Los_Angeles)", now) is None


def test_session_limit_branch_attaches_hint(monkeypatch) -> None:
    monkeypatch.setattr(cc, "_reset_hint_seconds", lambda text, now: 7200.0)
    exc = cc._classify_agent_text(
        "You've hit your session limit · resets 4am (America/Los_Angeles)")
    assert isinstance(exc, perr.RateLimitError)
    assert exc.detail["retry_after_seconds"] == 7200.0


def test_429_branch_attaches_hint(monkeypatch) -> None:
    monkeypatch.setattr(cc, "_reset_hint_seconds", lambda text, now: 5400.0)
    exc = cc._classify_agent_text("API Error: 429 rate limited")
    assert isinstance(exc, perr.RateLimitError)
    assert exc.detail["retry_after_seconds"] == 5400.0


def test_no_hint_attaches_nothing(monkeypatch) -> None:
    monkeypatch.setattr(cc, "_reset_hint_seconds", lambda text, now: None)
    exc = cc._classify_agent_text(
        "You've hit your session limit · resets 4am (America/Los_Angeles)")
    assert isinstance(exc, perr.RateLimitError)
    assert "retry_after_seconds" not in exc.detail


def test_hint_survives_termination_merge(monkeypatch) -> None:
    monkeypatch.setattr(cc, "_reset_hint_seconds", lambda text, now: 9000.0)
    sdk = result(is_error=True, api_error_status=None)
    term = cc._termination_from_result(
        sdk,
        "You've hit your session limit · resets 4am (America/Los_Angeles)",
        {},
    )
    assert isinstance(term, perr.RateLimitError)
    # the hint set at classification survives labeled.detail.update(diagnostic)...
    assert term.detail["retry_after_seconds"] == 9000.0
    # ...and the disjoint diagnostic keys are merged in alongside it.
    assert term.detail["is_error"] is True


def test_weekly_dated_same_day_plus_margin() -> None:
    # now Jul 16 09:00, resets Jul 16 8pm -> 11h + 120s
    now = _epoch(2026, 7, 16, 9, 0)
    assert _reset_hint_seconds(
        "You've hit your weekly limit · resets Jul 16, 8pm (America/Los_Angeles)",
        now,
    ) == 39720.0


def test_weekly_dated_rolls_over_to_next_year() -> None:
    # now Dec 31 23:00, resets Jan 2 8pm -> current-year Jan 2 is past ->
    # next year Jan 2 8pm -> 45h + 120s
    now = _epoch(2026, 12, 31, 23, 0)
    assert _reset_hint_seconds(
        "resets Jan 2, 8pm (America/Los_Angeles)", now) == 162120.0


def test_weekly_dated_past_target_returns_none() -> None:
    # now Jul 16 21:00, resets Jul 16 8pm already passed -> next-year target
    # is ~365 days out, beyond the 8-day ceiling -> None
    now = _epoch(2026, 7, 16, 21, 0)
    assert _reset_hint_seconds(
        "resets Jul 16, 8pm (America/Los_Angeles)", now) is None


def test_weekly_dated_beyond_eight_days_returns_none() -> None:
    # now Jul 16 09:00, resets Jul 27 8pm -> ~11 days out > 8-day ceiling -> None
    now = _epoch(2026, 7, 16, 9, 0)
    assert _reset_hint_seconds(
        "resets Jul 27, 8pm (America/Los_Angeles)", now) is None


def test_weekly_dated_unknown_month_returns_none() -> None:
    now = _epoch(2026, 7, 16, 9, 0)
    assert _reset_hint_seconds(
        "resets Xxx 16, 8pm (America/Los_Angeles)", now) is None


def test_weekly_dated_with_minutes_and_24h() -> None:
    # now Jul 16 09:00, resets Jul 16 20:30 -> 11h30m + 120s
    now = _epoch(2026, 7, 16, 9, 0)
    assert _reset_hint_seconds(
        "resets Jul 16, 20:30 (America/Los_Angeles)", now) == 41520.0


def test_weekly_dated_invalid_day_for_month_returns_none() -> None:
    now = _epoch(2026, 2, 10, 9, 0)
    assert _reset_hint_seconds(
        "resets Feb 30, 8pm (America/Los_Angeles)", now) is None


def test_weekly_dated_month_is_case_insensitive() -> None:
    # now Jul 16 09:00, resets JUL 16 8pm -> 11h + 120s (same as lowercase)
    now = _epoch(2026, 7, 16, 9, 0)
    assert _reset_hint_seconds(
        "resets JUL 16, 8pm (America/Los_Angeles)", now) == 39720.0


def test_weekly_dated_at_eight_day_ceiling_is_trusted() -> None:
    # now Jul 16 20:00, resets Jul 24 20:00 -> exactly 8 days out -> trusted,
    # 8*24*3600 + 120s margin.
    now = _epoch(2026, 7, 16, 20, 0)
    assert _reset_hint_seconds(
        "resets Jul 24, 20:00 (America/Los_Angeles)", now) == 691320.0


def test_weekly_text_only_classifies_as_rate_limit(monkeypatch) -> None:
    # status absent -> the widened wording alternative alone must classify.
    monkeypatch.setattr(cc, "_reset_hint_seconds", lambda text, now: 7200.0)
    exc = cc._classify_agent_text(
        "You've hit your weekly limit · resets Jul 16, 8pm (America/Los_Angeles)")
    assert isinstance(exc, perr.RateLimitError)
    assert exc.detail["retry_after_seconds"] == 7200.0


def test_weekly_429_classifies_as_rate_limit_not_terminated() -> None:
    sdk = result(is_error=True, api_error_status=429)
    term = cc._termination_from_result(
        sdk,
        "You've hit your weekly limit · resets Jul 16, 8pm (America/Los_Angeles)",
        {},
    )
    assert isinstance(term, perr.RateLimitError)
    assert not isinstance(term, perr.TerminatedError)
