"""The oracle. score_recall asks the judge agent, per expected finding, whether
the review surfaced it (spec Grader §). The judge runs through the same
drive_agent seam as the reviewers, so oracle-independence is just a different
(provider, model) bind. Precision is added in Task 12."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

from imp.engine.loader import parse_agent_file
from imp.evals.driver import drive_agent_sync
from imp.evals.matching import dedup_findings, flags_known_locus
from imp.evals.models import (
    ExpectedFinding,
    FindingMatch,
    Fixture,
    GradeResult,
    ReviewFinding,
    RunResult,
)


def _judge_agent_path() -> Path:
    return Path(str(files("imp"))) / "evals" / "data" / "agents" / "grader-judge.md"


class OracleIndependenceError(RuntimeError):
    """The judge shares a model with a reviewer under test (spec: don't judge
    gpt-5.5 reviews with a gpt-5.5 judge). The most damaging silent bias, one CLI
    typo away — so it's a hard failure, not prose."""


def require_oracle_independence(
    judge_provider: str,
    judge_model: str,
    reviewer_pairs: list[tuple[str, str]],
) -> list[str]:
    """Raise if the judge's (provider, model) EXACTLY matches any reviewer's.
    Return soft warnings for same-vendor-different-model pairs (e.g. opus judging
    sonnet) — allowed, but worth surfacing. A strict family-level ban would
    forbid that reasonable setup, so the HARD rule is exact-model identity."""
    judge = (judge_provider, judge_model)
    if judge in reviewer_pairs:
        raise OracleIndependenceError(
            f"judge {judge} is also a reviewer under test; pick a different judge model"
        )
    return [
        f"judge {judge} shares vendor {judge_provider} with reviewer {rp} — "
        "cross-vendor judging would be more independent"
        for rp in reviewer_pairs
        if rp[0] == judge_provider
    ]


def _judge_one(
    expected: ExpectedFinding,
    review: str,
    *,
    project_root: Path,
    judge_provider: str,
    judge_model: str,
) -> str:
    agent_def = parse_agent_file(_judge_agent_path())
    payload = json.dumps(
        {
            "expected": {
                "symbol": expected.symbol,
                "locus": expected.locus,
                "description": expected.description,
                "severity": expected.severity,
            },
            "review": review,
        }
    )
    output, _telem = drive_agent_sync(
        agent_def=agent_def,
        provider_name=judge_provider,
        model_name=judge_model,
        project_root=project_root,
        opening_message=payload,
        agent_id=f"eval:judge:{expected.symbol}:{expected.locus}",
    )
    if not output:
        return "none"
    try:
        verdict = json.loads(output.get("verdicts", "{}"))
    except (json.JSONDecodeError, TypeError):
        # TypeError guards a non-string `verdicts` (None/dict/int) sneaking past
        # the output schema — default to "none" rather than crash the scoring loop.
        return "none"
    kind = verdict.get("match_kind", "none")
    return kind if kind in ("exact", "partial", "none") else "none"


def score_recall(
    fixture: Fixture,
    run_result: RunResult,
    *,
    project_root: Path,
    judge_provider: str,
    judge_model: str,
) -> tuple[FindingMatch, ...]:
    """For each expected finding, ask the judge whether the review surfaced it."""
    matches: list[FindingMatch] = []
    for ef in fixture.expected_findings:
        kind = _judge_one(
            ef,
            run_result.raw_review,
            project_root=project_root,
            judge_provider=judge_provider,
            judge_model=judge_model,
        )
        rf = _best_review_finding(ef, run_result.findings)
        matches.append(FindingMatch(expected=ef, match_kind=kind, review_finding=rf))
    return tuple(matches)


def _best_review_finding(
    ef: ExpectedFinding, findings: tuple[ReviewFinding, ...]
) -> ReviewFinding | None:
    """Pair an expected finding with the review finding that best matches its
    LOCUS, not just its symbol (M8: two curated bugs share symbol='decide', so a
    symbol-only pairing attaches the same finding to both). exact > partial; no
    match -> None."""
    from imp.evals.matching import match_kind

    rank = {"exact": 0, "partial": 1, "none": 2}
    best: ReviewFinding | None = None
    best_rank = 2
    for f in findings:
        r = rank[match_kind(ef.symbol, ef.locus, f.symbol, f.locus)]
        if r < best_rank:
            best, best_rank = f, r
    return best if best_rank < 2 else None


def grade(
    fixture: Fixture,
    run_result: RunResult,
    *,
    project_root: Path,
    judge_provider: str,
    judge_model: str,
) -> GradeResult:
    """Full grade: recall (judge) + precision classification. A review finding
    that hits a labeled bug is a true positive. Out-of-locus flags are FALSE
    POSITIVES on curated fixtures (hand-verified) but only directional
    INSPECT-FLAGS on mined fixtures (merged-clean is not trustworthy)."""
    matches = score_recall(
        fixture,
        run_result,
        project_root=project_root,
        judge_provider=judge_provider,
        judge_model=judge_model,
    )
    # M1: recall is judged by the LLM; precision must AGREE with it. A review
    # finding the judge confirmed as a catch is a true positive even if the
    # deterministic locus match missed it — otherwise the same finding is credited
    # for recall AND penalized as an FP (double penalty). So a judge-caught review
    # finding suppresses its FP and counts as a TP flag.
    judge_caught_rfs = {
        m.review_finding for m in matches if m.caught and m.review_finding is not None
    }
    false_positives: list[ReviewFinding] = []
    inspect_flags: list[ReviewFinding] = []
    true_positive_flags: list[ReviewFinding] = []
    for rf in dedup_findings(run_result.findings):
        if flags_known_locus(rf, fixture.expected_findings) or rf in judge_caught_rfs:
            true_positive_flags.append(rf)  # hit a labeled bug — precision numerator
        elif fixture.source == "curated":
            false_positives.append(rf)
        else:  # mined merged-clean: directional inspect signal only
            inspect_flags.append(rf)
    return GradeResult(
        variant_id=run_result.variant_id,
        fixture_id=fixture.id,
        repeat_index=run_result.repeat_index,
        matches=matches,
        false_positives=tuple(false_positives),
        inspect_flags=tuple(inspect_flags),
        true_positive_flags=tuple(true_positive_flags),
    )
