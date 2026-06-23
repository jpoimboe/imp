from __future__ import annotations

from typing import get_args

from imp.engine import error_policy as ep
from imp.providers.errors import Category


def test_pause_categories_return_pause_regardless_of_attempts() -> None:
    for cat in ("config", "quota", "content_filter",
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


def test_rate_limit_doubles_unbounded() -> None:
    # §10: no cap — doubles forever: 1, 2, 4, 8, 16, ...
    expected = [1.0, 2.0, 4.0, 8.0, 16.0, 32.0]
    for i, want in enumerate(expected):
        d = ep.decide("rate_limit", prior_backoff_attempts=i)
        assert isinstance(d, ep.RetryWithBackoff)
        assert d.delay_seconds == want


def test_network_doubles() -> None:
    d = ep.decide("network", prior_backoff_attempts=2)
    assert isinstance(d, ep.RetryWithBackoff)
    assert d.delay_seconds == 4.0


def test_no_attempt_ceiling_for_backoff_categories() -> None:
    # §10: no attempt cap — attempt 4 (previously caused Pause) now retries.
    d = ep.decide("rate_limit", prior_backoff_attempts=4)
    assert isinstance(d, ep.RetryWithBackoff)
    assert d.delay_seconds == 16.0
    d = ep.decide("network", prior_backoff_attempts=4)
    assert isinstance(d, ep.RetryWithBackoff)
    assert d.delay_seconds == 16.0


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
        "config", prior_backoff_attempts=0, hint_delay_seconds=5.0,
    )
    assert isinstance(d, ep.Pause)


def test_pause_for_agent_incomplete() -> None:
    for attempts in (0, 1, 99):
        assert isinstance(
            ep.decide("agent_incomplete", prior_backoff_attempts=attempts),
            ep.Pause,
        )


def test_policy_covers_every_category() -> None:
    assert set(ep._POLICY.keys()) == set(get_args(Category))


def test_backoff_doubles_unbounded_no_cap() -> None:
    from imp.engine.error_policy import RetryWithBackoff, decide
    delays = [decide("network", prior_backoff_attempts=n) for n in range(8)]
    assert all(isinstance(d, RetryWithBackoff) for d in delays)   # never Pause
    assert [d.delay_seconds for d in delays] == [1, 2, 4, 8, 16, 32, 64, 128]


def test_retry_after_hint_still_capped_at_60() -> None:
    from imp.engine.error_policy import RetryWithBackoff, decide
    d = decide("network", prior_backoff_attempts=0, hint_delay_seconds=300)
    assert isinstance(d, RetryWithBackoff) and d.delay_seconds == 60.0


def test_auth_retries_with_backoff_below_cap() -> None:
    expected = [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0]
    for n, want in enumerate(expected):
        d = ep.decide("auth", prior_backoff_attempts=n)
        assert isinstance(d, ep.RetryWithBackoff)
        assert d.delay_seconds == want


def test_auth_pauses_at_and_above_cap() -> None:
    for n in (7, 8, 99):
        assert isinstance(
            ep.decide("auth", prior_backoff_attempts=n), ep.Pause,
        )


def test_auth_hint_honored_below_cap() -> None:
    d = ep.decide("auth", prior_backoff_attempts=0, hint_delay_seconds=10.0)
    assert isinstance(d, ep.RetryWithBackoff)
    assert d.delay_seconds == 10.0


def test_uncapped_backoff_categories_never_pause_at_high_attempts() -> None:
    for cat in ("rate_limit", "network"):
        d = ep.decide(cat, prior_backoff_attempts=50)
        assert isinstance(d, ep.RetryWithBackoff)
        assert d.delay_seconds == float(2 ** 50)
