"""Q1 (overlap) and Q4 (fast-path) analysis. Both pool over fixtures, gate on the
minimum diff count (Task 28), and apply the pre-registered verdict rules. Q1 uses
spec-task-reviewer's UNIQUE BLOCKING contribution vs the union of the other two —
blocking recall, not nit overlap (spec Decision rules §)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from imp.evals.aggregate import wilson_ci
from imp.evals.decisions import min_diff_count, q1_verdict, q4_verdict
from imp.evals.matching import match_kind
from imp.evals.models import Fixture, GradeResult, RunResult


def is_self_graded(fixture: Fixture, candidate_role: str) -> bool:
    """True if scoring `candidate_role` against this fixture would grade the role
    against a NOISY COPY OF ITS OWN historical mined output (spec Component 2),
    which inflates that role's recall toward 'keep'. The `source == "mined"`
    guard is load-bearing: curated fixtures are blind-/independently-authored, so
    they are never self-graded even though all 7 carry reviewer_type=='code-reviewer'
    and Q4's candidate role is code-reviewer."""
    return fixture.source == "mined" and fixture.meta.reviewer_type == candidate_role


def compute_q1(paired_by_fixture: Mapping[str, Mapping[str, RunResult]]) -> dict[str, Any]:
    """paired_by_fixture[fixture_id][agent_name] -> RunResult (from run_paired).
    Pools spec-task-reviewer's unique-blocking fraction over all fixtures."""
    n_diffs = len(paired_by_fixture)
    unique = 0
    blocking_total = 0
    for res in paired_by_fixture.values():
        target = res.get("spec-task-reviewer")
        if target is None:
            continue
        # Only a BLOCKING finding from another reviewer counts as coverage — a
        # non-blocking duplicate doesn't provide the same gate (else Q1 is biased
        # toward "drop").
        others = [
            f
            for name, rr in res.items()
            if name != "spec-task-reviewer"
            for f in rr.findings
            if f.blocking
        ]
        blocking = [f for f in target.findings if f.blocking]
        blocking_total += len(blocking)
        for f in blocking:
            if not any(match_kind(f.symbol, f.locus, o.symbol, o.locus) != "none" for o in others):
                unique += 1

    mean = (unique / blocking_total) if blocking_total else 0.0
    ci = wilson_ci(unique, blocking_total) if blocking_total else (0.0, 1.0)
    if n_diffs < min_diff_count():
        verdict = "insufficient-diffs"
    else:
        verdict = q1_verdict(unique_mean=mean, unique_ci=ci)
    return {
        "verdict": verdict,
        "unique_blocking_mean": mean,
        "unique_blocking_ci": ci,
        "n_diffs": n_diffs,
        "min_diffs_required": min_diff_count(),
    }


def compute_q4(
    *, fast_c: int, fast_t: int, union_c: int, union_t: int, n_diffs: int
) -> dict[str, Any]:
    # NOTE (M3): unlike Q1 (Wilson CI on the per-diff unique-blocking proportion),
    # Q4 compares POOLED POINT ESTIMATES within a fixed margin, guarded only by the
    # minimum-diff-count gate — NOT a CI-separation test. This is intentional and
    # pre-registered. We surface the RAW caught/total counts (not just the ratio)
    # so a human can read 4/5-vs-5/5 as the noise it is on thin data — the
    # substitute for the CI Q4 deliberately does not compute (spec Component 3).
    fastpath_recall = (fast_c / fast_t) if fast_t else 0.0
    union_recall = (union_c / union_t) if union_t else 0.0
    return {
        "verdict": q4_verdict(
            fastpath_recall=fastpath_recall, union_recall=union_recall, n_diffs=n_diffs
        ),
        "fastpath_blocking_recall": fastpath_recall,
        "union_blocking_recall": union_recall,
        "fast_caught": fast_c,
        "fast_total": fast_t,
        "union_caught": union_c,
        "union_total": union_t,
        "n_diffs": n_diffs,
        "min_diffs_required": min_diff_count(),
    }


def _blocking_counts(grade_result: GradeResult) -> tuple[int, int]:
    """(catches, relevant) over must_catch blocking findings in one grade."""
    from imp.evals.models import BLOCKING_SEVERITIES

    catches = relevant = 0
    for m in grade_result.matches:
        if m.expected.must_catch and m.expected.severity in BLOCKING_SEVERITIES:
            relevant += 1
            catches += 1 if m.caught else 0
    return catches, relevant


def run_q1_q4(
    control_root: Path,
    fixtures: list[Fixture],
    model_map: dict[str, tuple[str, str]],
    *,
    judge_provider: str,
    judge_model: str,
    cache_dir: Path | None = None,
    provenance: str = "",
    concurrency: int = 1,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compute Q1 (overlap) and Q4 (fast-path) by running the THREE reviewers
    paired over each fixture and grading. On a small curated-only set both
    correctly return 'insufficient-diffs' (n_diffs << min_diff_count) — that is
    the honest answer the spec asks for, not a skipped computation.

    `cache_dir`/`provenance` are threaded into `run_paired` so each reviewer run
    is cached as it completes: a crash mid-paired loses at most one in-flight call
    (this is the dominant, otherwise-uncached Stage 2 cost), and a re-run resumes
    from cache. `provenance` MUST be model-scoped by the caller so the paired pool's
    models never collide on the (variant, fixture, repeat) key. `concurrency`>1
    fans the per-fixture work (reviewers + judge grading) across a thread pool; each
    fixture materializes its own uniquely-slugged worktree, so this is collision-safe.
    A fixture whose processing raises is dropped (contributes nothing) rather than
    failing the whole phase — its already-cached reviewers persist for the re-run."""
    from concurrent.futures import ThreadPoolExecutor

    from imp.evals.grader import grade
    from imp.evals.matching import dedup_findings
    from imp.evals.models import RunResult, Telemetry
    from imp.evals.paired import run_paired

    def _process(fx: Fixture) -> dict[str, Any] | None:
        try:
            res = run_paired(
                control_root, fx, model_map,
                cache_dir=cache_dir, provenance=provenance,
            )
            rec: dict[str, Any] = {
                "id": fx.id, "res": res,
                # Q1 pool: exclude a mined fixture authored by spec-task-reviewer (the
                # role Q1 evaluates dropping) — don't score it against its own output.
                "in_q1": not is_self_graded(fx, "spec-task-reviewer"),
                "in_q4": False, "fast_c": 0, "fast_t": 0, "union_c": 0, "union_t": 0,
            }
            # Q4 pool: exclude a mined fixture authored by code-reviewer (Q4's
            # fast-path role). Curated code-reviewer fixtures are NOT excluded.
            if not is_self_graded(fx, "code-reviewer"):
                rec["in_q4"] = True
                cr = res.get("code-reviewer")
                if cr is not None:
                    c, t = _blocking_counts(
                        grade(fx, cr, project_root=control_root,
                              judge_provider=judge_provider, judge_model=judge_model)
                    )
                    rec["fast_c"], rec["fast_t"] = c, t
                union = dedup_findings(
                    f
                    for name in ("spec-task-reviewer", "quality-reviewer")
                    if name in res
                    for f in res[name].findings
                )
                union_rr = RunResult("union", fx.id, 0, "", union, Telemetry.zero())
                c, t = _blocking_counts(
                    grade(fx, union_rr, project_root=control_root,
                          judge_provider=judge_provider, judge_model=judge_model)
                )
                rec["union_c"], rec["union_t"] = c, t
            return rec
        except Exception:
            return None

    if concurrency > 1:
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            records = list(ex.map(_process, fixtures))
    else:
        records = [_process(fx) for fx in fixtures]
    records = [r for r in records if r is not None]

    paired_by_fixture: dict[str, dict[str, RunResult]] = {
        r["id"]: r["res"] for r in records if r["in_q1"]
    }
    q1 = compute_q1(paired_by_fixture)
    q4 = compute_q4(
        fast_c=sum(r["fast_c"] for r in records),
        fast_t=sum(r["fast_t"] for r in records),
        union_c=sum(r["union_c"] for r in records),
        union_t=sum(r["union_t"] for r in records),
        n_diffs=sum(1 for r in records if r["in_q4"]),
    )
    return q1, q4
