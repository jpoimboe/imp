"""FIFO merge queue, partitioned by base_branch, ordered by enqueued_at.

A single shared file at <control_root>/.git/imp/merge-queue.json, mutated only
under RepoGitLock (the same lock-adjacent, sandbox-writable git common dir that
holds repo.lock — NOT the per-worktree gitignored .imp/). Each entry:
    {"quest_id": "...", "base_branch": "...", "enqueued_at": "<iso8601>"}
"""
from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from imp.engine.worktree import RepoGitLock


def _queue_path(control_root: Path) -> Path:
    return control_root / ".git" / "imp" / "merge-queue.json"


def read(control_root: Path) -> list[dict[str, str]]:
    """Read the raw queue (no lock; callers needing atomicity hold RepoGitLock)."""
    p = _queue_path(control_root)
    if not p.exists():
        return []
    return cast(list[dict[str, str]], json.loads(p.read_text()))


def _write(control_root: Path, entries: list[dict[str, str]]) -> None:
    p = _queue_path(control_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(entries, indent=2, sort_keys=True))
    os.replace(tmp, p)


def _lane(entries: list[dict[str, str]], base_branch: str) -> list[dict[str, str]]:
    return sorted(
        (e for e in entries if e["base_branch"] == base_branch),
        key=lambda e: e["enqueued_at"],
    )


def enqueue_if_absent(control_root: Path, quest_id: str, base_branch: str) -> None:
    with RepoGitLock(control_root).acquire():
        entries = read(control_root)
        if any(e["quest_id"] == quest_id for e in entries):
            return
        entries.append({
            "quest_id": quest_id,
            "base_branch": base_branch,
            "enqueued_at": datetime.now(UTC).isoformat(),
        })
        _write(control_root, entries)


def is_head(control_root: Path, quest_id: str, base_branch: str) -> bool:
    lane = _lane(read(control_root), base_branch)
    return bool(lane) and lane[0]["quest_id"] == quest_id


def position(control_root: Path, quest_id: str, base_branch: str) -> int:
    """Number of entries ahead of quest_id in its lane (0 == head). Returns the
    lane length when quest_id is absent (treated as 'behind everyone')."""
    lane = _lane(read(control_root), base_branch)
    ids = [e["quest_id"] for e in lane]
    return ids.index(quest_id) if quest_id in ids else len(ids)


def remove(control_root: Path, quest_id: str) -> None:
    """Drop a change's entry (abandon of a non-head, or any cleanup). No wake."""
    with RepoGitLock(control_root).acquire():
        entries = [e for e in read(control_root) if e["quest_id"] != quest_id]
        _write(control_root, entries)


def ensure_head_driven(
    control_root: Path, base_branch: str,
    *, spawn_fn: Callable[..., Any] | None = None,
) -> None:
    """Spawn a live driver for this lane's current head; tolerant and never-raising."""
    from imp.engine.errors import LoadError
    from imp.engine.quest import Quest
    with RepoGitLock(control_root).acquire():
        lane = _lane(read(control_root), base_branch)
        head_id = lane[0]["quest_id"] if lane else None
    if head_id is None:
        return
    try:
        change = Quest.load(control_root, head_id)
    except (LoadError, OSError):
        return
    if spawn_fn is None:
        from imp.remote.session.attach import spawn_session_daemon
        spawn_fn = spawn_session_daemon
    spawn_fn(change, resume=True)


def pop_and_wake(
    control_root: Path, quest_id: str, base_branch: str,
    *, spawn_fn: Callable[..., Any] | None = None,
) -> None:
    """Remove self from the queue; wake the new lane head if self was head."""
    with RepoGitLock(control_root).acquire():
        entries = read(control_root)
        lane_before = _lane(entries, base_branch)
        was_head = bool(lane_before) and lane_before[0]["quest_id"] == quest_id
        new_entries = [e for e in entries if e["quest_id"] != quest_id]
        if new_entries != entries:
            _write(control_root, new_entries)
        entries = new_entries
        lane_after = _lane(entries, base_branch)
        should_wake = was_head and bool(lane_after)
    if should_wake:
        ensure_head_driven(control_root, base_branch, spawn_fn=spawn_fn)
