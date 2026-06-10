"""Refill MISSING slice3 cells after a usage-limit interruption, then re-grade by
content and re-render the scorecard. Parametrized by reviewer model (argv[1]).

A rate-limited reviewer call fails -> the cell is NOT cached (sweep.py drops it).
run_sweep does not consult the cache before running, so re-running the whole
script would redo every cell. Instead this script:
  1. computes the full whole-diff matrix (3 variants x 11 fixtures x 3 repeats),
  2. scans the cache for cells already present (non-failed, tool_calls>0),
  3. re-runs ONLY the missing cells (validate tool_calls>0 before caching),
  4. re-grades ALL cells for these variants by content + re-renders the scorecard.

Same provenance/variants as scripts/slice3_run.py, so stored cells form one set.
Billable (reviewer = argv model; judge = opus). Gated by IMP_RUN_EVALS=1.

Run: IMP_RUN_EVALS=1 PYTHONPATH=src python3 scripts/slice3_refill.py haiku
"""
from __future__ import annotations

import glob
import json
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
    Variant(id=f"mono-{key}", role_to_model_map={"code-reviewer": M},
            structure="monolithic", n_repeats=3),
    Variant(id=f"slice1-{key}",
            role_to_model_map={"code-reviewer": M, "completeness-slice": M},
            structure="slice-panel", n_repeats=3),
    Variant(id=f"slice3-{key}",
            role_to_model_map={"code-reviewer": M, "completeness-slice": M,
                               "spec-compliance-slice": M, "boundaries-slice": M},
            structure="slice-panel", n_repeats=3),
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

# full whole-diff matrix (all variants are whole-diff -> every fixture applies)
full = {(v.id, fxid, r) for v in VARIANTS for fxid in fixtures for r in range(v.n_repeats)}

# present = cells already cached for these variants, non-failed and non-empty.
# Discovery is BY CONTENT (variant_id/fixture_id/repeat in the json), independent
# of provenance, so a drift in the cache key can't hide a good cell from us.
present = set()
for f in glob.glob(str(cache_dir / "*.json")):
    d = json.loads(Path(f).read_text())
    vid = d.get("variant_id")
    if vid not in vmap or d.get("fixture_id") not in fixtures:
        continue
    if d.get("failed"):
        continue
    if (d.get("telemetry", {}).get("tool_calls") or 0) <= 0:
        continue
    present.add((vid, d["fixture_id"], d["repeat_index"]))

missing = sorted(full - present)
print(f"[{key}] full={len(full)} present={len(present)} missing={len(missing)}", flush=True)
for m in missing:
    print("   missing:", m, flush=True)


def _refill(item):
    vid, fxid, r = item
    res = run_variant(control_root, vmap[vid], fixtures[fxid], repeat_index=r)
    tc = res.telemetry.tool_calls
    if tc > 0 and not res.failed:  # validate it actually ran before caching
        store_run(cache_dir, res, provenance=prov, imp_commit_sha=sha)
        return (item, "ok", tc, len(res.findings))
    return (item, "STILL-FAILED", tc, 0)


if missing:
    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(_refill, missing))
    for r in results:
        print("   refill:", r, flush=True)
    still = [r for r in results if r[1] != "ok"]
    print(f"[{key}] recovered={len(results) - len(still)} still_failed={len(still)}", flush=True)
else:
    print(f"[{key}] no missing cells — nothing to refill", flush=True)

# re-grade ALL cells for these variants by content + re-render
best = {}
for f in glob.glob(str(cache_dir / "*.json")):
    d = json.loads(Path(f).read_text())
    if d.get("variant_id") not in vmap or d.get("fixture_id") not in fixtures:
        continue
    if d.get("failed") or (d.get("telemetry", {}).get("tool_calls") or 0) <= 0:
        continue
    k = (d["variant_id"], d["fixture_id"], d["repeat_index"])
    import os
    mt = os.path.getmtime(f)
    if k not in best or mt > best[k][0]:
        best[k] = (mt, RunResult.from_dict(d))
loaded = [(fixtures[rr.fixture_id], rr) for _, rr in best.values()]
print(f"[{key}] cells to grade: {len(loaded)}", flush=True)
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
out = control_root / ".imp" / f"slice3-{key}-scorecard.md"
out.write_text(render_markdown(sc))
print(f"[{key}] scorecard -> {out}", flush=True)
