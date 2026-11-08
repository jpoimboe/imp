from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from imp.cli.quota_share import (
    NO_CHARGE,
    Charge,
    charges_by_index,
    increments,
    parse_ts,
    weighted_units,
)
from imp.cli.render_summary import (
    build_summary,
    charges_by_row,
    overlay_charges,
    rows_charge,
)
from imp.cli.store import GaugeWeek
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
    log = [_SEED, _qr("e1", "01:00", "s1", 1000,
                      {"claude-haiku-4-5": {"inputTokens": 1_000_000},
                       "claude-opus-5": {"outputTokens": 1_000_000}})]
    assert [round(i.units, 4) for i in increments(log)] == [26.0]


def test_an_unpriced_model_draws_zero_rather_than_a_guessed_rate() -> None:
    heavy = BillingUsage(output=1_000_000)
    assert weighted_units("claude", "no-such-model", heavy) == 0.0
    assert weighted_units("codex", None, heavy) == 0.0


def test_a_naive_timestamp_reads_utc() -> None:
    assert parse_ts("2026-01-01T00:00:00") == datetime(2026, 1, 1, tzinfo=UTC)
    assert parse_ts(None) is None
    assert parse_ts("not a timestamp") is None


_CODEX_USAGE = {"input_tokens": 1000, "cache_read_input_tokens": 2000,
                "cache_creation_input_tokens": 3000, "output_tokens": 4000}


def _seed(provider: str, model: str) -> dict[str, Any]:
    """The `agent_message` whose triple a1's later patch and `query_result`
    rows inherit."""
    return {"id": "m0", "type": "agent_message", "agent_id": "a1",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "payload": {"provider": provider, "model": model,
                        "auth_mode": "subscription"}}


_SEED = _seed("claude", "claude-sonnet-4-6")


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
    row's absolute total would be 28.00 u."""
    log = [_SEED, _qr("e1", "01:00", "s1", 1000, _out(200_000)),
           _qr("e2", "02:00", "s1", 1000,
               {**_out(200_000), "claude-opus-5": {"outputTokens": 1_000_000}})]
    assert [round(i.units, 4) for i in increments(log)] == [3.0, 25.0]


def test_a_new_session_restarts_differencing_from_zero() -> None:
    """200k then 100k output in a *different* session is 3.00 + 1.50, never the
    -1.50 that differencing across the session boundary would give."""
    log = [_SEED, _qr("e1", "01:30", "s1", 1000, _out(200_000)),
           _qr("e2", "02:00", "s2", 1000, _out(100_000))]
    assert [round(i.units, 4) for i in increments(log)] == [3.0, 1.5]


def test_a_query_occupies_the_interval_its_duration_names() -> None:
    (inc,) = increments([_SEED, _qr("e1", "01:30", "s1", 60_000, _out(200_000))])
    assert (inc.start, inc.end) == (datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC),
                                    datetime(2026, 1, 1, 0, 1, 30, tzinfo=UTC))
    assert (inc.provider, inc.gauge) == ("claude", "claude")


def test_a_codex_agent_message_is_a_point_in_time_increment() -> None:
    """A codex `agent_message` has no interval, so it carries no start."""
    message = {"id": "e1", "type": "agent_message", "agent_id": "a1",
               "timestamp": "2026-01-01T00:02:00+00:00",
               "payload": {"provider": "codex", "model": "gpt-5.3-codex",
                           "auth_mode": "subscription", "usage": _CODEX_USAGE}}
    (inc,) = increments([message])
    assert (inc.start, inc.provider, inc.gauge) == (None, "codex", "codex")
    assert inc.units == 0.06335


def test_the_first_row_of_a_pair_differences_against_its_predecessor() -> None:
    """200k output and then 300k cumulative is a 100k increment — 1.50 u. Drop
    the earlier row and the later one differences against zero instead,
    over-counting it to 4.50 u."""
    earlier = _qr("e0", "00:30", "s1", 1000, _out(200_000))
    later = _qr("e1", "02:00", "s1", 1000, _out(300_000))
    assert [round(i.units, 4) for i in increments([_SEED, earlier, later])] == [3.0, 1.5]
    assert [round(i.units, 4) for i in increments([_SEED, later])] == [4.5]


def test_a_patch_row_is_priced_from_the_agents_last_message() -> None:
    """`state.py` persists the patch with exactly three keys — `type`,
    `agent_id`, `payload` — so it carries no provider, model, timestamp or event
    id of its own. It takes its agent's triple and the last timestamp seen.
    With no message before it there is no rate to price it at."""
    patch = {"type": "agent_message_usage_patch", "agent_id": "a1",
             "payload": {"usage": _CODEX_USAGE}}
    started = {"id": "e1", "type": "step_started", "agent_id": "a1",
               "timestamp": "2026-01-01T00:02:00+00:00", "payload": {"step": "s"}}
    (seeded,) = increments([_seed("codex", "gpt-5.3-codex"), started, patch])
    assert seeded.index == 2
    assert seeded.end == datetime(2026, 1, 1, 0, 2, tzinfo=UTC)
    assert (seeded.gauge, seeded.units) == ("codex", 0.06335)
    (bare,) = increments([started, patch])
    assert (bare.provider, bare.gauge, bare.units) == (None, None, 0.0)


def test_claudes_own_agent_message_usage_is_ignored() -> None:
    """It is the start-of-message snapshot; the authoritative figures arrive on
    `query_result`, which `build_summary` already relies on."""
    log = [{"id": "e1", "type": "agent_message", "agent_id": "a1",
            "timestamp": "2026-01-01T00:02:00+00:00",
            "payload": {"provider": "claude", "model": "claude-sonnet-4-6",
                        "auth_mode": "subscription",
                        "usage": {"output_tokens": 1_000_000}}}]
    assert increments(log) == []


def _quest_log() -> list[dict[str, Any]]:
    """One step, one subagent, and a third agent owning no row at all.

    The step's pair `s1` reports 200k sonnet output tokens and then 500k
    cumulative, so its increments are 3.00 u and 4.50 u — 7.50 u in total, which
    must ride the second row, the only one surviving the cumulative collapse. The
    subagent's pair `s2` draws 1.50 u. `ghost` draws 15.00 u that no row owns."""
    def msg(eid: str, agent: str, second: int) -> dict[str, Any]:
        return {"id": eid, "type": "agent_message", "agent_id": agent,
                "timestamp": f"2026-01-01T00:00:{second:02d}+00:00",
                "payload": {"provider": "claude", "model": "claude-sonnet-4-6",
                            "auth_mode": "subscription"}}

    def result(eid: str, agent: str, second: int, session: str,
               tokens: int) -> dict[str, Any]:
        return {"id": eid, "type": "query_result", "agent_id": agent,
                "timestamp": f"2026-01-01T00:00:{second:02d}+00:00",
                "payload": {"session_id": session, "duration_ms": 1000,
                            "model_usage": {
                                "claude-sonnet-4-6": {"outputTokens": tokens}}}}

    return [
        {"id": "e0", "type": "quest_created", "agent_id": None,
         "timestamp": "2026-01-01T00:00:00+00:00", "payload": {}},
        {"id": "e1", "type": "step_started", "agent_id": None,
         "timestamp": "2026-01-01T00:00:00+00:00",
         "payload": {"step": "build", "phase": "impl"}},
        msg("e2", "step:impl/build", 1),
        result("e3", "step:impl/build", 10, "s1", 200_000),
        {"id": "e4", "type": "tool_call", "agent_id": "step:impl/build",
         "timestamp": "2026-01-01T00:00:11+00:00",
         "payload": {"name": "sub_agent", "id": "t1",
                     "input": {"subagent_name": "explorer"}}},
        msg("e5", "sub:e4", 12),
        result("e6", "sub:e4", 20, "s2", 100_000),
        {"id": "e7", "type": "tool_result", "agent_id": "step:impl/build",
         "timestamp": "2026-01-01T00:00:21+00:00", "payload": {"id": "t1"}},
        result("e8", "step:impl/build", 30, "s1", 500_000),
        msg("e9", "ghost", 31),
        result("e10", "ghost", 32, "s9", 1_000_000),
        {"id": "e11", "type": "step_completed", "agent_id": None,
         "timestamp": "2026-01-01T00:00:40+00:00", "payload": {"step": "build"}},
    ]


def _codex_log() -> list[dict[str, Any]]:
    """One codex step. codex emits its assistant message with `usage={}` and
    patches the totals afterwards, and `state.py:783` persists that patch with
    three keys and no `id` — so the patch holds the step's whole 0.06335 u."""
    return [
        {"id": "e0", "type": "step_started", "agent_id": None,
         "timestamp": "2026-01-01T00:00:00+00:00",
         "payload": {"step": "build", "phase": "impl"}},
        {"id": "e1", "type": "agent_message", "agent_id": "step:impl/build",
         "timestamp": "2026-01-01T00:00:01+00:00",
         "payload": {"provider": "codex", "model": "gpt-5.3-codex",
                     "auth_mode": "subscription", "usage": {}}},
        {"type": "agent_message_usage_patch", "agent_id": "step:impl/build",
         "payload": {"usage": _CODEX_USAGE}},
        {"id": "e3", "type": "step_completed", "agent_id": None,
         "timestamp": "2026-01-01T00:00:02+00:00", "payload": {"step": "build"}},
    ]


_WEEK1 = 1767312000  # 2026-01-02T00:00Z: the week holding every event below


def _charges(log: list[dict[str, Any]], peak: float = 24.0,
             provider: str = "claude") -> dict[int, Charge]:
    """`log` priced in one week no other quest drew on."""
    return charges_by_index(log, [GaugeWeek(provider, _WEEK1, peak, 0.0)])


def _pts(charge: Charge, gauge: str = "claude") -> float:
    points = dict(charge.per_gauge)[gauge].points
    assert points is not None
    return points


def test_each_increment_is_charged_at_its_own_log_position() -> None:
    """The step's pair reports 200k then 500k cumulative sonnet output, so its two
    increments are 3.00 u at index 3 and 4.50 u at index 8 — the second of which
    the cumulative collapse keeps and the first of which it drops, both belonging
    to the same row."""
    charges = _charges(_quest_log())
    assert {i: round(_pts(c), 4) for i, c in charges.items()} == {
        3: 3.0, 6: 1.5, 8: 4.5, 10: 15.0}


def test_an_id_less_usage_patch_still_reaches_the_row_that_owns_it() -> None:
    """A join keyed on the event id would leave a codex run's whole draw
    uncharged, while the dollar column, which resolves the row from the log
    position, prices the same usage fine. The patch is the week's only codex
    draw, so it carries all 5 points."""
    log = _codex_log()
    rows = build_summary(log, _charges(log, peak=5.0, provider="codex"))
    assert _pts(rows[0].charge, "codex") == 5.0
    assert _pts(rows_charge(rows), "codex") == 5.0
    assert rows[0].usage.dollars > 0


def test_a_filtered_render_keeps_the_charge_the_whole_log_computed() -> None:
    """`--range` can drop the one `query_result` a cumulative pair's dollars ride
    on — which moves the dollar cell, as the first assertion shows — so the
    rendered rows take the charge the unfiltered log computed, matched by row
    rather than by event."""
    log = _quest_log()
    whole = build_summary(log, _charges(log))
    narrowed = build_summary([e for e in log if e["id"] != "e8"])
    assert round(narrowed[0].usage.dollars, 4) < round(whole[0].usage.dollars, 4)
    assert narrowed[0].charge == NO_CHARGE
    overlay_charges(narrowed, charges_by_row(whole))
    assert round(_pts(narrowed[0].charge), 4) == 7.5
    assert round(_pts(narrowed[0].subagents[0].charge), 4) == 1.5


def test_charges_land_on_the_step_and_subagent_rows_that_own_the_agent() -> None:
    log = _quest_log()
    rows = build_summary(log, _charges(log))
    assert len(rows) == 1
    step = rows[0]
    assert round(_pts(step.charge), 4) == 7.5
    assert [s.name for s in step.subagents] == ["explorer"]
    assert round(_pts(step.subagents[0].charge), 4) == 1.5
    assert round(_pts(step.subtree_charge), 4) == 9.0


def test_an_unowned_agents_units_dilute_the_week_but_render_on_no_row() -> None:
    """`ghost` draws 15.00 of the week's 24.00 u and no row owns it, so the rows
    carry 9/24 of the 24 points. Without it they carry all 24."""
    log = _quest_log()
    assert round(_pts(rows_charge(build_summary(log, _charges(log)))), 4) == 9.0
    solo = [e for e in log if e["agent_id"] != "ghost"]
    assert round(_pts(rows_charge(build_summary(solo, _charges(solo)))), 4) == 24.0


def test_build_summary_without_charges_leaves_every_row_at_no_charge() -> None:
    rows = build_summary(_quest_log())
    assert rows[0].charge == NO_CHARGE
    assert rows[0].subagents[0].charge == NO_CHARGE
    assert rows_charge(rows) == NO_CHARGE


def test_a_row_is_identified_by_its_occurrence_not_by_its_agent_id() -> None:
    """Two runs of one step share the agent id `step:impl/build`, so the keys the
    overlay matches on must still tell the occurrences apart."""
    log = _quest_log()
    rows = build_summary(log, _charges(log))
    assert rows[0].key == "step:impl/build@2026-01-01T00:00:00+00:00"
    assert rows[0].subagents[0].key == "sub:e4"
    assert sorted(charges_by_row(rows)) == [
        "step:impl/build@2026-01-01T00:00:00+00:00", "sub:e4"]
