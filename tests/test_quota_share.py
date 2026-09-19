from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from imp.cli.quota_share import (
    NO_UNITS,
    Increment,
    Units,
    increments,
    parse_ts,
    rollup_units,
    run_span,
    units_in_span,
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


_SPAN = (datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
         datetime(2026, 1, 1, 0, 5, tzinfo=UTC))
_CLAUDE = {"a1": ("claude", "claude-sonnet-4-6", "subscription")}
_CODEX = {"a1": ("codex", "gpt-5.3-codex", "subscription")}
_CODEX_USAGE = {"input_tokens": 1000, "cache_read_input_tokens": 2000,
                "cache_creation_input_tokens": 3000, "output_tokens": 4000}


def _qr(eid: str, clock: str, session: str, duration_ms: int,
        rollup: dict[str, Any]) -> dict[str, Any]:
    return {"id": eid, "type": "query_result", "agent_id": "a1",
            "timestamp": f"2026-01-01T00:{clock}+00:00",
            "payload": {"session_id": session, "duration_ms": duration_ms,
                        "model_usage": rollup}}


def _out(tokens: int, model: str = "claude-sonnet-4-6") -> dict[str, Any]:
    return {model: {"outputTokens": tokens}}


def test_a_cumulative_pair_yields_its_per_model_bucket_difference() -> None:
    """AC 21(a): sonnet output is $15/M and opus-5 output $25/M, so 200k sonnet
    followed by the same 200k plus 1M of opus-5 is 3.00 u then 25.00 u. The later
    row's absolute total would be 28.00 u, and differencing the flat `usage`
    block — a 1M output delta priced at the agent's sonnet rate — would be
    15.00 u."""
    log = [_qr("e1", "01:00", "s1", 1000, _out(200_000)),
           _qr("e2", "02:00", "s1", 1000,
               {**_out(200_000), "claude-opus-5": {"outputTokens": 1_000_000}})]
    assert [round(i.units, 4) for i in increments(log, dict(_CLAUDE))] == [3.0, 25.0]


def test_a_new_session_restarts_differencing_from_zero() -> None:
    """AC 21(b): 200k then 100k output in a *different* session is 3.00 + 1.50,
    never the -1.50 that differencing across the session boundary would give.
    Both rows sit wholly inside `_SPAN`, so the whole 4.50 u is in-span."""
    log = [_qr("e1", "01:30", "s1", 1000, _out(200_000)),
           _qr("e2", "02:00", "s2", 1000, _out(100_000))]
    rows = increments(log, dict(_CLAUDE))
    assert [round(i.units, 4) for i in rows] == [3.0, 1.5]
    assert round(units_in_span(rows, *_SPAN).get("claude"), 4) == 4.5


def test_a_query_straddling_the_span_start_contributes_its_overlap() -> None:
    """AC 21(c): a 60 s query ending 30 s after the span opens contributes half
    of its 3.00 u."""
    log = [_qr("e1", "01:30", "s1", 60_000, _out(200_000))]
    rows = increments(log, dict(_CLAUDE))
    assert rows[0].start == datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC)
    assert round(units_in_span(rows, *_SPAN).get("claude"), 4) == 1.5


def test_an_increment_wholly_outside_the_span_contributes_nothing() -> None:
    """AC 21(d)."""
    log = [_qr("e1", "00:30", "s1", 1000, _out(200_000))]
    rows = increments(log, dict(_CLAUDE))
    assert round(rows[0].units, 4) == 3.0
    assert units_in_span(rows, *_SPAN) == Units((("claude", 0.0),))


def test_a_point_in_time_row_is_credited_whole_and_never_prorated() -> None:
    """AC 21(e): a codex `agent_message` has no interval, so it belongs whole to
    the span holding its timestamp — 0.06335 u in, nothing out."""
    def message(ts: str) -> dict[str, Any]:
        return {"id": "e1", "type": "agent_message", "agent_id": "a1",
                "timestamp": ts,
                "payload": {"provider": "codex", "model": "gpt-5.3-codex",
                            "auth_mode": "subscription", "usage": _CODEX_USAGE}}

    inside = increments([message("2026-01-01T00:02:00+00:00")], {})
    assert inside[0].start is None
    assert inside[0].provider == "codex"
    assert units_in_span(inside, *_SPAN).get("codex") == 0.06335
    outside = increments([message("2026-01-01T00:00:30+00:00")], {})
    assert units_in_span(outside, *_SPAN).get("codex") == 0.0


def test_the_first_in_span_row_differences_against_the_pre_span_anchor() -> None:
    """AC 21(f): 200k output before the span and 300k cumulative inside it is a
    100k increment — 1.50 u. Drop the anchor and the in-span row differences
    against zero instead, over-counting it to 4.50 u."""
    anchor = _qr("e0", "00:30", "s1", 1000, _out(200_000))
    inside = _qr("e1", "02:00", "s1", 1000, _out(300_000))
    with_anchor = units_in_span(increments([anchor, inside], dict(_CLAUDE)), *_SPAN)
    without = units_in_span(increments([inside], dict(_CLAUDE)), *_SPAN)
    assert round(with_anchor.get("claude"), 4) == 1.5
    assert round(without.get("claude"), 4) == 4.5


def test_a_patch_row_is_priced_from_the_resolved_per_agent_triple() -> None:
    """AC 21(g): `state.py:783` persists the patch with exactly three keys —
    `type`, `agent_id`, `payload` — so it carries no provider, model, timestamp or
    event id of its own and takes the seeded triple with the last timestamp seen.
    Unseeded there is no rate to price it at, so it draws nothing at all."""
    log: list[dict[str, Any]] = [
        {"id": "e1", "type": "step_started", "agent_id": "a1",
         "timestamp": "2026-01-01T00:02:00+00:00", "payload": {"step": "s"}},
        {"type": "agent_message_usage_patch", "agent_id": "a1",
         "payload": {"usage": _CODEX_USAGE}},
    ]
    seeded = increments(log, dict(_CODEX))
    assert seeded[0].index == 1
    assert seeded[0].end == datetime(2026, 1, 1, 0, 2, tzinfo=UTC)
    assert units_in_span(seeded, *_SPAN).get("codex") == 0.06335
    assert units_in_span(increments(log, {}), *_SPAN) == NO_UNITS


def test_claudes_own_agent_message_usage_is_ignored() -> None:
    """It is the start-of-message snapshot; the authoritative figures arrive on
    `query_result`, which `build_summary` already relies on."""
    log = [{"id": "e1", "type": "agent_message", "agent_id": "a1",
            "timestamp": "2026-01-01T00:02:00+00:00",
            "payload": {"provider": "claude", "model": "claude-sonnet-4-6",
                        "auth_mode": "subscription",
                        "usage": {"output_tokens": 1_000_000}}}]
    assert increments(log, {}) == []


def test_units_merge_per_provider_and_keep_a_zero_draw() -> None:
    assert Units.of(None, 5.0) == NO_UNITS
    assert Units.of("claude", 0.0).providers == ("claude",)
    merged = Units.of("codex", 1.0) + Units.of("claude", 2.0) + Units.of("codex", 3.0)
    assert merged.per_provider == (("claude", 2.0), ("codex", 4.0))
    assert merged.get("claude") == 2.0
    assert merged.get("anthropic") == 0.0


def test_the_span_start_splits_an_interval_but_not_an_instant() -> None:
    """A 1 s query ending exactly as the span opens intersects it in zero length
    and so carries none of its 3.00 u, while a point-in-time row at that same
    instant belongs whole to the span — only one of the two has an interval to
    measure."""
    edge = increments([_qr("e1", "01:00", "s1", 1000, _out(200_000))],
                      dict(_CLAUDE))
    assert round(edge[0].units, 4) == 3.0
    assert units_in_span(edge, *_SPAN).get("claude") == 0.0
    instant = Increment(index=0, agent_id="a1", session_id=None,
                        provider="claude", units=3.0, start=None, end=_SPAN[0])
    assert instant.credit(*_SPAN) == 3.0
