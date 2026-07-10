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


def test_rate_limit_doubles_then_caps_at_60() -> None:
    # One formula, always 60s-capped: 1, 2, 4, 8, 16, 32, 60, 60.
    expected = [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]
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


def test_large_hint_escapes_60s_cap_and_sets_hinted() -> None:
    # Reset-aware change: a hint above 60s is authoritative, not capped, and flagged.
    d = ep.decide(
        "rate_limit", prior_backoff_attempts=0, hint_delay_seconds=999.0,
    )
    assert isinstance(d, ep.RetryWithBackoff)
    assert d.delay_seconds == 999.0
    assert d.hinted is True


def test_hint_ignored_for_pause_category() -> None:
    d = ep.decide(
        "config", prior_backoff_attempts=0, hint_delay_seconds=5.0,
    )
    assert isinstance(d, ep.Pause)


def test_agent_incomplete_category_removed() -> None:
    from imp.engine import error_policy
    assert "agent_incomplete" not in error_policy._POLICY


def test_policy_covers_every_category() -> None:
    assert set(ep._POLICY.keys()) == set(get_args(Category))


def test_backoff_doubles_then_caps_at_60() -> None:
    from imp.engine.error_policy import RetryWithBackoff, decide
    delays = [decide("network", prior_backoff_attempts=n) for n in range(8)]
    assert all(isinstance(d, RetryWithBackoff) for d in delays)   # never Pause
    assert [d.delay_seconds for d in delays] == [1, 2, 4, 8, 16, 32, 60, 60]


def test_large_retry_after_hint_escapes_cap_for_network() -> None:
    # Intended side effect: an HTTP Retry-After: 300 is now honored past 60s.
    from imp.engine.error_policy import RetryWithBackoff, decide
    d = decide("network", prior_backoff_attempts=0, hint_delay_seconds=300)
    assert isinstance(d, RetryWithBackoff)
    assert d.delay_seconds == 300.0 and d.hinted is True


def test_auth_retries_with_60s_capped_backoff() -> None:
    expected = [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0]
    for n, want in enumerate(expected):
        d = ep.decide("auth", prior_backoff_attempts=n)
        assert isinstance(d, ep.RetryWithBackoff)
        assert d.delay_seconds == want


def test_auth_hint_honored() -> None:
    d = ep.decide("auth", prior_backoff_attempts=0, hint_delay_seconds=10.0)
    assert isinstance(d, ep.RetryWithBackoff)
    assert d.delay_seconds == 10.0


def test_backoff_categories_never_pause_and_cap_at_60() -> None:
    for cat in ("rate_limit", "network"):
        d = ep.decide(cat, prior_backoff_attempts=50)
        assert isinstance(d, ep.RetryWithBackoff)
        assert d.delay_seconds == 60.0


def test_budget_for_network_and_rate_limit_is_1800() -> None:
    assert ep.budget_for("network") == 1800.0
    assert ep.budget_for("rate_limit") == 1800.0


def test_budget_for_auth_is_1800() -> None:
    # A 401 is a transient refresh-path failure that heals on its own; give auth
    # the same patience as network / rate_limit rather than a tight 120 s budget.
    assert ep.budget_for("auth") == 1800.0


def test_budget_for_pause_and_retry_now_categories_is_none() -> None:
    for cat in ("config", "quota", "content_filter", "context_window",
                "api", "loop", "terminated", "unknown",
                "agent_output_missing"):
        assert ep.budget_for(cat) is None


def test_delay_caps_at_60_without_hint() -> None:
    # AC: attempt 9 -> 60.0 (not 512.0); no server hint required.
    d = ep.decide("network", prior_backoff_attempts=9)
    assert isinstance(d, ep.RetryWithBackoff)
    assert d.delay_seconds == 60.0


def test_hint_folded_into_outer_max_formula() -> None:
    # A hint is the outer-max term: max(min(60, 2^n), hint). A small hint (<=60)
    # stays capped/unflagged; a large hint (>60) wins the delay and is flagged.
    small = ep.decide("network", prior_backoff_attempts=0, hint_delay_seconds=30.0)
    assert isinstance(small, ep.RetryWithBackoff)
    assert small.delay_seconds == 30.0 and small.hinted is False
    large = ep.decide("network", prior_backoff_attempts=0,
                      hint_delay_seconds=999.0)
    assert isinstance(large, ep.RetryWithBackoff)
    assert large.delay_seconds == 999.0 and large.hinted is True


def test_auth_never_pauses_in_decide() -> None:
    # The attempt cap is gone; auth's give-up is the executor's 1800s budget,
    # not a Pause from decide().
    for n in (0, 7, 50):
        d = ep.decide("auth", prior_backoff_attempts=n)
        assert isinstance(d, ep.RetryWithBackoff)
    assert ep.decide("auth", prior_backoff_attempts=50).delay_seconds == 60.0


def test_stalled_is_a_category_member() -> None:
    assert "stalled" in get_args(Category)


def test_stalled_decide_follows_backoff_schedule() -> None:
    # Reuses the one delay formula min(60, 2 ** prior_backoff_attempts):
    # 1, 2, 4, 8, 16, 32, 60, 60.
    expected = [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]
    for i, want in enumerate(expected):
        d = ep.decide("stalled", prior_backoff_attempts=i)
        assert isinstance(d, ep.RetryWithBackoff)
        assert d.delay_seconds == want


def test_stalled_never_pauses_in_decide() -> None:
    for n in (0, 7, 50):
        assert isinstance(
            ep.decide("stalled", prior_backoff_attempts=n), ep.RetryWithBackoff,
        )


def test_budget_for_stalled_is_1800() -> None:
    # Same patience as the other transient budgets (network / rate_limit / auth).
    assert ep.budget_for("stalled") == 1800.0


def test_hint_exactly_60s_is_not_hinted() -> None:
    # Boundary: strictly > 60 is hinted; == 60 stays in the capped/budgeted regime.
    d = ep.decide("rate_limit", prior_backoff_attempts=0, hint_delay_seconds=60.0)
    assert isinstance(d, ep.RetryWithBackoff)
    assert d.delay_seconds == 60.0 and d.hinted is False


def test_no_hint_blind_backoff_is_not_hinted() -> None:
    d = ep.decide("network", prior_backoff_attempts=3)
    assert isinstance(d, ep.RetryWithBackoff)
    assert d.delay_seconds == 8.0 and d.hinted is False


def test_is_retry_with_backoff_true_for_backoff_categories() -> None:
    for cat in ("auth", "network", "rate_limit", "stalled"):
        assert ep.is_retry_with_backoff(cat) is True


def test_is_retry_with_backoff_false_for_non_backoff_categories() -> None:
    for cat in ("config", "quota", "content_filter", "context_window",
                "api", "loop", "agent_output_missing", "terminated", "unknown"):
        assert ep.is_retry_with_backoff(cat) is False


def test_is_retry_with_backoff_false_for_unrecognized_category() -> None:
    assert ep.is_retry_with_backoff("not_a_category") is False


def test_is_retry_with_backoff_matches_policy_table() -> None:
    for cat, policy in ep._POLICY.items():
        assert ep.is_retry_with_backoff(cat) is (
            policy.decision is ep.RetryWithBackoff
        )
