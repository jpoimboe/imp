"""Assemble + render the four-question scorecard (spec v1 deliverable). Each
question is a calibrated verdict or 'insufficient diffs'; rankings are reported
only when CIs separate; Q2 is down-ranked below the cross-cutting floor; Q3 is
$-normalized; every scorecard carries a provenance stamp. The instrument NEVER
presents a single sample as a result — low-n cells are flagged."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from imp.evals.aggregate import aggregate_cell, macro_average
from imp.evals.decisions import CROSS_CUTTING_FLOOR, q2_verdict
from imp.evals.models import CellResult, Fixture, GradeResult
from imp.evals.provenance import stamp


@dataclass
class Scorecard:
    cells: list[CellResult]
    q1: dict[str, Any]
    q2: dict[str, Any]
    q3: dict[str, Any]
    q4: dict[str, Any]
    provenance: dict[str, Any]
    notes: list[str] = field(default_factory=list)
    runs_shas: set[str] = field(default_factory=set)


def _group(grades: Sequence[GradeResult]) -> dict[tuple[str, str], list[GradeResult]]:
    out: dict[tuple[str, str], list[GradeResult]] = defaultdict(list)
    for g in grades:
        out[(g.variant_id, g.fixture_id)].append(g)
    return out


def _oracle_independence_notes(provenance: dict[str, Any] | None) -> list[str]:
    """Flag any variant whose reviewer model basename equals the judge's basename
    across the claude_code/anthropic boundary (same underlying model, but the
    exact (provider, model) tuple differs so require_oracle_independence misses
    it). Decision S3: label, don't re-route."""
    if not provenance:
        return []
    judge = provenance.get("judge_model") or ""
    judge_base = judge.split("/")[-1]
    if not judge_base:
        return []
    notes: list[str] = []
    for vid, models in (provenance.get("cell_models") or {}).items():
        for pm in models:
            if pm.split("/")[-1] == judge_base and pm != judge:
                notes.append(
                    f"`{vid}` is NOT oracle-independent (reviewer {pm} shares the "
                    f"judge's underlying model {judge_base}); read it on the recall "
                    f"axis only."
                )
    return notes


def build_scorecard(
    *,
    grades: Sequence[GradeResult],
    fixtures: dict[str, Fixture],
    dollars_by_cell: dict[tuple[str, str], list[float]],
    prompt_dirs: dict[str, Path],
    provenance: dict[str, Any] | None = None,
    paired_overlap: dict[str, Any] | None = None,
    fastpath: dict[str, Any] | None = None,
    min_diffs_available: int = 0,
    runs_shas: set[str] | None = None,
) -> Scorecard:
    grouped = _group(grades)
    cells = [
        aggregate_cell(vid, fid, gs, dollars=dollars_by_cell.get((vid, fid), []))
        for (vid, fid), gs in grouped.items()
    ]
    notes: list[str] = []
    if any(c.low_n for c in cells):
        notes.append("Some cells are low-n (< floor); rankings on them are suppressed.")

    # Q2 (structure): completeness vs monolithic, MACRO-AVERAGED across the
    # cross-cutting fixtures (not pooled — avoids Simpson's paradox).
    cc_ids = {fid for fid, f in fixtures.items() if f.meta.stratum == "cross-cutting"}
    n_cc = len(cc_ids)

    def _macro(variant_substr: str) -> tuple[float | None, tuple[float, float] | None]:
        means = [
            c.blocking_recall_mean
            for c in cells
            if variant_substr in c.variant_id and c.fixture_id in cc_ids
        ]
        return macro_average(means)

    comp_mean, comp_ci = _macro("completeness")
    mono_mean, mono_ci = _macro("monolithic")
    if comp_ci and mono_ci:
        verdict = q2_verdict(
            n_cross_cutting=n_cc, completeness_ci=comp_ci, monolithic_ci=mono_ci
        )
    else:
        verdict = "down-ranked" if n_cc < CROSS_CUTTING_FLOOR else "insufficient-data"
    q2 = {
        "verdict": verdict,
        "n_cross_cutting": n_cc,
        "floor": CROSS_CUTTING_FLOOR,
        "completeness_macro_recall": comp_mean,
        "monolithic_macro_recall": mono_mean,
    }
    if n_cc < CROSS_CUTTING_FLOOR:
        notes.append(
            f"Q2 down-ranked: {n_cc} cross-cutting fixture(s) < floor {CROSS_CUTTING_FLOOR}."
        )

    # Q1 / Q4 are now PER-MODEL maps {model_id: result}. Empty == no paired runs
    # supplied (the curated-only sweep does not execute them; Stage 2 does).
    q1 = dict(paired_overlap or {})
    q4 = dict(fastpath or {})

    # Q3 ($-normalized): cheapest cell per blocking-recall point
    q3_rows = [
        {
            "variant": c.variant_id,
            "fixture": c.fixture_id,
            "blocking_recall": c.blocking_recall_mean,
            "blocking_recall_ci": c.blocking_recall_ci,
            "dollars": c.dollars_mean,
            "low_n": c.low_n,
        }
        for c in cells
    ]
    q3 = {"rows": q3_rows, "axis": "dollars-per-unit-detection"}

    notes.extend(_oracle_independence_notes(provenance))
    return Scorecard(
        cells=cells,
        q1=q1,
        q2=q2,
        q3=q3,
        q4=q4,
        provenance=provenance if provenance is not None else stamp(prompt_dirs=prompt_dirs),
        notes=notes,
        runs_shas=runs_shas or set(),
    )


def render_markdown(sc: Scorecard) -> str:
    lines = ["# imp review-eval scorecard", ""]
    lines.append("## Provenance")
    lines.append(f"- imp commit: `{sc.provenance.get('imp_commit_sha')}`")
    if sc.runs_shas:
        runs = ", ".join(sorted(sc.runs_shas))
        head = sc.provenance.get("imp_commit_sha")
        lines.append(f"- cached runs produced at: `{runs}` (rendered at HEAD `{head}`)")
    for name, h in sc.provenance.get("prompt_hashes", {}).items():
        lines.append(f"- prompt `{name}`: `{h[:16]}`")
    lines.append("")
    lines.append("## Q1 — drop spec-task-reviewer? (per model)")
    if sc.q1:
        for model_id, res in sorted(sc.q1.items()):
            line = f"- `{model_id}`: **{res.get('verdict')}**"
            if "unique_blocking_mean" in res:
                ci = res.get("unique_blocking_ci") or (0.0, 1.0)
                line += (
                    f" — unique-blocking {res['unique_blocking_mean']:.2f} "
                    f"CI [{ci[0]:.2f}, {ci[1]:.2f}] over n={res.get('n_diffs')} diffs"
                )
            lines.append(line)
    else:
        lines.append("- verdict: **insufficient-diffs** (no paired runs supplied)")
    lines.append("## Q2 — structure (monolithic vs completeness slice)")
    lines.append(f"- verdict: **{sc.q2.get('verdict')}** "
                 f"({sc.q2.get('n_cross_cutting')} cross-cutting fixtures)")
    lines.append("## Q3 — model/provider matrix ($-normalized)")
    for r in sc.q3["rows"]:
        lines.append(
            f"- {r['variant']} @ {r['fixture']}: recall={r['blocking_recall']} "
            f"ci={r['blocking_recall_ci']} ${r['dollars']:.4f}"
            + ("  ⚠ low-n" if r["low_n"] else "")
        )
    lines.append("## Q4 — fast-path replaces per-task review? (per model)")
    if sc.q4:
        for model_id, res in sorted(sc.q4.items()):
            line = f"- `{model_id}`: **{res.get('verdict')}**"
            if "fast_total" in res:
                line += (
                    f" — fast-path {res.get('fast_caught')}/{res.get('fast_total')} "
                    f"({res['fastpath_blocking_recall']:.2f}) vs union "
                    f"{res.get('union_caught')}/{res.get('union_total')} "
                    f"({res['union_blocking_recall']:.2f}) over "
                    f"n={res.get('n_diffs')} diffs (point estimates, no CI)"
                )
            lines.append(line)
    else:
        lines.append("- verdict: **insufficient-diffs** (no fast-path comparison supplied)")
    if sc.notes:
        lines.append("")
        lines.append("## Notes")
        lines.extend(f"- {n}" for n in sc.notes)
    return "\n".join(lines) + "\n"
