from __future__ import annotations

import pytest

from imp.cli.quota_column import (
    BELOW_CELL,
    FOOTNOTE,
    LEGEND_BELOW,
    LEGEND_UNREAD,
    NO_FIGURE,
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


def _pts(gauge: str, points: float) -> Charge:
    return Charge.of(gauge, GaugeCharge.resolved(points))


def test_a_rows_cell_is_its_resolved_points() -> None:
    column = QuotaColumn(_pts("claude", 2.7))
    assert column.cell(_pts("claude", 2.7)) == "2.70%"
    assert column.cell(_pts("claude", 0.9)) == "0.90%"
    assert column.breakdown == ()
    assert column.legends == ()


def test_the_floor_sits_at_half_a_hundredth_of_a_point() -> None:
    column = QuotaColumn(_pts("claude", 1.0))
    assert column.cell(_pts("claude", 0.0)) == "<0.01%"
    assert column.cell(_pts("claude", 0.004)) == "<0.01%"
    assert column.cell(_pts("claude", 0.005)) == "0.01%"
    assert column.cell(_pts("claude", 0.02)) == "0.02%"


def test_a_row_with_no_resolved_week_or_with_two_gauges_reads_dash() -> None:
    column = QuotaColumn(_pts("claude", 1.0) + _pts("codex", 1.0))
    assert column.cell(NO_CHARGE) == NO_FIGURE
    assert column.cell(Charge.of("claude", GAUGE_BELOW)) == NO_FIGURE
    assert column.cell(Charge.of("claude", GAUGE_UNREAD)) == NO_FIGURE
    assert column.cell(_pts("claude", 1.0) + _pts("codex", 1.0)) == NO_FIGURE


def test_a_row_straddling_a_resolved_and_an_unread_week_shows_the_resolved_part() -> None:
    column = QuotaColumn(_pts("claude", 1.0))
    assert column.cell(Charge.of("claude", GaugeCharge(1.5, False, True))) == "1.50%"


@pytest.mark.parametrize("charge,expected", [
    (_pts("claude", 2.7), "2.70%"),
    (Charge.of("claude", GAUGE_BELOW), "<1%"),
    (Charge.of("claude", GaugeCharge(1.8, True, False)), "1.80%"),
    (Charge.of("claude", GAUGE_UNREAD), "—"),
    (_pts("claude", 1.0) + _pts("codex", 1.0), "—"),
    (NO_CHARGE, "—"),
], ids=["resolved", "below", "resolved-and-below", "unread", "mixed", "nothing"])
def test_the_total_reads_under_one_point_only_when_no_week_resolved(
        charge: Charge, expected: str) -> None:
    assert QuotaColumn(charge).total(charge) == expected


def test_a_mixed_run_breaks_each_gauge_out_and_prefixes_its_legends() -> None:
    run = _pts("claude", 2.7) + Charge.of("codex", GAUGE_BELOW + GAUGE_UNREAD)
    column = QuotaColumn(run)
    assert column.breakdown == (("claude", "2.70%"), ("codex", BELOW_CELL))
    assert column.legends == (f"codex: {LEGEND_BELOW}", f"codex: {LEGEND_UNREAD}")


def test_a_single_gauge_run_names_below_then_unread_unprefixed() -> None:
    column = QuotaColumn(Charge.of("claude", GaugeCharge(1.8, True, True)))
    assert column.breakdown == ()
    assert column.legends == (LEGEND_BELOW, LEGEND_UNREAD)


@pytest.mark.parametrize("run", [
    _pts("claude", 1.0),
    Charge.of("claude", GAUGE_BELOW),
    Charge.of("claude", GAUGE_UNREAD) + _pts("codex", 0.5),
], ids=["resolved", "below", "one-gauge-unread"])
def test_any_usable_reading_takes_the_column(run: Charge) -> None:
    assert quota_view(run) == QuotaColumn(run)


def test_no_usable_reading_anywhere_falls_back_to_cost() -> None:
    assert quota_view(Charge.of("claude", GAUGE_UNREAD)) == QuotaFallback((LEGEND_UNREAD,))
    assert quota_view(NO_CHARGE) == QuotaFallback((LEGEND_UNREAD,))


def test_two_unread_gauges_are_each_named_by_provider() -> None:
    run = Charge.of("claude", GAUGE_UNREAD) + Charge.of("codex", GAUGE_UNREAD)
    assert quota_view(run) == QuotaFallback(
        (f"claude: {LEGEND_UNREAD}", f"codex: {LEGEND_UNREAD}"))


def test_the_copy_is_verbatim() -> None:
    assert FOOTNOTE == ("* this quest's part of all imp work this week, applied to"
                        " the week's gauge; final once the week resets; usage"
                        " outside imp skews it high")
    assert LEGEND_BELOW == ("quota below measurement resolution; the weekly gauge"
                            " has not moved yet this week")
    assert LEGEND_UNREAD == ("quota unavailable; no weekly gauge readings exist"
                             " for this run's week")
    assert (NO_FIGURE, BELOW_CELL) == ("—", "<1%")
