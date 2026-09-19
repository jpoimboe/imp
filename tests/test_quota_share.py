from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from imp.cli.quota_share import (
    parse_ts,
    rollup_units,
    run_span,
    weighted_units,
)
from imp.providers.catalog import BillingUsage


def test_anthropic_units_ignore_cache_reads_entirely() -> None:
    """AC 4(a): sonnet is $3/M input, $6/M cache write and $15/M output, so
    1000 fresh + 2000 written + 500 out is $0.0225 — however many cache reads
    ride along, since their weight fit to 0.0000."""
    plain = BillingUsage(fresh_input=1000, cache_write=2000, output=500)
    assert weighted_units("claude", "claude-sonnet-4-6", plain) == 0.0225
    for reads in (1, 900_000, 50_000_000):
        loaded = BillingUsage(fresh_input=1000, cached_input=reads,
                              cache_write=2000, output=500)
        assert weighted_units("claude", "claude-sonnet-4-6", loaded) == 0.0225


def test_anthropic_cache_writes_cost_exactly_twice_input() -> None:
    """AC 4(b): the 1-hour-TTL rate, not the 5-minute TTL's 1.25x."""
    million = 1_000_000
    fresh = weighted_units("claude", "claude-sonnet-4-6",
                           BillingUsage(fresh_input=million))
    written = weighted_units("claude", "claude-sonnet-4-6",
                             BillingUsage(cache_write=million))
    assert (fresh, written) == (3.0, 6.0)
    assert written == 2 * fresh


def test_codex_units_count_every_bucket_at_list_price() -> None:
    """AC 4(c): gpt-5.3-codex is $1.75/M in, $0.175/M cache read, $1.75/M cache
    write and $14.00/M out — cache reads included, unlike the Anthropic family."""
    billing = BillingUsage(fresh_input=1000, cached_input=2000,
                           cache_write=3000, output=4000)
    assert weighted_units("codex", "gpt-5.3-codex", billing) == 0.06335


def test_a_rollup_is_priced_per_model_not_at_one_blended_rate() -> None:
    """AC 4(d): a million haiku input tokens ($1/M) plus a million opus-5 output
    tokens ($25/M) is $26. Summing the buckets first and pricing the total at
    either model's rates gives $6 or $30."""
    rollup = {"claude-haiku-4-5": {"inputTokens": 1_000_000},
              "claude-opus-5": {"outputTokens": 1_000_000}}
    assert rollup_units("claude", rollup) == 26.0


def test_an_unpriced_model_draws_zero_rather_than_a_guessed_rate() -> None:
    heavy = BillingUsage(output=1_000_000)
    assert weighted_units("claude", "no-such-model", heavy) == 0.0
    assert weighted_units("codex", None, heavy) == 0.0


def test_the_span_is_the_first_timestamped_event_to_the_last() -> None:
    """AC 23(a), first half: a `quest_created` before the first `step_started`
    widens the span, a trailing `quest_merged` extends it, and a usage patch —
    which carries no timestamp of its own — never bounds it."""
    events: list[dict[str, Any]] = [
        {"type": "quest_created", "timestamp": "2026-01-01T00:00:00+00:00"},
        {"type": "step_started", "timestamp": "2026-01-01T00:05:00+00:00"},
        {"type": "agent_message_usage_patch", "payload": {"usage": {}}},
        {"type": "step_completed", "timestamp": "2026-01-01T00:10:00+00:00"},
        {"type": "quest_merged", "timestamp": "2026-01-01T00:11:00+00:00"},
    ]
    assert run_span(events) == (datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
                               datetime(2026, 1, 1, 0, 11, tzinfo=UTC))
    assert run_span([]) is None


def test_a_naive_timestamp_reads_utc() -> None:
    assert parse_ts("2026-01-01T00:00:00") == datetime(2026, 1, 1, tzinfo=UTC)
    assert parse_ts(None) is None
    assert parse_ts("not a timestamp") is None
