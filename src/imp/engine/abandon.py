"""Destroy a Change: status -> abandoned, journal it, remove worktree + branch."""
from __future__ import annotations

from imp.engine import worktree
from imp.engine.change import Change
from imp.engine.state import ProjectLock, State


def tear_down_change(change: Change) -> None:
    """Destroy a change: mark abandoned, journal, remove worktree and branch."""
    control = change.control_root
    change_id = change.id
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
    # branch delete must happen after worktree removal (git forbids deleting a checked-out branch)
    worktree.remove_worktree(control, change_id, force=True)
    worktree.delete_branch(control, change_id)
