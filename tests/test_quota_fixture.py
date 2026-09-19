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
from imp.cli.quota_neighbours import neighbour_slice
from imp.cli.quota_share import (
    QuotaFigure,
    increments,
    parse_ts,
    quota_attribution,
    rollup_units,
    run_span,
    units_in_span,
)
from imp.cli.render_summary import (
    build_summary,
    cost_cell,
    exclusion_spans,
    rows_units,
    summary_total,
    summary_totals,
)
from imp.cli.render_text import fmt_seconds
from imp.cli.store import NeighbourQuota
from imp.paths import imp_home
from tests._quota_fixture import NEIGHBOUR_ID, SUBJECT_ID, quota_events

_SPAN_START = datetime(2026, 9, 18, 3, 29, 57, 904744, tzinfo=UTC)
_SPAN_END = datetime(2026, 9, 18, 5, 38, 3, 485364, tzinfo=UTC)
_PROSE_KEYS = frozenset({"text", "content", "task_description", "message",
                         "prompt", "title"})


def _span() -> tuple[datetime, datetime]:
    span = run_span(quota_events(SUBJECT_ID))
    assert span is not None
    return span


def _slice(span: tuple[datetime, datetime]) -> NeighbourQuota:
    found = neighbour_slice("other", NEIGHBOUR_ID, quota_events(NEIGHBOUR_ID),
                            *(t.isoformat() for t in span))
    assert found is not None
    return found


def _claude_figure(span: tuple[datetime, datetime]) -> QuotaFigure:
    events = quota_events(SUBJECT_ID)
    rows = build_summary(events, span)
    figures = quota_attribution(events, span, rows_units(rows),
                                [_slice(span)]).figures
    assert [f.provider for f in figures] == ["claude"]
    return figures[0]


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


def _whole_session_units(events: list[dict[str, Any]],
                         span: tuple[datetime, datetime]) -> float:
    """The forbidden method: the last in-span `query_result` per pair counted at
    its absolute cumulative total, crediting the pre-span burn to the span."""
    start, end = span
    last: dict[tuple[Any, str], dict[str, Any]] = {}
    for e in events:
        when = parse_ts(e.get("timestamp"))
        if e.get("type") != "query_result" or when is None:
            continue
        if not start <= when <= end:
            continue
        p = e["payload"]
        last[(e.get("agent_id"), str(p.get("session_id") or ""))] = p
    return sum(rollup_units("claude", p.get("model_usage") or {})
               for p in last.values())


def test_the_capture_is_the_replay_the_figures_were_measured_over() -> None:
    """A re-capture that drifts must fail loudly rather than move the pinned
    figures underneath the tests below."""
    subject, neighbour = quota_events(SUBJECT_ID), quota_events(NEIGHBOUR_ID)
    assert (len(subject), len(neighbour)) == (1069, 101)
    assert run_span(subject) == (_SPAN_START, _SPAN_END)
    rows = build_summary(subject)
    assert len(rows) == 46
    assert sum(len(r.subagents) for r in rows) == 24
    assert [r.step for r in rows if r.errored] == []


def test_the_capture_carries_no_conversation_prose() -> None:
    """Scope §8: only the fields the summary reads survived the projection, so
    the fixture is publishable as it stands."""
    for quest_id in (SUBJECT_ID, NEIGHBOUR_ID):
        for e in quota_events(quest_id):
            assert set(e) <= {"id", "type", "timestamp", "agent_id", "payload"}
            assert _prose_keys(e["payload"]) == set()
            if e["type"] == "tool_call":
                # `label` is free text the dispatching agent writes.
                assert set(e["payload"]) == {"name", "id", "id_ns", "input"}
                assert set(e["payload"]["input"]) == {"subagent_name"}


def test_the_neighbour_slice_carries_only_what_the_pipeline_needs() -> None:
    """AC 8(d) over real data: 9 readings, 65 usage rows, one pre-span anchor and
    one resolved triple — no whole log."""
    found = _slice(_span())
    assert (len(found.readings), len(found.usage), len(found.anchors)) == (9, 65, 1)
    assert found.agents == {
        "step:threshold/threshold": ("claude", "claude-opus-5", "subscription")}


def test_samo71_prices_from_the_table_to_the_vendors_own_figure() -> None:
    """AC 15: $42.20 from MODEL_PRICES, matching the vendor's reported total to
    the cent — it was $35.63, 15.6% low, before the cache-write fix."""
    events = quota_events(SUBJECT_ID)
    _work, total = summary_totals(build_summary(events), summary_total(events),
                                  exclusion_spans(events))
    assert cost_cell(total) == "$42.20*"
    assert round(_table_priced(events), 2) == 42.20


def test_the_threshold_step_excludes_both_of_its_human_waits() -> None:
    """AC 16(a): 386.1 s of wall clock less 80.3 s of human wait is 305.8 s of
    work, where the old single-marker accounting rendered `5m 20s`."""
    step = next(r for r in build_summary(quota_events(SUBJECT_ID))
                if r.step == "threshold")
    wall = step.wall_seconds
    assert wall is not None
    assert round(wall, 1) == 386.1
    assert round(step.wait_seconds, 1) == 80.3
    assert step.dur_cell("running") == "5m 5s"


def test_the_run_total_subtracts_the_same_union() -> None:
    """AC 16(d): 2h 8m of wall clock, 2h 6m of work."""
    events = quota_events(SUBJECT_ID)
    raw = summary_total(events)
    work, _total = summary_totals(build_summary(events), raw,
                                  exclusion_spans(events))
    assert raw is not None and work is not None
    assert (fmt_seconds(raw), fmt_seconds(work)) == ("2h 8m", "2h 6m")


def test_samo71_renders_283_percent_of_one_weeks_quota() -> None:
    """AC 1(a) / AC 22(b): 3.00 points of weekly movement x a 94.23% unit
    share."""
    figure = _claude_figure(_span())
    assert figure.movement == 3.0
    assert round(figure.subject_units, 4) == 29.9455
    assert round(100 * figure.subject_units / figure.total_units, 2) == 94.23
    assert f"{figure.percent:.2f}%" == "2.83%"


def test_the_straddling_neighbour_contributes_only_its_prorated_share() -> None:
    """AC 22(a)/(c): `fego87`'s steps ran across the window edge, so whole-session
    counting inflates it by 81% — and that inflation is precisely the superseded
    90.00% share behind the old 2.70% figure, which the shipped path must not
    produce."""
    span = _span()
    figure = _claude_figure(span)
    prorated = figure.total_units - figure.subject_units
    naive = _whole_session_units(quota_events(NEIGHBOUR_ID), span)
    assert round(prorated, 4) == 1.8338
    assert round(naive, 4) == 3.3260
    assert round(100 * figure.subject_units / figure.total_units, 2) == 94.23
    assert round(100 * figure.subject_units
                 / (figure.subject_units + naive), 2) == 90.00


def test_dropping_the_pre_span_anchor_over_counts_the_neighbour() -> None:
    """AC 21(f) over real data: the one anchor row is the whole difference
    between 1.8338 u and the naive 3.3260 u."""
    span = _span()
    found = _slice(span)
    with_anchor = units_in_span(
        increments([*found.anchors, *found.usage], dict(found.agents)), *span)
    without = units_in_span(
        increments(list(found.usage), dict(found.agents)), *span)
    assert round(with_anchor.get("claude"), 4) == 1.8338
    assert round(without.get("claude"), 4) == 3.3260


def test_the_step_bounded_span_moves_the_figure_by_under_a_hundredth() -> None:
    """AC 23(b): the alternative span pins a real ambiguity without being a
    delicate choice."""
    events = quota_events(SUBJECT_ID)
    wide = _span()
    starts = [w for w in (parse_ts(e.get("timestamp")) for e in events
                          if e.get("type") == "step_started") if w is not None]
    ends = [w for w in (parse_ts(e.get("timestamp")) for e in events
                        if e.get("type") == "step_completed") if w is not None]
    narrow = (starts[0], ends[-1])
    assert narrow != wide
    narrow_figure = quota_attribution(
        events, narrow, rows_units(build_summary(events, narrow)),
        [_slice(narrow)]).figures[0]
    assert abs(_claude_figure(wide).percent - narrow_figure.percent) <= 0.01


def test_no_test_can_reach_the_maintainers_live_archive(tmp_path: Path) -> None:
    """AC 22(d): conftest's `_isolate_global_config` pins IMP_HOME at an absent
    per-test directory and the figures above read one committed file, so nothing
    here can reach the live archive."""
    assert imp_home() == tmp_path / ".imp-isolated"
    assert not imp_home().exists()
    assert (Path(__file__).parent / "fixtures" / "quota" / "samo71"
            / f"{SUBJECT_ID}.jsonl.gz").is_file()
