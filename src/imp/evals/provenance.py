"""Provenance stamp for every scorecard (spec Cost & reproducibility §): the imp
commit SHA + a content hash of each prompt directory that produced the result.
Baseline reviewer prompts live in the evolving asset tree; Variants point at
override dirs — both are hashed so a scorecard pins the exact prompts."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any


def hash_prompt_dir(path: Path) -> str:
    """SHA-256 over the sorted (relative-path, bytes) of every file under `path`.
    Path-sensitive and content-sensitive so a renamed or edited prompt changes
    the hash."""
    h = hashlib.sha256()
    for f in sorted(p for p in path.rglob("*") if p.is_file()):
        h.update(str(f.relative_to(path)).encode())
        h.update(b"\0")
        h.update(f.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def imp_commit_sha() -> str:
    """The imp HEAD SHA, or 'unknown' outside a git checkout."""
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True
    )
    return proc.stdout.strip() if proc.returncode == 0 else "unknown"


def stamp(
    *,
    prompt_dirs: dict[str, Path],
    cell_models: dict[str, list[str]] | None = None,
    judge_model: str | None = None,
    pricing_version: str | None = None,
    fixture_label_hash: str | None = None,
) -> dict[str, Any]:
    """A result is only reproducible/comparable if the stamp pins everything that
    moves a number: the imp SHA, the prompt-dir hashes, the per-cell model ids,
    the judge model, the pricing table version, and a hash of the fixture labels.
    Two scorecards with identical stamps must be genuinely comparable."""
    return {
        "imp_commit_sha": imp_commit_sha(),
        "prompt_hashes": {name: hash_prompt_dir(p) for name, p in prompt_dirs.items()},
        "cell_models": cell_models or {},
        "judge_model": judge_model,
        "pricing_version": pricing_version,
        "fixture_label_hash": fixture_label_hash,
    }


def core_digest(stamp_dict: dict[str, Any]) -> str:
    """The HARD cache key: a digest of everything that genuinely moves a number —
    prompt hashes, cell models, judge model, pricing version, fixture label hash —
    but NOT `imp_commit_sha`. A docs-only or harness-only imp commit no longer busts
    the cache; a real scoring-input change still does. The SHA rides on each cached
    cell (RunResult.imp_commit_sha) and is surfaced as a warn-prompt at re-render
    time instead (cmd_scorecard)."""
    core = {k: v for k, v in stamp_dict.items() if k != "imp_commit_sha"}
    return provenance_digest(core)


def provenance_digest(stamp_dict: dict[str, Any]) -> str:
    """A short, stable digest of a stamp — the cache-key provenance component
    (cache.py). Any change to imp SHA / prompts / models / pricing changes it, so
    a stale cell misses instead of being served under a fresh stamp. Lists are
    sorted before hashing (`sort_keys` orders dict KEYS, not list VALUES — without
    this, model-id list order would cause spurious cache misses)."""

    def _norm(v: Any) -> Any:
        if isinstance(v, list):
            return sorted(_norm(x) for x in v)
        if isinstance(v, dict):
            return {k: _norm(x) for k, x in v.items()}
        return v

    raw = json.dumps(_norm(stamp_dict), sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
