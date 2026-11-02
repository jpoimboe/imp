# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Abandon a quest: mark abandoned, archive a non-resumable tombstone, drop the
live home; on a built-out quest also pop the merge queue and remove the worktree + branch."""
from __future__ import annotations

import shutil

from imp.engine import merge_queue, worktree
from imp.engine.quest import Quest
from imp.engine.sandbox import force_rmtree
from imp.engine.state import ProjectLock, State


def tear_down_quest(quest: Quest) -> None:
    """Destroy a quest: mark abandoned, archive a non-resumable tombstone, drop the
    live home. On a built-out quest also pop the merge queue (waking the next head if
    this quest was the head), remove the worktree, and delete the branch; on a draft
    those three are skipped (there is no worktree, branch, or queue slot)."""
    control = quest.control_root
    quest_id = quest.id
    base_branch = quest.base_branch
    materialized = quest.materialized
    with ProjectLock(quest.quest_dir / "lock").acquire():  # raises LockHeld if runner is live
        state = State.load(quest.state_dir)
        state.status = "abandoned"
        state.save(quest.state_dir)
        quest.event_log().append(
            agent_id=None, type="quest_abandoned", payload={})
        # archive before worktree removal (the runtime files live inside the worktree)
        from imp.engine import archive
        archive.archive_quest(
            control, quest_id,
            events_path=quest.events_path,
            quest_json_path=quest.quest_dir / "quest.json",
            state_json_path=quest.state_dir / "state.json",
        )
    if materialized:
        # Uses RepoGitLock, independent of the per-quest ProjectLock held above.
        merge_queue.pop_and_wake(control, quest_id, base_branch)
        # branch delete after worktree removal (git forbids deleting a checked-out branch)
        worktree.remove_worktree(control, quest_id, force=True)
        worktree.delete_branch(control, quest_id)
    force_rmtree(quest.quest_dir)
    shutil.rmtree(quest.tmp_dir, ignore_errors=True)
    force_rmtree(quest.var_tmp_dir)


def archive_conversation(quest: Quest) -> None:
    """Archive a shelved conversation as resumable (paused, not abandoned); drop the live home."""
    control = quest.control_root
    quest_id = quest.id
    base_branch = quest.base_branch
    materialized = quest.materialized
    with ProjectLock(quest.quest_dir / "lock").acquire():  # raises LockHeld if runner is live
        state = State.load(quest.state_dir)
        state.status = "shelved"
        state.arm_step_resume(should_continue=False)
        state.save(quest.state_dir)
        quest.event_log().append(
            agent_id=None, type="conversation_archived", payload={})
        from imp.engine import archive
        archive.archive_quest(
            control, quest_id,
            events_path=quest.events_path,
            quest_json_path=quest.quest_dir / "quest.json",
            state_json_path=quest.state_dir / "state.json",
            steps_dir=quest.quest_dir / "steps",
            resumable=True,
        )
    if materialized:
        merge_queue.pop_and_wake(control, quest_id, base_branch)
        worktree.remove_worktree(control, quest_id, force=True)
        worktree.delete_branch(control, quest_id)
    force_rmtree(quest.quest_dir)
    shutil.rmtree(quest.tmp_dir, ignore_errors=True)
    force_rmtree(quest.var_tmp_dir)


def save_as_draft(quest: Quest, reason: str | None = None) -> None:
    """Pause a conversation: the un-materialized quest-home is kept (not archived) and
    remains resumable. Zone is derived from both `materialized` and `origin` — no bespoke
    status string is needed for zone placement. When the save was driven by a mid-run
    interrupt leave, `reason` (e.g. "ctrl-d") is stamped onto the conversation_saved
    payload as interrupt_reason; a plain save carries an empty payload."""
    with ProjectLock(quest.quest_dir / "lock").acquire():
        state = State.load(quest.state_dir)
        state.status = "paused"
        state.arm_step_resume(should_continue=False)
        state.save(quest.state_dir)
        payload = {"interrupt_reason": reason} if reason else {}
        quest.event_log().append(
            agent_id=None, type="conversation_saved", payload=payload)


def discard_conversation(quest: Quest) -> None:
    """Discard a zero-exchange open — no archive, no record; raises LockHeld if runner is live."""
    with ProjectLock(quest.quest_dir / "lock").acquire():
        pass
    force_rmtree(quest.quest_dir)
    shutil.rmtree(quest.tmp_dir, ignore_errors=True)
    force_rmtree(quest.var_tmp_dir)
