"""Focused Q2 + per-task-granularity run with opus as the reviewer.

Runs the 4 structure variants (whole-change × {mono, slice}, per-task × {mono,
slice}) bound to claude_code/opus over the curated fixtures, grades with the opus
judge, and writes a Q2-focused scorecard. Answers Q2 (monolithic vs
completeness-slice) and the per-task-vs-whole-change granularity cut directionally.

Self-judge (opus reviews + opus judges): symmetric across all arms, so the
RELATIVE Q2/granularity comparison is robust. Gated by IMP_RUN_EVALS=1; needs
IMP_EVAL_ALLOW_SELF_JUDGE=1.
"""
from __future__ import annotations

from pathlib import Path

from imp.evals.cli import _curated_fixtures, find_control_root, PRICING_VERSION
from imp.evals.models import Variant
from imp.evals.provenance import core_digest, fixture_label_hash, stamp
from imp.evals.scorecard import build_scorecard, render_markdown
from imp.evals.sweep import collect_dollars, plan_sweep, run_sweep

OPUS = ("claude_code", "claude-opus-4-8")
JUDGE_PROVIDER, JUDGE_MODEL = "claude_code", "claude-opus-4-8"


def _variants() -> list[Variant]:
    return [
        Variant(id="baseline-monolithic",
                role_to_model_map={"code-reviewer": OPUS},
                structure="monolithic", n_repeats=3),
        Variant(id="whole-change-completeness",
                role_to_model_map={"code-reviewer": OPUS, "completeness-slice": OPUS},
                structure="slice-panel", n_repeats=3),
        Variant(id="per-task-mono",
                role_to_model_map={"code-reviewer": OPUS},
                structure="per-task-monolithic", n_repeats=5),
        Variant(id="per-task-slice",
                role_to_model_map={"code-reviewer": OPUS, "completeness-slice": OPUS},
                structure="per-task-slice-panel", n_repeats=5),
    ]


control_root = find_control_root(Path.cwd())
cache_dir = control_root / ".imp" / "eval-cache"
variants = _variants()
fixtures = _curated_fixtures()
prompt_dirs = {"baseline": Path("src/imp/assets/workflows/default/agents")}

prov = core_digest(stamp(
    prompt_dirs=prompt_dirs,
    cell_models={v.id: [f"{p}/{m}" for p, m in v.role_to_model_map.values()] for v in variants},
    judge_model=f"{JUDGE_PROVIDER}/{JUDGE_MODEL}",
    pricing_version=PRICING_VERSION,
    fixture_label_hash=fixture_label_hash(fixtures),
))

plan = plan_sweep(variants, fixtures)
print(f"plan: {plan.n_cells} cells, {plan.n_agent_runs} reviewer runs, "
      f"{plan.n_judge_calls} judge calls", flush=True)

grades = run_sweep(
    control_root, variants, fixtures,
    cache_dir=cache_dir,
    judge_provider=JUDGE_PROVIDER, judge_model=JUDGE_MODEL,
    concurrency=8, provenance=prov,
)
print(f"graded cells: {len(grades)}", flush=True)

run_stamp = stamp(
    prompt_dirs=prompt_dirs,
    cell_models={v.id: [f"{p}/{m}" for p, m in v.role_to_model_map.values()] for v in variants},
    judge_model=f"{JUDGE_PROVIDER}/{JUDGE_MODEL}",
    pricing_version=PRICING_VERSION,
)
sc = build_scorecard(
    grades=grades,
    fixtures={f.id: f for f in fixtures},
    dollars_by_cell=collect_dollars(cache_dir, variants, fixtures, prov),
    prompt_dirs=prompt_dirs,
    provenance=run_stamp,
)
out = control_root / ".imp" / "eval-q2-opus-scorecard.md"
out.write_text(render_markdown(sc))
print(f"scorecard -> {out}", flush=True)
