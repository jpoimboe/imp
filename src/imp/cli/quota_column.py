# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""The quota column's render-facing projection: one cell per row and for the
total, the per-provider breakdown a mixed run needs, and the verbatim legend a
withheld gauge carries.

Two states, and no third: the column (some gauge this run drew on resolved) and
the `cost` fallback with one legend line per gauge."""

from __future__ import annotations

from dataclasses import dataclass

from imp.cli.quota_share import (
    QuotaAttribution,
    QuotaFigure,
    QuotaWithheld,
    Units,
    Withheld,
)

FOOTNOTE = ("* estimated to ±1 point of the weekly gauge; usage outside imp "
            "during the run skews it high")
NO_FIGURE = "—"

_WITHHELD: dict[Withheld, str] = {
    Withheld.NO_READINGS: "quota unavailable; this run recorded no quota readings",
    Withheld.WINDOW_RESET: ("quota unmeasurable; the weekly window reset during "
                            "this run"),
    Withheld.BELOW_RESOLUTION: ("quota below measurement resolution; the weekly "
                                "gauge did not move during this run"),
    Withheld.NO_TRACKED_USAGE: ("quota unattributable; no tracked usage in this "
                                "run's window"),
}


def share_cell(points: float) -> str:
    return "<0.01%" if points < 0.005 else f"{points:.2f}%"


@dataclass(frozen=True)
class QuotaColumn:
    """At least one gauge resolved, so the share replaces the dollar column."""

    gauges: tuple[QuotaFigure | QuotaWithheld, ...]

    def cell(self, units: Units) -> str:
        """One row's share, and the total's over every row's units. A subtree
        spanning two gauges has no single window to report, and one whose gauge
        was withheld has no figure to report."""
        providers = units.providers
        if len(providers) != 1:
            return NO_FIGURE
        figure = next((g for g in self.gauges if isinstance(g, QuotaFigure)
                       and g.provider == providers[0]), None)
        return NO_FIGURE if figure is None else share_cell(figure.scale(units))

    @property
    def breakdown(self) -> tuple[tuple[str, str], ...]:
        """(provider, cell) per gauge, beneath the total of a mixed run. One
        gauge is one window, which the total already reports."""
        if len(self.gauges) == 1:
            return ()
        return tuple((g.provider, share_cell(g.percent)
                      if isinstance(g, QuotaFigure) else NO_FIGURE)
                     for g in self.gauges)

    @property
    def legends(self) -> tuple[str, ...]:
        return tuple(f"{g.provider}: {_WITHHELD[g.reason]}" for g in self.gauges
                     if isinstance(g, QuotaWithheld))


@dataclass(frozen=True)
class QuotaFallback:
    """No gauge yielded a figure: the `cost` column stays as it is today, with
    one dim legend line naming why."""

    legends: tuple[str, ...]


def quota_view(attribution: QuotaAttribution, *,
               providers: tuple[str, ...]) -> QuotaColumn | QuotaFallback:
    """Only the gauges this run's own steps drew on are its own: the weekly
    window is account-global, so a neighbour sampling it reports providers this
    run never touched, and a run is mixed by what it ran, not by what it
    overlapped. A provider the run touched keeps its gauge even at a zero draw."""
    gauges = tuple(g for g in attribution.gauges if g.provider in providers)
    if any(isinstance(g, QuotaFigure) for g in gauges):
        return QuotaColumn(gauges)
    withheld = [g for g in gauges if isinstance(g, QuotaWithheld)]
    if not withheld:
        # Nothing survived the filter, so this run carried no units at all:
        # unattributable if a gauge was read, unread if there was no gauge.
        reason = (Withheld.NO_READINGS if not attribution.gauges
                  else Withheld.NO_TRACKED_USAGE)
        return QuotaFallback((_WITHHELD[reason],))
    if len(withheld) == 1:
        return QuotaFallback((_WITHHELD[withheld[0].reason],))
    return QuotaFallback(tuple(f"{g.provider}: {_WITHHELD[g.reason]}"
                               for g in withheld))
