from __future__ import annotations

import pytest

from imp.cli.accounting import RowUsage
from imp.cli.quota_column import (
    FOOTNOTE,
    LEGEND_BELOW,
    LEGEND_UNREAD,
    QuotaColumn,
    QuotaFallback,
    quota_view,
)
from imp.cli.quota_share import (
    GAUGE_BELOW,
    GAUGE_UNREAD,
    NO_CHARGE,
    Charge,
    GaugeCharge,
)
from imp.cli.render_markdown import render_summary_markdown
from imp.cli.render_summary import (
    StepRow,
    SubagentRow,
    SummaryHeader,
    render_summary,
    rows_charge,
    total_cell,
)

_HDR = SummaryHeader(title="feature", quest_id="abc", status="completed",
                     total_seconds=2342.0)


def _row(step: str, secs: int, usage: RowUsage, charge: Charge,
         subs: list[SubagentRow] | None = None) -> StepRow:
    return StepRow(phase="plan", step=step, start_ts="2026-01-01T00:00:00+00:00",
                   end_ts=f"2026-01-01T00:{secs // 60:02d}:{secs % 60:02d}+00:00",
                   usage=usage, charge=charge, subagents=subs or [])


def _usage(fresh: int = 1_000_000, out: int = 20_000) -> RowUsage:
    """A notional (subscription) row: with no quota column it renders $3.00*."""
    return RowUsage(fresh_input=fresh, output=out, dollars=3.0, priced=1,
                    notional=1)


def _metered(fresh: int = 200_000, out: int = 9_000) -> RowUsage:
    """An API-key row: no notional usage, so its cost cell carries the ~ marker."""
    return RowUsage(fresh_input=fresh, output=out, dollars=0.73, priced=1)


def _pts(gauge: str, points: float) -> Charge:
    return Charge.of(gauge, GaugeCharge.resolved(points))


_BELOW = Charge.of("claude", GAUGE_BELOW)
_UNREAD = Charge.of("claude", GAUGE_UNREAD)
_CODEX_UNREAD = Charge.of("codex", GAUGE_UNREAD)


def _view(rows: list[StepRow]) -> QuotaColumn | QuotaFallback:
    return quota_view(rows_charge(rows))


def _render(rows: list[StepRow], *, color: bool = False) -> list[str]:
    return render_summary(rows, header=_HDR, color=color, width=80,
                          quota=_view(rows))


def test_a_single_gauge_replaces_the_cost_column_with_each_rows_share() -> None:
    rows = [_row("plan-writer", 90, _usage(), _pts("claude", 1.8)),
            _row("plan-reviewer", 120, _usage(500_000, 9_000), _pts("claude", 0.9)),
            _row("coder", 60, _usage(200_000, 3_000), _pts("claude", 0.0009)),
            _row("plan-finalized", 0, RowUsage(), NO_CHARGE)]
    lines = _render(rows)
    assert lines[1:3] == ["                             tokens       quota",
                          "                    time      in    out  weekly*"]
    assert lines[4:8] == [
        "  plan-writer     1m 30s    1.0M  20.0k   1.80%",
        "  plan-reviewer    2m 0s  500.0k   9.0k   0.90%",
        "  coder            1m 0s  200.0k   3.0k  <0.01%",
        "  plan-finalized      0s       —      —       —"]
    # The total is the rows' sum, 2.7009 points.
    assert lines[-2] == "  total           39m 2s    1.7M  32.0k   2.70%"
    assert lines[-1] == f"  {FOOTNOTE}"
    assert not any("$" in ln or "cost" in ln for ln in lines)


def test_an_unmoved_week_reads_dash_per_row_and_under_one_point_in_total() -> None:
    """State 2 with no week resolved."""
    rows = [_row("plan-writer", 90, _usage(), _BELOW),
            _row("coder", 60, _usage(200_000, 3_000), _BELOW)]
    lines = _render(rows)
    assert "weekly*" in lines[2]
    assert [ln.split()[-1] for ln in lines[4:6]] == ["—", "—"]
    assert lines[-3].split()[0] == "total"
    assert lines[-3].split()[-1] == "<1%"
    assert lines[-2:] == [f"  {FOOTNOTE}", f"  {LEGEND_BELOW}"]


def test_once_one_week_resolves_the_total_is_the_resolved_rows_sum() -> None:
    """State 2 beside a resolved week."""
    rows = [_row("plan-writer", 90, _usage(), _pts("claude", 1.8)),
            _row("coder", 60, _usage(200_000, 3_000), _BELOW)]
    lines = _render(rows)
    assert [ln.split()[-1] for ln in lines[4:6]] == ["1.80%", "—"]
    assert lines[-3].split()[-1] == "1.80%"
    assert lines[-2:] == [f"  {FOOTNOTE}", f"  {LEGEND_BELOW}"]


def test_a_metered_row_in_a_subscription_run_reads_dash_and_adds_nothing() -> None:
    """AC 5(h)."""
    rows = [_row("coder", 90, _metered(), NO_CHARGE),
            _row("task-lead", 120, _usage(), _pts("claude", 1.5))]
    lines = _render(rows)
    assert [ln.split()[-1] for ln in lines[4:6]] == ["—", "1.50%"]
    assert lines[-2].endswith("  1.50%")
    assert not any("$" in ln or "~" in ln for ln in lines)


def test_no_quota_argument_leaves_the_cost_column_exactly_as_it_was() -> None:
    rows = [_row("plan-writer", 90, _usage(), _pts("claude", 0.9))]
    lines = render_summary(rows, header=_HDR, color=False, width=80)
    assert lines[1] == "                 time    in    out    cost"
    assert lines[-2] == "  total        39m 2s  1.0M  20.0k  $3.00*"
    assert lines[-1] == ("  * theoretical API list price; actual subscription"
                         " usage is much lower")


def test_no_usable_reading_keeps_todays_cost_column_and_adds_one_legend() -> None:
    """State 3 across the whole run."""
    rows = [_row("plan-writer", 90, _usage(), _UNREAD)]
    assert _view(rows) == QuotaFallback((LEGEND_UNREAD,))
    lines = _render(rows)
    assert lines[1] == "                 time    in    out    cost"
    assert lines[3] == "  plan-writer  1m 30s  1.0M  20.0k  $3.00*"
    assert lines[-3] == "  total        39m 2s  1.0M  20.0k  $3.00*"
    assert lines[-2] == ("  * theoretical API list price; actual subscription"
                         " usage is much lower")
    assert lines[-1] == f"  {LEGEND_UNREAD}"
    assert not any("weekly" in ln or "—" in ln or FOOTNOTE in ln
                   for ln in lines[:-1])


def test_the_fallback_legend_is_dim_like_the_rest() -> None:
    rows = [_row("plan-writer", 90, _usage(), _UNREAD)]
    lines = _render(rows, color=True)
    assert lines[-1] == f"\x1b[2m  {LEGEND_UNREAD}\x1b[0m"


def test_a_mixed_run_whose_codex_gauge_is_unread_keeps_the_column() -> None:
    """AC 5(f)."""
    rows = [_row("plan-writer", 90, _usage(), _pts("claude", 0.9)),
            _row("coder", 120, _usage(500_000, 9_000), _CODEX_UNREAD),
            _row("task-lead", 60, _usage(200_000, 3_000),
                 _pts("claude", 1.8) + _CODEX_UNREAD)]
    lines = _render(rows)
    assert lines[4:7] == [
        "  plan-writer  1m 30s    1.0M  20.0k   0.90%",
        "  coder         2m 0s  500.0k   9.0k       —",
        "  task-lead     1m 0s  200.0k   3.0k       —"]
    assert lines[-5] == "  total        39m 2s    1.7M  32.0k       —"
    assert lines[-4] == "    claude                             2.70%"
    assert lines[-3] == "    codex                                  —"
    assert lines[-2] == f"  {FOOTNOTE}"
    assert lines[-1] == f"  codex: {LEGEND_UNREAD}"


def test_an_unmoved_codex_week_reads_under_one_point_in_its_breakdown() -> None:
    rows = [_row("plan-writer", 90, _usage(), _pts("claude", 0.9)),
            _row("coder", 120, _usage(500_000, 9_000), Charge.of("codex", GAUGE_BELOW))]
    lines = _render(rows)
    assert lines[-4].split() == ["claude", "0.90%"]
    assert lines[-3].split() == ["codex", "<1%"]
    assert lines[-2] == f"  {FOOTNOTE}"
    assert lines[-1] == f"  codex: {LEGEND_BELOW}"


def test_a_single_gauge_run_with_an_unread_week_names_it_unprefixed() -> None:
    """AC 5(g)."""
    rows = [_row("plan-writer", 90, _usage(), _pts("claude", 1.8)),
            _row("coder", 60, _usage(200_000, 3_000), _UNREAD)]
    lines = _render(rows)
    assert [ln.split()[-1] for ln in lines[4:6]] == ["1.80%", "—"]
    assert lines[-3].split()[-1] == "1.80%"
    assert lines[-2:] == [f"  {FOOTNOTE}", f"  {LEGEND_UNREAD}"]


def test_a_breakdown_row_can_be_the_widest_thing_in_its_column() -> None:
    # `claude` is wider than any step name, and codex's 100.00% (the sum of its
    # two rows) is wider than any cell above it.
    rows = [_row("coder", 90, _usage(), _pts("claude", 0.5)),
            _row("plan", 120, _usage(500_000, 9_000), _pts("codex", 60.0)),
            _row("rev", 60, _usage(200_000, 3_000), _pts("codex", 40.0))]
    lines = _render(rows)
    assert lines[1:3] == ["                       tokens        quota",
                          "              time      in    out   weekly*"]
    assert lines[4:7] == ["  coder     1m 30s    1.0M  20.0k    0.50%",
                          "  plan       2m 0s  500.0k   9.0k   60.00%",
                          "  rev        1m 0s  200.0k   3.0k   40.00%"]
    assert lines[8] == "  total     39m 2s    1.7M  32.0k        —"
    assert lines[9] == "    claude                           0.50%"
    assert lines[10] == "    codex                          100.00%"
    assert {len(ln) for ln in lines[4:7] + lines[8:11]} == {42}


def test_a_single_provider_run_renders_no_breakdown_rows() -> None:
    rows = [_row("plan-writer", 90, _usage(), _pts("claude", 0.9))]
    lines = _render(rows)
    assert lines[-3] == ""
    assert lines[-2] == "  total        39m 2s  1.0M  20.0k   0.90%"
    assert lines[-1] == f"  {FOOTNOTE}"


def test_a_subagent_rows_numbers_and_duration_recede_behind_its_step() -> None:
    subs = [SubagentRow(name="explorer", start_ts="2026-01-01T00:00:05+00:00",
                        end_ts="2026-01-01T00:00:50+00:00",
                        usage=_usage(100_000, 2_000),
                        charge=_pts("claude", 0.15)),
            SubagentRow(name="coder", start_ts="2026-01-01T00:00:05+00:00",
                        end_ts="2026-01-01T00:00:55+00:00", stop="failed",
                        usage=_usage(50_000, 1_000),
                        charge=_pts("claude", 0.07))]
    rows = [_row("task-lead", 90, _usage(), _pts("claude", 0.9), subs)]
    lines = _render(rows, color=True)
    assert lines[1] == "\x1b[2m                           tokens       quota\x1b[0m"
    assert lines[2] == "\x1b[2m                  time      in    out  weekly*\x1b[0m"
    # The step's own cells stay undimmed and carry its subtree (0.9 + 0.15 + 0.07).
    assert lines[4] == ("  \x1b[94mtask-lead   \x1b[0m  \x1b[94m1m 30s\x1b[0m"
                        "    1.1M  23.0k   1.12%")
    assert lines[5] == ("    \x1b[35m↳ explorer\x1b[0m  \x1b[2m   45s\x1b[0m"
                        "  \x1b[2m100.0k   2.0k   0.15%\x1b[0m")
    assert lines[6] == ("    \x1b[1m\x1b[31m↳ coder   \x1b[0m"
                        "  \x1b[1m\x1b[31m   50s\x1b[0m"
                        "  \x1b[2m 50.0k   1.0k   0.07%\x1b[0m")
    assert lines[-1] == f"\x1b[2m  {FOOTNOTE}\x1b[0m"


def _terminal_total_cell(rows: list[StepRow],
                         quota: QuotaColumn | QuotaFallback | None) -> str:
    lines = render_summary(rows, header=_HDR, color=False, width=80, quota=quota)
    total = next(ln for ln in lines if ln.startswith("  total"))
    return total.split()[-1]


def _markdown_total_cell(rows: list[StepRow],
                         quota: QuotaColumn | QuotaFallback | None) -> str:
    out = render_summary_markdown(rows, header=_HDR, quota=quota)
    total = next(ln for ln in out.splitlines() if ln.startswith("| total |"))
    return total.split("|")[-2].strip()


_RESOLVED = [_row("plan-writer", 90, _usage(), _pts("claude", 0.9))]
_BELOW_ROWS = [_row("plan-writer", 90, _usage(), _BELOW)]
_MIXED = [_row("plan-writer", 90, _usage(), _pts("claude", 0.9)),
          _row("coder", 120, _usage(500_000, 9_000), _CODEX_UNREAD)]
_METERED = [_row("coder", 90, _metered(), NO_CHARGE)]
_UNREAD_ROWS = [_row("plan-writer", 90, _usage(), _UNREAD)]


@pytest.mark.parametrize("rows,quota,expected", [
    (_RESOLVED, _view(_RESOLVED), "0.90%"),
    (_BELOW_ROWS, _view(_BELOW_ROWS), "<1%"),
    # Two gauges in the total's charge: no single window to report.
    (_MIXED, _view(_MIXED), "—"),
    # No quota view at all: the metered dollar cell, ~ marker and all.
    (_METERED, None, "~$0.73"),
    # No usable reading: the notional dollar cell, * marker and all.
    (_UNREAD_ROWS, _view(_UNREAD_ROWS), "$3.00*"),
], ids=["resolved", "below", "mixed", "metered", "unread"])
def test_total_cell_is_the_total_rows_last_cell_in_both_renderers(
    rows: list[StepRow],
    quota: QuotaColumn | QuotaFallback | None,
    expected: str,
) -> None:
    """One helper, three call sites that cannot disagree: the terminal table, the
    markdown export, and the card's `run:` charge."""
    assert total_cell(rows, quota) == expected
    assert _terminal_total_cell(rows, quota) == expected
    assert _markdown_total_cell(rows, quota) == expected
