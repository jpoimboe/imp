from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from imp.cli.accounting import (
    RowUsage,
    collapse_cumulative_query_results,
    computed_contribution,
    model_rows,
)
from imp.cli.narrative import fmt_seconds
from imp.cli.quota_share import (
    Charge,
    charges_by_index,
    extract,
    gauge_weeks,
    parse_ts,
)
from imp.cli.render_summary import (
    build_summary,
    cost_cell,
    exclusion_spans,
    rows_charge,
    summary_total,
    summary_totals,
)
from imp.paths import imp_home
from tests._quota_fixture import NEIGHBOUR_ID, SUBJECT_ID, quota_events

_SPAN_START = datetime(2026, 9, 18, 3, 29, 57, 904744, tzinfo=UTC)
_SPAN_END = datetime(2026, 9, 18, 5, 38, 3, 485364, tzinfo=UTC)
_RESET = 1789758000  # 2026-09-18T19:00Z
_PROSE_KEYS = frozenset({"text", "content", "task_description", "message",
                         "prompt", "title"})


def _prose_keys(node: Any) -> set[str]:
    if isinstance(node, dict):
        found = {k for k in node if k in _PROSE_KEYS}
        for value in node.values():
            found |= _prose_keys(value)
        return found
    if isinstance(node, list):
        return {k for item in node for k in _prose_keys(item)}
    return set()


def _table_priced(events: list[dict[str, Any]]) -> float:
    """Every collapsed `query_result` priced from MODEL_PRICES alone, ignoring
    the vendor's own `total_cost_usd`."""
    total = RowUsage()
    for e in collapse_cumulative_query_results(events):
        if e.get("type") != "query_result":
            continue
        rollup = (e.get("payload") or {}).get("model_usage") or {}
        for model, billing in model_rows(rollup):
            total = total + computed_contribution(model, billing, None,
                                                  metered=False)
    return total.dollars


def _points(charge: Charge) -> float:
    ((gauge, found),) = charge.per_gauge
    assert gauge == "claude" and found.points is not None
    return found.points


def _charged(others: list[list[dict[str, Any]]]) -> tuple[float, float]:
    """samo71's run total and threshold-step cell, in points, with `others` as
    the week's other quests."""
    events = quota_events(SUBJECT_ID)
    weeks = gauge_weeks(extract(events), [extract(o) for o in others])
    rows = build_summary(events, charges_by_index(events, weeks))
    step = next(r for r in rows if r.step == "threshold")
    return _points(rows_charge(rows)), _points(step.subtree_charge)


def test_the_capture_is_the_replay_the_figures_were_measured_over() -> None:
    """A re-capture that drifts must fail loudly rather than move the pinned
    figures underneath the tests below."""
    subject, neighbour = quota_events(SUBJECT_ID), quota_events(NEIGHBOUR_ID)
    assert (len(subject), len(neighbour)) == (1069, 101)
    stamps = [w for w in (parse_ts(e.get("timestamp")) for e in subject)
              if w is not None]
    assert (stamps[0], stamps[-1]) == (_SPAN_START, _SPAN_END)
    rows = build_summary(subject)
    assert len(rows) == 46
    assert sum(len(r.subagents) for r in rows) == 24
    assert [r.step for r in rows if r.errored] == []


def test_the_capture_carries_no_conversation_prose() -> None:
    """Only the fields the summary reads survived the projection, so the
    fixture is publishable as it stands."""
    for quest_id in (SUBJECT_ID, NEIGHBOUR_ID):
        for e in quota_events(quest_id):
            assert set(e) <= {"id", "type", "timestamp", "agent_id", "payload"}
            assert _prose_keys(e["payload"]) == set()
            if e["type"] == "tool_call":
                # `label` is free text the dispatching agent writes.
                assert set(e["payload"]) == {"name", "id", "id_ns", "input"}
                assert set(e["payload"]["input"]) == {"subagent_name"}


def test_samo71_prices_from_the_table_to_the_vendors_own_figure() -> None:
    """$42.20 from MODEL_PRICES, matching the vendor's reported total to the
    cent."""
    events = quota_events(SUBJECT_ID)
    _work, total = summary_totals(build_summary(events), summary_total(events),
                                  exclusion_spans(events))
    assert cost_cell(total) == "$42.20*"
    assert round(_table_priced(events), 2) == 42.20


def test_the_threshold_step_excludes_both_of_its_human_waits() -> None:
    """386.1 s of wall clock less 80.3 s of human wait is 305.8 s of work."""
    step = next(r for r in build_summary(quota_events(SUBJECT_ID))
                if r.step == "threshold")
    wall = step.wall_seconds
    assert wall is not None
    assert round(wall, 1) == 386.1
    assert round(step.wait_seconds, 1) == 80.3
    assert step.dur_cell("running") == "5m 5s"


def test_the_run_total_subtracts_the_same_union() -> None:
    """2h 8m of wall clock, 2h 6m of work."""
    events = quota_events(SUBJECT_ID)
    raw = summary_total(events)
    work, _total = summary_totals(build_summary(events), raw,
                                  exclusion_spans(events))
    assert raw is not None and work is not None
    assert (fmt_seconds(raw), fmt_seconds(work)) == ("2h 8m", "2h 6m")


def test_both_quests_read_one_claude_week_that_peaks_at_81() -> None:
    """samo71 read the gauge from 78% to 81%; fego87, sampling the same week,
    peaked at 80%."""
    subject = extract(quota_events(SUBJECT_ID)).readings
    neighbour = extract(quota_events(NEIGHBOUR_ID)).readings
    assert {(r.provider, r.resets_at) for r in (*subject, *neighbour)} == {
        ("claude", _RESET)}
    assert (min(r.used_pct for r in subject),
            max(r.used_pct for r in subject)) == (78.0, 81.0)
    assert max(r.used_pct for r in neighbour) == 80.0


def test_the_week_holds_the_neighbours_draw_and_never_the_subjects() -> None:
    """AC 3(d): other_units is fego87's 5.8947 u alone; samo71's 29.9455 u
    enter per increment when it is charged."""
    subject = extract(quota_events(SUBJECT_ID))
    neighbour = extract(quota_events(NEIGHBOUR_ID))
    assert round(sum(i.units for i in subject.increments
                     if i.gauge == "claude"), 4) == 29.9455
    weeks = gauge_weeks(subject, [neighbour])
    assert [(w.provider, w.resets_at, w.peak_pct, round(w.other_units, 4))
            for w in weeks] == [("claude", _RESET, 81.0, 5.8947)]


def test_samo71_is_charged_its_part_of_the_weeks_81_points() -> None:
    """81 x 29.9455 / (29.9455 + 5.8947) is 67.68 points; the threshold
    step's 1.96816 u of it are 4.45."""
    total, threshold = _charged([quota_events(NEIGHBOUR_ID)])
    assert (f"{total:.2f}", f"{threshold:.2f}") == ("67.68", "4.45")


def test_alone_in_its_week_samo71_carries_the_whole_peak() -> None:
    total, threshold = _charged([])
    assert (f"{total:.2f}", f"{threshold:.2f}") == ("81.00", "5.32")


def test_no_test_can_reach_the_maintainers_live_archive(tmp_path: Path) -> None:
    """conftest's `_isolate_global_config` pins IMP_HOME at an absent per-test
    directory and the figures above read one committed file, so nothing here
    can reach the live archive."""
    assert imp_home() == tmp_path / ".imp-isolated"
    assert not imp_home().exists()
    assert (Path(__file__).parent / "fixtures" / "quota" / "samo71"
            / f"{SUBJECT_ID}.jsonl.gz").is_file()
