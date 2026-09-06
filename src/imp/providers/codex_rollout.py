# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Persist and restore codex's native on-disk rollout across an imp pause.

codex is stateful via a rollout file at
`$CODEX_HOME/sessions/…/rollout-<ts>-<thread_id>.jsonl`. imp provisions a fresh
per-run CODEX_HOME and `shutil.rmtree`s it at teardown, so the rollout — the
actual conversation context — is deleted at the pause. To resume, imp must own
that file across the pause: copy it (and the thread_id) into the durable
`transcript_dir` on pause, restore it into the fresh CODEX_HOME on resume, then
call `Codex.resume_thread(thread_id)`.

The same file is also the only place codex reports live context occupancy, so
`latest_context_reading` reads it in place — no stdout JSON mode emits it — and
the only place codex records that it compacted history (`compaction_seen`).
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from imp.providers.quota import QuotaReading, QuotaWindow

_THREAD_JSON = "codex_thread.json"
_ROLLOUT_SUBDIR = "codex_rollout"


def _newest_matching_rollout(codex_home: Path, thread_id: str) -> Path | None:
    """The rollout file for `thread_id`. codex names it
    `rollout-<ts>-<thread_id>.jsonl`; prefer the newest name-match (a chained
    resume may write a fresh rollout for the same thread), else the sole rollout.
    """
    sessions = codex_home / "sessions"
    if not sessions.exists():
        return None
    rollouts = list(sessions.rglob("*.jsonl"))
    matches = [p for p in rollouts if thread_id in p.name]
    if matches:
        return max(matches, key=lambda p: p.stat().st_mtime)
    if len(rollouts) == 1:
        return rollouts[0]
    return None


def _window_label(minutes: int) -> str:
    """From duration, never position: 300 -> `5h`, 10080 -> `7d`."""
    return f"{minutes // 60}h" if minutes < 1440 else f"{minutes // 1440}d"


def quota_from_rate_limits(block: Any) -> QuotaReading | None:
    """codex's `rate_limits` block from a `token_count` record, normalized.
    None when it carries no usable window — the caller then records nothing."""
    if not isinstance(block, dict):
        return None
    windows: list[tuple[int, QuotaWindow]] = []
    for key in ("primary", "secondary"):
        w = block.get(key)
        if not isinstance(w, dict):
            continue
        minutes = w.get("window_minutes")
        used = w.get("used_percent")
        if not isinstance(minutes, int) or minutes <= 0:
            continue
        if not isinstance(used, (int, float)):
            continue
        resets = w.get("resets_at")
        windows.append((minutes, QuotaWindow(
            label=_window_label(minutes), used_pct=float(used),
            resets_at=resets if isinstance(resets, int) else None)))
    if not windows:
        return None
    plan = block.get("plan_type")
    return QuotaReading(
        provider="codex", plan=plan if isinstance(plan, str) else None,
        windows=tuple(w for _, w in sorted(windows, key=lambda pair: pair[0])),
        raw=block)


def latest_context_reading(codex_home: Path, thread_id: str) -> tuple[int, int] | None:
    """codex's own live context reading from its rollout: the newest usable
    `(last_token_usage.total_tokens, model_context_window)` pair, or None when
    the rollout is absent or carries no usable `token_count` record. Never a
    partial reading — both halves come from the same record."""
    rollout = _newest_matching_rollout(codex_home, thread_id)
    if rollout is None:
        return None
    reading: tuple[int, int] | None = None
    with rollout.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            # Pre-filter: rollouts run to megabytes, few lines are token_count.
            if "token_count" not in line:
                continue
            try:
                rec = json.loads(line)
                payload = rec["payload"]
                if rec["type"] != "event_msg" or payload["type"] != "token_count":
                    continue
                info = payload["info"]
                tokens = info["last_token_usage"]["total_tokens"]
                window = info["model_context_window"]
            except (ValueError, KeyError, TypeError):
                continue
            if (isinstance(tokens, int) and tokens > 0
                    and isinstance(window, int) and window > 0):
                reading = (tokens, window)
    return reading


def compaction_seen(codex_home: Path, thread_id: str) -> bool:
    """True when codex compacted its own history in this rollout — a top-level
    `{"type": "compacted"}` record. Nothing about a compaction reaches the
    `codex exec --json` stdout stream, so the rollout is the only signal."""
    rollout = _newest_matching_rollout(codex_home, thread_id)
    if rollout is None:
        return False
    with rollout.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            # Pre-filter: rollouts run to megabytes, few lines mention it.
            if "compacted" not in line:
                continue
            try:
                if json.loads(line)["type"] == "compacted":
                    return True
            except (ValueError, KeyError, TypeError):
                continue
    return False


def persist_rollout(*, codex_home: Path, thread_id: str, transcript_dir: Path) -> bool:
    """Copy codex's rollout out of the ephemeral CODEX_HOME into `transcript_dir`
    and record `thread_id` + the rollout's relative path. Call BEFORE
    `teardown_codex_home`. Returns True iff a rollout was persisted (False when
    codex wrote none yet — e.g. a run that failed before its first turn)."""
    rollout = _newest_matching_rollout(codex_home, thread_id)
    if rollout is None:
        return False
    rel = rollout.relative_to(codex_home)  # sessions/…/rollout-….jsonl
    dest = transcript_dir / _ROLLOUT_SUBDIR / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(rollout, dest)
    (transcript_dir / _THREAD_JSON).write_text(
        json.dumps({"thread_id": thread_id, "rollout_relpath": str(rel)}))
    return True


def restore_rollout(
    *, codex_home: Path, transcript_dir: Path,
) -> str:
    """Restore the persisted rollout into the fresh CODEX_HOME and return the
    saved thread_id. Call AFTER provisioning the fresh CODEX_HOME and BEFORE
    `Codex.resume_thread`. Fails loudly (`FileNotFoundError`) if the thread
    metadata or rollout bytes are missing — converting otherwise-silent amnesia
    (teardown ran too early, ephemeral CI, cross-host) into a real error.
    """
    meta_path = transcript_dir / _THREAD_JSON
    if not meta_path.exists():
        raise FileNotFoundError(
            f"codex resume: no {_THREAD_JSON} in {transcript_dir} — "
            "rollout was not persisted on pause")
    meta = json.loads(meta_path.read_text())
    thread_id: str = meta["thread_id"]
    rel: str = meta["rollout_relpath"]
    saved = transcript_dir / _ROLLOUT_SUBDIR / rel
    if not saved.exists():
        raise FileNotFoundError(
            f"codex resume: persisted rollout missing at {saved}")
    dest = codex_home / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(saved, dest)
    if not dest.exists():  # the spec's explicit "assert it exists" guard
        raise FileNotFoundError(
            f"codex resume: rollout restore failed at {dest}")
    return thread_id
