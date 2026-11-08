from __future__ import annotations

import pytest

from imp.cli.accounting import RowUsage
from imp.cli.quota_column import FOOTNOTE, QuotaColumn, QuotaFallback
from imp.cli.quota_share import QuotaFigure, QuotaWithheld, Units, Withheld
from imp.cli.render_markdown import render_summary_markdown
from imp.cli.render_summary import (
    StepRow,
    SubagentRow,
    SummaryHeader,
    render_summary,
    total_cell,
)

_HDR = SummaryHeader(title="feature", quest_id="abc", status="completed",
                     total_seconds=2342.0)


def _row(step: str, secs: int, usage: RowUsage, units: Units,
         subs: list[SubagentRow] | None = None) -> StepRow:
    return StepRow(phase="plan", step=step, start_ts="2026-01-01T00:00:00+00:00",
                   end_ts=f"2026-01-01T00:{secs // 60:02d}:{secs % 60:02d}+00:00",
                   usage=usage, units=units, subagents=subs or [])


def _usage(fresh: int = 1_000_000, out: int = 20_000) -> RowUsage:
    """A notional (subscription) row: with no quota column it renders $3.00*."""
    return RowUsage(fresh_input=fresh, output=out, dollars=3.0, priced=1,
                    notional=1)


def test_a_single_gauge_replaces_the_cost_column_with_each_rows_share() -> None:
    rows = [_row("plan-writer", 90, _usage(), Units.of("claude", 2.0)),
            _row("plan-reviewer", 120, _usage(500_000, 9_000),
                 Units.of("claude", 1.0)),
            _row("coder", 60, _usage(200_000, 3_000),
                 Units.of("claude", 0.001)),
            _row("plan-finalized", 0, RowUsage(), Units())]
    # 9 points of movement over a 10-unit window: each row's share is 0.9x its
    # own units, and the total is 0.9x their sum (3.001) -- 2.70%.
    column = QuotaColumn((QuotaFigure("claude", 9.0, 3.001, 10.0),))
    lines = render_summary(rows, header=_HDR, color=False, width=80,
                           quota=column)
    assert lines[1:3] == ["                             tokens       quota",
                          "                    time      in    out  weekly*"]
    assert lines[4:8] == [
        "  plan-writer     1m 30s    1.0M  20.0k   1.80%",
        "  plan-reviewer    2m 0s  500.0k   9.0k   0.90%",
        "  coder            1m 0s  200.0k   3.0k  <0.01%",
        "  plan-finalized      0s       —      —       —"]
    assert lines[-2] == "  total           39m 2s    1.7M  32.0k   2.70%"
    assert lines[-1] == ("  * estimated to ±1 point of the weekly gauge; usage "
                         "outside imp during the run skews it high")
    assert not any("$" in ln or "cost" in ln for ln in lines)


def test_one_point_of_movement_still_renders_the_column() -> None:
    rows = [_row("plan-writer", 90, _usage(), Units.of("claude", 4.0))]
    column = QuotaColumn((QuotaFigure("claude", 1.0, 4.0, 8.0),))
    lines = render_summary(rows, header=_HDR, color=False, width=80,
                           quota=column)
    assert lines[-2].endswith("  0.50%")
    assert lines[-1] == f"  {FOOTNOTE}"


def test_a_run_mixing_metered_and_notional_steps_shows_no_dollar_figure() -> None:
    metered = RowUsage(fresh_input=200_000, output=9_000, dollars=0.73, priced=1)
    rows = [_row("coder", 90, metered, Units.of("claude", 1.0)),
            _row("task-lead", 120, _usage(), Units.of("claude", 3.0))]
    column = QuotaColumn((QuotaFigure("claude", 4.0, 4.0, 8.0),))
    lines = render_summary(rows, header=_HDR, color=False, width=80,
                           quota=column)
    assert [ln.split()[-1] for ln in lines[4:6]] == ["0.50%", "1.50%"]
    assert lines[-2].endswith("  2.00%")
    assert not any("$" in ln or "~" in ln for ln in lines)


def test_no_quota_argument_leaves_the_cost_column_exactly_as_it_was() -> None:
    rows = [_row("plan-writer", 90, _usage(), Units.of("claude", 3.0))]
    lines = render_summary(rows, header=_HDR, color=False, width=80)
    assert lines[1] == "                 time    in    out    cost"
    assert lines[-2] == "  total        39m 2s  1.0M  20.0k  $3.00*"
    assert lines[-1] == ("  * theoretical API list price; actual subscription"
                         " usage is much lower")


_NO_READINGS = "quota unavailable; this run recorded no quota readings"


def test_a_withheld_gauge_keeps_todays_cost_column_and_adds_one_legend() -> None:
    rows = [_row("plan-writer", 90, _usage(), Units.of("claude", 3.0))]
    lines = render_summary(rows, header=_HDR, color=False, width=80,
                           quota=QuotaFallback((_NO_READINGS,)))
    assert lines[1] == "                 time    in    out    cost"
    assert lines[3] == "  plan-writer  1m 30s  1.0M  20.0k  $3.00*"
    assert lines[-3] == "  total        39m 2s  1.0M  20.0k  $3.00*"
    assert lines[-2] == ("  * theoretical API list price; actual subscription"
                         " usage is much lower")
    assert lines[-1] == f"  {_NO_READINGS}"
    assert not any("weekly" in ln or "—" in ln or FOOTNOTE in ln
                   for ln in lines)


def test_the_fallback_legend_is_dim_like_the_rest() -> None:
    rows = [_row("plan-writer", 90, _usage(), Units.of("claude", 3.0))]
    lines = render_summary(rows, header=_HDR, color=True, width=80,
                           quota=QuotaFallback((_NO_READINGS,)))
    assert lines[-1] == f"\x1b[2m  {_NO_READINGS}\x1b[0m"


_STILL = ("quota below measurement resolution; the weekly gauge did not move "
          "during this run")


def test_two_gauges_report_one_line_each_below_a_dashed_total() -> None:
    rows = [_row("plan-writer", 90, _usage(), Units.of("claude", 3.0)),
            _row("coder", 120, _usage(500_000, 9_000), Units.of("codex", 2.0)),
            _row("task-lead", 60, _usage(200_000, 3_000),
                 Units.of("claude", 1.0) + Units.of("codex", 1.0))]
    column = QuotaColumn((QuotaFigure("claude", 3.0, 9.0, 10.0),
                          QuotaWithheld("codex", Withheld.BELOW_RESOLUTION)))
    lines = render_summary(rows, header=_HDR, color=False, width=80,
                           quota=column)
    assert lines[4:7] == [
        "  plan-writer  1m 30s    1.0M  20.0k   0.90%",
        "  coder         2m 0s  500.0k   9.0k       —",
        "  task-lead     1m 0s  200.0k   3.0k       —"]
    assert lines[-5] == "  total        39m 2s    1.7M  32.0k       —"
    assert lines[-4] == "    claude                             2.70%"
    assert lines[-3] == "    codex                                  —"
    assert lines[-2] == f"  {FOOTNOTE}"
    assert lines[-1] == f"  codex: {_STILL}"


def test_a_breakdown_row_can_be_the_widest_thing_in_its_column() -> None:
    # Both breakdown fields out-measure every row: `claude` is wider than any
    # step name, and 100.00% (the codex gauge's whole-quest share, of which
    # these two rows render a sliver) is wider than any cell above it.
    rows = [_row("coder", 90, _usage(), Units.of("claude", 1.0)),
            _row("plan", 120, _usage(500_000, 9_000), Units.of("codex", 0.001))]
    column = QuotaColumn((QuotaFigure("claude", 2.0, 1.0, 4.0),
                          QuotaFigure("codex", 100.0, 1.0, 1.0)))
    lines = render_summary(rows, header=_HDR, color=False, width=80,
                           quota=column)
    assert lines[1:3] == ["                       tokens        quota",
                          "              time      in    out   weekly*"]
    assert lines[4:6] == ["  coder     1m 30s    1.0M  20.0k    0.50%",
                          "  plan       2m 0s  500.0k   9.0k    0.10%"]
    assert lines[-4] == "  total     39m 2s    1.5M  29.0k        —"
    assert lines[-3] == "    claude                           0.50%"
    assert lines[-2] == "    codex                          100.00%"


def test_a_single_provider_run_renders_no_breakdown_rows() -> None:
    rows = [_row("plan-writer", 90, _usage(), Units.of("claude", 3.0))]
    column = QuotaColumn((QuotaFigure("claude", 3.0, 3.0, 10.0),))
    lines = render_summary(rows, header=_HDR, color=False, width=80,
                           quota=column)
    assert lines[-3] == ""
    assert lines[-2] == "  total        39m 2s  1.0M  20.0k   0.90%"
    assert lines[-1] == f"  {FOOTNOTE}"


def test_a_subagent_rows_numbers_and_duration_recede_behind_its_step() -> None:
    subs = [SubagentRow(name="explorer", start_ts="2026-01-01T00:00:05+00:00",
                        end_ts="2026-01-01T00:00:50+00:00",
                        usage=_usage(100_000, 2_000),
                        units=Units.of("claude", 0.5)),
            SubagentRow(name="coder", start_ts="2026-01-01T00:00:05+00:00",
                        end_ts="2026-01-01T00:00:55+00:00", errored=True,
                        usage=_usage(50_000, 1_000),
                        units=Units.of("claude", 0.25))]
    rows = [_row("task-lead", 90, _usage(), Units.of("claude", 3.0), subs)]
    column = QuotaColumn((QuotaFigure("claude", 3.0, 3.75, 10.0),))
    lines = render_summary(rows, header=_HDR, color=True, width=80,
                           quota=column)
    assert lines[1] == "\x1b[2m                           tokens       quota\x1b[0m"
    assert lines[2] == "\x1b[2m                  time      in    out  weekly*\x1b[0m"
    # The step's own cells stay undimmed; the subagent's go dim as one block.
    assert lines[4] == ("  \x1b[94mtask-lead   \x1b[0m  \x1b[94m1m 30s\x1b[0m"
                        "    1.1M  23.0k   1.12%")
    assert lines[5] == ("    \x1b[35m↳ explorer\x1b[0m  \x1b[2m   45s\x1b[0m"
                        "  \x1b[2m100.0k   2.0k   0.15%\x1b[0m")
    assert lines[6] == ("    \x1b[1m\x1b[31m↳ coder   \x1b[0m"
                        "  \x1b[1m\x1b[31m   50s\x1b[0m"
                        "  \x1b[2m 50.0k   1.0k   0.07%\x1b[0m")
    assert lines[-1] == f"\x1b[2m  {FOOTNOTE}\x1b[0m"


def _metered(fresh: int = 200_000, out: int = 9_000) -> RowUsage:
    """An API-key row: no notional usage, so its cost cell carries the ~ marker."""
    return RowUsage(fresh_input=fresh, output=out, dollars=0.73, priced=1)


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


_RESOLVED = [_row("plan-writer", 90, _usage(), Units.of("claude", 3.0))]
_MIXED = [_row("plan-writer", 90, _usage(), Units.of("claude", 3.0)),
          _row("coder", 120, _metered(), Units.of("codex", 2.0))]


@pytest.mark.parametrize("rows,quota,expected", [
    # A resolved gauge: 3 points over a 10-unit window, 3 units drawn -> 0.90%.
    (_RESOLVED, QuotaColumn((QuotaFigure("claude", 3.0, 3.0, 10.0),)), "0.90%"),
    # Two providers in the total's units: no single window to report.
    (_MIXED, QuotaColumn((QuotaFigure("claude", 3.0, 9.0, 10.0),
                          QuotaWithheld("codex", Withheld.BELOW_RESOLUTION))),
     "—"),
    # No quota view at all: the metered dollar cell, ~ marker and all.
    ([_row("coder", 90, _metered(), Units.of("claude", 1.0))], None, "~$0.73"),
    # A withheld gauge: the notional dollar cell, * marker and all.
    (_RESOLVED, QuotaFallback((_NO_READINGS,)), "$3.00*"),
], ids=["resolved", "mixed", "metered", "withheld"])
def test_total_cell_is_the_total_rows_last_cell_in_both_renderers(
    rows: list[StepRow],
    quota: QuotaColumn | QuotaFallback | None,
    expected: str,
) -> None:
    """One helper, three call sites that cannot disagree: the terminal table, the
    markdown export, and (in part 3) the card's `run:` charge."""
    assert total_cell(rows, quota) == expected
    assert _terminal_total_cell(rows, quota) == expected
    assert _markdown_total_cell(rows, quota) == expected
