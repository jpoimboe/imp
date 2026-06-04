"""Pre-registered decision rules (spec Decision rules §). PRE-REGISTERED means:
set HERE, in code, before any sweep — not chosen after seeing data. The specific
numbers are debatable and may be revised in THIS file before a run; what is
non-negotiable is that they are fixed in advance. Verdicts hinge on
must_catch-stratified BLOCKING recall, never nit overlap."""

from __future__ import annotations

import math

# -- pre-registered constants --
LOW_N_FLOOR = 3  # n_repeats floor; contested cells escalate
Q1_UNIQUE_BLOCKING_THRESHOLD = 0.05  # drop spec-task-reviewer if unique blocking < 5%
Q1_UNIQUE_BLOCKING_UPPER_CI = 0.10  # ...AND upper CI bound < 10%
CROSS_CUTTING_FLOOR = 2  # Q2 needs >= 2 independent cross-cutting fixtures
Q4_FASTPATH_MARGIN = 0.05  # fast-path may trail the union by at most this
MIN_DIFF_CI_HALFWIDTH_TARGET = 0.10  # Q1/Q4 minimum-diff gate
_Z = 1.96


# CI provenance (M2): the cross-fixture VERDICT (q2_verdict) must be fed the
# between-fixture `macro_average` CI (scorecard passes it), NOT the per-cell
# pooled Wilson CI — pooling correlated repeats/findings makes the pooled interval
# too narrow (Task 13 caveat). Q1 uses a Wilson CI on the PER-DIFF proportion,
# where each diff is one independent trial, so pooling is correct THERE. Per-cell
# pooled CIs are reporting-only (Q3 scatter), never a verdict gate.


def cis_separated(a: tuple[float, float], b: tuple[float, float]) -> bool:
    """True if the two CIs do not overlap."""
    return a[1] < b[0] or b[1] < a[0]


def rank(name_a: str, ci_a: tuple[float, float], name_b: str, ci_b: tuple[float, float]) -> str:
    """Return the winner's name only if CIs separate; else 'indistinguishable'
    (spec: a ranking is reported only when it survives the noise)."""
    if not cis_separated(ci_a, ci_b):
        return "indistinguishable"
    return name_a if ci_a[0] > ci_b[1] else name_b


def q1_verdict(*, unique_mean: float, unique_ci: tuple[float, float]) -> str:
    """drop only if unique blocking-recall contribution is below threshold AND its
    upper CI bound is also below the upper bound."""
    if unique_mean < Q1_UNIQUE_BLOCKING_THRESHOLD and unique_ci[1] < Q1_UNIQUE_BLOCKING_UPPER_CI:
        return "drop"
    return "keep"


def q2_verdict(
    *,
    n_cross_cutting: int,
    completeness_ci: tuple[float, float],
    monolithic_ci: tuple[float, float],
) -> str:
    """endorse the completeness slice only if the cross-cutting floor is met AND it
    strictly improves cross-cutting blocking recall with separated CIs; one fixture
    -> down-ranked, not a verdict."""
    if n_cross_cutting < CROSS_CUTTING_FLOOR:
        return "down-ranked"
    if not cis_separated(completeness_ci, monolithic_ci):
        return "indistinguishable"
    return "endorse" if completeness_ci[0] > monolithic_ci[1] else "no-improvement"


def min_diff_count(target_halfwidth: float = MIN_DIFF_CI_HALFWIDTH_TARGET) -> int:
    """Smallest N whose worst-case (p=0.5) Wilson half-width falls below target.
    Derived, not magic (spec)."""
    n = 1
    while n < 100000:
        p = 0.5
        denom = 1 + _Z * _Z / n
        half = (_Z * math.sqrt(p * (1 - p) / n + _Z * _Z / (4 * n * n))) / denom
        if half < target_halfwidth:
            return n
        n += 1
    return n


def q4_verdict(*, fastpath_recall: float, union_recall: float, n_diffs: int) -> str:
    """replace per-task review with a single change-level review only if its
    blocking recall is >= the union within the margin AND enough diffs exist."""
    if n_diffs < min_diff_count():
        return "insufficient-diffs"
    if fastpath_recall >= union_recall - Q4_FASTPATH_MARGIN:
        return "replace"
    return "keep-per-task"
