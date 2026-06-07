"""Recovery re-grade: build the Q2/Q3 scorecard from THIS run's cached cells by
CONTENT (variant_id/fixture_id/repeat_index in each cell JSON), bypassing the
provenance-keyed cache lookup. Needed because the cells were written under a
provenance whose content-hash (prompt dir / cell models) has since drifted
(shared-tree), so cmd_scorecard's recomputed provenance misses them all.

Billable: re-grades each cell with the claude_code opus judge (grades aren't
cached in v1). Gated by IMP_RUN_EVALS=1; opus-judges-opus cells need
IMP_EVAL_ALLOW_SELF_JUDGE=1.
"""
from __future__ import annotations

import glob
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from imp.evals.cli import (
    PRICING_VERSION,
    _curated_fixtures,
    _default_variants,
    _structure_variants,
    find_control_root,
)
from imp.evals.grader import grade
from imp.evals.models import RunResult
from imp.evals.provenance import stamp
from imp.evals.scorecard import build_scorecard, render_markdown

JUDGE_PROVIDER = "claude_code"
JUDGE_MODEL = "claude-opus-4-8"

control_root = find_control_root(Path.cwd())
cache_dir = control_root / ".imp" / "eval-cache"
fixtures = {f.id: f for f in _curated_fixtures()}
variants = _default_variants() + _structure_variants()

log = sorted(glob.glob(str(control_root / ".imp" / "eval-rerun-*.log")))[-1]
t0 = os.path.getmtime(log)

# Load THIS-run cells by content, dedupe (variant,fixture,repeat) keeping newest.
best: dict[tuple[str, str, int], tuple[float, RunResult]] = {}
for f in glob.glob(str(cache_dir / "*.json")):
    mt = os.path.getmtime(f)
    if mt <= t0:
        continue
    d = json.load(open(f))
    vid = d.get("variant_id", "")
    if vid.startswith("paired:"):
        continue
    if d.get("fixture_id") not in fixtures:  # Q2/Q3 are curated-only
        continue
    if d.get("failed"):
        continue
    key = (vid, d["fixture_id"], d["repeat_index"])
    if key not in best or mt > best[key][0]:
        best[key] = (mt, RunResult.from_dict(d))

loaded = [(fixtures[rr.fixture_id], rr) for _, rr in best.values()]
print(f"cells to grade: {len(loaded)}")
runs_shas = {rr.imp_commit_sha for _, rr in loaded if rr.imp_commit_sha}

# Dollars per cell straight from the cached RunResult (no prov lookup).
dollars_by_cell: dict[tuple[str, str], list[float]] = {}
for _, rr in loaded:
    dollars_by_cell.setdefault((rr.variant_id, rr.fixture_id), []).append(rr.dollars)


def _g(fr):
    fx, rr = fr
    return grade(fx, rr, project_root=control_root,
                 judge_provider=JUDGE_PROVIDER, judge_model=JUDGE_MODEL)


with ThreadPoolExecutor(max_workers=8) as ex:
    grades = list(ex.map(_g, loaded))
print(f"graded: {len(grades)}")

prompt_dirs = {"baseline": Path("src/imp/assets/workflows/default/agents")}
run_stamp = stamp(
    prompt_dirs=prompt_dirs,
    cell_models={v.id: [f"{p}/{m}" for p, m in v.role_to_model_map.values()]
                 for v in variants},
    judge_model=f"{JUDGE_PROVIDER}/{JUDGE_MODEL}",
    pricing_version=PRICING_VERSION,
)
sc = build_scorecard(
    grades=grades, fixtures=fixtures, dollars_by_cell=dollars_by_cell,
    prompt_dirs=prompt_dirs, provenance=run_stamp, runs_shas=runs_shas,
)
out = control_root / ".imp" / "eval-scorecard.md"
out.write_text(render_markdown(sc))
print(f"scorecard -> {out}")
