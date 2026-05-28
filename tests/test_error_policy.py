from __future__ import annotations

from typing import get_args

from imp.engine import error_policy as ep
from imp.providers.errors import Category


def test_pause_categories_return_pause_regardless_of_attempts() -> None:
    for cat in ("auth", "config", "quota", "content_filter",
                "context_window", "unknown"):
        for attempts in (0, 1, 2, 99):
            assert isinstance(
                ep.decide(cat, prior_backoff_attempts=attempts), ep.Pause,
            )


def test_retry_now_for_agent_output_missing() -> None:
    assert isinstance(
        ep.decide("agent_output_missing", prior_backoff_attempts=0),
        ep.RetryNow,
    )
    assert isinstance(
        ep.decide("agent_output_missing", prior_backoff_attempts=99),
        ep.RetryNow,
    )


def test_rate_limit_uses_schedule() -> None:
    expected = [1.0, 2.0, 4.0, 8.0]
    for i, want in enumerate(expected):
        d = ep.decide("rate_limit", prior_backoff_attempts=i)
        assert isinstance(d, ep.RetryWithBackoff)
        assert d.delay_seconds == want


def test_network_uses_schedule() -> None:
    d = ep.decide("network", prior_backoff_attempts=2)
    assert isinstance(d, ep.RetryWithBackoff)
    assert d.delay_seconds == 4.0


def test_schedule_exhausted_returns_pause() -> None:
    d = ep.decide("rate_limit", prior_backoff_attempts=4)
    assert isinstance(d, ep.Pause)
    d = ep.decide("network", prior_backoff_attempts=4)
    assert isinstance(d, ep.Pause)


def test_hint_overrides_when_larger() -> None:
    d = ep.decide(
        "rate_limit", prior_backoff_attempts=0, hint_delay_seconds=30.0,
    )
    assert isinstance(d, ep.RetryWithBackoff)
    assert d.delay_seconds == 30.0


def test_hint_floored_to_schedule_when_smaller() -> None:
    d = ep.decide(
        "rate_limit", prior_backoff_attempts=1, hint_delay_seconds=0.5,
    )
    assert isinstance(d, ep.RetryWithBackoff)
    assert d.delay_seconds == 2.0


def test_hint_capped_at_60s() -> None:
    d = ep.decide(
        "rate_limit", prior_backoff_attempts=0, hint_delay_seconds=999.0,
    )
    assert isinstance(d, ep.RetryWithBackoff)
    assert d.delay_seconds == 60.0


def test_hint_ignored_for_pause_category() -> None:
    d = ep.decide(
        "auth", prior_backoff_attempts=0, hint_delay_seconds=5.0,
    )
    assert isinstance(d, ep.Pause)


def test_policy_covers_every_category() -> None:
    assert set(ep._POLICY.keys()) == set(get_args(Category))
