"""Refill the per-task cells that came back EMPTY (0 tool-calls / 0 output — a
rate-limited response that slipped past failed-detection and cached as a spurious
0-finding result). Re-runs ONLY those cells, validates each is non-empty before
caching (so a re-interruption doesn't re-poison), then re-grades all 106 cells by
content and re-renders the Q2 scorecard.

Billable (opus). Gated by IMP_RUN_EVALS=1.
"""
from __future__ import annotations

import glob
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from imp.evals.cache import store_run
from imp.evals.cli import PRICING_VERSION, _curated_fixtures, find_control_root
from imp.evals.grader import grade
from imp.evals.models import RunResult, Variant
from imp.evals.provenance import core_digest, fixture_label_hash, imp_commit_sha, stamp
from imp.evals.scorecard import build_scorecard, render_markdown
from imp.evals.structures import run_variant

OPUS = ("claude_code", "claude-opus-4-8")
JUDGE_PROVIDER, JUDGE_MODEL = "claude_code", "claude-opus-4-8"

VARIANTS = [
    Variant(id="baseline-monolithic", role_to_model_map={"code-reviewer": OPUS},
            structure="monolithic", n_repeats=3),
    Variant(id="whole-change-completeness",
            role_to_model_map={"code-reviewer": OPUS, "completeness-slice": OPUS},
            structure="slice-panel", n_repeats=3),
    Variant(id="per-task-mono", role_to_model_map={"code-reviewer": OPUS},
            structure="per-task-monolithic", n_repeats=5),
    Variant(id="per-task-slice",
            role_to_model_map={"code-reviewer": OPUS, "completeness-slice": OPUS},
            structure="per-task-slice-panel", n_repeats=5),
]
vmap = {v.id: v for v in VARIANTS}

control_root = find_control_root(Path.cwd())
cache_dir = control_root / ".imp" / "eval-cache"
fixtures = {f.id: f for f in _curated_fixtures()}
prompt_dirs = {"baseline": Path("src/imp/assets/workflows/default/agents")}
prov = core_digest(stamp(
    prompt_dirs=prompt_dirs,
    cell_models={v.id: [f"{p}/{m}" for p, m in v.role_to_model_map.values()] for v in VARIANTS},
    judge_model=f"{JUDGE_PROVIDER}/{JUDGE_MODEL}",
    pricing_version=PRICING_VERSION,
    fixture_label_hash=fixture_label_hash(list(fixtures.values())),
))
sha = imp_commit_sha()
ref = os.path.getmtime("/tmp/q2_progress_ref")

# 1) find empty per-task cells (0 tool-calls) from this run
empties = []
for f in glob.glob(str(cache_dir / "*.json")):
    if os.path.getmtime(f) <= ref:
        continue
    d = json.loads(Path(f).read_text())
    v = d.get("variant_id", "")
    tool_calls = d.get("telemetry", {}).get("tool_calls") or 0
    if v in ("per-task-mono", "per-task-slice") and tool_calls == 0:
        empties.append((v, d["fixture_id"], d["repeat_index"]))
print(f"empty cells to refill: {len(empties)}", flush=True)
for e in sorted(empties):
    print("   empty:", e, flush=True)


def _refill(item):
    vid, fxid, r = item
    res = run_variant(control_root, vmap[vid], fixtures[fxid], repeat_index=r)
    tc = res.telemetry.tool_calls
    if tc > 0:  # validate it actually ran before caching
        store_run(cache_dir, res, provenance=prov, imp_commit_sha=sha)
        return (item, "ok", tc, len(res.findings))
    return (item, "STILL-EMPTY", tc, 0)


if empties:
    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(_refill, empties))
    for r in results:
        print("   refill:", r, flush=True)
    still = [r for r in results if r[1] == "STILL-EMPTY"]
    print(f"recovered={len(results) - len(still)} still_empty={len(still)}", flush=True)
else:
    print("no empty cells — nothing to refill", flush=True)

# 2) re-grade ALL this-run structure cells by content + re-render scorecard
best = {}
for f in glob.glob(str(cache_dir / "*.json")):
    if os.path.getmtime(f) <= ref:
        continue
    d = json.loads(Path(f).read_text())
    if d.get("variant_id") not in vmap or d.get("fixture_id") not in fixtures:
        continue
    if d.get("failed"):
        continue
    key = (d["variant_id"], d["fixture_id"], d["repeat_index"])
    mt = os.path.getmtime(f)
    if key not in best or mt > best[key][0]:
        best[key] = (mt, RunResult.from_dict(d))
loaded = [(fixtures[rr.fixture_id], rr) for _, rr in best.values()]
print(f"cells to grade: {len(loaded)}", flush=True)
dollars = {}
for _, rr in loaded:
    dollars.setdefault((rr.variant_id, rr.fixture_id), []).append(rr.dollars)
with ThreadPoolExecutor(max_workers=8) as ex:
    grades = list(ex.map(
        lambda fr: grade(fr[0], fr[1], project_root=control_root,
                         judge_provider=JUDGE_PROVIDER, judge_model=JUDGE_MODEL),
        loaded))
run_stamp = stamp(
    prompt_dirs=prompt_dirs,
    cell_models={v.id: [f"{p}/{m}" for p, m in v.role_to_model_map.values()] for v in VARIANTS},
    judge_model=f"{JUDGE_PROVIDER}/{JUDGE_MODEL}", pricing_version=PRICING_VERSION,
)
sc = build_scorecard(grades=grades, fixtures=fixtures, dollars_by_cell=dollars,
                     prompt_dirs=prompt_dirs, provenance=run_stamp)
out = control_root / ".imp" / "eval-q2-opus-scorecard.md"
out.write_text(render_markdown(sc))
print(f"scorecard -> {out}", flush=True)
