"""Plan A: 3-slice review-panel experiment (completeness + spec-compliance +
boundaries), whole-diff, parametrized by reviewer model.

Three arms, all at ONE reviewer model (argv[1] in {haiku, sonnet}):
  - mono-<m>    : {code-reviewer}                          monolithic   (baseline)
  - slice1-<m>  : {code-reviewer, completeness}            slice-panel  (replicates
                  the opus 1-slice result on this model — comparability anchor)
  - slice3-<m>  : {code-reviewer, completeness, spec, boundaries}  slice-panel

Augmentation design: every panel arm KEEPS the generalist code-reviewer and ADDS
focused lenses, so the arms vary only in #lenses-added (0 -> 1 -> 3). This is
directly comparable to the existing whole-change-completeness data (which was
mono + completeness). Judge = opus, independent of haiku/sonnet reviewers (no
self-judge flag needed). Gated by IMP_RUN_EVALS=1.

Run: IMP_RUN_EVALS=1 PYTHONPATH=src python3 scripts/slice3_run.py haiku
"""
from __future__ import annotations

import sys
from pathlib import Path

from imp.evals.cli import PRICING_VERSION, _curated_fixtures, find_control_root
from imp.evals.models import Variant
from imp.evals.provenance import core_digest, fixture_label_hash, stamp
from imp.evals.scorecard import build_scorecard, render_markdown
from imp.evals.sweep import collect_dollars, plan_sweep, run_sweep

MODELS = {
    "haiku": ("claude_code", "claude-haiku-4-5-20251001"),
    "sonnet": ("claude_code", "claude-sonnet-4-6"),
}
JUDGE_PROVIDER, JUDGE_MODEL = "claude_code", "claude-opus-4-8"

key = sys.argv[1] if len(sys.argv) > 1 else "haiku"
if key not in MODELS:
    raise SystemExit(f"model key must be one of {list(MODELS)}, got {key!r}")
M = MODELS[key]


def _variants() -> list[Variant]:
    return [
        Variant(id=f"mono-{key}",
                role_to_model_map={"code-reviewer": M},
                structure="monolithic", n_repeats=3),
        Variant(id=f"slice1-{key}",
                role_to_model_map={"code-reviewer": M, "completeness-slice": M},
                structure="slice-panel", n_repeats=3),
        Variant(id=f"slice3-{key}",
                role_to_model_map={"code-reviewer": M, "completeness-slice": M,
                                   "spec-compliance-slice": M, "boundaries-slice": M},
                structure="slice-panel", n_repeats=3),
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
print(f"[{key}] plan: {plan.n_cells} cells, {plan.n_agent_runs} reviewer runs, "
      f"{plan.n_judge_calls} judge calls", flush=True)

grades = run_sweep(
    control_root, variants, fixtures,
    cache_dir=cache_dir,
    judge_provider=JUDGE_PROVIDER, judge_model=JUDGE_MODEL,
    concurrency=8, provenance=prov,
)
print(f"[{key}] graded cells: {len(grades)}", flush=True)

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
out = control_root / ".imp" / f"slice3-{key}-scorecard.md"
out.write_text(render_markdown(sc))
print(f"[{key}] scorecard -> {out}", flush=True)
