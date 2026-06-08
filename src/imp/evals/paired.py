"""Paired-run capability for Q1 (spec v1 deliverable #1). Run all three reviewers
over the SAME materialized diff (one worktree, fresh runs) so overlap is measured
on paired data — mined episodes are unpaired and cannot answer this. The overlap
metric is `spec-task-reviewer`'s UNIQUE BLOCKING findings (the other two miss),
because the verdict hinges on blocking recall, not nit overlap (spec Decision
rules §)."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from pathlib import Path

from imp.evals.cache import load_run, store_run
from imp.evals.matching import match_kind
from imp.evals.models import Fixture, ReviewFinding, RunResult
from imp.evals.provenance import imp_commit_sha
from imp.evals.runner import drive_review
from imp.evals.tree import materialize_fixture_tree, teardown_fixture_tree


def run_paired(
    control_root: Path,
    fixture: Fixture,
    model_map: dict[str, tuple[str, str]],
    *,
    repeat_index: int = 0,
    cache_dir: Path | None = None,
    provenance: str = "",
) -> dict[str, RunResult]:
    """Run each reviewer in `model_map` over ONE shared worktree. Returns
    agent_name -> RunResult.

    When `cache_dir` is given, each reviewer run is cached as it completes (keyed
    `paired:{agent}` under `provenance`): a crash mid-paired loses at most one
    in-flight call (this is the dominant, otherwise-uncached Stage 2 cost) and a
    re-run resumes; a FULLY cached fixture skips the (expensive) git worktree
    entirely. A per-call `uuid` token isolates the worktree from concurrent
    same-fixture cells. Failed (empty/rate-limited) runs are never cached."""
    results: dict[str, RunResult] = {}
    pending: dict[str, tuple[str, str]] = {}
    for agent_name, pm in model_map.items():
        cached = (
            load_run(cache_dir, f"paired:{agent_name}", fixture.id, repeat_index, provenance)
            if cache_dir is not None
            else None
        )
        if cached is not None and not cached.failed:
            results[agent_name] = cached
        else:
            pending[agent_name] = pm
    if not pending:
        return results  # fully cached -> skip the worktree

    token = uuid.uuid4().hex[:8]
    sha = imp_commit_sha()
    worktree = materialize_fixture_tree(control_root, fixture, unique=token)
    try:
        for agent_name, (provider, model) in pending.items():
            rr = drive_review(
                worktree,
                fixture,
                agent_name,
                provider,
                model,
                repeat_index=repeat_index,
                variant_id=f"paired:{agent_name}",
            )
            results[agent_name] = rr
            if cache_dir is not None and not rr.failed:
                store_run(cache_dir, rr, provenance=provenance, imp_commit_sha=sha)
    finally:
        teardown_fixture_tree(control_root, fixture, unique=token)
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
