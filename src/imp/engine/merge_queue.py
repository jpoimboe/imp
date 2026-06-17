"""FIFO merge queue, partitioned by base_branch, ordered by enqueued_at.

A single shared file at <control_root>/.git/imp/merge-queue.json, mutated only
under RepoGitLock (the same lock-adjacent, sandbox-writable git common dir that
holds repo.lock — NOT the per-worktree gitignored .imp/). Each entry:
    {"change_id": "...", "base_branch": "...", "enqueued_at": "<iso8601>"}
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


def enqueue_if_absent(control_root: Path, change_id: str, base_branch: str) -> None:
    with RepoGitLock(control_root).acquire():
        entries = read(control_root)
        if any(e["change_id"] == change_id for e in entries):
            return
        entries.append({
            "change_id": change_id,
            "base_branch": base_branch,
            "enqueued_at": datetime.now(UTC).isoformat(),
        })
        _write(control_root, entries)


def is_head(control_root: Path, change_id: str, base_branch: str) -> bool:
    with RepoGitLock(control_root).acquire():
        lane = _lane(read(control_root), base_branch)
    return bool(lane) and lane[0]["change_id"] == change_id


def position(control_root: Path, change_id: str, base_branch: str) -> int:
    """Number of entries ahead of change_id in its lane (0 == head). Returns the
    lane length when change_id is absent (treated as 'behind everyone')."""
    with RepoGitLock(control_root).acquire():
        lane = _lane(read(control_root), base_branch)
    ids = [e["change_id"] for e in lane]
    return ids.index(change_id) if change_id in ids else len(ids)


def remove(control_root: Path, change_id: str) -> None:
    """Drop a change's entry (abandon of a non-head, or any cleanup). No wake."""
    with RepoGitLock(control_root).acquire():
        entries = [e for e in read(control_root) if e["change_id"] != change_id]
        _write(control_root, entries)


def pop_and_wake(
    control_root: Path, change_id: str, base_branch: str,
    *, spawn_fn: Callable[..., Any] | None = None,
) -> None:
    """Remove self from the queue; wake the new lane head if self was head."""
    with RepoGitLock(control_root).acquire():
        entries = read(control_root)
        lane_before = _lane(entries, base_branch)
        was_head = bool(lane_before) and lane_before[0]["change_id"] == change_id
        entries = [e for e in entries if e["change_id"] != change_id]
        _write(control_root, entries)
        lane_after = _lane(entries, base_branch)
        next_id = lane_after[0]["change_id"] if (was_head and lane_after) else None
    if next_id is not None:
        _wake(control_root, next_id, spawn_fn)


def _wake(control_root: Path, next_id: str, spawn_fn: Callable[..., Any] | None) -> None:
    from imp.engine.change import Change
    if spawn_fn is None:
        from imp.remote.session.attach import spawn_session_daemon
        spawn_fn = spawn_session_daemon
    spawn_fn(Change.load(control_root, next_id), resume=True)
