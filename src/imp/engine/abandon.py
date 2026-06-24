"""Abandon a Change: mark abandoned, remove from merge queue, archive, remove worktree + branch."""
from __future__ import annotations

import shutil

from imp.engine import merge_queue, worktree
from imp.engine.change import Change
from imp.engine.state import ProjectLock, State


def tear_down_change(change: Change) -> None:
    """Destroy a change: mark abandoned, archive, remove from the merge queue
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
        # archive before worktree removal (the runtime files live inside the worktree)
        from imp.engine import archive
        archive.archive_change(
            control, change_id,
            events_path=change.events_path,
            change_json_path=change.change_dir / "change.json",
            state_json_path=change.state_dir / "state.json",
        )
    # Uses RepoGitLock, independent of the per-change ProjectLock held above.
    merge_queue.pop_and_wake(control, change_id, base_branch)
    # branch delete must happen after worktree removal (git forbids deleting a checked-out branch)
    worktree.remove_worktree(control, change_id, force=True)
    worktree.delete_branch(control, change_id)
    shutil.rmtree(change.change_dir, ignore_errors=True)


def archive_conversation(change: Change) -> None:
    """Archive a left conversation as resumable (paused, not abandoned); drop the live home."""
    control = change.control_root
    change_id = change.id
    base_branch = change.base_branch
    materialized = change.materialized
    with ProjectLock(change.change_dir / "lock").acquire():  # raises LockHeld if runner is live
        state = State.load(change.state_dir)
        state.status = "paused"
        state.save(change.state_dir)
        change.event_log().append(
            agent_id=None, type="conversation_archived", payload={})
        from imp.engine import archive
        archive.archive_change(
            control, change_id,
            events_path=change.events_path,
            change_json_path=change.change_dir / "change.json",
            state_json_path=change.state_dir / "state.json",
            resumable=True,
        )
    if materialized:
        merge_queue.pop_and_wake(control, change_id, base_branch)
        worktree.remove_worktree(control, change_id, force=True)
        worktree.delete_branch(control, change_id)
    shutil.rmtree(change.change_dir, ignore_errors=True)


def save_to_backlog(change: Change) -> None:
    """Pause a conversation: the un-materialized change-home is kept (not archived) and
    remains resumable. Zone is derived from both `materialized` and `origin` — no bespoke
    status string is needed for zone placement."""
    with ProjectLock(change.change_dir / "lock").acquire():
        state = State.load(change.state_dir)
        state.status = "paused"
        state.save(change.state_dir)
        change.event_log().append(
            agent_id=None, type="conversation_saved", payload={})


def discard_conversation(change: Change) -> None:
    """Discard a zero-exchange open — no archive, no record; raises LockHeld if runner is live."""
    with ProjectLock(change.change_dir / "lock").acquire():
        pass
    shutil.rmtree(change.change_dir, ignore_errors=True)
