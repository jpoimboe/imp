"""Paired-run capability for Q1 (spec v1 deliverable #1). Run all three reviewers
over the SAME materialized diff (one worktree, fresh runs) so overlap is measured
on paired data — mined episodes are unpaired and cannot answer this. The overlap
metric is `spec-task-reviewer`'s UNIQUE BLOCKING findings (the other two miss),
because the verdict hinges on blocking recall, not nit overlap (spec Decision
rules §)."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from imp.evals.matching import match_kind
from imp.evals.models import Fixture, ReviewFinding, RunResult
from imp.evals.runner import drive_review
from imp.evals.tree import materialize_fixture_tree, teardown_fixture_tree


def run_paired(
    control_root: Path,
    fixture: Fixture,
    model_map: dict[str, tuple[str, str]],
    *,
    repeat_index: int = 0,
) -> dict[str, RunResult]:
    """Run each reviewer in `model_map` over ONE shared worktree. Returns
    agent_name -> RunResult."""
    worktree = materialize_fixture_tree(control_root, fixture)
    results: dict[str, RunResult] = {}
    try:
        for agent_name, (provider, model) in model_map.items():
            results[agent_name] = drive_review(
                worktree,
                fixture,
                agent_name,
                provider,
                model,
                repeat_index=repeat_index,
                variant_id=f"paired:{agent_name}",
            )
    finally:
        teardown_fixture_tree(control_root, fixture)
    return results


def unique_blocking_fraction(
    target: Iterable[ReviewFinding], others: Iterable[ReviewFinding]
) -> float:
    """Fraction of `target`'s BLOCKING findings not matched by any finding in
    `others` (the shared (symbol, locus) match). Denominator is target's blocking
    count; 0 blocking findings -> 0.0 (nothing unique to contribute)."""
    blocking_others = [o for o in others if o.blocking]  # only blocking covers (Q1 bias fix)
    blocking = [f for f in target if f.blocking]
    if not blocking:
        return 0.0
    unique = [
        f
        for f in blocking
        if not any(
            match_kind(f.symbol, f.locus, o.symbol, o.locus) != "none"
            for o in blocking_others
        )
    ]
    return len(unique) / len(blocking)
