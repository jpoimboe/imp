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
