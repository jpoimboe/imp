"""Two-hop human calibration of the oracle (spec Grader §, 'Calibration is
mandatory — on two hops'). Hop 1 (judge): does the judge agree with a human on
whether each review caught each labeled finding? Hop 2 (normalization): does the
normalizer recover the human's structured labels from raw prose? Judge agreement
is reported PER OUTPUT-STYLE so a style-correlated bias can't hide in a pooled
number. Each hop needs enough items to report an agreement rate with a CI
(CALIBRATION_FLOOR; n=2 is not acceptable)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from imp.evals.aggregate import wilson_ci
from imp.evals.grader import _judge_one
from imp.evals.matching import match_kind
from imp.evals.models import ExpectedFinding

CALIBRATION_FLOOR = 20  # spec target >=20-30 per hop


@dataclass(frozen=True)
class CalibrationItem:
    """One human-labeled judge-hop item. `human_caught` is the ground-truth call:
    did THIS review surface THIS finding?"""

    output_style: str  # e.g. "anthropic", "openai", "panel" — the reviewer style
    expected_symbol: str
    expected_locus: str
    expected_description: str
    expected_severity: str
    review_prose: str
    human_caught: bool


PER_STYLE_FLOOR = 10  # per-style minimum; even this is modest (+/-~0.3 CI) — a true
#                       style-bias check wants more. 5 (the naive choice) is far
#                       too small to detect the bias it targets.


@dataclass(frozen=True)
class Agreement:
    n: int
    agreement: float  # raw observed agreement
    ci: tuple[float, float]
    kappa: float | None  # chance-corrected; raw agreement is misleading on imbalanced labels


@dataclass(frozen=True)
class JudgeHopReport:
    pooled: Agreement
    by_style: dict[str, Agreement]
    floor_met: bool


def calibration_floor_met(*, n: int, floor: int = CALIBRATION_FLOOR) -> bool:
    return n >= floor


def cohen_kappa(pairs: Sequence[tuple[bool, bool]]) -> float | None:
    """Chance-corrected agreement over (judge_caught, human_caught) pairs. On
    imbalanced labels (most findings ARE caught), a degenerate always-'caught'
    judge scores ~base-rate raw agreement and passes the CI floor; kappa exposes it
    (kappa~0). None when no pairs."""
    n = len(pairs)
    if n == 0:
        return None
    po = sum(1 for j, h in pairs if j == h) / n
    pj = sum(1 for j, _ in pairs if j) / n
    ph = sum(1 for _, h in pairs if h) / n
    pe = pj * ph + (1 - pj) * (1 - ph)
    if pe >= 1.0:
        return 1.0 if po >= 1.0 else 0.0
    return (po - pe) / (1 - pe)


def _agreement(pairs: Sequence[tuple[bool, bool]]) -> Agreement:
    n = len(pairs)
    hits = sum(1 for j, h in pairs if j == h)
    return Agreement(
        n=n,
        agreement=(hits / n if n else 0.0),
        ci=wilson_ci(hits, n),
        kappa=cohen_kappa(pairs),
    )


def calibrate_judge_hop(
    items: Sequence[CalibrationItem],
    *,
    project_root: Path,
    judge_provider: str,
    judge_model: str,
) -> JudgeHopReport:
    pooled_pairs: list[tuple[bool, bool]] = []
    by_style_pairs: dict[str, list[tuple[bool, bool]]] = defaultdict(list)
    for it in items:
        ef = ExpectedFinding(
            locus=it.expected_locus,
            symbol=it.expected_symbol,
            description=it.expected_description,
            severity=it.expected_severity,
            must_catch=True,
        )
        kind = _judge_one(
            ef,
            it.review_prose,
            project_root=project_root,
            judge_provider=judge_provider,
            judge_model=judge_model,
        )
        pair = (kind in ("exact", "partial"), it.human_caught)
        pooled_pairs.append(pair)
        by_style_pairs[it.output_style].append(pair)
    by_style = {style: _agreement(pairs) for style, pairs in by_style_pairs.items()}
    return JudgeHopReport(
        pooled=_agreement(pooled_pairs),
        by_style=by_style,
        floor_met=calibration_floor_met(n=len(items))
        and all(calibration_floor_met(n=s.n, floor=PER_STYLE_FLOOR) for s in by_style.values()),
    )


@dataclass(frozen=True)
class NormalizationItem:
    """One human-labeled normalization-hop item: raw prose + the findings a human
    extracted from it."""

    output_style: str
    review_prose: str
    human_findings: tuple[ExpectedFinding, ...]


def normalization_agreement(
    human: Sequence[ExpectedFinding], machine: Sequence[ExpectedFinding]
) -> float:
    """F1-style agreement: fraction of human findings the normalizer recovered AND
    fraction of normalizer findings that are real, harmonic-meaned. Uses the
    shared symbol-first match so it agrees with the grader's notion of 'same'."""
    if not human and not machine:
        return 1.0
    if not human or not machine:
        return 0.0

    def matched(a: ExpectedFinding, bs: Sequence[ExpectedFinding]) -> bool:
        return any(match_kind(a.symbol, a.locus, b.symbol, b.locus) != "none" for b in bs)

    recall = sum(1 for h in human if matched(h, machine)) / len(human)
    precision = sum(1 for m in machine if matched(m, human)) / len(machine)
    if recall + precision == 0:
        return 0.0
    return 2 * recall * precision / (recall + precision)
