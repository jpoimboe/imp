# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""The quota column's render-facing projection of a run's `Charge`: one cell per
row and for the total, the per-gauge breakdown a mixed run needs, and the legend
lines naming a week whose gauge had not moved or had no readings.

Two shapes, and no third: the column (some gauge has a usable reading in one of
the run's weeks) and the `cost` fallback (none has)."""

from __future__ import annotations

from dataclasses import dataclass

from imp.cli.quota_share import Charge, GaugeCharge

FOOTNOTE = ("* this quest's part of all imp work this week, applied to the week's"
            " gauge; final once the week resets; usage outside imp skews it high")
NO_FIGURE = "—"
BELOW_CELL = "<1%"
LEGEND_BELOW = ("quota below measurement resolution; the weekly gauge has not"
                " moved yet this week")
LEGEND_UNREAD = ("quota unavailable; no weekly gauge readings exist for this"
                 " run's week")


def share_cell(points: float) -> str:
    return "<0.01%" if points < 0.005 else f"{points:.2f}%"


def _gauge_total(charge: GaugeCharge) -> str:
    """The resolved weeks' sum; `<1%` only when none resolved and one had not
    ticked yet."""
    if charge.points is not None:
        return share_cell(charge.points)
    return BELOW_CELL if charge.below else NO_FIGURE


def _legends(run: Charge) -> tuple[str, ...]:
    prefixed = len(run.per_gauge) > 1
    out: list[str] = []
    for gauge, charge in run.per_gauge:
        for flagged, legend in ((charge.below, LEGEND_BELOW),
                                (charge.unread, LEGEND_UNREAD)):
            if flagged:
                out.append(f"{gauge}: {legend}" if prefixed else legend)
    return tuple(out)


@dataclass(frozen=True)
class QuotaColumn:
    """Some gauge read a week of the run, so the share replaces the dollar column."""

    run: Charge

    def cell(self, charge: Charge) -> str:
        """A row spanning two gauges has no single week's gauge to report."""
        if len(charge.per_gauge) != 1:
            return NO_FIGURE
        points = charge.per_gauge[0][1].points
        return NO_FIGURE if points is None else share_cell(points)

    def total(self, charge: Charge) -> str:
        if len(charge.per_gauge) != 1:
            return NO_FIGURE
        return _gauge_total(charge.per_gauge[0][1])

    @property
    def breakdown(self) -> tuple[tuple[str, str], ...]:
        """(gauge, total) beneath a mixed run's total. One gauge's total is the
        run's own, which the total row already reports."""
        if len(self.run.per_gauge) < 2:
            return ()
        return tuple((gauge, _gauge_total(charge)) for gauge, charge in self.run.per_gauge)

    @property
    def legends(self) -> tuple[str, ...]:
        return _legends(self.run)


@dataclass(frozen=True)
class QuotaFallback:
    """No gauge read any week of the run: the `cost` column stays as it is
    today, with a dim legend line naming why."""

    legends: tuple[str, ...]


def quota_view(run: Charge) -> QuotaColumn | QuotaFallback:
    """A run whose rows drew on no gauge at all has no week to read either."""
    if any(c.points is not None or c.below for _, c in run.per_gauge):
        return QuotaColumn(run)
    return QuotaFallback(_legends(run) or (LEGEND_UNREAD,))
