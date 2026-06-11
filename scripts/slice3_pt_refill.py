"""Refill MISSING per-task slice3 cells after a usage-limit interruption, then
re-grade by content and re-render. Per-task analog of slice3_refill.py.

Per-task variants run ONLY on multi-task (mt-*) fixtures, so the full matrix is
{2 variants x mt-* fixtures x 3 repeats}. Discovers present cells by content
(provenance-independent), re-runs only the missing ones (validate tool_calls>0),
then re-grades all cells for these variants and re-renders. Idempotent.

Billable (reviewer = argv model; judge = opus). Gated by IMP_RUN_EVALS=1.
Run: IMP_RUN_EVALS=1 PYTHONPATH=src python3 scripts/slice3_pt_refill.py haiku
"""
from __future__ import annotations

import glob
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from imp.evals.cache import store_run
from imp.evals.cli import PRICING_VERSION, _curated_fixtures, find_control_root
from imp.evals.grader import grade
from imp.evals.models import RunResult, Variant
from imp.evals.provenance import core_digest, fixture_label_hash, imp_commit_sha, stamp
from imp.evals.scorecard import build_scorecard, render_markdown
from imp.evals.structures import run_variant

MODELS = {
    "haiku": ("claude_code", "claude-haiku-4-5-20251001"),
    "sonnet": ("claude_code", "claude-sonnet-4-6"),
}
JUDGE_PROVIDER, JUDGE_MODEL = "claude_code", "claude-opus-4-8"

key = sys.argv[1] if len(sys.argv) > 1 else "haiku"
if key not in MODELS:
    raise SystemExit(f"model key must be one of {list(MODELS)}, got {key!r}")
M = MODELS[key]

VARIANTS = [
    Variant(id=f"per-task-mono-{key}", role_to_model_map={"code-reviewer": M},
            structure="per-task-monolithic", n_repeats=3),
    Variant(id=f"per-task-slice3-{key}",
            role_to_model_map={"code-reviewer": M, "completeness-slice": M,
                               "spec-compliance-slice": M, "boundaries-slice": M},
            structure="per-task-slice-panel", n_repeats=3),
]
vmap = {v.id: v for v in VARIANTS}

control_root = find_control_root(Path.cwd())
cache_dir = control_root / ".imp" / "eval-cache"
all_fixtures = {f.id: f for f in _curated_fixtures()}
# per-task variants only apply to multi-task fixtures
mt_fixtures = {fid: f for fid, f in all_fixtures.items() if getattr(f, "tasks", ()) }
prompt_dirs = {"baseline": Path("src/imp/assets/workflows/default/agents")}
prov = core_digest(stamp(
    prompt_dirs=prompt_dirs,
    cell_models={v.id: [f"{p}/{m}" for p, m in v.role_to_model_map.values()] for v in VARIANTS},
    judge_model=f"{JUDGE_PROVIDER}/{JUDGE_MODEL}",
    pricing_version=PRICING_VERSION,
    fixture_label_hash=fixture_label_hash(list(all_fixtures.values())),
))
sha = imp_commit_sha()

full = {(v.id, fxid, r) for v in VARIANTS for fxid in mt_fixtures for r in range(v.n_repeats)}
present = set()
for f in glob.glob(str(cache_dir / "*.json")):
    d = json.loads(Path(f).read_text())
    vid = d.get("variant_id")
    if vid not in vmap or d.get("fixture_id") not in mt_fixtures:
        continue
    if d.get("failed") or (d.get("telemetry", {}).get("tool_calls") or 0) <= 0:
        continue
    present.add((vid, d["fixture_id"], d["repeat_index"]))

missing = sorted(full - present)
print(f"[pt-{key}] full={len(full)} present={len(present)} missing={len(missing)}", flush=True)
for m in missing:
    print("   missing:", m, flush=True)


def _refill(item):
    vid, fxid, r = item
    res = run_variant(control_root, vmap[vid], mt_fixtures[fxid], repeat_index=r)
    tc = res.telemetry.tool_calls
    if tc > 0 and not res.failed:
        store_run(cache_dir, res, provenance=prov, imp_commit_sha=sha)
        return (item, "ok", tc, len(res.findings))
    return (item, "STILL-FAILED", tc, 0)


if missing:
    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(_refill, missing))
    for r in results:
        print("   refill:", r, flush=True)
    still = [r for r in results if r[1] != "ok"]
    print(f"[pt-{key}] recovered={len(results) - len(still)} still_failed={len(still)}", flush=True)
else:
    print(f"[pt-{key}] no missing cells — nothing to refill", flush=True)

best = {}
for f in glob.glob(str(cache_dir / "*.json")):
    d = json.loads(Path(f).read_text())
    if d.get("variant_id") not in vmap or d.get("fixture_id") not in mt_fixtures:
        continue
    if d.get("failed") or (d.get("telemetry", {}).get("tool_calls") or 0) <= 0:
        continue
    k = (d["variant_id"], d["fixture_id"], d["repeat_index"])
    mt = os.path.getmtime(f)
    if k not in best or mt > best[k][0]:
        best[k] = (mt, RunResult.from_dict(d))
loaded = [(mt_fixtures[rr.fixture_id], rr) for _, rr in best.values()]
print(f"[pt-{key}] cells to grade: {len(loaded)}", flush=True)
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
sc = build_scorecard(grades=grades, fixtures=all_fixtures, dollars_by_cell=dollars,
                     prompt_dirs=prompt_dirs, provenance=run_stamp)
out = control_root / ".imp" / f"slice3-pt-{key}-scorecard.md"
out.write_text(render_markdown(sc))
print(f"[pt-{key}] scorecard -> {out}", flush=True)
