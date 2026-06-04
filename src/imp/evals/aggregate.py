"""Aggregate per-repeat grades into a CellResult (spec Metrics §). Blocking
recall is POOLED across repeats (total catches / total relevant) with a Wilson
score interval — it widens correctly at small n, so a 2/3 cell reads as the
uncertain thing it is. Precision is pooled the same way over curated findings.
`low_n` flags cells below the repeat floor; the scorecard refuses rankings on
overlapping CIs (Task 28).

Caveat: pooled Wilson treats each repeat x finding as i.i.d., so the interval is
optimistically NARROW when trials are correlated (multiple findings in one review,
repeats of one fixture). For cross-fixture verdicts use `macro_average` (the honest
between-fixture statistic); the pooled Wilson interval is a secondary, optimistic
bound."""

from __future__ import annotations

import math
from collections.abc import Sequence

from imp.evals.models import BLOCKING_SEVERITIES, CellResult, GradeResult

_Z = 1.96  # 95%


def wilson_ci(successes: int, trials: int, z: float = _Z) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion. trials==0 -> (0,1)."""
    if trials <= 0:
        return (0.0, 1.0)
    p = successes / trials
    denom = 1 + z * z / trials
    center = (p + z * z / (2 * trials)) / denom
    half = (z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials))) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def _pooled_blocking(grades: Sequence[GradeResult]) -> tuple[int, int]:
    """(catches, relevant) for must_catch blocking findings across all repeats."""
    catches = relevant = 0
    for g in grades:
        for m in g.matches:
            if m.expected.must_catch and m.expected.severity in BLOCKING_SEVERITIES:
                relevant += 1
                if m.caught:
                    catches += 1
    return catches, relevant


def _pooled_nit(grades: Sequence[GradeResult]) -> tuple[int, int]:
    """(catches, relevant) over must_catch NON-blocking (nit-level) findings."""
    catches = relevant = 0
    for g in grades:
        for m in g.matches:
            if m.expected.must_catch and m.expected.severity not in BLOCKING_SEVERITIES:
                relevant += 1
                catches += 1 if m.caught else 0
    return catches, relevant


def _pooled_precision(grades: Sequence[GradeResult]) -> tuple[int, int] | None:
    """(true_pos, true_pos+false_pos) — BOTH counted as ReviewFindings (flags that
    hit a known locus vs flags outside one), the same unit, so the ratio is a real
    precision. None if no flags were produced (precision undefined)."""
    tp = fp = 0
    for g in grades:
        tp += len(g.true_positive_flags)
        fp += len(g.false_positives)
    total = tp + fp
    return (tp, total) if total > 0 else None


def aggregate_cell(
    variant_id: str,
    fixture_id: str,
    grades: Sequence[GradeResult],
    *,
    dollars: Sequence[float],
    low_n_floor: int = 3,
) -> CellResult:
    n = len(grades)
    bc, br = _pooled_blocking(grades)
    blocking_mean = (bc / br) if br else None
    blocking_ci = wilson_ci(bc, br) if br else None

    nc, nr = _pooled_nit(grades)
    nit_mean = (nc / nr) if nr else None

    prec = _pooled_precision(grades)
    precision_mean = (prec[0] / prec[1]) if prec else None
    precision_ci = wilson_ci(prec[0], prec[1]) if prec else None

    dollars_mean = (sum(dollars) / len(dollars)) if dollars else 0.0
    return CellResult(
        variant_id=variant_id,
        fixture_id=fixture_id,
        n=n,
        blocking_recall_mean=blocking_mean,
        blocking_recall_ci=blocking_ci,
        precision_mean=precision_mean,
        precision_ci=precision_ci,
        dollars_mean=dollars_mean,
        low_n=n < low_n_floor,
        nit_recall_mean=nit_mean,
    )


def macro_average(
    per_fixture_means: list[float | None],
) -> tuple[float | None, tuple[float, float] | None]:
    """Mean-of-per-fixture-means with a between-fixture CI (normal approx). Use
    this for CROSS-FIXTURE verdicts (Q2): pooling raw catches lets a bug-dense
    fixture dominate and can rank variants opposite to their per-fixture record
    (Simpson's paradox). Equal weight per fixture + a CI driven by between-fixture
    spread is the honest cross-fixture statistic. None CI when < 2 fixtures."""
    import statistics

    vals = [m for m in per_fixture_means if m is not None]
    if not vals:
        return None, None
    mean = sum(vals) / len(vals)
    if len(vals) < 2:
        return mean, None
    se = statistics.stdev(vals) / (len(vals) ** 0.5)
    return mean, (max(0.0, mean - 1.96 * se), min(1.0, mean + 1.96 * se))
