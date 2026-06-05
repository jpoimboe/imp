"""Result cache keyed (variant_id, fixture_id, repeat_index) — spec Cost &
reproducibility §. The repeat index is in the key DELIBERATELY: dropping it would
collapse all repeats to one value and destroy the variance the framework measures.
The cache serves scorecard RE-RENDERS (re-grade / re-aggregate without re-running
agents); a sweep still runs every repeat fresh."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

from imp.evals.models import RunResult


def cache_key(
    variant_id: str, fixture_id: str, repeat_index: int, provenance: str = ""
) -> str:
    """`provenance` (a digest of imp commit SHA + prompt-dir hashes + the cell's
    model id) is part of the key so a re-render after a prompt/SHA/model change
    does NOT serve a stale cell under a fresh provenance stamp — that silent
    corruption was the cache's worst failure mode."""
    raw = f"{variant_id}\0{fixture_id}\0{repeat_index}\0{provenance}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _path(
    cache_dir: Path, variant_id: str, fixture_id: str, repeat_index: int, provenance: str
) -> Path:
    return cache_dir / f"{cache_key(variant_id, fixture_id, repeat_index, provenance)}.json"


def store_run(
    cache_dir: Path, run_result: RunResult, provenance: str = "", imp_commit_sha: str = ""
) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    # frozen dataclass: stamp the SHA via replace, never attribute assignment.
    rr = replace(run_result, imp_commit_sha=imp_commit_sha)
    p = _path(cache_dir, rr.variant_id, rr.fixture_id, rr.repeat_index, provenance)
    p.write_text(json.dumps(rr.to_dict(), indent=2))


def load_run(
    cache_dir: Path,
    variant_id: str,
    fixture_id: str,
    repeat_index: int,
    provenance: str = "",
) -> RunResult | None:
    p = _path(cache_dir, variant_id, fixture_id, repeat_index, provenance)
    if not p.exists():
        return None
    return RunResult.from_dict(json.loads(p.read_text()))
