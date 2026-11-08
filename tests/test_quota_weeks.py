"""The per-week rate: a week's highest reading over all imp subscription units
drawn in it, each increment priced at the week it falls in."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from imp.cli.quota_share import (
    GAUGE_BELOW,
    GAUGE_UNREAD,
    Charge,
    GaugeCharge,
    charges_by_index,
    extract,
    gauge_weeks,
    increments,
    week_parts,
    week_spans,
    weekly_readings,
)
from imp.cli.store import GaugeWeek

_R1 = 1767312000                    # 2026-01-02T00:00Z
_R2 = _R1 + 604800
_AT_R1 = datetime.fromtimestamp(_R1, UTC)


def _iso(when: datetime) -> str:
    return when.isoformat()


def _msg(when: datetime, *, agent: str = "a1", provider: str = "claude",
         model: str = "claude-sonnet-4-6", auth: str = "subscription") -> dict[str, Any]:
    return {"id": f"m-{agent}", "type": "agent_message", "agent_id": agent,
            "timestamp": _iso(when),
            "payload": {"provider": provider, "model": model, "auth_mode": auth}}


def _qr(when: datetime, tokens: int, *, agent: str = "a1", session: str = "s1",
        duration_ms: int = 1000) -> dict[str, Any]:
    return {"id": f"q-{session}", "type": "query_result", "agent_id": agent,
            "timestamp": _iso(when),
            "payload": {"session_id": session, "duration_ms": duration_ms,
                        "model_usage": {"claude-sonnet-4-6": {"outputTokens": tokens}}}}


def _reading(when: datetime, used: float, *, resets_at: int | None = _R1,
             provider: str = "claude") -> dict[str, Any]:
    window: dict[str, Any] = {"label": "7d", "used_pct": used}
    if resets_at is not None:
        window["resets_at"] = resets_at
    return {"id": "qk", "type": "quota_reading", "agent_id": "a1",
            "timestamp": _iso(when),
            "payload": {"provider": provider, "windows": [window]}}


def _points(charge: Charge) -> float:
    ((_gauge, gc),) = charge.per_gauge
    assert gc.points is not None
    return round(gc.points, 6)


def test_a_short_run_straddling_a_tick_is_charged_its_units_at_the_week_rate() -> None:
    """AC 3(a): 0.3 of 400 week units at a 40-point week is 0.03 points, not the
    whole point the gauge ticked during the run."""
    t = _AT_R1 - timedelta(days=1)
    log = [_msg(t), _reading(t, 39.0), _qr(t + timedelta(seconds=30), 20_000),
           _reading(t + timedelta(seconds=40), 40.0)]
    got = charges_by_index(log, [GaugeWeek("claude", _R1, 40.0, 399.7)])
    assert list(got) == [2]
    assert _points(got[2]) == 0.03


def test_a_run_straddling_no_tick_still_gets_a_figure() -> None:
    """AC 3(b): the gauge never moved during the run; the week rate prices it
    anyway — 3 own units of a 6-unit, 42-point week is 21 points."""
    t = _AT_R1 - timedelta(days=1)
    log = [_msg(t), _reading(t, 42.0), _qr(t + timedelta(minutes=5), 200_000),
           _reading(t + timedelta(minutes=6), 42.0)]
    got = charges_by_index(log, [GaugeWeek("claude", _R1, 42.0, 3.0)])
    assert _points(got[2]) == 21.0


def test_a_reading_without_resets_at_is_ignored() -> None:
    """AC 3(e): it anchors no week and never raises a week's peak."""
    t = _AT_R1 - timedelta(days=1)
    log = [_reading(t, 90.0, resets_at=None), _reading(t, 10.0)]
    assert [r.used_pct for r in weekly_readings(log)] == [10.0]
    assert gauge_weeks(extract(log), []) == [GaugeWeek("claude", _R1, 10.0, 0.0)]
    assert gauge_weeks(extract([_reading(t, 90.0, resets_at=None)]), []) == []


def test_the_peak_is_the_highest_reading_from_any_quest() -> None:
    t = _AT_R1 - timedelta(days=2)
    subject = extract([_reading(t, 10.0)])
    others = [extract([_reading(t, 50.0)]), extract([_reading(t, 30.0)])]
    assert gauge_weeks(subject, others) == [GaugeWeek("claude", _R1, 50.0, 0.0)]


def test_other_units_sum_every_other_quest_and_never_the_subject() -> None:
    t = _AT_R1 - timedelta(days=2)
    subject = extract([_msg(t), _reading(t, 20.0), _qr(t, 200_000)])
    others = [extract([_msg(t), _qr(t, 200_000)]),
              extract([_msg(t), _qr(t, 100_000)])]
    (week,) = gauge_weeks(subject, others)
    assert (week.peak_pct, round(week.other_units, 6)) == (20.0, 4.5)


def test_an_increment_anchored_only_by_another_quests_reading_is_priced() -> None:
    """AC 3(f): the subject recorded no reading of its own."""
    t = _AT_R1 - timedelta(days=3)
    log = [_msg(t), _qr(t, 200_000)]
    weeks = gauge_weeks(extract(log), [extract([_reading(t, 12.0)])])
    assert weeks == [GaugeWeek("claude", _R1, 12.0, 0.0)]
    assert _points(charges_by_index(log, weeks)[1]) == 12.0


def test_overlapping_anchors_assign_each_instant_to_the_smallest_enclosing_week() -> None:
    """AC 3(g): anchors R and R+3d overlap on [R-4d, R); that overlap belongs to R."""
    r3 = _R1 + 3 * 86400
    spans = week_spans([r3, _R1])
    log = [_msg(_AT_R1 - timedelta(days=6)),
           _qr(_AT_R1 - timedelta(days=6), 1, session="a", duration_ms=0),
           _qr(_AT_R1 - timedelta(days=2), 1, session="b", duration_ms=0),
           _qr(_AT_R1 - timedelta(seconds=1), 1, session="c", duration_ms=0),
           _qr(_AT_R1, 1, session="d", duration_ms=0),
           _qr(_AT_R1 + timedelta(days=2), 1, session="e", duration_ms=0)]
    got = [week_parts(inc, spans) for inc in increments(log)]
    assert got == [[(_R1, 1.0)], [(_R1, 1.0)], [(_R1, 1.0)],
                   [(r3, 1.0)], [(r3, 1.0)]]


def test_rows_either_side_of_a_reset_are_priced_at_each_weeks_rate() -> None:
    """AC 4(a)(b): one step, one query each side of R1. 3 units alone in a
    10-point week and 3 alone in a 4-point week sum to 14, with nothing withheld."""
    log = [_msg(_AT_R1 - timedelta(hours=1)),
           _qr(_AT_R1 - timedelta(minutes=30), 200_000, session="s1"),
           _qr(_AT_R1 + timedelta(minutes=30), 200_000, session="s2")]
    weeks = [GaugeWeek("claude", _R1, 10.0, 0.0), GaugeWeek("claude", _R2, 4.0, 0.0)]
    got = charges_by_index(log, weeks)
    assert {i: _points(c) for i, c in got.items()} == {1: 10.0, 2: 4.0}
    assert _points(got[1] + got[2]) == 14.0


def test_an_interval_spanning_the_reset_splits_by_overlap() -> None:
    """AC 4(c): a 60 s query ending 30 s after R1 is half in each week. Its 3
    units price as 1.5 of a 3-unit 6-point week plus 1.5 of a 1.5-unit 6-point
    week: 3 + 6 = 9 points."""
    log = [_msg(_AT_R1 - timedelta(hours=1)),
           _qr(_AT_R1 + timedelta(seconds=30), 200_000, duration_ms=60_000)]
    weeks = [GaugeWeek("claude", _R1, 6.0, 1.5), GaugeWeek("claude", _R2, 6.0, 0.0)]
    (inc,) = increments(log)
    assert week_parts(inc, week_spans([_R1, _R2])) == [(_R1, 0.5), (_R2, 0.5)]
    assert _points(charges_by_index(log, weeks)[1]) == 9.0


def test_a_point_increment_at_the_reset_instant_belongs_to_the_next_week() -> None:
    log = [_msg(_AT_R1 - timedelta(hours=1)), _qr(_AT_R1, 200_000, duration_ms=0)]
    (inc,) = increments(log)
    assert week_parts(inc, week_spans([_R1, _R2])) == [(_R2, 1.0)]


def test_time_no_anchored_week_covers_is_unread() -> None:
    """An interval starting before the earliest week keeps its uncovered share
    unread; an instant outside every week is wholly unread."""
    start = _AT_R1 - timedelta(days=7)
    log = [_msg(start - timedelta(hours=1)),
           _qr(start + timedelta(seconds=30), 200_000, session="s1", duration_ms=60_000),
           _qr(start - timedelta(minutes=5), 200_000, session="s2", duration_ms=0)]
    a, b = increments(log)
    assert week_parts(a, week_spans([_R1])) == [(_R1, 0.5), (None, 0.5)]
    assert week_parts(b, week_spans([_R1])) == [(None, 1.0)]
    got = charges_by_index(log, [GaugeWeek("claude", _R1, 8.0, 0.0)])
    assert got[2] == Charge.of("claude", GAUGE_UNREAD)
    assert got[1] == Charge.of("claude", GaugeCharge(8.0, False, True))


def test_an_unmoved_week_prices_below_resolution() -> None:
    t = _AT_R1 - timedelta(days=1)
    log = [_msg(t), _qr(t, 200_000)]
    got = charges_by_index(log, [GaugeWeek("claude", _R1, 0.0, 0.0)])
    assert got == {1: Charge.of("claude", GAUGE_BELOW)}


def test_a_zero_unit_increment_is_charged_zero() -> None:
    t = _AT_R1 - timedelta(days=1)
    log = [_msg(t), _qr(t, 0)]
    got = charges_by_index(log, [GaugeWeek("claude", _R1, 30.0, 0.0)])
    assert got == {1: Charge.of("claude", GaugeCharge.resolved(0.0))}


def test_metered_units_draw_on_no_gauge() -> None:
    """AC 6: an API-key quest adds nothing to a week's denominator, and the
    subject's own metered increments carry no charge."""
    t = _AT_R1 - timedelta(days=1)
    apikey = [_msg(t, auth="apikey"), _qr(t, 200_000)]
    subject = extract([_reading(t, 20.0)])
    assert gauge_weeks(subject, [extract(apikey)]) == [GaugeWeek("claude", _R1, 20.0, 0.0)]
    assert charges_by_index(apikey, [GaugeWeek("claude", _R1, 20.0, 0.0)]) == {}


def test_gauge_charges_merge_points_and_flags() -> None:
    assert GaugeCharge.resolved(1.0) + GAUGE_BELOW == GaugeCharge(1.0, True, False)
    assert GAUGE_UNREAD + GaugeCharge.resolved(2.0) == GaugeCharge(2.0, False, True)
    assert GaugeCharge(None, True, True) == GAUGE_BELOW + GAUGE_UNREAD
    assert (GaugeCharge.resolved(0.0) + GAUGE_BELOW).points == 0.0


def test_charges_merge_per_gauge() -> None:
    a = Charge.of("codex", GaugeCharge.resolved(1.0))
    b = Charge.of("claude", GAUGE_BELOW) + Charge.of("codex", GAUGE_UNREAD)
    assert (a + b).per_gauge == (("claude", GAUGE_BELOW),
                                 ("codex", GaugeCharge(1.0, False, True)))
    assert (a + b).gauges == ("claude", "codex")
