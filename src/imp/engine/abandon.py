"""Abandon a Change: mark abandoned, remove from merge queue, journal, remove worktree + branch."""
from __future__ import annotations

import shutil

from imp.engine import merge_queue, worktree
from imp.engine.change import Change
from imp.engine.state import ProjectLock, State


def tear_down_change(change: Change) -> None:
    """Destroy a change: mark abandoned, journal, remove from the merge queue
    (waking the next head if this change was the head), remove worktree + branch."""
    control = change.control_root
    change_id = change.id
    base_branch = change.base_branch
    with ProjectLock(change.change_dir / "lock").acquire():  # raises LockHeld if runner is live
        state = State.load(change.state_dir)
        state.status = "abandoned"
        state.save(change.state_dir)
        change.event_log().append(
            agent_id=None, type="change_abandoned", payload={})
        # journal commit must happen before worktree removal (records live inside the worktree)
        worktree.fold_wip_into_journal(control, change_id, {
            "events.jsonl": change.events_path,
            "change.json": change.change_dir / "change.json",
            "state.json": change.state_dir / "state.json",
        })
    # Uses RepoGitLock, independent of the per-change ProjectLock held above.
    merge_queue.pop_and_wake(control, change_id, base_branch)
    # branch delete must happen after worktree removal (git forbids deleting a checked-out branch)
    worktree.remove_worktree(control, change_id, force=True)
    worktree.delete_branch(control, change_id)
    shutil.rmtree(change.change_dir, ignore_errors=True)
