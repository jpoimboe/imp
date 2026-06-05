"""Plan + execute the run matrix (spec Cost & reproducibility §). plan_sweep is
the --dry-run path: it reports the matrix and an estimated dollar cost WITHOUT
calling any model. run_sweep is gated behind the opt-in env var, runs FIXTURES
concurrently (each has its own worktree), caches every (variant, fixture, repeat)
run, and grades each cell."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from imp.evals.cache import load_run, store_run
from imp.evals.grader import grade
from imp.evals.models import Fixture, GradeResult, Telemetry, Variant
from imp.evals.pricing import dollars_for
from imp.evals.provenance import imp_commit_sha
from imp.evals.structures import run_variant

EVAL_OPTIN_ENV = "IMP_RUN_EVALS"
# Rough per-agent-run token assumption for the dry-run ESTIMATE only (real runs
# record true telemetry). Tune from observed runs.
_EST_INPUT_TOKENS = 40_000
_EST_OUTPUT_TOKENS = 2_000


class SweepError(RuntimeError):
    """Raised when a billable sweep is attempted without the opt-in gate."""


_EST_JUDGE_INPUT_TOKENS = 4_000   # one judge call per expected finding (small)
_EST_JUDGE_OUTPUT_TOKENS = 200


@dataclass(frozen=True)
class SweepPlan:
    n_cells: int
    n_agent_runs: int
    n_judge_calls: int
    estimated_dollars: float


def plan_sweep(
    variants: list[Variant],
    fixtures: list[Fixture],
    *,
    judge_provider: str = "anthropic",
    judge_model: str = "claude-opus-4-8",
) -> SweepPlan:
    """Dry-run: matrix size + dollar estimate (reviewer runs AND judge calls), no
    model calls. The judge fires once per expected finding per cell — omitting it
    understated cost materially."""
    n_cells = 0
    n_agent_runs = 0
    n_judge_calls = 0
    est = 0.0
    rev_telem = Telemetry(_EST_INPUT_TOKENS, _EST_OUTPUT_TOKENS, 0, 0, 0, 0.0)
    judge_telem = Telemetry(_EST_JUDGE_INPUT_TOKENS, _EST_JUDGE_OUTPUT_TOKENS, 0, 0, 0, 0.0)

    def _add(telem: Telemetry, provider: str, model: str) -> float:
        try:
            return dollars_for(telem, provider, model)
        except KeyError:
            return 0.0  # unpriced model: estimate omits it (real run still records $)

    for v in variants:
        for fx in fixtures:
            for _r in range(v.n_repeats):
                n_cells += 1
                for _agent, (provider, model) in v.role_to_model_map.items():
                    n_agent_runs += 1
                    est += _add(rev_telem, provider, model)
                n_judge_calls += len(fx.expected_findings)
                est += len(fx.expected_findings) * _add(judge_telem, judge_provider, judge_model)
    return SweepPlan(
        n_cells=n_cells,
        n_agent_runs=n_agent_runs,
        n_judge_calls=n_judge_calls,
        estimated_dollars=est,
    )


def require_optin() -> None:
    if os.environ.get(EVAL_OPTIN_ENV) != "1":
        raise SweepError(
            f"refusing to run a billable sweep; set {EVAL_OPTIN_ENV}=1 to opt in"
        )


def _run_fixture_cells(
    control_root: Path,
    variants: list[Variant],
    fixture: Fixture,
    cache_dir: Path,
    judge_provider: str,
    judge_model: str,
    provenance: str,
    sha: str,
) -> list[GradeResult]:
    """All cells for ONE fixture, run sequentially (shared worktree slug)."""
    grades: list[GradeResult] = []
    for v in variants:
        for repeat in range(v.n_repeats):
            rr = run_variant(control_root, v, fixture, repeat_index=repeat)
            store_run(cache_dir, rr, provenance, imp_commit_sha=sha)
            g = grade(
                fixture,
                rr,
                project_root=control_root,
                judge_provider=judge_provider,
                judge_model=judge_model,
            )
            grades.append(g)
    return grades


def run_sweep(
    control_root: Path,
    variants: list[Variant],
    fixtures: list[Fixture],
    *,
    cache_dir: Path,
    judge_provider: str,
    judge_model: str,
    concurrency: int = 4,
    provenance: str = "",
) -> list[GradeResult]:
    """Gated, fixture-concurrent sweep. Returns one GradeResult per
    (variant, fixture, repeat). `provenance` (a digest of imp SHA + prompts +
    models) is folded into every cache key so a re-render after a change misses
    stale cells (cache.py)."""
    require_optin()
    sha = imp_commit_sha()  # captured ONCE — not per cell (avoids a spurious mixed set)
    from imp.evals.grader import require_oracle_independence

    reviewer_pairs = list({pm for v in variants for pm in v.role_to_model_map.values()})
    for warning in require_oracle_independence(judge_provider, judge_model, reviewer_pairs):
        print(f"warning: {warning}")
    grades: list[GradeResult] = []
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = [
            pool.submit(
                _run_fixture_cells,
                control_root,
                variants,
                fx,
                cache_dir,
                judge_provider,
                judge_model,
                provenance,
                sha,
            )
            for fx in fixtures
        ]
        for fut in futures:
            grades.extend(fut.result())
    return grades


def collect_dollars(
    cache_dir: Path, variants: list[Variant], fixtures: list[Fixture], provenance: str
) -> dict[tuple[str, str], list[float]]:
    """Build {(variant_id, fixture_id): [dollars per repeat]} from cached runs —
    for the scorecard's Q3 axis. `provenance` is REQUIRED (no default) and MUST
    match what run_sweep stored under, or every lookup misses and Q3 reads
    $0.0000 (M4)."""
    out: dict[tuple[str, str], list[float]] = {}
    for v in variants:
        for fx in fixtures:
            vals: list[float] = []
            for r in range(v.n_repeats):
                rr = load_run(cache_dir, v.id, fx.id, r, provenance)
                if rr is not None:
                    vals.append(rr.dollars)
            if vals:
                out[(v.id, fx.id)] = vals
    return out
