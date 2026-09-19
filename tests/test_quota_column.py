from __future__ import annotations

from datetime import UTC, datetime

import pytest

from imp.cli.quota_column import (
    FOOTNOTE,
    SESSION_PREDATES,
    QuotaColumn,
    QuotaFallback,
    quota_view,
)
from imp.cli.quota_share import (
    NO_UNITS,
    QuotaAttribution,
    QuotaFigure,
    QuotaWithheld,
    Units,
    Withheld,
)

_NO_READINGS = "quota unavailable; this run recorded no quota readings"
_RESET = "quota unmeasurable; the weekly window reset during this run"
_STILL = ("quota below measurement resolution; the weekly gauge did not move "
          "during this run")
_NO_USAGE = "quota unattributable; no tracked usage in this run's window"

_T = datetime(2026, 1, 1, tzinfo=UTC)


def _attr(*gauges: QuotaFigure | QuotaWithheld) -> QuotaAttribution:
    return QuotaAttribution(span=(_T, _T), gauges=gauges)


def test_a_rows_share_is_its_units_fraction_of_the_windows_movement() -> None:
    # 3 points of movement over a 10-unit window: 9 units is 2.70% of the week.
    column = QuotaColumn((QuotaFigure("claude", 3.0, 9.0, 10.0),))
    assert column.cell(Units.of("claude", 9.0)) == "2.70%"
    assert column.cell(Units.of("claude", 3.0)) == "0.90%"
    assert column.breakdown == ()
    assert column.legends == ()


def test_a_share_below_a_hundredth_of_a_point_reads_under_the_floor() -> None:
    column = QuotaColumn((QuotaFigure("claude", 3.0, 9.0, 10.0),))
    assert column.cell(Units.of("claude", 0.001)) == "<0.01%"
    assert column.cell(Units.of("claude", 0.0)) == "<0.01%"
    assert column.cell(Units.of("claude", 0.02)) == "0.01%"


def test_the_floor_sits_at_half_a_hundredth_of_a_point() -> None:
    # scale() is the identity here, so the argument IS the point figure.
    column = QuotaColumn((QuotaFigure("claude", 1.0, 1.0, 1.0),))
    assert column.cell(Units.of("claude", 0.004)) == "<0.01%"
    assert column.cell(Units.of("claude", 0.005)) == "0.01%"


def test_no_tracked_work_and_a_subtree_spanning_two_gauges_both_read_dash() -> None:
    column = QuotaColumn((QuotaFigure("claude", 3.0, 9.0, 10.0),
                          QuotaFigure("codex", 2.0, 1.0, 4.0)))
    assert column.cell(NO_UNITS) == "—"
    assert column.cell(Units.of("claude", 1.0) + Units.of("codex", 1.0)) == "—"
    assert column.breakdown == (("claude", "2.70%"), ("codex", "0.50%"))


def test_a_withheld_gauge_beside_a_resolved_one_is_named_not_implied() -> None:
    column = QuotaColumn((QuotaFigure("claude", 3.0, 9.0, 10.0),
                          QuotaWithheld("codex", Withheld.BELOW_RESOLUTION)))
    assert column.breakdown == (("claude", "2.70%"), ("codex", "—"))
    assert column.cell(Units.of("codex", 5.0)) == "—"
    assert column.legends == (f"codex: {_STILL}",)


def test_a_gauge_this_run_never_drew_on_is_not_this_runs_gauge() -> None:
    # The weekly window is account-global, so an overlapping codex quest's
    # reading reaches the attribution even for a pure-claude run.
    view = quota_view(_attr(QuotaFigure("claude", 3.0, 9.0, 9.0),
                            QuotaWithheld("codex", Withheld.NO_TRACKED_USAGE)),
                      providers=("claude",))
    assert view == QuotaColumn((QuotaFigure("claude", 3.0, 9.0, 9.0),))
    assert isinstance(view, QuotaColumn)
    assert view.cell(Units.of("claude", 9.0)) == "3.00%"
    assert view.breakdown == ()
    assert view.legends == ()


def test_filtering_to_one_provider_leaves_one_unprefixed_legend() -> None:
    view = quota_view(_attr(QuotaWithheld("claude", Withheld.WINDOW_RESET),
                            QuotaWithheld("codex", Withheld.NO_TRACKED_USAGE)),
                      providers=("claude",))
    assert view == QuotaFallback((_RESET,))


def test_a_session_predating_the_op_falls_back_with_its_own_legend() -> None:
    assert quota_view(None, providers=("claude",)) == QuotaFallback(
        (SESSION_PREDATES,))
    assert SESSION_PREDATES == ("quota unavailable; this quest's session "
                                "predates the quota column")


@pytest.mark.parametrize("reason,legend", [
    (Withheld.NO_READINGS, _NO_READINGS),
    (Withheld.WINDOW_RESET, _RESET),
    (Withheld.BELOW_RESOLUTION, _STILL),
    (Withheld.NO_TRACKED_USAGE, _NO_USAGE),
])
def test_each_withhold_state_carries_its_verbatim_legend(
        reason: Withheld, legend: str) -> None:
    assert quota_view(_attr(QuotaWithheld("claude", reason)),
                      providers=("claude",)) == QuotaFallback((legend,))


def test_a_run_whose_gauge_set_is_empty_read_nothing_at_all() -> None:
    assert quota_view(_attr(), providers=()) == QuotaFallback((_NO_READINGS,))


def test_readings_with_no_units_anywhere_are_unattributable_not_unread() -> None:
    # The run recorded readings but drew nothing the units walk could credit,
    # so no provider survives the filter -- that is AC 6(d), not "no readings".
    assert quota_view(_attr(QuotaWithheld("claude", Withheld.NO_TRACKED_USAGE)),
                      providers=()) == QuotaFallback((_NO_USAGE,))


def test_two_withheld_gauges_are_each_named_by_provider() -> None:
    view = quota_view(_attr(QuotaWithheld("claude", Withheld.WINDOW_RESET),
                            QuotaWithheld("codex", Withheld.NO_TRACKED_USAGE)),
                      providers=("claude", "codex"))
    assert view == QuotaFallback((f"claude: {_RESET}", f"codex: {_NO_USAGE}"))


def test_one_resolved_gauge_takes_the_column() -> None:
    view = quota_view(_attr(QuotaFigure("claude", 1.0, 1.0, 2.0)),
                      providers=("claude",))
    assert isinstance(view, QuotaColumn)
    assert view.cell(Units.of("claude", 1.0)) == "0.50%"


def test_the_footnote_is_verbatim() -> None:
    assert FOOTNOTE == ("* estimated to ±1 point of the weekly gauge; usage "
                        "outside imp during the run skews it high")
